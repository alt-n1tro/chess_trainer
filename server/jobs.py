"""Importing and reviewing a game while the page watches.

A review is a minute of engine work, so it cannot happen inside a request.
It runs on its own thread and reports where it has got to; the page asks for
that every half second and draws a progress card. One job at a time: two
reviews would fight over the same two engine processes and finish no sooner.
"""
from __future__ import annotations

import os
import threading
import time

from . import corpus, db, review


class Analysis:
    """One import-and-review, with a progress report the page can read."""

    def __init__(self, source: str, pool, depth: int, budget: float,
                 game_id: int | None = None):
        self.source = source.strip()
        self.game_id = game_id
        self.pool = pool
        self.depth = depth
        self.budget = budget
        self.lock = threading.Lock()
        self.started = time.time()
        self.state = {
            "active": True,
            "stage": "importing",     # importing | reviewing | done | error
            "source": self.source[:120],
            "game": None,             # "You (White) vs opp"
            "game_id": None,
            "games": 0,               # games this source brought in
            "game_index": 0,          # which one is being reviewed
            "moves_done": 0,
            "moves_total": 0,
            "positions": 0,           # drill positions extracted so far
            "accuracy": None,
            "error": None,
            "seconds": 0.0,
        }
        self.thread = threading.Thread(target=self._run, daemon=True)

    # -- reading
    def snapshot(self) -> dict:
        with self.lock:
            out = dict(self.state)
        out["seconds"] = round(time.time() - self.started, 1)
        return out

    def _set(self, **fields) -> None:
        with self.lock:
            self.state.update(fields)

    def start(self) -> None:
        self.thread.start()

    # -- doing
    def _run(self) -> None:
        conn = db.connect()
        try:
            ids = self._import(conn)
            self._set(stage="reviewing", games=len(ids))
            for i, game_id in enumerate(ids, 1):
                row = conn.execute("SELECT * FROM games WHERE id=?",
                                   (game_id,)).fetchone()
                if row is None:
                    continue
                self._set(game_index=i, game_id=game_id,
                          game=_label(row), moves_done=0,
                          moves_total=_ply_estimate(row))
                if row["my_colour"] not in ("white", "black"):
                    raise LookupError(_not_yours(conn, row))
                summary = review.review_game(
                    conn, self.pool, row, self.depth, self._progress,
                    budget=self.budget)
                self._set(positions=_positions(conn, ids),
                          accuracy=(summary or {}).get("accuracy"))
            self._set(active=False, stage="done", game_id=ids[0] if ids else None,
                      positions=_positions(conn, ids))
        except Exception as err:                    # reported, never raised
            self._set(active=False, stage="error", error=str(err) or type(err).__name__)

    def _import(self, conn) -> list[int]:
        if self.game_id:
            # Already in the database: nothing to fetch, only to analyse.
            row = conn.execute("SELECT id FROM games WHERE id=?",
                               (self.game_id,)).fetchone()
            if row is None:
                raise LookupError("no such game")
            return [row["id"]]
        src = self.source
        if src.startswith("http") or os.path.exists(src):
            ids = corpus.import_source(conn, src)
        else:
            ids = corpus.import_pgn_text(conn, src)   # the PGN itself, pasted
        if not ids:
            raise LookupError("Nothing importable there: paste a chess.com game"
                              " link, or the PGN itself.")
        return ids

    def _progress(self, i: int, total: int) -> None:
        """review_game searches positions two at a time, so the index it
        reports is not in order. Count what has finished instead."""
        with self.lock:
            self.state["moves_total"] = total
            self.state["moves_done"] = min(total, self.state["moves_done"] + 1)


def _label(row) -> str:
    white = f"{row['white']}" + (f" ({row['white_elo']})" if row["white_elo"] else "")
    black = f"{row['black']}" + (f" ({row['black_elo']})" if row["black_elo"] else "")
    return f"{white} vs {black}"


def _ply_estimate(row) -> int:
    """Enough for a progress bar before the PGN is parsed properly."""
    return max(1, len((row["pgn"] or "").split()) // 2)


def _positions(conn, ids: list[int]) -> int:
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    row = conn.execute(
        f"SELECT COUNT(*) n FROM positions WHERE source_game IN ({marks})",
        ids).fetchone()
    return row["n"] or 0


def _not_yours(conn, row) -> str:
    you = corpus.my_username(conn) or None
    if you:
        return (f"{row['white']} vs {row['black']} is not one of {you}'s games,"
                " so there is no side to grade. Import a game you played.")
    return ("No player name is set, so there is no side to grade."
            " Run ./run whoami <your-chess.com-name> first.")
