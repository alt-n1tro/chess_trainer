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
DEPTH_FILTER = 12       # shallow pass during phases --build
MULTIPV_CANDIDATES = 5


class EngineMissing(RuntimeError):
    pass


def logistic_wp(cp: int) -> float:
    """Lichess logistic, for cached values that predate UCI_ShowWDL."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


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
        wdl = info.get("wdl")
        if wdl is not None:
            w = wdl.pov(board.turn)
            wp = 100.0 * (w.wins + w.draws / 2) / max(1, w.total())
        elif pov is not None:
            w = pov.wdl(model="sf", ply=board.ply())
            wp = 100.0 * (w.wins + w.draws / 2) / max(1, w.total())
        else:
            wp = 50.0
        out.append(
            {
                "move": pv[0].uci() if pv else None,
                "cp": cp,
                "mate": mate,
                "wp": round(wp, 3),
                "pv": [m.uci() for m in pv[:12]],
            }
        )
    out = [l for l in out if l["move"]]
    return out[:multipv]


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
        row = conn.execute(
            "SELECT lines FROM analysis WHERE pos_hash=? AND depth=? AND multipv=?"
            " AND engine_ver=?",
            (db.pos_hash(board, phase), depth, multipv, self.version),
        ).fetchone()
        return json.loads(row["lines"]) if row else None

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
