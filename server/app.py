"""HTTP server, routing, JSON API.

Localhost only. The server owns the game state; the client renders exactly what
it is told and holds no chess logic.
"""
from __future__ import annotations

import io
import json
import os
import posixpath
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chess
import chess.pgn

from . import corpus, db, drills, engine
from .drills import Drill

WEB_DIR = os.path.join(db.ROOT, "web")
HOST, PORT = "127.0.0.1", 8770

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
        ".json": "application/json", ".ico": "image/x-icon"}


class Trainer:
    """Application state. One local user, so one of everything."""

    def __init__(self, pool: engine.Pool):
        self.lock = threading.RLock()
        self.pool = pool
        self.conn = db.connect()
        self.mode = db.meta_get(self.conn, "mode", "openings")
        if self.mode not in drills.MODES:
            self.mode = "openings"
        self.stack: list[Drill] = []
        self.game = None            # game-walk state, when one is loaded

    # -- helpers
    @property
    def drill(self) -> Drill | None:
        return self.stack[-1] if self.stack else None

    def conn_t(self):
        return db.connect()

    def set_mode(self, mode: str) -> None:
        if mode not in drills.MODES:
            raise ValueError(f"no such mode: {mode}")
        self.mode = mode
        db.meta_set(self.conn_t(), "mode", mode)

    # -- starting drills
    def new_drill(self, fen: str, *, name=None, source=None,
                  mode=None) -> Drill:
        drill = Drill(self.conn_t(), self.pool, fen, mode or self.mode,
                      name=name, source=source)
        self.stack = [drill]
        self.game = None
        return drill

    def random_drill(self, name=None, colour=None) -> Drill:
        conn = self.conn_t()
        row = drills.pick_random(conn, self.mode, name, colour)
        if row is None:
            raise LookupError(EMPTY_POOL[self.mode])
        return self.new_drill(row["fen"], name=row["name"],
                              source={"kind": "pool", "phase": row["phase"],
                                      "tail": row["tail_san"],
                                      "game": self._game_label(row["source_game"])})

    def _game_label(self, game_id) -> dict | None:
        if not game_id:
            return None
        row = self.conn_t().execute(
            "SELECT white, black, white_elo, black_elo, result, my_colour, url"
            " FROM games WHERE id=?", (game_id,),
        ).fetchone()
        return dict(row) if row else None

    def deeper(self) -> Drill:
        """Take the position after your move one level deeper and ask the same
        question there."""
        cur = self.drill
        if cur is None or cur.round_state is None:
            raise ValueError("nothing to go deeper from")
        answer = cur.round_state["answer"]
        if answer is None:
            raise ValueError("answer first")
        if cur.depth_level + 1 > drills.MAX_DEPTH_LEVEL:
            raise ValueError(f"Depth is capped at {drills.MAX_DEPTH_LEVEL} levels.")
        node_id = answer.get("my_node")
        drill = Drill(self.conn_t(), self.pool, answer["fen_after"], cur.mode,
                      name=cur.name, source=cur.source,
                      depth_level=cur.depth_level + 1,
                      root_hash=cur.root_hash, node_id=node_id)
        self.stack.append(drill)
        return drill

    def back(self) -> Drill | None:
        """Steps up. Navigating is not answering; nothing is recorded."""
        if len(self.stack) > 1:
            self.stack.pop()
        elif self.drill is not None:
            self.drill.replay()
        return self.drill

    def start(self) -> Drill | None:
        if self.stack:
            self.stack = self.stack[:1]
        return self.drill

    def goto(self, node_id: int) -> Drill:
        """Clicking any row re-enters that position -- ancestor, sibling,
        anything."""
        conn = self.conn_t()
        row = conn.execute("SELECT * FROM tree_nodes WHERE id=?",
                           (node_id,)).fetchone()
        if row is None:
            raise LookupError("no such tree row")
        board = chess.Board(row["fen"])
        cur = self.drill
        base = cur.source if cur else None
        name = cur.name if cur else None
        if board.turn == (cur.opponent if cur else board.turn):
            drill = Drill(conn, self.pool, row["fen"], self.mode, name=name,
                          source=base, depth_level=row["depth_level"],
                          root_hash=row["root_hash"], node_id=row["id"])
            self.stack = (self.stack[:1] if self.stack else []) + [drill]
            return drill
        # You are to move here: re-enter the parent ask-root on this candidate.
        parent = conn.execute("SELECT * FROM tree_nodes WHERE id=?",
                              (row["parent_id"],)).fetchone()
        if parent is None:
            raise LookupError("that row has no question above it")
        drill = Drill(conn, self.pool, parent["fen"], self.mode, name=name,
                      source=base, depth_level=parent["depth_level"],
                      root_hash=parent["root_hash"], node_id=parent["id"])
        for i, cand in enumerate(drill.candidates):
            if cand["uci"] == row["move_uci"]:
                drill.select_candidate(i)
                break
        self.stack = (self.stack[:1] if self.stack else []) + [drill]
        return drill

    # -- state for the client
    def state(self) -> dict:
        # "kind" marks a full state payload. Several endpoints also return a
        # "mode" field, and the client must not mistake those for one.
        out = {"kind": "state", "mode": self.mode, "modes": self.mode_summary(),
               "stack_depth": len(self.stack), "game": self.game_state()}
        drill = self.drill
        if drill is not None:
            out["drill"] = drill.to_json()
            out["drill"]["board"] = drills.board_array(out["drill"]["fen"])
            out["drill"]["can_deeper"] = (
                drill.round_state is not None
                and drill.round_state["answer"] is not None
                and drill.depth_level < drills.MAX_DEPTH_LEVEL
            )
            out["tree"] = drills.tree_rows(self.conn_t(), drill.root_hash)
        return out

    def mode_summary(self) -> list[dict]:
        conn = self.conn_t()
        out = []
        for mode in drills.MODES:
            row = conn.execute("SELECT COUNT(*) n FROM positions WHERE phase=?",
                               (drills.MODE_PHASE[mode],)).fetchone()
            out.append({"mode": mode, "count": row["n"] or 0,
                        "active": mode == self.mode})
        return out

    # -- game walk
    def load_game(self, url_or_pgn: str) -> dict:
        conn = self.conn_t()
        url = corpus.canonical_url(url_or_pgn.strip())
        row = conn.execute("SELECT * FROM games WHERE url=?", (url,)).fetchone()
        if row is None and url_or_pgn.strip().startswith("http"):
            ids = corpus.import_source(conn, url_or_pgn.strip())
            if not ids:
                raise LookupError("Nothing importable at that link.")
            row = conn.execute("SELECT * FROM games WHERE id=?", (ids[0],)).fetchone()
        elif row is None:
            ids = corpus.import_pgn_text(conn, url_or_pgn)
            if not ids:
                raise LookupError("That is not a PGN and not a chess.com game link.")
            row = conn.execute("SELECT * FROM games WHERE id=?", (ids[0],)).fetchone()
        game = chess.pgn.read_game(io.StringIO(row["pgn"]))
        if game is None:
            raise LookupError("The stored PGN will not parse.")
        board = chess.Board()
        moves = []
        node = game
        while node.variations:
            node = node.variations[0]
            moves.append({"uci": node.move.uci(), "san": board.san(node.move),
                          "fen_before": board.fen(),
                          "number": board.fullmove_number,
                          "white": board.turn == chess.WHITE,
                          "phase": db.classify_phase(board)})
            board.push(node.move)
        colour = row["my_colour"] or "white"
        self.game = {
            "id": row["id"], "url": row["url"],
            "white": row["white"], "black": row["black"],
            "white_elo": row["white_elo"], "black_elo": row["black_elo"],
            "result": row["result"], "my_colour": row["my_colour"],
            "foreign": row["my_colour"] is None,
            "moves": moves, "ply": len(moves), "colour": colour,
            "final_fen": board.fen(),
        }
        self.stack = []
        return self.game

    def game_state(self) -> dict | None:
        if not self.game:
            return None
        g = dict(self.game)
        ply = g["ply"]
        fen = (self.game["moves"][ply]["fen_before"] if ply < len(self.game["moves"])
               else self.game["final_fen"])
        g["fen"] = fen
        g["board"] = drills.board_array(fen)
        g["flip"] = g["colour"] == "black"
        return g

    def game_goto(self, ply: int) -> dict:
        if not self.game:
            raise ValueError("no game loaded")
        self.game["ply"] = max(0, min(int(ply), len(self.game["moves"])))
        return self.game_state()

    def game_colour(self, colour: str) -> dict:
        if not self.game:
            raise ValueError("no game loaded")
        self.game["colour"] = "black" if colour == "black" else "white"
        return self.game_state()

    def play_from_here(self) -> Drill:
        """Turns the current position into a drill: opponent to move, five
        candidate paths, its own tree."""
        if not self.game:
            raise ValueError("no game loaded")
        state = self.game_state()
        board = chess.Board(state["fen"])
        want = chess.WHITE if self.game["colour"] == "white" else chess.BLACK
        if board.turn == want:
            raise ValueError(
                "It is your move in this position. Step one move back, or "
                "switch colour: a drill starts with the opponent to move."
            )
        label = {"kind": "game", "game": {
            "white": self.game["white"], "black": self.game["black"],
            "white_elo": self.game["white_elo"], "black_elo": self.game["black_elo"],
            "result": self.game["result"], "my_colour": self.game["my_colour"],
            "url": self.game["url"]}}
        drill = self.new_drill(state["fen"], name=None, source=label)
        return drill


EMPTY_POOL = {
    "openings": "No openings yet. Import games with ./run import <pgn-or-url>,"
                " then ./run phases --build.",
    "middlegame": "The middlegame pool is empty. Run ./run phases --build.",
    "endgame": "The endgame pool is empty. Run ./run phases --build.",
}


# --- editor ---------------------------------------------------------------

def build_fen(pieces: dict, turn: str, castling: str) -> str:
    board = chess.Board(None)
    for square, symbol in pieces.items():
        try:
            sq = chess.parse_square(square)
            piece = chess.Piece.from_symbol(symbol)
        except ValueError as exc:
            raise ValueError(f"{square}/{symbol}: {exc}") from exc
        board.set_piece_at(sq, piece)
    board.turn = chess.BLACK if turn == "black" else chess.WHITE
    board.set_castling_fen(castling or "-")
    return board.fen()


def validate_setup(pieces: dict, turn: str, castling: str) -> str:
    """Refuse invalid positions with the specific reason."""
    fen = build_fen(pieces, turn, castling)
    board = chess.Board(fen)
    for colour, name in ((chess.WHITE, "White"), (chess.BLACK, "Black")):
        n = len(board.pieces(chess.KING, colour))
        if n != 1:
            raise ValueError(f"{name} needs exactly one king, not {n}.")
    for sq in board.pieces(chess.PAWN, chess.WHITE) | board.pieces(chess.PAWN, chess.BLACK):
        rank = chess.square_rank(sq)
        if rank in (0, 7):
            raise ValueError(
                f"A pawn on {chess.square_name(sq)} cannot exist."
            )
    status = board.status()
    if status & chess.STATUS_OPPOSITE_CHECK:
        raise ValueError("The side not to move is in check; that cannot happen.")
    wanted = (castling or "").replace("-", "")
    got = board.castling_xfen().replace("-", "")
    missing = [c for c in wanted if c not in got]
    if missing:
        raise ValueError(
            "Castling rights " + "".join(missing) +
            " do not match where the kings and rooks are."
        )
    if board.turn == chess.WHITE and not any(True for _ in board.legal_moves):
        pass   # a stalemate/mate position is a legal thing to set up
    return fen


# --- HTTP ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ChessTrainer/1.0"
    trainer: Trainer = None        # set by serve()

    def log_message(self, fmt, *args):
        return                      # the terminal is for ./run output, not a log

    def log_error(self, fmt, *args):
        return

    # -- plumbing
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The tab was closed or reloaded while a long analysis was running.
            # Nothing to report: the work is cached either way.
            self.close_connection = True

    def json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def fail(self, exc: Exception, code: int = 400) -> None:
        self.json({"error": str(exc) or exc.__class__.__name__}, code)

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routes
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            return self.api(path, {})
        return self.static(path)

    do_HEAD = do_GET

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            return self._send(404, b"not found", "text/plain")
        return self.api(path, self.body())

    def static(self, path: str):
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        rel = posixpath.normpath(path.lstrip("/")) or "index.html"
        if rel.startswith(".."):
            return self._send(403, b"no", "text/plain")
        full = os.path.join(WEB_DIR, rel)
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        with open(full, "rb") as fh:
            data = fh.read()
        ext = os.path.splitext(full)[1]
        return self._send(200, data, MIME.get(ext, "application/octet-stream"))

    def api(self, path: str, data: dict):
        t = self.trainer
        try:
            with t.lock:
                return self.route(t, path, data)
        except (ValueError, LookupError) as exc:
            return self.fail(exc, 400)
        except Exception as exc:                      # pragma: no cover
            traceback.print_exc()
            return self.fail(exc, 500)

    def _with_drill(self, t: Trainer, name=None, colour=None) -> dict:
        """Switching mode starts a drill in that mode. An empty pool is not an
        error that loses the mode switch -- it is a message."""
        try:
            t.random_drill(name, colour)
            return t.state()
        except LookupError as exc:
            t.stack, t.game = [], None
            state = t.state()
            state["message"] = str(exc)
            return state

    def route(self, t: Trainer, path: str, data: dict):
        conn = t.conn_t()

        if path == "/api/state":
            return self.json(t.state())

        if path == "/api/mode":
            t.set_mode(data.get("mode", ""))
            return self.json(self._with_drill(t))

        if path == "/api/random":
            return self.json(self._with_drill(t, data.get("name"),
                                              data.get("colour")))

        if path == "/api/drill":
            if data.get("name"):
                t.random_drill(data["name"], data.get("colour"))
            elif data.get("saved_id"):
                row = conn.execute("SELECT * FROM saved WHERE id=?",
                                   (data["saved_id"],)).fetchone()
                if row is None:
                    raise LookupError("no such saved position")
                t.new_drill(row["fen"], name=row["name"],
                            source={"kind": "saved"})
            elif data.get("fen"):
                chess.Board(data["fen"])          # raises on nonsense
                t.new_drill(data["fen"], name=data.get("label"),
                            source={"kind": "custom"})
            else:
                raise ValueError("nothing to drill")
            return self.json(t.state())

        if path == "/api/answer":
            drill = t.drill
            if drill is None:
                raise ValueError("no drill in progress")
            uci = data.get("uci")
            if not uci and data.get("from") and data.get("to"):
                uci = data["from"] + data["to"] + (data.get("promotion") or "")
            if not uci:
                raise ValueError("no move given")
            drill.answer(uci)
            return self.json(t.state())

        if path == "/api/select":
            drill = t.drill
            if drill is None:
                raise ValueError("no drill in progress")
            drill.select_candidate(int(data.get("index", 0)))
            return self.json(t.state())

        if path == "/api/show":
            if t.drill is None:
                raise ValueError("no drill in progress")
            t.drill.show()
            return self.json(t.state())

        if path == "/api/next":
            drill = t.drill
            if drill is None:
                raise ValueError("no drill in progress")
            if not drill.next_round():
                # Out of rounds: the next position comes from the pool, and an
                # empty pool is a message rather than a dead end.
                return self.json(self._with_drill(t))
            return self.json(t.state())

        if path == "/api/deeper":
            t.deeper()
            return self.json(t.state())

        if path == "/api/reset":
            if t.drill is None:
                raise ValueError("no drill in progress")
            t.drill.replay()
            return self.json(t.state())

        if path == "/api/restart":
            if t.drill is None:
                raise ValueError("no drill in progress")
            t.drill.restart()
            return self.json(t.state())

        if path == "/api/back":
            t.back()
            return self.json(t.state())

        if path == "/api/start":
            t.start()
            return self.json(t.state())

        if path == "/api/goto":
            t.goto(int(data.get("node_id")))
            return self.json(t.state())

        if path == "/api/save":
            drill = t.drill
            if drill is None:
                raise ValueError("nothing to save")
            fen = data.get("fen") or (drill.round_state or {}).get("fen") or drill.fen
            conn.execute(
                "INSERT INTO saved(fen, name, saved_at, from_root)"
                " VALUES(?,?,strftime('%s','now'),?)",
                (fen, data.get("name") or drill.name or "Saved position",
                 drill.root_hash),
            )
            conn.commit()
            return self.json({"ok": True})

        if path == "/api/saved":
            rows = conn.execute(
                "SELECT id, fen, name, saved_at FROM saved ORDER BY id DESC"
            ).fetchall()
            return self.json({"saved": [dict(r) for r in rows]})

        if path == "/api/groups":
            return self.json({"groups": drills.groups(conn, t.mode),
                              "mode": t.mode})

        if path == "/api/editor/validate":
            fen = validate_setup(data.get("pieces") or {},
                                 data.get("turn") or "white",
                                 data.get("castling") or "-")
            return self.json({"ok": True, "fen": fen})

        if path == "/api/editor/set":
            fen = validate_setup(data.get("pieces") or {},
                                 data.get("turn") or "white",
                                 data.get("castling") or "-")
            t.new_drill(fen, name=data.get("name") or "Hand-set position",
                        source={"kind": "custom"})
            return self.json(t.state())

        if path == "/api/game/load":
            t.load_game(data.get("url") or data.get("pgn") or "")
            return self.json(t.state())

        if path == "/api/game/goto":
            t.game_goto(data.get("ply", 0))
            return self.json(t.state())

        if path == "/api/game/colour":
            t.game_colour(data.get("colour", "white"))
            return self.json(t.state())

        if path == "/api/game/play":
            t.play_from_here()
            return self.json(t.state())

        if path == "/api/game/setup":
            if not t.game:
                raise ValueError("no game loaded")
            return self.json({"fen": t.game_state()["fen"]})

        if path == "/api/leaks":
            return self.json(drills.leaks(conn, t.pool))

        if path == "/api/stats":
            row = conn.execute(
                "SELECT COUNT(*) answers, SUM(verdict='best') best,"
                " SUM(verdict IN ('second','third','fourth','fifth','good')) good,"
                " SUM(verdict='inaccuracy') inaccuracy,"
                " SUM(verdict='mistake') mistake,"
                " SUM(verdict IN ('blunder','missed_mate')) blunder,"
                " SUM(verdict='shown') shown FROM answers"
            ).fetchone()
            cached = conn.execute("SELECT COUNT(*) n FROM analysis").fetchone()
            return self.json({"answers": dict(row),
                              "cached_positions": cached["n"],
                              "engine": t.pool.version,
                              "modes": t.mode_summary()})

        if path == "/api/import":
            ids = corpus.import_source(conn, data.get("source") or "")
            return self.json({"imported": len(ids)})

        return self.json({"error": "no such endpoint"}, 404)


def serve(host: str = HOST, port: int = PORT) -> None:
    db.init()
    pool = engine.Pool()
    Handler.trainer = Trainer(pool)
    trainer = Handler.trainer
    try:                     # a drill is already running when the page loads
        trainer.random_drill()
    except LookupError:
        pass
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Chess Trainer on http://{host}:{port}/  (engine: {pool.version})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
        pool.close()
