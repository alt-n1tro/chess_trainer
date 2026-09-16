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

from . import db, explain as explain_mod, grading, themes
from . import engine
from .engine import with_wp

REVIEW_DEPTH = 20          # the floor everywhere: the same depth that grades
                           # your drills, so a verdict never depends on which
                           # path reached the position. Simple positions still
                           # go deeper within BUDGET.
BUDGET = 0.6               # seconds of extra search allowed per position
MATE_HORIZON = 11          # a mate this long or shorter counts as one you had


def move_accuracy(delta_wp: float) -> float:
    """Lichess's published accuracy curve: 100 for no loss, falling steeply.
    (103.1668 * e^(-0.04354 * loss) - 3.1669, clamped to 0-100.)"""
    acc = 103.1668 * math.exp(-0.04354 * max(0.0, delta_wp)) - 3.1669
    return max(0.0, min(100.0, acc))


def game_accuracy(move_accs: list[float], white_wps: list[float]) -> float | None:
    """A game's accuracy the way lichess computes it (lila, AccuracyPercent):
    the average of a volatility-weighted mean and the harmonic mean of the
    move accuracies. The harmonic mean is what makes a blunder cost a game
    real accuracy; a plain mean lets two blunders hide behind forty good
    moves.

    `move_accs` are this side's move accuracies, in order. `white_wps` is the
    game's win probability from White's side before each of this side's
    moves, used to weight moves in volatile stretches more heavily.
    """
    n = len(move_accs)
    if not n:
        return None
    window = max(2, min(8, n // 10))
    weights = []
    for i in range(n):
        lo = max(0, i - window + 1)
        slice_ = white_wps[lo:i + 1]
        if len(slice_) < 2:
            slice_ = white_wps[:window]
        mean = sum(slice_) / len(slice_)
        std = (sum((x - mean) ** 2 for x in slice_) / len(slice_)) ** 0.5
        weights.append(max(0.5, min(12.0, std)))
    weighted = sum(a * w for a, w in zip(move_accs, weights)) / sum(weights)
    harmonic = n / sum(1.0 / max(a, 1.0) for a in move_accs)
    return round((weighted + harmonic) / 2, 1)


def _flip(line: dict) -> dict:
    out = dict(line)
    if line.get("cp") is not None:
        out["cp"] = -line["cp"]
    if line.get("mate") is not None:
        out["mate"] = -line["mate"]
    out["wp"] = round(100.0 - engine.wp_or_even(line.get("wp")), 3)
    return out


def _final(board: chess.Board) -> dict | None:
    """The evaluation of a finished game, from the side to move."""
    if board.is_checkmate():
        return {"move": None, "cp": None, "mate": -1, "wp": 0.0, "pv": []}
    if board.is_game_over(claim_draw=True):
        return {"move": None, "cp": 0, "mate": None, "wp": 50.0, "pv": []}
    return None


def review_game(conn, pool, row, depth: int = REVIEW_DEPTH, progress=None,
                budget: float = BUDGET) -> dict | None:
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
        lines = pool.analyse_deep(board, depth, 1, budget=budget,
                                  slot="fg" if i % 2 == 0 else "bg",
                                  phase=db.classify_phase(board))
        if progress:
            progress(i, len(boards))
        return with_wp(lines)[0] if lines else {"move": None, "cp": 0, "mate": None,
                                                 "wp": 50.0, "pv": []}

    with ThreadPoolExecutor(max_workers=2) as ex:
        evals = list(ex.map(analyse, range(len(boards))))

    conn.execute("DELETE FROM review_moves WHERE game_id=?", (row["id"],))
    my_acc, my_wps = [], []
    for i, move in enumerate(moves):
        board = boards[i]
        mover = board.turn
        before = evals[i]
        after = _flip(evals[i + 1])
        best_uci = before.get("move")
        delta = max(0.0, engine.wp_or_even(before.get("wp"))
                    - engine.wp_or_even(after.get("wp")))
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
        # Work the explanation out here, where there is time to check it
        # against a search, rather than in the half second a drill has. The
        # positions worth the effort are the ones you will be asked about:
        # where it was your move, and where the engine wanted something else.
        if mover == me and best_uci and best_uci != move.uci():
            _explain_here(conn, pool, board, before, phase, depth)

        if mover == me:
            my_acc.append(acc)
            wp = engine.wp_or_even(before.get("wp"))
            my_wps.append(wp if mover == chess.WHITE else 100.0 - wp)
        conn.execute(
            "INSERT INTO review_moves(game_id, ply, is_me, fen, move, best,"
            " wp_before, wp_after, delta_wp, verdict, accuracy, mate_in,"
            " kept_mate, phase, themes, allowed, depth)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], i + 1, 1 if mover == me else 0, board.fen(), move.uci(),
             best_uci, before.get("wp"), after.get("wp"), round(delta, 2),
             verdict["verdict"], round(acc, 1), mate_in, kept, phase,
             json.dumps(tags), json.dumps(allowed) if allowed is not None else None,
             before.get("depth") or depth),
        )
    accuracy = game_accuracy(my_acc, my_wps)
    conn.commit()
    # The review has evaluated every position, so the drill pools can grow
    # from it without another search.
    from . import corpus
    corpus.pool_from_review(conn, row["id"])
    conn.execute(
        "INSERT OR REPLACE INTO reviews(game_id, depth, engine_ver, reviewed_at,"
        " accuracy, plies) VALUES(?,?,?,?,?,?)",
        (row["id"], depth, pool.version, int(time.time()), accuracy, len(moves)),
    )
    conn.commit()
    return {"game_id": row["id"], "accuracy": accuracy, "plies": len(moves)}


def _explain_here(conn, pool, board, before: dict, phase: str,
                  depth: int) -> None:
    """Explain the engine's move in this position and keep it.

    The drill panel reads this back instead of recomputing, so the reasoning
    you are shown was checked against a real search and not a half-second
    guess. Failures are swallowed: an explanation is never worth losing a
    review over.
    """
    best_uci = before.get("move")
    try:
        # Two lines, so the explanation can say what the runner-up was worth.
        lines = pool.analyse(board, explain_mod.JUDGE_DEPTH, 2, slot="bg",
                             phase=phase) or []
        alts = []
        if len(lines) > 1 and lines[0].get("wp") is not None:
            alts = [{"move": l["move"], "pv": l.get("pv") or [],
                     "gap": round(lines[0]["wp"] - l["wp"], 1)}
                    for l in lines[1:2]]
        judge = explain_mod.Judge(
            lambda fen, d: _one_line(pool, fen, d), depth=explain_mod.JUDGE_DEPTH)
        root = board.copy(stack=False)
        pv = list(before.get("pv") or [])
        if not pv or pv[0] != best_uci:
            pv = [best_uci] + pv
        walked, sans, walked_boards = explain_mod._walk(root.fen(), pv,
                                                        explain_mod.PV_PLIES)
        items = explain_mod.why_best(
            root, chess.Move.from_uci(best_uci), sans, walked_boards, root.turn,
            line=pv, alts=alts, judge=judge)
        explain_mod.store(conn, db.pos_hash(board, phase), best_uci,
                          explain_mod.JUDGE_DEPTH, pool.version, items)
    except Exception:
        return


def _one_line(pool, fen: str, depth: int):
    """One engine line for the judge, from the cache where possible."""
    board = chess.Board(fen)
    if board.is_game_over(claim_draw=True):
        return None
    phase = db.classify_phase(board)
    lines = pool.analyse(board, depth, 1, slot="bg", phase=phase)
    return lines[0] if lines else None


def pending(conn, depth: int = REVIEW_DEPTH, limit: int | None = None,
            redo: bool = False) -> list:
    """Your games not yet reviewed at this depth, newest first. With
    ``redo`` every game, so a change to the grading rules can be applied to
    reviews already done (the engine work is cached, so this is quick)."""
    sql = ("SELECT g.* FROM games g LEFT JOIN reviews r ON r.game_id = g.id"
           " WHERE g.my_colour IS NOT NULL AND (? OR r.game_id IS NULL OR r.depth < ?)"
           " ORDER BY g.played_at DESC, g.id DESC")
    args: list = [1 if redo else 0, depth]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()


def recompute(conn) -> int:
    """Recompute every stored game accuracy from its move rows. Needs no
    engine; used when the aggregation changes."""
    n = 0
    for g in conn.execute("SELECT game_id FROM reviews").fetchall():
        rows = conn.execute(
            "SELECT m.accuracy, m.wp_before, m.ply FROM review_moves m"
            " WHERE m.game_id=? AND m.is_me=1 ORDER BY m.ply", (g["game_id"],)
        ).fetchall()
        accs = [r["accuracy"] for r in rows if r["accuracy"] is not None]
        wps = [(r["wp_before"] if r["ply"] % 2 == 1 else 100.0 - r["wp_before"])
               for r in rows if r["accuracy"] is not None]
        conn.execute("UPDATE reviews SET accuracy=? WHERE game_id=?",
                     (game_accuracy(accs, wps), g["game_id"]))
        n += 1
    conn.commit()
    return n


def coverage(conn) -> dict:
    total = conn.execute(
        "SELECT COUNT(*) n FROM games WHERE my_colour IS NOT NULL").fetchone()["n"]
    done = conn.execute("SELECT COUNT(*) n FROM reviews").fetchone()["n"]
    return {"games": total, "reviewed": done}
