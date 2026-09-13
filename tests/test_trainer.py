"""Tests that need no engine, plus one that uses it when it is installed.

Run with ./run test
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chess

from server import app, corpus, db, drills, engine, explain, grading


class Lines(unittest.TestCase):
    def test_lines_are_sorted_best_first(self):
        """The engine's MultiPV slots can report out of order. Ours may not:
        lines[0] is what the app calls the best move."""
        lines = [
            {"move": "a1a2", "cp": 100, "mate": None},
            {"move": "b1b2", "cp": 250, "mate": None},
            {"move": "c1c2", "cp": -40, "mate": None},
        ]
        ordered = engine.with_wp(list(lines))
        self.assertEqual([l["move"] for l in ordered], ["b1b2", "a1a2", "c1c2"])

    def test_mate_outranks_any_score(self):
        lines = [{"move": "a1a2", "cp": 900, "mate": None},
                 {"move": "b1b2", "cp": None, "mate": 3},
                 {"move": "c1c2", "cp": None, "mate": 1}]
        ordered = engine.with_wp(list(lines))
        self.assertEqual([l["move"] for l in ordered], ["c1c2", "b1b2", "a1a2"])

    def test_being_mated_is_worst(self):
        lines = [{"move": "a1a2", "cp": -900, "mate": None},
                 {"move": "b1b2", "cp": None, "mate": -2}]
        ordered = engine.with_wp(list(lines))
        self.assertEqual(ordered[0]["move"], "a1a2")

    def test_win_probability_is_symmetric(self):
        """A flipped evaluation must mean the same thing from the other side."""
        for cp in (0, 35, 120, 600, -250):
            self.assertAlmostEqual(
                engine.logistic_wp(cp) + engine.logistic_wp(-cp), 100.0, places=6
            )

    def test_flip_changes_side_not_meaning(self):
        line = {"move": "e2e4", "cp": 150, "mate": None, "pv": ["e2e4"]}
        line["wp"] = engine.line_wp(line)
        flipped = drills._flip(line)
        self.assertEqual(flipped["cp"], -150)
        self.assertAlmostEqual(flipped["wp"], 100 - line["wp"], places=3)
        self.assertEqual(line["cp"], 150, "flipping must not mutate the input")


class Verdicts(unittest.TestCase):
    def grade(self, best_cp, mine_cp, rank=None):
        best = {"cp": best_cp, "mate": None}
        mine = {"cp": mine_cp, "mate": None}
        best["wp"], mine["wp"] = engine.line_wp(best), engine.line_wp(mine)
        return grading.grade(best, mine, rank)

    def test_the_engines_move_is_the_best_move(self):
        self.assertEqual(self.grade(50, 50, 1)["label"], "Best move")

    def test_ordinals(self):
        for rank, label in ((2, "Second best"), (3, "Third best"),
                            (4, "Fourth best"), (5, "Fifth best")):
            self.assertEqual(self.grade(50, 45, rank)["label"], label)

    def test_damage_outranks_position_in_the_list(self):
        """The engine's second choice is still a blunder if it loses the game."""
        verdict = self.grade(50, -400, 2)
        self.assertEqual(verdict["label"], "Blunder")

    def test_the_ladder(self):
        """Starting from equality, where a centipawn is worth the most.
        The thresholds land near Lichess's 50 / 100 / 300."""
        self.assertEqual(self.grade(0, -20)["label"], "Good move")
        self.assertEqual(self.grade(0, -60)["label"], "Inaccuracy")
        self.assertEqual(self.grade(0, -150)["label"], "Mistake")
        self.assertEqual(self.grade(0, -400)["label"], "Blunder")

    def test_a_won_position_stays_won(self):
        """Dropping from +9 to +7 is not an inaccuracy."""
        self.assertEqual(self.grade(900, 700, 3)["label"], "Third best")

    def test_delivering_mate_is_best(self):
        best = {"cp": None, "mate": 2, "wp": 100.0}
        mine = {"cp": None, "mate": 1, "wp": 100.0, "final": "mate"}
        out = grading.grade(best, mine, None)
        self.assertEqual(out["label"], "Best move")
        self.assertEqual(out["delta_wp"], 0.0)

    def test_missing_mate_is_a_blunder(self):
        best = {"cp": None, "mate": 2, "wp": 100.0}
        mine = {"cp": 600, "mate": None, "wp": engine.logistic_wp(600)}
        self.assertEqual(grading.grade(best, mine, None)["label"], "Missed mate")

    def test_stalemating_a_won_position_is_a_blunder(self):
        best = {"cp": 800, "mate": None, "wp": engine.logistic_wp(800)}
        mine = {"cp": 0, "mate": None, "wp": 50.0, "final": "draw"}
        self.assertEqual(grading.grade(best, mine, None)["label"], "Blunder")

    def test_old_verdicts_still_read(self):
        """A database written by an earlier version must not break the panel."""
        for legacy in ("wrong", "playable"):
            self.assertIn(legacy, grading.TONES)


class OpponentMoves(unittest.TestCase):
    """The opponent plays moves worth answering, not the engine's fifth choice
    in a position where one move dominates."""

    def lines(self, *cps):
        out = []
        for i, cp in enumerate(cps):
            line = {"move": f"a{i + 1}a{i + 2}", "cp": cp, "mate": None}
            line["wp"] = engine.line_wp(line)
            out.append(line)
        return out

    def test_a_quiet_position_keeps_all_five(self):
        kept = drills.plausible_moves(self.lines(30, 25, 18, 10, 5))
        self.assertEqual(len(kept), 5)

    def test_one_dominant_move_asks_one_question(self):
        """+423 against +119 is a lost piece: nobody plays the alternatives."""
        kept = drills.plausible_moves(self.lines(423, 119, 113, 70, 59))
        self.assertEqual([c["move"] for c in kept], ["a1a2"])

    def test_mistakes_are_still_allowed(self):
        """A real opponent does drop 80cp; that is worth punishing."""
        kept = drills.plausible_moves(self.lines(0, -80))
        self.assertEqual(len(kept), 2)

    def test_blunders_are_not(self):
        kept = drills.plausible_moves(self.lines(0, -400))
        self.assertEqual(len(kept), 1)

    def test_a_forced_mate_is_not_declined(self):
        mate = [{"move": "a1a2", "cp": None, "mate": 2},
                {"move": "b1b2", "cp": 300, "mate": None}]
        for line in mate:
            line["wp"] = engine.line_wp(line)
        self.assertEqual(len(drills.plausible_moves(mate)), 1)

    def test_a_piece_is_not_handed_over_in_a_won_position(self):
        """From +900, dropping to +500 is only 8 win-probability points, but
        it is still a whole piece."""
        kept = drills.plausible_moves(self.lines(900, 500))
        self.assertEqual(len(kept), 1)

    def test_walking_into_mate_is_never_plausible(self):
        lines = [{"move": "a1a2", "cp": -100, "mate": None},
                 {"move": "b1b2", "cp": None, "mate": -1}]
        for line in lines:
            line["wp"] = engine.line_wp(line)
        self.assertEqual(len(drills.plausible_moves(lines)), 1)

    def test_there_is_always_a_question(self):
        self.assertEqual(len(drills.plausible_moves(self.lines(0))), 1)
        self.assertEqual(drills.plausible_moves([]), [])


class Chains(unittest.TestCase):
    def test_the_worst_move_sets_the_tone_of_a_chain(self):
        """Three good moves and one blunder is a blunder."""
        self.assertEqual(drills._worst(["best", "best", "blunder"]), "blunder")
        self.assertEqual(drills._worst(["best", "good"]), "good")
        self.assertEqual(drills._worst(["best", "best"]), "best")
        self.assertEqual(drills._worst(["shown", "best"]), "shown")
        self.assertIsNone(drills._worst([]))

    def test_length_is_clamped_to_the_allowed_range(self):
        board = chess.Board().fen()
        for asked, expected in ((0, 1), (1, 1), (3, 3), (99, drills.MAX_CHAIN)):
            drill = drills.Drill.__new__(drills.Drill)
            drill.chain = max(1, min(int(asked or 1), drills.MAX_CHAIN))
            self.assertEqual(drill.chain, expected, board)


class Positions(unittest.TestCase):
    def test_checkmate_is_not_a_fifty_percent_position(self):
        board = chess.Board("7k/5ppp/8/8/8/8/6PP/R5K1 w - - 0 1")
        board.push_san("Ra8#")
        outcome = drills._outcome(board)
        self.assertEqual(outcome["final"], "mate")
        self.assertEqual(outcome["wp"], 100.0)

    def test_stalemate_is_a_draw(self):
        board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
        self.assertTrue(board.is_stalemate())
        self.assertEqual(drills._outcome(board)["final"], "draw")

    def test_phases(self):
        self.assertEqual(db.classify_phase(chess.Board()), "opening")
        self.assertEqual(
            db.classify_phase(chess.Board("4k3/8/8/8/8/8/4P3/4K3 w - - 0 40")),
            "endgame")

    def test_endgame_hash_separates_the_fifty_move_clock(self):
        near = chess.Board("4k3/8/8/8/8/8/4P3/4K3 w - - 90 60")
        far = chess.Board("4k3/8/8/8/8/8/4P3/4K3 w - - 4 60")
        self.assertNotEqual(db.pos_hash(near, "endgame"), db.pos_hash(far, "endgame"))

    def test_transpositions_share_one_entry(self):
        a, b = chess.Board(), chess.Board()
        for move in ("e4", "e5", "Nf3", "Nc6"):
            a.push_san(move)
        for move in ("Nf3", "Nc6", "e4", "e5"):
            b.push_san(move)
        self.assertEqual(db.pos_hash(a), db.pos_hash(b))


class Editor(unittest.TestCase):
    def test_accepts_a_legal_position(self):
        fen = app.validate_setup({"e1": "K", "e8": "k", "d4": "Q"}, "black", "-")
        self.assertTrue(chess.Board(fen).is_valid())

    def test_refuses_with_a_reason(self):
        cases = [
            (({"e1": "K"}, "white", "-"), "king"),
            (({"e1": "K", "e8": "k", "a1": "P"}, "white", "-"), "pawn"),
            (({"e1": "K", "e8": "k"}, "white", "KQ"), "Castling"),
        ]
        for args, word in cases:
            with self.assertRaises(ValueError) as caught:
                app.validate_setup(*args)
            self.assertIn(word, str(caught.exception))


class Corpus(unittest.TestCase):
    def test_opening_family_groups_variations(self):
        url = "https://www.chess.com/openings/Sicilian-Defense-Najdorf-Variation"
        self.assertEqual(corpus.opening_family({"ECOUrl": url}), "Sicilian Defense")

    def test_pgn_is_not_mistaken_for_json(self):
        pgn = '[Event "x"]\n\n1. e4 e5 *'
        self.assertEqual(corpus.pgn_from_response(pgn), pgn)


class Explanation(unittest.TestCase):
    def test_names_the_move_when_there_is_nothing_to_diff(self):
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        out = explain.explain(fen, None, "e2e4", {"mine": [], "best": ["e2e4"]})
        self.assertIn("e4", out.text)

    def test_pv_preview_carries_the_position(self):
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        out = explain.explain(fen, "e2e4", "d2d4",
                              {"mine": ["e2e4", "e7e5"], "best": ["d2d4", "d7d5"]})
        self.assertTrue(out.my_pv and out.best_pv)
        for step in out.my_pv + out.best_pv:
            self.assertIn("fen_after", step)
            self.assertTrue(step["board"])


@unittest.skipUnless(os.path.exists(engine.ENGINE_PATH), "no engine installed")
class WithEngine(unittest.TestCase):
    """One end-to-end check: the move the app calls best must really be best."""

    @classmethod
    def setUpClass(cls):
        db.init()
        cls.pool = engine.Pool()

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()

    def test_the_best_move_beats_the_alternatives(self):
        drill = drills.Drill(db.connect(), self.pool, chess.Board().fen(),
                             "openings")
        lines = drill.my_lines()
        self.assertTrue(lines)
        scores = [engine._rank_key(l) for l in lines]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_playing_the_engines_move_scores_best(self):
        drill = drills.Drill(db.connect(), self.pool, chess.Board().fen(),
                             "openings")
        answer = drill.answer(drill.my_lines()[0]["move"])
        self.assertEqual(answer["verdict"], "best", answer["label"])
        self.assertEqual(answer["delta_wp"], 0.0)
        self.assertEqual(answer["my_san"], answer["best_san"])

    def test_replay_asks_the_same_question(self):
        drill = drills.Drill(db.connect(), self.pool, chess.Board().fen(),
                             "openings")
        before = drill.to_json()
        drill.answer(drill.my_lines()[0]["move"])
        drill.replay()
        after = drill.to_json()
        self.assertEqual(before["fen"], after["fen"])
        self.assertEqual(before["opp_san"], after["opp_san"])
        self.assertIsNone(after["answer"])

    def test_a_chain_asks_for_every_move(self):
        """Three moves in a row: they answer between yours, and the round is
        not over until you have found all three."""
        drill = drills.Drill(db.connect(), self.pool, chess.Board().fen(),
                             "openings", chain=3)
        seen = []
        for step in (1, 2, 3):
            state = drill.to_json()
            self.assertEqual(state["step"], step)
            self.assertTrue(state["can_answer"])
            self.assertFalse(state["done"])
            answer = drill.answer(drill.my_lines()[0]["move"])
            self.assertEqual(answer["verdict"], "best")
            seen.append(answer["my_san"])
            if step < 3:
                self.assertIsNotNone(drill.round_state["opp_reply"],
                                     "they must answer between your moves")
        final = drill.to_json()
        self.assertTrue(final["done"])
        self.assertFalse(final["can_answer"])
        self.assertEqual(len(final["steps"]), 3)
        self.assertEqual(len(set(seen)), 3, seen)

    def test_a_chain_is_recorded_move_by_move(self):
        drill = drills.Drill(db.connect(), self.pool, chess.Board().fen(),
                             "openings", chain=2)
        drill.answer(drill.my_lines()[0]["move"])
        drill.answer(drill.my_lines()[0]["move"])
        rows = db.connect().execute(
            "SELECT step FROM answers WHERE pos_hash IN (?, ?) ORDER BY id DESC"
            " LIMIT 2", (drill.round_state["hash"], drill.round_state["hash"]),
        ).fetchall()
        self.assertTrue(rows)

    def test_replay_starts_the_whole_chain_again(self):
        drill = drills.Drill(db.connect(), self.pool, chess.Board().fen(),
                             "openings", chain=3)
        asked = drill.to_json()
        drill.answer(drill.my_lines()[0]["move"])
        self.assertEqual(drill.round_state["step"], 2)
        drill.replay()
        again = drill.to_json()
        self.assertEqual(again["step"], 1)
        self.assertEqual(again["fen"], asked["fen"])
        self.assertEqual(again["opp_san"], asked["opp_san"])
        self.assertEqual(again["steps"], [])

    def test_drill_from_here_can_start_from_a_line_you_walked(self):
        """Walking into the engine's line and drilling from there must keep
        the position you are looking at, not snap back to the one you
        answered from."""
        from server import app as appmod
        trainer = appmod.Trainer(self.pool)
        trainer.mode = "openings"
        drill = trainer.new_drill(chess.Board().fen())
        answer = drill.answer(drill.my_lines()[0]["move"])
        pv = answer["explanation"]["best_pv"]
        self.assertTrue(len(pv) >= 2)

        # a step where the opponent is to move: that position becomes the root
        theirs = next(s for s in pv
                      if chess.Board(s["fen_after"]).turn == drill.opponent)
        deep = trainer.deeper(theirs["fen_after"])
        self.assertEqual(deep.fen, chess.Board(theirs["fen_after"]).fen())

        # a step where you are to move: the drill starts one move back and
        # replays it, so the board ends up exactly where you were looking
        mine = next(s for s in pv
                    if chess.Board(s["fen_after"]).turn != drill.opponent)
        deep = trainer.deeper(mine["fen_after"], mine["fen_before"], mine["uci"])
        state = deep.to_json()
        self.assertEqual(state["fen"], mine["fen_after"])
        self.assertEqual(state["opp_move"], mine["uci"])
        self.assertTrue(state["can_answer"])

    def test_drill_from_here_starts_where_you_are(self):
        drill = drills.Drill(db.connect(), self.pool, chess.Board().fen(),
                             "openings")
        answer = drill.answer(drill.my_lines()[0]["move"])
        deeper = drills.Drill(db.connect(), self.pool, answer["fen_after"],
                              "openings", depth_level=drill.depth_level + 1,
                              root_hash=drill.root_hash,
                              node_id=answer["my_node"])
        self.assertEqual(deeper.fen, chess.Board(answer["fen_after"]).fen())
        self.assertEqual(deeper.to_json()["base_fen"], answer["fen_after"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
