"""Game import, PGN parsing, phase extraction.

Positions come from games. Nothing is hand-typed here.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.error
import urllib.request

import chess
import chess.pgn

from . import db
from .engine import DEPTH_FILTER, DEPTH_GRADE

GAMES_DIR = os.path.join(db.ROOT, "data", "games")
UA = {"User-Agent": "chess-trainer/1.0 (local, single user)"}

# Eval window from your side (spec 4.3). Below -200 there is no defensive
# resource to find; above +250 the skill is conversion, which is a different
# skill.
EVAL_LOW, EVAL_HIGH = -200, 250
MAX_PER_GAME = 3
MATERIAL_TOLERANCE = 1       # pawns

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
          chess.ROOK: 5, chess.QUEEN: 9}

FAMILY_STOPS = ("Defense", "Defence", "Opening", "Game", "Attack", "Gambit",
                "System", "Formation", "Countergambit")


# --- naming ---------------------------------------------------------------

def opening_family(headers: dict) -> str:
    """The title every variation groups under: "Sicilian Defense"."""
    url = headers.get("ECOUrl") or ""
    slug = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
    if slug:
        words = slug.replace("_", "-").split("-")
        out = []
        for w in words:
            out.append(w)
            if w in FAMILY_STOPS:
                break
        return " ".join(out)
    eco = headers.get("ECO")
    return f"ECO {eco}" if eco else "Unnamed opening"


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _played_at(headers: dict) -> int:
    date = headers.get("UTCDate") or headers.get("Date") or ""
    clock = headers.get("UTCTime") or "00:00:00"
    try:
        return int(time.mktime(time.strptime(f"{date} {clock}",
                                             "%Y.%m.%d %H:%M:%S")))
    except ValueError:
        return 0


# --- import ---------------------------------------------------------------

def my_username(conn) -> str | None:
    return db.meta_get(conn, "username")


def detect_colour(headers: dict, me: str | None) -> str | None:
    """'white', 'black', or None for a game where neither player is you."""
    if not me:
        return None
    me = me.lower()
    if (headers.get("White") or "").lower() == me:
        return "white"
    if (headers.get("Black") or "").lower() == me:
        return "black"
    return None


def store_game(conn, game: chess.pgn.Game, url: str | None = None,
               time_class: str | None = None) -> int:
    h = dict(game.headers)
    me = my_username(conn)
    colour = detect_colour(h, me)
    exporter = chess.pgn.StringExporter(headers=True, variations=False,
                                        comments=False)
    pgn_text = game.accept(exporter)
    url = url or h.get("Link") or h.get("Site") or None
    if url:
        row = conn.execute("SELECT id FROM games WHERE url=?", (url,)).fetchone()
        if row:
            return row["id"]
    cur = conn.execute(
        "INSERT INTO games(url, white, black, result, white_elo, black_elo,"
        " time_class, played_at, pgn, my_colour) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (url, h.get("White"), h.get("Black"), h.get("Result"),
         _int(h.get("WhiteElo")), _int(h.get("BlackElo")),
         time_class or h.get("TimeControl"), _played_at(h), pgn_text, colour),
    )
    conn.commit()
    return cur.lastrowid


def import_pgn_text(conn, text: str, url: str | None = None,
                    progress=None) -> list[int]:
    stream = io.StringIO(text)
    ids, n = [], 0
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        if game.end().ply() < 4:
            continue
        ids.append(store_game(conn, game, url if len(ids) == 0 else None))
        n += 1
        if progress:
            progress(n)
    return ids


ARCHIVES_URL = "https://api.chess.com/pub/player/{user}/games/archives"
TIME_CLASSES = ("rapid", "blitz", "bullet", "daily")


def fetch_archives(username: str) -> list[str]:
    """Every month chess.com holds games for, oldest first."""
    raw = fetch_text(ARCHIVES_URL.format(user=username.strip().lower()))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LookupError(
            "chess.com did not return the archive list as JSON."
        ) from exc
    months = payload.get("archives") if isinstance(payload, dict) else None
    if not months:
        raise LookupError(
            f"chess.com lists no games for {username}. Check the spelling: it "
            f"is the username, not the display name."
        )
    return list(months)


def import_player(conn, username: str, time_classes=("rapid",), since=None,
                  progress=None) -> dict:
    """Import a player's whole history, one time class at a time.

    Months are fetched oldest first and games already stored are skipped by
    URL, so re-running this picks up only what is new.
    """
    wanted = {c.lower() for c in time_classes} if time_classes else None
    months = fetch_archives(username)
    if since:
        months = [m for m in months if m[-7:].replace("/", "-") >= since]
    counts = {"months": len(months), "seen": 0, "kept": 0, "new": 0, "ids": []}

    for i, month in enumerate(months):
        if progress:
            progress(i, len(months), month[-7:].replace("/", "-"), counts)
        try:
            payload = json.loads(fetch_text(month))
        except json.JSONDecodeError as exc:
            raise LookupError(f"{month} did not return JSON.") from exc
        for entry in payload.get("games", []):
            counts["seen"] += 1
            klass = (entry.get("time_class") or "").lower()
            if wanted and klass not in wanted:
                continue
            pgn_text = entry.get("pgn")
            if not pgn_text:
                continue
            game = chess.pgn.read_game(io.StringIO(pgn_text))
            if game is None or game.end().ply() < 4:
                continue
            counts["kept"] += 1
            before = conn.execute("SELECT COUNT(*) n FROM games").fetchone()["n"]
            gid = store_game(conn, game, entry.get("url"), klass)
            after = conn.execute("SELECT COUNT(*) n FROM games").fetchone()["n"]
            if after > before:
                counts["new"] += 1
            counts["ids"].append(gid)
        conn.commit()
        if progress:
            progress(i + 1, len(months), month[-7:].replace("/", "-"), counts)
    return counts


def import_source(conn, source: str, progress=None) -> list[int]:
    """A local PGN file, a chess.com game URL, or a chess.com archive URL."""
    if os.path.exists(source):
        with open(source, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        _archive(os.path.basename(source), text)
        return import_pgn_text(conn, text, progress=progress)
    if source.startswith("http"):
        if re.search(r"/game/(live|daily)/\d+", source):
            game, pgn_text = fetch_chesscom_game(source)
            _archive(source.rsplit("/", 1)[-1] + ".pgn", pgn_text)
            return [store_game(conn, game, canonical_url(source))]
        text = pgn_from_response(fetch_text(source))
        _archive("archive-%d.pgn" % int(time.time()), text)
        return import_pgn_text(conn, text, progress=progress)
    raise ValueError(f"Not a file and not a URL: {source}")


def _archive(name: str, text: str) -> None:
    os.makedirs(GAMES_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    with open(os.path.join(GAMES_DIR, safe), "w", encoding="utf-8") as fh:
        fh.write(text)


def fetch_text(url: str) -> str:
    """Read-only GET. Failures name the actual cause, never a generic error."""
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=30
        ) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise LookupError(f"chess.com has no such game ({url}).") from exc
        if exc.code == 429:
            raise LookupError(
                "chess.com is rate-limiting this address; wait a minute."
            ) from exc
        raise LookupError(f"chess.com returned HTTP {exc.code} for {url}.") from exc
    except urllib.error.URLError as exc:
        raise LookupError(
            f"Cannot reach chess.com ({exc.reason}). Offline, or blocked."
        ) from exc


def pgn_from_response(text: str) -> str:
    """A monthly archive from api.chess.com is JSON with a pgn per game; a
    plain .pgn download is already PGN. Accept either."""
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        # A PGN starts with a [Event "..."] tag, which also starts with "[",
        # so only an object is assumed to be JSON without parsing it first.
        return text
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise LookupError("That URL returned neither PGN nor JSON.") from exc
    games = payload.get("games") if isinstance(payload, dict) else payload
    if not isinstance(games, list):
        raise LookupError("That JSON holds no games.")
    pgns = [g.get("pgn") for g in games if isinstance(g, dict) and g.get("pgn")]
    if not pgns:
        raise LookupError(
            "That archive lists games but none carry a PGN. If it is a month "
            "still in progress, try again later."
        )
    return "\n\n".join(pgns)


def canonical_url(url: str) -> str:
    m = re.search(r"/game/(live|daily)/(\d+)", url)
    return f"https://www.chess.com/game/{m.group(1)}/{m.group(2)}" if m else url


MOVE_CHARS = ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
              "0123456789!?{~}(^)[_]@#$,./&-*++=")
PROMOTIONS = {"=": "q", "{": "n", "~": "b", "}": "r", "^": "q", "_": "q",
              "#": "q", "$": "q"}


def decode_move_list(move_list: str) -> list[str]:
    """Decode chess.com's compact moveList into UCI.

    Two characters per move: origin square, destination square, indexed into
    chess.com's alphabet. Indices above 63 encode a pawn promotion. Callers
    must validate the result against the rules -- see fetch_chesscom_game,
    which refuses a decode that does not play out legally rather than
    importing a corrupted game.
    """
    out = []
    for i in range(0, len(move_list) - 1, 2):
        a, b = move_list[i], move_list[i + 1]
        if a not in MOVE_CHARS or b not in MOVE_CHARS:
            raise ValueError("unrecognised character in moveList")
        src, dst = MOVE_CHARS.index(a), MOVE_CHARS.index(b)
        promo = ""
        if dst > 63:
            offset = dst - 64
            promo = "qbnr"[offset // 11] if offset // 11 < 4 else "q"
            dst = src % 8 + (offset % 11) - 1 + (56 if src >= 32 else 0)
        out.append(chess.square_name(src) + chess.square_name(dst) + promo)
    return out


def fetch_chesscom_game(url: str) -> tuple[chess.pgn.Game, str]:
    """Fetch a single game through the public read-only callback endpoint."""
    m = re.search(r"/game/(live|daily)/(\d+)", url)
    if not m:
        raise ValueError(f"Not a chess.com game link: {url}")
    kind, gid = m.group(1), m.group(2)
    raw = fetch_text(f"https://www.chess.com/callback/{kind}/game/{gid}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LookupError(
            "chess.com returned something that is not JSON; the game may be "
            "private or the endpoint may have changed."
        ) from exc
    game_data = payload.get("game") or {}
    headers = game_data.get("pgnHeaders") or {}

    pgn_text = game_data.get("pgn") or payload.get("pgn")
    if pgn_text:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if game is None:
            raise LookupError("chess.com returned a PGN that will not parse.")
        return game, pgn_text

    move_list = game_data.get("moveList")
    if not move_list:
        raise LookupError(
            "chess.com returned no moves for this game (private, aborted, or "
            "still in progress)."
        )
    game = chess.pgn.Game()
    for key, value in headers.items():
        game.headers[key] = str(value)
    game.headers["Link"] = canonical_url(url)
    board = chess.Board()
    node = game
    for uci in decode_move_list(move_list):
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            move = None
        if move is None or move not in board.legal_moves:
            raise LookupError(
                "chess.com's move encoding did not decode to a legal game; "
                "refusing to import a corrupted game. Download the PGN from "
                "chess.com and use ./run import <file> instead."
            )
        node = node.add_variation(move)
        board.push(move)
    exporter = chess.pgn.StringExporter(headers=True, variations=False,
                                        comments=False)
    return game, game.accept(exporter)


# --- phase extraction -----------------------------------------------------

def material_balance(board: chess.Board, me: bool) -> int:
    total = 0
    for pt, val in VALUES.items():
        total += val * (len(board.pieces(pt, me)) - len(board.pieces(pt, not me)))
    return total


def tail_san(node, count: int = 4) -> str:
    """The last few moves only. A middlegame position is forty plies deep and
    printing all of it pushes the options out of the panel."""
    moves, cur = [], node
    while cur.parent is not None and len(moves) < count:
        moves.append((cur.parent.board(), cur.move))
        cur = cur.parent
    moves.reverse()
    out = []
    for board, move in moves:
        prefix = ""
        if board.turn == chess.WHITE:
            prefix = f"{board.fullmove_number}. "
        elif not out:
            prefix = f"{board.fullmove_number}... "
        out.append(prefix + board.san(move))
    return " ".join(out)


def candidate_positions(game: chess.pgn.Game, colour: str):
    """Positions from one game worth considering for the pools.

    Only positions where the opponent is to move -- that is the question.
    """
    me = chess.WHITE if colour == "white" else chess.BLACK
    node = game
    while node.variations:
        node = node.variations[0]
        board = node.board()
        if board.turn == me:
            continue                       # you to move: not a question
        if board.is_game_over(claim_draw=True):
            continue
        phase = db.classify_phase(board)
        if phase == "opening":
            continue
        if phase == "middlegame" and board.fullmove_number <= 10:
            continue
        if abs(material_balance(board, me)) > MATERIAL_TOLERANCE:
            continue                       # material alone, first gate
        yield node, board, phase


def opening_node(game: chess.pgn.Game, colour: str):
    """Where an opening drill starts: the opponent to move, a few moves in."""
    me = chess.WHITE if colour == "white" else chess.BLACK
    node = game
    while node.variations:
        node = node.variations[0]
        board = node.board()
        if board.turn != me and board.fullmove_number >= 6:
            return node, board
    return None, None


def build_phases(conn, pool, progress=None, yes: bool = False,
                 log=print) -> dict:
    """Rebuild the middlegame and endgame pools.

    Balance is verified twice: material within one pawn, then Stockfish at
    depth 12, then depth 20 for survivors. Material alone calls positions
    equal that are not, routinely.
    """
    games = conn.execute(
        "SELECT * FROM games WHERE my_colour IS NOT NULL ORDER BY id"
    ).fetchall()
    conn.execute("DELETE FROM positions")
    conn.commit()
    counts = {"opening": 0, "middlegame": 0, "endgame": 0,
              "examined": 0, "games": len(games)}

    for gi, row in enumerate(games):
        game = chess.pgn.read_game(io.StringIO(row["pgn"]))
        if game is None:
            continue
        colour = row["my_colour"]
        me = chess.WHITE if colour == "white" else chess.BLACK
        family = opening_family(dict(game.headers))

        node, board = opening_node(game, colour)
        if node is not None:
            _insert(conn, board, "opening", row["id"], node.ply(), None,
                    tail_san(node), colour, family)
            counts["opening"] += 1

        survivors = []
        for node, board, phase in candidate_positions(game, colour):
            counts["examined"] += 1
            if progress:
                progress(gi, len(games), counts["examined"])
            lines = pool.analyse(board, DEPTH_FILTER, 1, phase=phase)
            if not lines:
                continue
            cp = _my_cp(lines[0], board, me)
            if cp is None or not (EVAL_LOW - 60 <= cp <= EVAL_HIGH + 60):
                continue
            survivors.append((node, board, phase))

        # Spread the keepers across the game so no single game dominates, and
        # confirm the eval at full depth.
        for node, board, phase in _spread(survivors, MAX_PER_GAME):
            lines = pool.analyse(board, DEPTH_GRADE, 1, phase=phase)
            if not lines:
                continue
            if lines[0].get("mate") is not None:
                continue               # mate-in-N is excluded from both pools
            cp = _my_cp(lines[0], board, me)
            if cp is None or not (EVAL_LOW <= cp <= EVAL_HIGH):
                continue
            _insert(conn, board, phase, row["id"], node.ply(), cp,
                    tail_san(node), colour, family)
            counts[phase] += 1
        conn.commit()
    return counts


def _my_cp(line: dict, board: chess.Board, me: bool):
    """Cached lines score from the side to move; the pools score from yours."""
    cp = line.get("cp")
    if cp is None:
        return None
    return cp if board.turn == me else -cp


def _spread(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _insert(conn, board, phase, game_id, ply, cp, tail, colour, name) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO positions(pos_hash, fen, phase, source_game,"
        " ply, eval_cp, tail_san, my_colour, name) VALUES(?,?,?,?,?,?,?,?,?)",
        (db.pos_hash(board, phase), board.fen(), phase, game_id, ply, cp,
         tail, colour, name),
    )
