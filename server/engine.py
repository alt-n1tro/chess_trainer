"""Stockfish pool, UCI handling, analysis cache.

One persistent process per pool slot, pool size 2. Slot A serves the
foreground request; slot B is the warmer. Processes start once at boot and live
until shutdown, because the transposition table is where the speed is and a
spawn-per-request design throws it away.
"""
from __future__ import annotations

import json
import math
import os
import queue
import threading
import time

import chess
import chess.engine

from . import db

ROOT = db.ROOT
ENGINE_PATH = os.path.join(ROOT, "vendor", "stockfish")

DEPTH_CANDIDATES = 20   # opponent's five candidate moves, MultiPV 5
DEPTH_GRADE = 20        # grading your reply, MultiPV 1
MULTIPV_CANDIDATES = 5  # for ranking your reply
MULTIPV_ROOT = 8        # the opponent's options, before blunders are dropped


class EngineMissing(RuntimeError):
    pass


def logistic_wp(cp: int) -> float:
    """Centipawns to win probability, the Lichess logistic.

    This, and not Stockfish's UCI_ShowWDL, is what verdicts are measured in.
    The engine's WDL is normalised per ply: at move two it maps +111cp to 86%,
    because a pawn that early really does win that often *between engines*. For
    a human training tool that makes every second-rate opening move read as a
    catastrophe. The logistic is stationary, so the same centipawn loss means
    the same thing on move 2 and on move 40.
    """
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def line_wp(line: dict) -> float:
    """Win probability for a stored line, from your side to move."""
    if line.get("mate") is not None:
        return 100.0 if line["mate"] > 0 else 0.0
    if line.get("cp") is not None:
        return round(logistic_wp(line["cp"]), 3)
    return wp_or_even(line.get("wp"))


def wp_or_even(value) -> float:
    """A missing win probability is an even one. A zero is not missing: a
    side that is being mated is at 0.0, and ``or 50.0`` would read it as
    even, which is exactly backwards."""
    return 50.0 if value is None else float(value)


def with_wp(lines: list[dict]) -> list[dict]:
    """Recompute wp on every read, so entries cached under an older rule are
    graded by the current one without invalidating the cache. Entries written
    before the sort above are re-sorted here for the same reason."""
    for line in lines:
        line["wp"] = line_wp(line)
    lines.sort(key=_rank_key, reverse=True)
    return lines


def threads_per_process() -> int:
    """Two threads each, unless the machine has fewer than six cores."""
    return 2 if (os.cpu_count() or 1) >= 6 else 1


class Slot:
    """One persistent UCI process."""

    def __init__(self, path: str = ENGINE_PATH):
        if not (os.path.exists(path) and os.access(path, os.X_OK)):
            raise EngineMissing(
                f"No executable engine at {path}. See vendor/README.md."
            )
        self.lock = threading.Lock()
        self.engine = chess.engine.SimpleEngine.popen_uci(path)
        self.version = self.engine.id.get("name", "unknown")
        opts = self.engine.options
        cfg = {}
        if "Threads" in opts:
            cfg["Threads"] = threads_per_process()
        if "Hash" in opts:
            cfg["Hash"] = 256
        if "UCI_ShowWDL" in opts:
            cfg["UCI_ShowWDL"] = True
        if cfg:
            self.engine.configure(cfg)

    def analyse(self, board: chess.Board, depth: int, multipv: int):
        with self.lock:
            return self.engine.analyse(
                board,
                chess.engine.Limit(depth=depth),
                multipv=multipv if multipv > 1 else None,
            )

    def close(self) -> None:
        try:
            self.engine.quit()
        except Exception:
            pass


def _lines_from_info(board: chess.Board, infos, multipv: int) -> list[dict]:
    """Normalise python-chess info dicts into our cached line format.

    Scores and WDL are stored from the point of view of the side to move.
    """
    if isinstance(infos, dict):
        infos = [infos]
    out = []
    for info in infos:
        pv = info.get("pv") or []
        score = info.get("score")
        pov = score.pov(board.turn) if score is not None else None
        cp = pov.score() if pov is not None else None
        mate = pov.mate() if pov is not None else None
        # The engine's own WDL is recorded for reference; grading uses the
        # stationary logistic in line_wp() instead. See logistic_wp().
        wdl = info.get("wdl")
        engine_wp = None
        if wdl is not None:
            w = wdl.pov(board.turn)
            engine_wp = round(100.0 * (w.wins + w.draws / 2) / max(1, w.total()), 3)
        line = {
            "move": pv[0].uci() if pv else None,
            "cp": cp,
            "mate": mate,
            "engine_wp": engine_wp,
            "pv": [m.uci() for m in pv[:12]],
        }
        line["wp"] = line_wp(line)
        out.append(line)
    # Sort by score. MultiPV slots are refreshed at different points inside
    # the final iteration, so the engine's own slot order can disagree with
    # the scores it last reported for them -- and then "the best move" would
    # be whichever line happened to be refreshed first.
    out = [l for l in out if l["move"]]
    out.sort(key=_rank_key, reverse=True)
    return out[:multipv]


def _rank_key(line: dict) -> float:
    """How good a line is, from the side to move. Mate beats any centipawn
    score; mate against is worse than any of them."""
    mate = line.get("mate")
    if mate is not None:
        return 1e6 - mate if mate > 0 else -1e6 - mate
    return float(line.get("cp") or 0)


class Pool:
    """Two slots: foreground and warmer, plus the analysis cache."""

    def __init__(self, path: str = ENGINE_PATH, db_path: str = db.DB_PATH):
        self.db_path = db_path
        self.fg = Slot(path)
        try:
            self.bg = Slot(path)
        except Exception:
            self.bg = None
        self.version = self.fg.version
        self._warm_queue: queue.Queue = queue.Queue()
        self._warm_generation = 0
        self._warm_lock = threading.Lock()
        self._stop = threading.Event()
        self._warmer = threading.Thread(target=self._warm_loop, daemon=True)
        self._warmer.start()

    # --- cache ------------------------------------------------------------

    def cached(self, board: chess.Board, depth: int, multipv: int, phase=None):
        conn = db.connect(self.db_path)
        # Anything at least this deep answers the question; the deepest wins.
        # Shallower never does: a lookup at 20 that finds only 12 is a miss.
        # A wider entry is a superset of a narrower one, so it serves too.
        row = conn.execute(
            "SELECT lines, depth FROM analysis WHERE pos_hash=? AND depth>=?"
            " AND multipv>=? AND engine_ver=? ORDER BY depth DESC, multipv DESC"
            " LIMIT 1",
            (db.pos_hash(board, phase), depth, multipv, self.version),
        ).fetchone()
        if not row:
            return None
        lines = with_wp(json.loads(row["lines"]))
        for line in lines:
            line["depth"] = row["depth"]
        return lines

    def _store(self, board, depth, multipv, lines, phase=None) -> None:
        conn = db.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO analysis"
            "(pos_hash, depth, multipv, engine_ver, fen, lines, computed_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                db.pos_hash(board, phase),
                depth,
                multipv,
                self.version,
                board.fen(),
                json.dumps(lines),
                int(time.time()),
            ),
        )
        conn.commit()

    def analyse(self, board: chess.Board, depth: int, multipv: int = 1,
                slot: str = "fg", phase=None) -> list[dict]:
        """Cached analysis. A miss at this exact parameter set recomputes."""
        hit = self.cached(board, depth, multipv, phase)
        if hit is not None:
            return hit
        if board.is_game_over(claim_draw=True):
            return []
        target = self.bg if (slot == "bg" and self.bg) else self.fg
        infos = target.analyse(board, depth, multipv)
        lines = _lines_from_info(board, infos, multipv)
        self._store(board, depth, multipv, lines, phase)
        for line in lines:
            line["depth"] = depth
        return with_wp(lines)

    def analyse_deep(self, board: chess.Board, base: int, multipv: int = 1,
                     budget: float = 0.6, cap: int = 40, step: int = 4,
                     slot: str = "fg", phase=None) -> list[dict]:
        """Search to `base`, then keep deepening while the search stays cheap.

        Simple positions -- endgames above all -- reach the base depth in
        milliseconds and go on to 30 or 40 in the same time a middlegame
        takes to reach the base. The engine keeps its hash table between
        calls, so each step builds on the last rather than starting over.
        The result is cached at the depth actually reached.
        """
        hit = self.cached(board, base, multipv, phase)
        started = time.monotonic()
        depth = hit[0]["depth"] if hit else base
        lines = hit or self.analyse(board, base, multipv, slot=slot, phase=phase)
        if not lines:
            return lines
        while depth < cap and time.monotonic() - started < budget:
            depth = min(cap, depth + step)
            lines = self.analyse(board, depth, multipv, slot=slot, phase=phase)
            if not lines:
                break
        return lines

    # --- warming ----------------------------------------------------------

    def warm(self, jobs: list[tuple[str, int, int]], reset: bool = True) -> None:
        """Queue (fen, depth, multipv) jobs. The warmer is cancelled and
        requeued the moment the foreground position changes."""
        with self._warm_lock:
            if reset:
                self._warm_generation += 1
                try:
                    while True:
                        self._warm_queue.get_nowait()
                except queue.Empty:
                    pass
            gen = self._warm_generation
            for job in jobs:
                self._warm_queue.put((gen, job))

    def _warm_loop(self) -> None:
        while not self._stop.is_set():
            try:
                gen, (fen, depth, multipv) = self._warm_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._warm_lock:
                if gen != self._warm_generation:
                    continue
            try:
                board = chess.Board(fen)
                self.analyse(board, depth, multipv, slot="bg")
            except Exception:
                pass

    def close(self) -> None:
        self._stop.set()
        self.fg.close()
        if self.bg:
            self.bg.close()


def probe(path: str = ENGINE_PATH) -> dict:
    """For ./run doctor: does the binary answer uci, and does it have NNUE?"""
    info: dict = {"path": path, "ok": False}
    if not os.path.exists(path):
        info["error"] = "missing"
        return info
    if not os.access(path, os.X_OK):
        info["error"] = "not executable"
        return info
    try:
        eng = chess.engine.SimpleEngine.popen_uci(path)
    except Exception as exc:
        info["error"] = f"does not answer uci: {exc}"
        return info
    try:
        info["version"] = eng.id.get("name", "unknown")
        opts = eng.options
        nnue = True
        if "Use NNUE" in opts:
            nnue = bool(opts["Use NNUE"].default)
        elif "EvalFile" not in opts:
            nnue = False
        info["nnue"] = nnue
        info["threads"] = threads_per_process()
        info["pool_size"] = 2
        info["ok"] = nnue
        if not nnue:
            info["error"] = "NNUE net missing"
    finally:
        eng.quit()
    return info
