"""Check the app's moves against a second, independent engine.

You should not have to trust this program blindly. `./run verify` takes
positions the gym would give you and asks a fresh Stockfish process -- no
cache, its own hash table, a deeper search -- the same questions the app
answered, then reports where they disagree and by how much:

  - the opponent's options: how much each really gives up
  - the reply the app calls best: how much it loses against the reference
  - a graded reply: whether the app's verdict matches the reference's loss
  - a chain step: whether the opponent's reply is the reference's too
  - a reviewed move from one of your games: the same, from the review

Disagreements of a few centipawns between two searches are normal. What
would matter is a "best" reply that loses a piece, or an option the
opponent is given that throws the game away; those are printed in capitals.
"""
from __future__ import annotations

import random

import chess
import chess.engine

from . import drills, engine, grading

REF_DEPTH = 22
BAD_REPLY_CP = 100         # a "best" reply that loses this much is wrong
BAD_OPTION_CP = 300        # an opponent option that gives up a piece is wrong


class Reference:
    """A separate Stockfish process, nothing shared with the app's pool."""

    def __init__(self, path=engine.ENGINE_PATH, depth=REF_DEPTH):
        self.engine = chess.engine.SimpleEngine.popen_uci(path)
        self.engine.configure({"Threads": 2, "Hash": 512})
        self.depth = depth

    def best(self, board):
        info = self.engine.analyse(board, chess.engine.Limit(depth=self.depth))
        return info["pv"][0], self._cp(info, board)

    def score_of(self, board, move):
        info = self.engine.analyse(board, chess.engine.Limit(depth=self.depth),
                                   root_moves=[move])
        return self._cp(info, board)

    @staticmethod
    def _cp(info, board):
        return info["score"].pov(board.turn).score(mate_score=10000)

    def close(self):
        self.engine.quit()


def verify(conn, pool, positions: int = 4, log=print) -> dict:
    ref = Reference()
    worst = {"reply": 0, "option": 0}
    checked = {"positions": 0, "options": 0, "replies": 0, "verdicts": 0,
               "verdict_agree": 0, "chain": 0, "chain_agree": 0}
    try:
        rows = conn.execute("SELECT fen, phase FROM positions WHERE phase != 'opening'"
                            " ORDER BY RANDOM() LIMIT ?", (positions,)).fetchall()
        if not rows:
            rows = conn.execute("SELECT fen, phase FROM positions ORDER BY RANDOM()"
                                " LIMIT ?", (positions,)).fetchall()
        for row in rows:
            root = chess.Board(row["fen"])
            if root.is_game_over():
                continue
            checked["positions"] += 1
            log(f"\n{root.fen()}")
            drill = drills.Drill(conn, pool, row["fen"], "middlegame", chain=2)
            ref_best, ref_cp = ref.best(root)
            log(f"  opponent to move. Independent best: {root.san(ref_best)} {ref_cp:+d}")
            for cand in drill.candidates:
                mv = chess.Move.from_uci(cand["uci"])
                cost = ref_cp - ref.score_of(root, mv)
                worst["option"] = max(worst["option"], cost)
                checked["options"] += 1
                flag = "   <-- GIVES UP A PIECE" if cost >= BAD_OPTION_CP else ""
                log(f"    they may play {root.san(mv):7s} gives up {cost:4d}cp{flag}")

            # the reply the app calls best, in the first round
            asked = chess.Board(drill.round_state["fen"])
            ours = drill.my_lines()[0]["move"]
            ours_mv = chess.Move.from_uci(ours)
            rbest, rcp = ref.best(asked)
            cost = rcp - ref.score_of(asked, ours_mv)
            worst["reply"] = max(worst["reply"], cost)
            checked["replies"] += 1
            flag = "   <-- WRONG" if cost >= BAD_REPLY_CP else ""
            log(f"  after {root.san(chess.Move.from_uci(drill.round_state['candidate']['uci']))}:"
                f" app says {asked.san(ours_mv)}, reference says {asked.san(rbest)}"
                f" (app's move loses {cost}cp by the reference){flag}")

            # grade a deliberately different move and compare the verdicts
            others = [m for m in asked.legal_moves if m != ours_mv]
            if others:
                probe = random.choice(others)
                answer = drill.answer(probe.uci())
                loss = rcp - ref.score_of(asked, probe)
                ref_wp_loss = engine.logistic_wp(rcp) - engine.logistic_wp(rcp - loss)
                ref_verdict = grading.grade(
                    {"wp": engine.logistic_wp(rcp), "cp": rcp, "mate": None},
                    {"wp": engine.logistic_wp(rcp - loss), "cp": rcp - loss, "mate": None},
                    None)["verdict"]
                same_band = _band(answer["verdict"]) == _band(ref_verdict)
                checked["verdicts"] += 1
                checked["verdict_agree"] += same_band
                log(f"  graded {answer['my_san']}: app {answer['label']} (-{answer['delta_wp']}),"
                    f" reference would say {grading.label_for(ref_verdict)}"
                    f" (-{ref_wp_loss:.1f}){'' if same_band else '   <-- differs'}")
                # chain: does the opponent's reply match the reference's?
                reply = drill.round_state.get("opp_reply")
                if reply:
                    after = chess.Board(answer["fen_after"])
                    rreply, _ = ref.best(after)
                    agree = rreply.uci() == reply["uci"]
                    checked["chain"] += 1
                    checked["chain_agree"] += agree
                    log(f"  chain: opponent answers {reply['san']};"
                        f" reference {after.san(rreply)}{'' if agree else '  (different, both fine if close)'}")

        # a reviewed move of yours, re-graded from scratch
        mv = conn.execute("SELECT fen, move, best, verdict, delta_wp FROM review_moves"
                          " WHERE is_me=1 ORDER BY RANDOM() LIMIT 1").fetchone()
        if mv:
            board = chess.Board(mv["fen"])
            rbest, rcp = ref.best(board)
            played = chess.Move.from_uci(mv["move"])
            loss = rcp - ref.score_of(board, played)
            log(f"\nreviewed move {board.san(played)}: review said {grading.label_for(mv['verdict'])}"
                f" (-{mv['delta_wp']}); reference: best {board.san(rbest)}, your move loses {loss}cp")
    finally:
        ref.close()
    log(f"\nchecked {checked['positions']} positions, {checked['options']} opponent options,"
        f" {checked['replies']} best replies")
    log(f"  worst opponent option gives up {worst['option']}cp"
        f" (limit {BAD_OPTION_CP}); worst 'best' reply loses {worst['reply']}cp"
        f" (limit {BAD_REPLY_CP})")
    if checked["verdicts"]:
        log(f"  verdict bands agree {checked['verdict_agree']}/{checked['verdicts']};"
            f" chain replies agree {checked['chain_agree']}/{checked['chain']}")
    ok = worst["option"] < BAD_OPTION_CP and worst["reply"] < BAD_REPLY_CP
    log("OK" if ok else "DISAGREEMENT -- see the lines in capitals")
    return {"ok": ok, **checked, **worst}


def _band(verdict: str) -> str:
    if verdict in ("best", "second", "third", "fourth", "fifth", "good", "playable"):
        return "fine"
    if verdict == "inaccuracy":
        return "inaccuracy"
    return "bad"
