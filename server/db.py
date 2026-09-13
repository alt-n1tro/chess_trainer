"""SQLite access: one file, WAL mode, versioned migrations.

Position identity lives here because every other module needs to agree on it.
"""
from __future__ import annotations

import os
import sqlite3
import threading

import chess
import chess.polyglot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "trainer.db")
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

SCHEMA_VERSION = 4

_local = threading.local()


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    """A connection for the calling thread. sqlite3 objects are not shareable."""
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == path:
        return conn
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _local.conn = conn
    _local.path = path
    return conn


def init(path: str = DB_PATH) -> sqlite3.Connection:
    conn = connect(path)
    with open(SCHEMA_PATH) as fh:
        conn.executescript(fh.read())
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    else:
        migrate(conn, int(row["value"]))
    conn.commit()
    return conn


def migrate(conn: sqlite3.Connection, have: int) -> None:
    """Apply migrations by version number. Version 1 is the initial schema.

    The script above only creates what is missing, so a migration is needed
    whenever an existing table changes.
    """
    if have >= SCHEMA_VERSION:
        return
    if have < 2:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(answers)")}
        if "step" not in columns:
            conn.execute(
                "ALTER TABLE answers ADD COLUMN step INTEGER NOT NULL DEFAULT 1")
    if have < 4:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(review_moves)")}
        if "depth" not in columns:
            conn.execute("ALTER TABLE review_moves ADD COLUMN depth INTEGER")
    conn.execute(
        "UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),)
    )


def meta_get(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


# --- position identity -----------------------------------------------------

def classify_phase(board: chess.Board) -> str:
    """opening | middlegame | endgame, by the rules in spec 4.3."""
    men = chess.popcount(board.occupied)
    queens = bool(board.queens)
    if (not queens and men <= 10) or (queens and men <= 6):
        return "endgame"
    if board.fullmove_number > 10:
        return "middlegame"
    return "opening"


def pos_hash(board: chess.Board, phase: str | None = None) -> int:
    """Zobrist hash of the position.

    Endgames fold in the halfmove clock bucketed by ten: in an endgame the
    fifty-move rule is a live tactical factor, so "drawn in four" and "drawn in
    forty" must not share a cache entry. Bucketing keeps most transposition
    sharing. Signed 64-bit so SQLite stores it as an INTEGER.
    """
    h = chess.polyglot.zobrist_hash(board)
    if (phase or classify_phase(board)) == "endgame":
        h ^= (board.halfmove_clock // 10) * 0x9E3779B97F4A7C15
    h &= 0xFFFFFFFFFFFFFFFF
    if h >= 1 << 63:
        h -= 1 << 64
    return h
