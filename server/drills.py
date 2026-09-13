"""Drill state machine, tree, rounds, weighted replay, leaks.

A position is set with the opponent to move. Stockfish plays one of its five
best moves; you must find the best response. One position is five rounds, one
per candidate, and every round resets to the same starting position. Nothing is
ever chained.
"""
from __future__ import annotations

import json
import random
import time

import chess

from . import db, explain as explain_mod, grading
from .engine import DEPTH_CANDIDATES, DEPTH_GRADE, MULTIPV_CANDIDATES

SESSION_ID = "local"          # one local user, one persistent session
MAX_DEPTH_LEVEL = 6           # Drill from here is capped at 6 levels
ROUNDS = MULTIPV_CANDIDATES
RECENCY_COLD = 10             # the last 10 positions drawn: weight 0
RECENCY_RAMP = 20             # ramp back to 1 over the following 20

MODES = ("openings", "middlegame", "endgame")
MODE_PHASE = {"openings": "opening", "middlegame": "middlegame",
              "endgame": "endgame"}


# --- tree ------------------------------------------------------------------

def tree_touch(conn, root_hash: int, parent_id, move_uci, board: chess.Board,
               depth_level: int, phase=None) -> int:
    """Record a visit. Node identity is path-keyed: a position reached by two
    move orders is two rows. Analysis stays position-keyed."""
    now = int(time.time())
    h = db.pos_hash(board, phase)
    row = conn.execute(
        "SELECT id FROM tree_nodes WHERE session_id=? AND root_hash=?"
        " AND parent_id IS ? AND move_uci IS ?",
        (SESSION_ID, root_hash, parent_id, move_uci),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE tree_nodes SET visit_count=visit_count+1, last_seen=?"
            " WHERE id=?", (now, row["id"]),
        )
        conn.commit()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO tree_nodes(session_id, root_hash, parent_id, move_uci,"
        " pos_hash, fen, depth_level, visit_count, first_seen, last_seen)"
        " VALUES(?,?,?,?,?,?,?,1,?,?)",
        (SESSION_ID, root_hash, parent_id, move_uci, h, board.fen(),
         depth_level, now, now),
    )
    conn.commit()
    return cur.lastrowid


def tree_rows(conn, root_hash: int) -> list[dict]:
    """The whole tree for this root, including branches you did not follow."""
    rows = conn.execute(
        "SELECT * FROM tree_nodes WHERE session_id=? AND root_hash=?"
        " ORDER BY id", (SESSION_ID, root_hash),
    ).fetchall()
    by_parent: dict = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)
    out: list[dict] = []

    def walk(parent_id, depth):
        for r in by_parent.get(parent_id, []):
            board = chess.Board(r["fen"])
            san = ""
            if r["move_uci"] and r["parent_id"]:
                parent = next((x for x in rows if x["id"] == r["parent_id"]), None)
                if parent:
                    pb = chess.Board(parent["fen"])
                    try:
                        san = pb.san(chess.Move.from_uci(r["move_uci"]))
                    except (ValueError, AssertionError):
                        san = r["move_uci"]
            answered = conn.execute(
                "SELECT verdict FROM answers WHERE pos_hash=?"
                " ORDER BY answered_at DESC LIMIT 1", (r["pos_hash"],),
            ).fetchone()
            out.append({
                "id": r["id"], "depth": depth, "san": san,
                "uci": r["move_uci"], "visits": r["visit_count"],
                "depth_level": r["depth_level"],
                "turn": "white" if board.turn == chess.WHITE else "black",
                "drilled": bool(answered),
                "verdict": answered["verdict"] if answered else None,
                "tone": grading.TONES.get(answered["verdict"]) if answered else None,
            })
            walk(r["id"], depth + 1)

    walk(None, 0)
    return out


def node_path(conn, node_id: int) -> list:
    path, cur = [], node_id
    while cur is not None:
        row = conn.execute("SELECT * FROM tree_nodes WHERE id=?", (cur,)).fetchone()
        if row is None:
            break
        path.append(row)
        cur = row["parent_id"]
    path.reverse()
    return path


# --- weighted replay -------------------------------------------------------

def position_stats(conn, pos_hash: int) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) seen,"
        f" SUM(verdict IN {_MISS_SQL}) misses,"
        " SUM(verdict='best') best"
        " FROM answers WHERE pos_hash=?", (pos_hash,),
    ).fetchone()
    return {k: (row[k] or 0) for k in ("seen", "misses", "best")}


def _recency_penalty(rank: int | None) -> float:
    """0 for the last 10 positions drawn, ramping back to 1 over the next 20."""
    if rank is None:
        return 1.0
    if rank < RECENCY_COLD:
        return 0.0
    if rank >= RECENCY_COLD + RECENCY_RAMP:
        return 1.0
    return (rank - RECENCY_COLD) / RECENCY_RAMP


def pick_random(conn, mode: str, name: str | None = None,
                colour: str | None = None):
    """Random does not pick uniformly. Positions you get wrong come back."""
    phase = MODE_PHASE[mode]
    sql = "SELECT * FROM positions WHERE phase=?"
    args: list = [phase]
    if name:
        sql += " AND name=?"
        args.append(name)
    if colour:
        sql += " AND my_colour=?"
        args.append(colour)
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return None
    recent = [
        r["pos_hash"] for r in conn.execute(
            "SELECT pos_hash FROM draws WHERE phase=? ORDER BY id DESC LIMIT ?",
            (phase, RECENCY_COLD + RECENCY_RAMP),
        ).fetchall()
    ]
    rank_of = {h: i for i, h in enumerate(recent)}

    weights = []
    for r in rows:
        s = position_stats(conn, r["pos_hash"])
        w = (s["misses"] + 1) / (s["seen"] + 1)
        w *= _recency_penalty(rank_of.get(r["pos_hash"]))
        weights.append(max(w, 0.0))
    if sum(weights) <= 0:
        weights = [1.0] * len(rows)
    chosen = random.choices(rows, weights=weights, k=1)[0]
    conn.execute("INSERT INTO draws(pos_hash, phase, drawn_at) VALUES(?,?,?)",
                 (chosen["pos_hash"], phase, int(time.time())))
    conn.commit()
    return chosen


def groups(conn, mode: str) -> list[dict]:
    """What the picker shows for this mode: one row per title, a button per
    colour you have played it with."""
    phase = MODE_PHASE[mode]
    rows = conn.execute(
        "SELECT name, my_colour, COUNT(*) n FROM positions WHERE phase=?"
        " GROUP BY name, my_colour ORDER BY name", (phase,),
    ).fetchall()
    merged: dict = {}
    for r in rows:
        entry = merged.setdefault(r["name"] or "Unnamed", {"name": r["name"] or "Unnamed",
                                                           "colours": {}, "count": 0})
        entry["colours"][r["my_colour"]] = r["n"]
        entry["count"] += r["n"]
    return sorted(merged.values(), key=lambda e: -e["count"])


# --- the drill -------------------------------------------------------------

class Drill:
    """One ask-root and its five rounds."""

    def __init__(self, conn, pool, fen: str, mode: str, *, name=None,
                 source=None, depth_level: int = 0, root_hash=None,
                 node_id=None, parent_node=None):
        # No connection is held: sqlite3 objects belong to the thread that
        # created them, and requests arrive on whichever thread is free.
        self.pool = pool
        self.board = chess.Board(fen)
        self.fen = self.board.fen()
        self.mode = mode
        self.name = name
        self.source = source or {}
        self.depth_level = depth_level
        self.phase = db.classify_phase(self.board)
        self.hash = db.pos_hash(self.board, self.phase)
        self.root_hash = root_hash if root_hash is not None else self.hash
        # "To move" is the side the opponent takes.
        self.opponent = self.board.turn
        self.my_colour = not self.board.turn
        # Drill-from-here re-uses the node that already represents this
        # position; a fresh drill creates the tree root.
        self.node_id = node_id or tree_touch(
            conn, self.root_hash, parent_node, None, self.board,
            depth_level, self.phase,
        )
        self.candidates = self._candidates()
        self.order = self._order()
        self.results: list = [None] * len(self.order)   # tone per round, for the dots
        self.index = 0
        self.round_state = None
        self.start_round(0)

    @property
    def conn(self):
        return db.connect()

    # -- candidates
    def _candidates(self) -> list[dict]:
        lines = self.pool.analyse(self.board, DEPTH_CANDIDATES,
                                  MULTIPV_CANDIDATES, phase=self.phase)
        out = []
        for line in lines:
            try:
                move = chess.Move.from_uci(line["move"])
                san = self.board.san(move)
            except (ValueError, AssertionError):
                continue
            out.append({"uci": line["move"], "san": san, "cp": line["cp"],
                        "wp": line["wp"], "mate": line["mate"],
                        "pv": line["pv"]})
        return out

    def _faced(self, uci: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM answers WHERE root_hash=? AND opp_move=?"
            " AND depth_level=?", (self.hash, uci, self.depth_level),
        ).fetchone()
        return row["n"] or 0

    def _order(self) -> list[int]:
        """Chosen randomly with a bias toward paths you have not yet faced."""
        idx = list(range(len(self.candidates)))
        random.shuffle(idx)
        idx.sort(key=lambda i: self._faced(self.candidates[i]["uci"]))
        return idx

    # -- rounds
    def start_round(self, index: int) -> None:
        """Every round resets to the same starting position."""
        if not self.candidates:
            self.round_state = None
            return
        self.index = max(0, min(index, len(self.order) - 1))
        cand = self.candidates[self.order[self.index]]
        board = self.board.copy(stack=False)
        board.push(chess.Move.from_uci(cand["uci"]))
        phase = db.classify_phase(board)
        opp_node = tree_touch(self.conn, self.root_hash, self.node_id,
                              cand["uci"], board, self.depth_level, phase)
        self.round_state = {
            "candidate": cand,
            "fen": board.fen(),
            "phase": phase,
            "hash": db.pos_hash(board, phase),
            "node_id": opp_node,
            "answer": None,
        }
        self._warm()

    def select_candidate(self, which: int) -> None:
        """Keys 1-5 pick the nth opponent option as displayed."""
        if 0 <= which < len(self.candidates) and which in self.order:
            self.start_round(self.order.index(which))

    def reset(self) -> None:
        """Restart this position from round one. It replays this position; it
        never fetches a fresh one."""
        self.order = self._order()
        self.results = [None] * len(self.order)
        self.start_round(0)

    def _warm(self) -> None:
        """The warmer always works on the most valuable unknown position."""
        jobs = []
        for cand in self.candidates:             # 1. the five children
            b = self.board.copy(stack=False)
            b.push(chess.Move.from_uci(cand["uci"]))
            jobs.append((b.fen(), DEPTH_GRADE, MULTIPV_CANDIDATES))
        for cand in self.candidates[:2]:         # 2. grandchildren, top 2
            b = self.board.copy(stack=False)
            b.push(chess.Move.from_uci(cand["uci"]))
            for line in self.pool.cached(b, DEPTH_GRADE, MULTIPV_CANDIDATES) or []:
                try:
                    b2 = b.copy(stack=False)
                    b2.push(chess.Move.from_uci(line["move"]))
                except (ValueError, AssertionError):
                    continue
                jobs.append((b2.fen(), DEPTH_CANDIDATES, MULTIPV_CANDIDATES))
        jobs.extend(self._queue_jobs())          # 3. and 4.
        self.pool.warm(jobs)

    def _queue_jobs(self, limit: int = 8) -> list:
        """Positions in the weighted-replay queue likely to come up soon, then
        anything in the phase pools not yet cached. These sit behind the
        children, so the foreground never waits on them."""
        phase = MODE_PHASE.get(self.mode, self.phase)
        rows = self.conn.execute(
            "SELECT p.fen FROM positions p"
            " LEFT JOIN analysis a ON a.pos_hash = p.pos_hash"
            "   AND a.depth = ? AND a.multipv = ?"
            " LEFT JOIN (SELECT pos_hash, COUNT(*) seen,"
            f"            SUM(verdict IN {_MISS_SQL}) misses"
            "            FROM answers GROUP BY pos_hash) s"
            "   ON s.pos_hash = p.pos_hash"
            " WHERE p.phase = ? AND a.pos_hash IS NULL"
            " ORDER BY COALESCE(s.misses, 0) DESC, COALESCE(s.seen, 0) ASC"
            " LIMIT ?",
            (DEPTH_CANDIDATES, MULTIPV_CANDIDATES, phase, limit),
        ).fetchall()
        return [(r["fen"], DEPTH_CANDIDATES, MULTIPV_CANDIDATES) for r in rows]

    # -- answering
    def my_lines(self) -> list[dict]:
        """The engine's ordered choices for you here. Ranking your move needs
        the whole list, not only the best of them."""
        rs = self.round_state
        if not rs:
            return []
        board = chess.Board(rs["fen"])
        return self.pool.analyse(board, DEPTH_GRADE, MULTIPV_CANDIDATES,
                                 phase=rs["phase"])

    def best_line(self) -> dict | None:
        lines = self.my_lines()
        return lines[0] if lines else None

    def answer(self, uci: str) -> dict:
        rs = self.round_state
        if rs is None:
            raise ValueError("no round in progress")
        board = chess.Board(rs["fen"])
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            raise ValueError(f"{uci} is not a move")
        if move not in board.legal_moves:
            raise ValueError("That move is not legal here.")
        san = board.san(move)
        lines = self.my_lines()
        if not lines:
            raise RuntimeError("no engine evaluation for this position")
        best = lines[0]
        rank = grading.rank_of(lines, move.uci())

        after = board.copy(stack=False)
        after.push(move)
        after_phase = db.classify_phase(after)
        mine = _outcome(after)
        if mine is None:
            lines_after = self.pool.analyse(after, DEPTH_GRADE, 1,
                                            phase=after_phase)
            mine = (_flip(lines_after[0]) if lines_after
                    else {"wp": 50.0, "cp": 0, "mate": None, "pv": []})
        ranked = next((l for l in lines if l["move"] == move.uci()), None)
        if ranked is not None:
            # The engine already searched this move: use its own number rather
            # than a second evaluation of the position after it.
            mine = dict(ranked)
        result = grading.grade(best, mine, rank)

        best_san = board.san(chess.Move.from_uci(best["move"]))
        exp = explain_mod.explain(
            rs["fen"], move.uci(), best["move"],
            {"mine": [move.uci()] + (mine.get("pv") or []),
             "best": best.get("pv") or [best["move"]]},
        )

        my_node = tree_touch(self.conn, self.root_hash, rs["node_id"],
                             move.uci(), after, self.depth_level, after_phase)
        self._record(rs, move.uci(), result["verdict"], result["delta_wp"])
        self.results[self.index] = result["tone"]
        rs["answer"] = {
            "my_move": move.uci(), "my_san": san,
            "best_move": best["move"], "best_san": best_san,
            "wp_best": best["wp"], "wp_mine": mine.get("wp"),
            "my_node": my_node, "fen_after": after.fen(),
            "explanation": exp.to_json(), **result,
        }
        return rs["answer"]

    def show(self) -> dict:
        """Asking to be shown the move marks the round shown. It counts as
        seen. It does not count as correct."""
        rs = self.round_state
        if rs is None:
            raise ValueError("no round in progress")
        board = chess.Board(rs["fen"])
        best = self.best_line()
        if best is None:
            raise RuntimeError("no engine evaluation for this position")
        move = chess.Move.from_uci(best["move"])
        after = board.copy(stack=False)
        after.push(move)
        self._record(rs, None, "shown", None)
        self.results[self.index] = "shown"
        rs["answer"] = {
            "my_move": None, "my_san": None,
            "best_move": best["move"], "best_san": board.san(move),
            "wp_best": best["wp"], "wp_mine": None,
            "verdict": "shown", "label": grading.LABELS["shown"],
            "delta_wp": None, "delta_cp": None,
            "fen_after": after.fen(),
            "explanation": explain_mod.explain(
                rs["fen"], None, best["move"],
                {"mine": [], "best": best.get("pv") or [best["move"]]},
            ).to_json(),
        }
        return rs["answer"]

    def _record(self, rs, my_move, verdict, delta_wp) -> None:
        """Only real answers are recorded. Navigation records nothing."""
        self.conn.execute(
            "INSERT INTO answers(pos_hash, root_hash, depth_level, opp_move,"
            " my_move, delta_wp, verdict, answered_at) VALUES(?,?,?,?,?,?,?,?)",
            (rs["hash"], self.hash, self.depth_level,
             rs["candidate"]["uci"], my_move, delta_wp, verdict,
             int(time.time())),
        )
        self.conn.commit()

    def has_next_round(self) -> bool:
        return self.index + 1 < len(self.order)

    def next_round(self) -> bool:
        if not self.has_next_round():
            return False
        self.start_round(self.index + 1)
        return True

    # -- state for the client
    def to_json(self) -> dict:
        rs = self.round_state
        board = chess.Board(rs["fen"]) if rs else self.board
        state = {
            "mode": self.mode,
            "name": self.name,
            "source": self.source,
            "root_fen": self.fen,
            "root_hash": str(self.hash),
            "tree_root": str(self.root_hash),
            "depth_level": self.depth_level,
            "max_depth_level": MAX_DEPTH_LEVEL,
            "my_colour": "white" if self.my_colour == chess.WHITE else "black",
            "phase": self.phase,
            "round": self.index + 1,
            "rounds": len(self.order),
            "round_results": list(self.results),
            "has_next_round": self.has_next_round(),
            "node_id": self.node_id,
        }
        if rs:
            state.update({
                "fen": rs["fen"],
                # The position before their move, so the board can play it out
                # rather than cutting to the answer.
                "base_fen": self.fen,
                "opp_move": rs["candidate"]["uci"],
                "opp_san": rs["candidate"]["san"],
                "answer": rs["answer"],
                "node_id_current": rs["node_id"],
                "legal": legal_map(board),
                "to_move": "white" if board.turn == chess.WHITE else "black",
                "can_answer": rs["answer"] is None,
            })
        else:
            state.update({"fen": self.fen, "legal": {}, "answer": None,
                          "can_answer": False,
                          "to_move": "white" if self.board.turn == chess.WHITE else "black"})
        return state


def _outcome(board: chess.Board) -> dict | None:
    """A finished game has no engine lines at all. Checkmate is not a 50%
    position, and the grader must not be handed one."""
    if board.is_checkmate():
        return {"cp": None, "mate": 1, "wp": 100.0, "pv": [], "final": "mate"}
    if board.is_game_over(claim_draw=True):
        return {"cp": 0, "mate": None, "wp": 50.0, "pv": [], "final": "draw"}
    return None


def _flip(line: dict) -> dict:
    """A line scored for the side to move, re-scored for the other side."""
    out = dict(line)
    if line.get("cp") is not None:
        out["cp"] = -line["cp"]
    if line.get("mate") is not None:
        out["mate"] = -line["mate"]
    out["wp"] = round(100.0 - (line.get("wp") or 50.0), 3)
    return out


def legal_map(board: chess.Board) -> dict:
    """The client holds no chess logic: it is told what is clickable."""
    out: dict = {}
    for move in board.legal_moves:
        src = chess.square_name(move.from_square)
        dst = chess.square_name(move.to_square)
        entry = out.setdefault(src, [])
        if move.promotion:
            entry.append({"to": dst, "promotion": chess.piece_symbol(move.promotion)})
        else:
            entry.append({"to": dst})
    return out


def board_array(fen: str) -> list:
    """[{square, piece}] for rendering. 'P' white pawn, 'p' black pawn."""
    board = chess.Board(fen)
    out = []
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece:
            out.append({"square": chess.square_name(sq), "piece": piece.symbol()})
    return out


# --- leaks -----------------------------------------------------------------

_MISS_SQL = "(" + ", ".join(f"'{v}'" for v in grading.MISSES) + ")"


def _cached_any(conn, pos_hash: int, pool, board):
    """The cached best line for a position. ./run leaks is read-only and never
    starts an engine, so it reads the cache directly -- newest entry wins,
    whichever engine version wrote it."""
    if pool is not None:
        lines = pool.cached(board, DEPTH_GRADE, 1)
        if lines:
            return lines
    row = conn.execute(
        "SELECT lines FROM analysis WHERE pos_hash=? AND depth>=? AND multipv=1"
        " ORDER BY depth DESC, computed_at DESC LIMIT 1",
        (pos_hash, DEPTH_GRADE),
    ).fetchone()
    return json.loads(row["lines"]) if row else None


def leaks(conn, pool=None, limit: int = 20) -> dict:
    """Which move do I keep getting wrong, and how deep does it start."""
    rows = conn.execute(
        "SELECT a.pos_hash, COUNT(*) attempts,"
        f" SUM(a.verdict IN {_MISS_SQL}) misses,"
        " MAX(a.answered_at) last"
        " FROM answers a GROUP BY a.pos_hash HAVING misses > 0"
    ).fetchall()
    out = []
    for r in rows:
        rate = r["misses"] / r["attempts"]
        worst = conn.execute(
            "SELECT my_move, COUNT(*) n FROM answers WHERE pos_hash=?"
            f" AND verdict IN {_MISS_SQL} AND my_move IS NOT NULL"
            " GROUP BY my_move ORDER BY n DESC LIMIT 1", (r["pos_hash"],),
        ).fetchone()
        # The answered position sits one move past the pooled one, so the
        # group it belongs to is keyed by the drill root.
        pos = conn.execute(
            "SELECT p.fen, p.name, p.phase FROM positions p"
            " JOIN answers a ON a.root_hash = p.pos_hash"
            " WHERE a.pos_hash=? LIMIT 1", (r["pos_hash"],),
        ).fetchone()
        fen_here = conn.execute(
            "SELECT fen FROM tree_nodes WHERE pos_hash=? LIMIT 1",
            (r["pos_hash"],),
        ).fetchone()
        fen = fen_here["fen"] if fen_here else None
        best_san = my_san = None
        if fen:
            board = chess.Board(fen)
            if worst and worst["my_move"]:
                try:
                    my_san = board.san(chess.Move.from_uci(worst["my_move"]))
                except (ValueError, AssertionError):
                    my_san = worst["my_move"]
            lines = _cached_any(conn, r["pos_hash"], pool, board)
            if lines:
                try:
                    best_san = board.san(chess.Move.from_uci(lines[0]["move"]))
                except (ValueError, AssertionError):
                    best_san = lines[0]["move"]
        out.append({
            "pos_hash": str(r["pos_hash"]), "fen": fen,
            "attempts": r["attempts"], "misses": r["misses"],
            "rate": round(rate, 2), "score": round(rate * r["attempts"], 2),
            "you_play": my_san, "refuted_by": best_san,
            "name": pos["name"] if pos else None,
            "phase": pos["phase"] if pos else None,
        })
    out.sort(key=lambda e: -e["score"])

    depths = [dict(r) for r in conn.execute(
        "SELECT depth_level, COUNT(*) attempts,"
        " ROUND(100.0*SUM(verdict='best')/COUNT(*), 1) accuracy"
        " FROM answers GROUP BY depth_level ORDER BY depth_level"
    ).fetchall()]
    families = [dict(r) for r in conn.execute(
        "SELECT p.name, p.phase, COUNT(*) attempts,"
        " ROUND(100.0*SUM(a.verdict='best')/COUNT(*), 1) accuracy"
        " FROM answers a JOIN positions p ON p.pos_hash=a.root_hash"
        " GROUP BY p.name, p.phase ORDER BY accuracy ASC, attempts DESC"
    ).fetchall()]
    return {"positions": out[:limit], "by_depth": depths, "by_group": families}
