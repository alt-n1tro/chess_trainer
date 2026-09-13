"""Engine review of whole games.

Every move of every game you played, graded the way the gym grades you and
tagged with what the best move was about. This is where the statistics come
from: not "you are weak in the middlegame" but "you miss forks, and you leave
the opening worse as Black".
"""
from __future__ import annotations

import io
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor

import chess
import chess.pgn

from . import db, grading, themes
from .engine import with_wp

REVIEW_DEPTH = 16          # what the game-review sites use; --depth raises it
MATE_HORIZON = 11          # a mate this long or shorter counts as one you had


def move_accuracy(delta_wp: float) -> float:
    """Lichess's published accuracy curve: 100 for no loss, falling steeply.
    (103.1668 * e^(-0.04354 * loss) - 3.1669, clamped to 0-100.)"""
    acc = 103.1668 * math.exp(-0.04354 * max(0.0, delta_wp)) - 3.1669
    return max(0.0, min(100.0, acc))


def _flip(line: dict) -> dict:
    out = dict(line)
    if line.get("cp") is not None:
        out["cp"] = -line["cp"]
    if line.get("mate") is not None:
        out["mate"] = -line["mate"]
    out["wp"] = round(100.0 - (line.get("wp") or 50.0), 3)
    return out


def _final(board: chess.Board) -> dict | None:
    """The evaluation of a finished game, from the side to move."""
    if board.is_checkmate():
        return {"move": None, "cp": None, "mate": -1, "wp": 0.0, "pv": []}
    if board.is_game_over(claim_draw=True):
        return {"move": None, "cp": 0, "mate": None, "wp": 50.0, "pv": []}
    return None


def review_game(conn, pool, row, depth: int = REVIEW_DEPTH, progress=None) -> dict | None:
    """Review one game. Returns a summary, or None if it cannot be parsed."""
    game = chess.pgn.read_game(io.StringIO(row["pgn"]))
    if game is None or row["my_colour"] not in ("white", "black"):
        return None
    me = chess.WHITE if row["my_colour"] == "white" else chess.BLACK

    boards, moves = [chess.Board()], []
    node = game
    while node.variations:
        node = node.variations[0]
        moves.append(node.move)
        nxt = boards[-1].copy(stack=False)
        nxt.push(node.move)
        boards.append(nxt)
    if not moves:
        return None

    # One search per position, spread over both engine slots.
    def analyse(i):
        board = boards[i]
        done = _final(board)
        if done is not None:
            return done
        lines = pool.analyse(board, depth, 1, slot="fg" if i % 2 == 0 else "bg",
                             phase=db.classify_phase(board))
        if progress:
            progress(i, len(boards))
        return with_wp(lines)[0] if lines else {"move": None, "cp": 0, "mate": None,
                                                 "wp": 50.0, "pv": []}

    with ThreadPoolExecutor(max_workers=2) as ex:
        evals = list(ex.map(analyse, range(len(boards))))

    conn.execute("DELETE FROM review_moves WHERE game_id=?", (row["id"],))
    my_acc = []
    for i, move in enumerate(moves):
        board = boards[i]
        mover = board.turn
        before = evals[i]
        after = _flip(evals[i + 1])
        best_uci = before.get("move")
        delta = max(0.0, (before.get("wp") or 50.0) - (after.get("wp") or 50.0))
        rank = 1 if best_uci == move.uci() else None
        mine = dict(before) if rank == 1 else after
        verdict = grading.grade(before, mine, rank)
        mate_in = before.get("mate") if (before.get("mate") or 0) > 0 else None
        kept = None
        if mate_in is not None:
            kept = 1 if (after.get("mate") or 0) > 0 or rank == 1 else 0
        phase = db.classify_phase(board)
        tags = themes.tag(board.fen(), best_uci, before.get("pv"), mate_in)
        tags.append(phase)
        allowed = None
        if verdict["verdict"] in ("mistake", "blunder", "missed_mate"):
            reply = evals[i + 1]
            allowed = themes.tag(boards[i + 1].fen(), reply.get("move"),
                                 reply.get("pv"),
                                 reply.get("mate") if (reply.get("mate") or 0) > 0 else None)
        acc = move_accuracy(delta)
        if mover == me:
            my_acc.append(acc)
        conn.execute(
            "INSERT INTO review_moves(game_id, ply, is_me, fen, move, best,"
            " wp_before, wp_after, delta_wp, verdict, accuracy, mate_in,"
            " kept_mate, phase, themes, allowed)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], i + 1, 1 if mover == me else 0, board.fen(), move.uci(),
             best_uci, before.get("wp"), after.get("wp"), round(delta, 2),
             verdict["verdict"], round(acc, 1), mate_in, kept, phase,
             json.dumps(tags), json.dumps(allowed) if allowed is not None else None),
        )
    accuracy = round(sum(my_acc) / len(my_acc), 1) if my_acc else None
    conn.execute(
        "INSERT OR REPLACE INTO reviews(game_id, depth, engine_ver, reviewed_at,"
        " accuracy, plies) VALUES(?,?,?,?,?,?)",
        (row["id"], depth, pool.version, int(time.time()), accuracy, len(moves)),
    )
    conn.commit()
    return {"game_id": row["id"], "accuracy": accuracy, "plies": len(moves)}


def pending(conn, depth: int = REVIEW_DEPTH, limit: int | None = None) -> list:
    """Your games not yet reviewed at this depth, newest first."""
    sql = ("SELECT g.* FROM games g LEFT JOIN reviews r ON r.game_id = g.id"
           " WHERE g.my_colour IS NOT NULL AND (r.game_id IS NULL OR r.depth < ?)"
           " ORDER BY g.played_at DESC, g.id DESC")
    args: list = [depth]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()


def coverage(conn) -> dict:
    total = conn.execute(
        "SELECT COUNT(*) n FROM games WHERE my_colour IS NOT NULL").fetchone()["n"]
    done = conn.execute("SELECT COUNT(*) n FROM reviews").fetchone()["n"]
    return {"games": total, "reviewed": done}
