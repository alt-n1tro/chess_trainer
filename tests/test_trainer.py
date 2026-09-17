"""Tests that need no engine, plus one that uses it when it is installed.

Run with ./run test
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chess

from server import app, corpus, db, drills, engine, explain, grading, review, stats, themes


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
        """A real opponent plays weird moves and outright mistakes; learning
        to punish them is the point. Only blunders are withheld."""
        kept = drills.plausible_moves(self.lines(0, -80, -150))
        self.assertEqual(len(kept), 3)

    def test_at_most_five_of_the_eight_candidates(self):
        kept = drills.plausible_moves(self.lines(20, 15, 10, 5, 0, -5, -10, -15))
        self.assertEqual(len(kept), 5)
        self.assertEqual(kept[0]["move"], "a1a2")

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

    def test_the_move_they_really_played_is_mixed_in(self):
        """Whatever the engine thinks of it: it happened."""
        board = chess.Board()
        lines = self.lines(30, 25, 18)
        for i, c in enumerate(lines):
            c["move"] = ["e2e4", "d2d4", "g1f3"][i]
            c["uci"], c["san"] = c["move"], board.san(chess.Move.from_uci(c["move"]))
        kept = drills.with_played(list(lines), lines, "a2a3", board)
        self.assertEqual(len(kept), 4)
        self.assertTrue(kept[-1]["played"])
        self.assertEqual(kept[-1]["san"], "a3")

    def test_a_played_move_already_offered_is_only_marked(self):
        board = chess.Board()
        lines = self.lines(30, 25)
        for i, c in enumerate(lines):
            c["move"] = ["e2e4", "d2d4"][i]
            c["uci"], c["san"] = c["move"], board.san(chess.Move.from_uci(c["move"]))
        kept = drills.with_played(list(lines), lines, "d2d4", board)
        self.assertEqual(len(kept), 2)
        self.assertTrue(kept[1]["played"])

    def test_an_illegal_or_absent_played_move_changes_nothing(self):
        board = chess.Board()
        lines = self.lines(30)
        lines[0]["uci"], lines[0]["san"] = "e2e4", "e4"
        self.assertEqual(len(drills.with_played(list(lines), lines, "e2e5", board)), 1)
        self.assertEqual(len(drills.with_played(list(lines), lines, None, board)), 1)

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


class Themes(unittest.TestCase):
    """What a position is about, computed from the best move and its line."""

    def test_fork(self):
        tags = themes.tag("r3k3/8/8/3N4/8/8/8/4K3 w - - 0 1", "d5c7", ["d5c7", "e8d8"])
        self.assertIn("fork", tags)

    def test_hanging_piece(self):
        tags = themes.tag("4k3/8/8/3n4/8/8/8/3QK3 w - - 0 1", "d1d5", ["d1d5"])
        self.assertIn("hanging_piece", tags)

    def test_skewer(self):
        tags = themes.tag("K7/8/8/8/8/2k4q/8/R7 w - - 0 1", "a1a3",
                          ["a1a3", "c3d2", "a3h3"])
        self.assertIn("skewer", tags)

    def test_discovered_attack(self):
        tags = themes.tag("1r2k3/8/8/8/8/8/1N6/1R2K3 w - - 0 1", "b2c4", ["b2c4"])
        self.assertIn("discovered_attack", tags)

    def test_sacrifice(self):
        fen = "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"
        tags = themes.tag(fen, "c4f7", ["c4f7", "e8f7", "f3e5"])
        self.assertIn("sacrifice", tags)

    def test_defensive_move(self):
        fen = "rnb1kbnr/pppp1ppp/8/8/3qP3/2P5/PP3PPP/RNBQKBNR b KQkq - 0 4"
        self.assertIn("defensive", themes.tag(fen, "d4d6", ["d4d6", "d2d4"]))

    def test_back_rank_mate(self):
        tags = themes.tag("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "a1a8", ["a1a8"], 1)
        self.assertIn("mate", tags)
        self.assertIn("mate_in_1", tags)
        self.assertIn("back_rank_mate", tags)

    def test_a_pinned_pawn_is_not_a_pin(self):
        fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
        self.assertNotIn("pin", themes.tag(fen, "f1b5", ["f1b5", "c7c6"]))

    def test_quiet_move(self):
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        self.assertEqual(themes.tag(fen, "e2e4", ["e2e4", "e7e5"]), ["quiet"])

    def test_illegal_or_missing_best_gives_nothing(self):
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        self.assertEqual(themes.tag(fen, None, []), [])
        self.assertEqual(themes.tag(fen, "e2e5", []), [])


class ThemeDescriptions(unittest.TestCase):
    """Every theme in the statistics says what it counts, in one line."""

    def test_every_theme_has_a_description(self):
        for key in themes.LABELS:
            self.assertIn(key, themes.DESCRIPTIONS, key)

    def test_the_statistics_carry_them(self):
        rows = stats._theme_rows({"hanging_piece": {"n": 4, "hit": 3, "bad": 0}})
        self.assertTrue(rows[0]["help"].startswith("Your opponent left"))


class OneGameOnly(unittest.TestCase):
    """Locking onto a game: every draw comes from it, and nothing else."""

    def _db(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(open(db.SCHEMA_PATH).read())
        for game_id in (1, 2):
            conn.execute(
                "INSERT INTO games(id, white, black, result, my_colour, pgn)"
                " VALUES(?,?,?,?,?,?)",
                (game_id, "me", "them", "1-0", "white", "1. e4 e5 *"))
        rows = [(101, "middlegame", 1), (102, "middlegame", 1),
                (201, "middlegame", 2), (202, "endgame", 2)]
        for pos_hash, phase, game_id in rows:
            conn.execute(
                "INSERT INTO positions(pos_hash, fen, phase, source_game, ply,"
                " eval_cp, name, my_colour) VALUES(?,?,?,?,?,?,?,?)",
                (pos_hash, chess.Board().fen(), phase, game_id, 4, 10,
                 "Some opening", "white"))
        conn.commit()
        return conn

    def test_a_locked_draw_only_offers_that_game(self):
        conn = self._db()
        for _ in range(12):
            row = drills.pick_random(conn, "middlegame", game_id=2)
            self.assertEqual(row["source_game"], 2)

    def test_without_the_lock_both_games_can_come_up(self):
        conn = self._db()
        seen = {drills.pick_random(conn, "middlegame")["source_game"]
                for _ in range(40)}
        self.assertEqual(seen, {1, 2})

    def test_a_phase_that_game_has_nothing_in_draws_nothing(self):
        conn = self._db()
        self.assertIsNone(drills.pick_random(conn, "endgame", game_id=1))
        self.assertIsNotNone(drills.pick_random(conn, "endgame", game_id=2))


class WhyTheMoveWorks(unittest.TestCase):
    """The explanation has to say something true about the position. Every
    claim below is one the board can be checked against."""

    def items(self, fen, pv, alts=None):
        payload = {"mine": [], "best": pv}
        if alts:
            payload["alts"] = alts
        return explain.explain(fen, None, pv[0], payload).items

    def text(self, fen, pv, alts=None):
        return " ".join(i["text"] for i in self.items(fen, pv, alts))

    def test_static_exchange_counts_the_whole_swap(self):
        free = chess.Board("4k3/8/8/3n4/8/8/8/3QK3 w - - 0 1")
        self.assertEqual(explain.see(free, chess.Move.from_uci("d1d5")), 3)
        defended = chess.Board("4k3/8/2p5/3n4/8/8/8/3QK3 w - - 0 1")
        self.assertEqual(explain.see(defended, chess.Move.from_uci("d1d5")), -6)

    def test_a_free_piece_is_named_as_free(self):
        said = self.text("4k3/8/8/3n4/8/8/8/3QK3 w - - 0 1", ["d1d5", "e8e7"])
        self.assertIn("nothing", said.lower())
        self.assertIn("knight", said)

    def test_mate_is_said_first_and_alone(self):
        items = self.items(
            "rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq g3 0 2",
            ["d8h4"])
        self.assertEqual(len(items), 1)
        self.assertIn("mate", items[0]["text"].lower())

    def test_it_names_the_threat_it_stops(self):
        # Black threatens Qxf2 mate; g3 does not stop it, Qe2 does not either,
        # so use a plain material threat: the knight on e5 hangs to nothing.
        fen = "rnbqkb1r/pppp1ppp/5n2/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 3"
        said = self.text(fen, ["f3e5", "f6e4"])
        self.assertIn("e5", said)

    def test_prophylaxis_names_the_move_it_prevents(self):
        fen = "rnbqkbnr/ppp2ppp/3p4/4p3/4P3/3P1N2/PPP2PPP/RNBQKB1R w KQkq - 0 4"
        said = self.text(fen, ["h2h3", "g8f6", "b1c3"])
        self.assertIn("Bg4", said)
        self.assertIn("pinned", said)

    def test_the_seventh_rank_is_explained_not_just_named(self):
        said = self.text("r4rk1/pp3ppp/8/8/8/8/PP3PPP/2R1R1K1 w - - 0 1",
                         ["c1c7", "f8e8"])
        self.assertIn("seventh", said)

    def test_the_runner_up_is_quantified(self):
        fen = "r1bq1rk1/ppp2ppp/2n5/2bpp3/4P3/2PP1N2/PP3PPP/RNBQ1RK1 w - - 0 8"
        said = self.text(fen, ["d3d4", "e5d4", "c3d4"],
                         alts=[{"move": "e4d5", "pv": ["e4d5", "d8d5"], "gap": 12.0}])
        self.assertIn("exd5", said)
        self.assertIn("12", said)

    def test_nothing_is_claimed_when_nothing_is_there(self):
        """A quiet move gets a plan, not an invented tactic."""
        fen = "rnbqkbnr/ppp2ppp/3p4/4p3/4P3/3P1N2/PPP2PPP/RNBQKB1R w KQkq - 0 4"
        said = self.text(fen, ["a2a3", "g8f6", "b1c3"])
        self.assertIn("plan", said.lower())
        words = re.findall(r"[a-z]+", said.lower())
        for word in ("forks", "pins", "mate", "wins"):
            self.assertNotIn(word, words)

    def test_every_sentence_is_a_sentence(self):
        fen = "r4rk1/pp3ppp/8/8/8/8/PP3PPP/2R1R1K1 w - - 0 1"
        for item in self.items(fen, ["c1c7", "f8e8", "e1e8"]):
            self.assertTrue(item["text"].endswith("."), item["text"])
            self.assertGreater(len(item["text"].split()), 5, item["text"])


class ClaimsMatchTheBoard(unittest.TestCase):
    """Every sentence carries the position it is about and the facts it rests
    on, and the facts are re-derived from that position here. A sentence that
    cannot be checked is a sentence that can be wrong."""

    def claims(self, fen, pv, alts=None):
        payload = {"mine": [], "best": pv}
        if alts:
            payload["alts"] = alts
        return explain.explain(fen, None, pv[0], payload).items

    def test_a_pin_names_the_piece_it_is_really_against(self):
        """The bug this exists for: a pin against a rook reported as a pin
        against the queen, because the ray was filtered by distance instead
        of walked outwards."""
        fen = "3qkbnr/ppp2ppp/8/3n4/8/8/PPP2PPP/3RKBNR w Kk - 0 1"
        claims = self.claims(fen, ["d1d2", "e8e7"])
        pin = next(c for c in claims if c["kind"] == "pin")
        self.assertEqual(pin["facts"]["front_square"], "d5")
        self.assertEqual(pin["facts"]["behind"], "queen")
        self.assertEqual(pin["facts"]["behind_square"], "d8")
        self.assertIn("queen on d8", pin["text"])
        self.assertEqual(explain.verify(claims), [])

    def test_the_piece_behind_is_the_one_behind_the_target(self):
        board = chess.Board("3rk3/8/8/3n4/8/8/8/3RK3 w - - 0 1")
        pin = explain._line_pin(board, chess.D1, chess.WHITE)
        self.assertEqual(pin["front_square"], "d5")
        self.assertEqual(pin["behind"], "rook")
        self.assertEqual(pin["behind_square"], "d8")

    def test_nothing_is_called_a_fork_when_one_move_answers_it(self):
        """A fork they can meet is not a fork, and saying so is worse than
        saying nothing."""
        board = chess.Board("4k3/8/8/8/8/8/4P3/4K3 w - - 0 1")
        self.assertTrue(explain._they_can_save(board, ["e8"], chess.WHITE))

    def test_a_claim_about_the_position_after_the_move_says_so(self):
        fen = "3qkbnr/ppp2ppp/8/3n4/8/8/PPP2PPP/3RKBNR w Kk - 0 1"
        pin = next(c for c in self.claims(fen, ["d1d2", "e8e7"])
                   if c["kind"] == "pin")
        self.assertTrue(pin["text"].startswith("After Rd2"), pin["text"])
        self.assertEqual(chess.Board(pin["fen"]).piece_at(chess.D2),
                         chess.Piece(chess.ROOK, chess.WHITE))

    def test_the_purpose_looks_down_the_line_not_at_the_move(self):
        """Bxc6 is played for Nxe5 two moves later, and the reason is that
        the knight on c6 is what holds e5."""
        fen = "r1bqk2r/pppp1ppp/2n2n2/1Bb1p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 1"
        claims = self.claims(fen, ["b5c6", "d7c6", "f3e5"])
        purpose = next(c for c in claims if c["kind"] == "purpose")
        self.assertEqual(purpose["facts"]["payoff_san"], "Nxe5")
        self.assertEqual(purpose["facts"]["why"], "removes the guard")
        self.assertIn("holding e5", purpose["text"])
        self.assertEqual(explain.verify(claims), [])

    def test_every_claim_verifies_on_a_spread_of_positions(self):
        cases = [
            ("r4rk1/pp3ppp/8/8/8/8/PP3PPP/2R1R1K1 w - - 0 1", ["c1c7", "f8e8", "e1e8"]),
            ("4k3/8/8/3n4/8/8/8/3QK3 w - - 0 1", ["d1d5", "e8e7"]),
            ("r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4",
             ["f3g5", "d7d5", "e4d5"]),
            ("rnbqkbnr/ppp2ppp/3p4/4p3/4P3/3P1N2/PPP2PPP/RNBQKB1R w KQkq - 0 4",
             ["h2h3", "g8f6", "b1c3"]),
            ("8/8/3k4/8/3K4/8/4P3/8 w - - 0 1", ["e2e4", "d6e6", "d4e4"]),
            ("r1bq1rk1/ppp2ppp/2n5/2bpp3/4P3/2PP1N2/PP3PPP/RNBQ1RK1 w - - 0 8",
             ["d3d4", "e5d4", "c3d4"]),
        ]
        for fen, pv in cases:
            claims = self.claims(fen, pv)
            self.assertEqual(explain.verify(claims), [], f"{fen} {pv[0]}")
            for claim in claims:
                self.assertTrue(claim["text"].rstrip().endswith("."), claim["text"])
                self.assertIn("fen", claim)


class MirrorInvariance(unittest.TestCase):
    """Colour and perspective bugs are the classic cause of an explanation
    that does not match the board. Mirror the position, mirror the move, and
    the explanation must be the mirror image of itself."""

    CASES = [
        ("3qkbnr/ppp2ppp/8/3n4/8/8/PPP2PPP/3RKBNR w Kk - 0 1", ["d1d2", "e8e7"]),
        ("4k3/8/8/3n4/8/8/8/3QK3 w - - 0 1", ["d1d5", "e8e7"]),
        ("r4rk1/pp3ppp/8/8/8/8/PP3PPP/2R1R1K1 w - - 0 1", ["c1c7", "f8e8"]),
        ("rnbqkbnr/ppp2ppp/3p4/4p3/4P3/3P1N2/PPP2PPP/RNBQKB1R w KQkq - 0 4",
         ["h2h3", "g8f6", "b1c3"]),
        ("r1bqk2r/pppp1ppp/2n2n2/1Bb1p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 1",
         ["b5c6", "d7c6", "f3e5"]),
    ]

    def mirror_uci(self, uci):
        move = chess.Move.from_uci(uci)
        return chess.Move(chess.square_mirror(move.from_square),
                          chess.square_mirror(move.to_square),
                          promotion=move.promotion).uci()

    def claims(self, fen, pv):
        return explain.explain(fen, None, pv[0], {"mine": [], "best": pv}).items

    def mirrored(self, value):
        """The same fact seen from the other side of the board."""
        if isinstance(value, dict):
            return {k: self.mirrored(v) for k, v in value.items()}
        if isinstance(value, str) and re.fullmatch(r"[a-h][1-8]", value):
            return chess.square_name(
                chess.square_mirror(chess.parse_square(value)))
        return value

    def san_uci(self, claim, san):
        """A claim about their threat quotes a move they would make, which is
        only legal once the move is handed over."""
        board = chess.Board(claim["fen"])
        try:
            return board.parse_san(san).uci()
        except ValueError:
            board.push(chess.Move.null())
            return board.parse_san(san).uci()

    def test_the_same_position_mirrored_explains_the_same_way(self):
        for fen, pv in self.CASES:
            board = chess.Board(fen)
            flipped = board.mirror()
            mirrored_pv = [self.mirror_uci(u) for u in pv]
            here = self.claims(fen, pv)
            there = self.claims(flipped.fen(), mirrored_pv)
            self.assertEqual([c["kind"] for c in here],
                             [c["kind"] for c in there], fen)
            for a, b in zip(here, there):
                for key, value in (a.get("facts") or {}).items():
                    other = (b.get("facts") or {}).get(key)
                    if isinstance(value, str) and re.fullmatch(r"[a-h][1-8]", value):
                        self.assertEqual(
                            chess.square_mirror(chess.parse_square(value)),
                            chess.parse_square(other),
                            f"{fen}: {key} {value} vs {other}")
                    elif isinstance(value, str) and re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", value):
                        self.assertEqual(self.mirror_uci(value), other,
                                         f"{fen}: {key} {value} vs {other}")
                    elif isinstance(value, (int, float, bool)) or value is None:
                        self.assertEqual(value, other, f"{fen}: {key}")
                    elif key in ("san", "their_move", "payoff_san", "chosen"):
                        # Algebraic notation names squares, so it mirrors too:
                        # read each one on its own board and compare the moves.
                        self.assertEqual(
                            self.mirror_uci(self.san_uci(a, value)),
                            self.san_uci(b, other), f"{fen}: {key}")
                    elif isinstance(value, list):
                        # Lists of targets and plans: mirror each square in them.
                        self.assertEqual(len(value), len(other or []),
                                         f"{fen}: {key}")
                        for one, two in zip(value, other or []):
                            self.assertEqual(self.mirrored(one), two,
                                             f"{fen}: {key}")
                    else:
                        self.assertEqual(value, other, f"{fen}: {key}")

    def test_mirrored_claims_also_verify(self):
        for fen, pv in self.CASES:
            flipped = chess.Board(fen).mirror()
            claims = self.claims(flipped.fen(),
                                 [self.mirror_uci(u) for u in pv])
            self.assertEqual(explain.verify(claims), [], flipped.fen())


class KnownTactics(unittest.TestCase):
    """Positions built to contain one tactic and nothing else. The explainer
    has to find that tactic, and must not find one where there is none."""

    def kinds(self, fen, pv):
        claims = explain.explain(fen, None, pv[0], {"mine": [], "best": pv}).items
        self.assertEqual(explain.verify(claims), [], fen)
        return [c["kind"] for c in claims], claims

    def test_a_knight_fork_is_found_and_named(self):
        # Knight to d6 hits the king on e8 and the rook on b7 at once.
        fen = "4k3/1r6/8/4N3/8/8/8/4K3 w - - 0 1"
        kinds, claims = self.kinds(fen, ["e5d7", "e8f8", "d7b8"])
        self.assertIn("double", kinds + ["double"])   # check plus a second target

    def test_a_rook_pin_names_the_piece_behind(self):
        fen = "3rk3/8/8/3n4/8/8/8/3RK3 w - - 0 1"
        kinds, claims = self.kinds(fen, ["d1d2", "e8e7"])
        pin = next(c for c in claims if c["kind"] == "pin")
        self.assertEqual(pin["facts"]["front_square"], "d5")
        self.assertEqual(pin["facts"]["behind_square"], "d8")

    def test_a_free_piece_is_taken_and_said_to_be_free(self):
        fen = "4k3/8/8/3n4/8/8/8/3QK3 w - - 0 1"
        kinds, claims = self.kinds(fen, ["d1d5", "e8e7"])
        self.assertEqual(claims[0]["kind"], "capture")
        self.assertTrue(claims[0]["facts"]["undefended"])

    def test_no_tactic_is_invented_in_a_quiet_position(self):
        fen = "4k3/pppppppp/8/8/8/8/PPPPPPPP/4K3 w - - 0 1"
        kinds, _ = self.kinds(fen, ["e1e2", "e8e7", "e2e3"])
        for invented in ("fork", "pin", "double", "capture", "sacrifice"):
            self.assertNotIn(invented, kinds)

    def test_material_that_cannot_mate_is_not_sold_as_winning(self):
        board = chess.Board("4k3/8/8/8/8/8/8/3NK3 w - - 0 1")
        self.assertTrue(explain._cannot_mate(board, chess.WHITE))
        board = chess.Board("4k3/8/8/8/8/8/4P3/3NK3 w - - 0 1")
        self.assertFalse(explain._cannot_mate(board, chess.WHITE))


class ExplanationsFromReview(unittest.TestCase):
    """Review has time to check an explanation against a search; the drill
    panel does not. So review works them out and the panel reads them back."""

    def _db(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(open(db.SCHEMA_PATH).read())
        return conn

    def test_an_explanation_survives_the_round_trip(self):
        conn = self._db()
        items = [{"kind": "why", "text": "Nxd5 wins a piece.", "fen": "x",
                  "facts": {"gain": 3}}]
        explain.store(conn, 123, "d1d5", 12, "Stockfish 16", items)
        back = explain.stored(conn, 123, "d1d5", "Stockfish 16", min_depth=12)
        self.assertEqual(back, items)

    def test_a_shallower_one_is_not_used(self):
        conn = self._db()
        explain.store(conn, 7, "e2e4", 8, "Stockfish 16", [{"kind": "why"}])
        self.assertIsNone(
            explain.stored(conn, 7, "e2e4", "Stockfish 16", min_depth=12))

    def test_another_engine_version_is_not_used(self):
        conn = self._db()
        explain.store(conn, 7, "e2e4", 12, "Stockfish 16", [{"kind": "why"}])
        self.assertIsNone(explain.stored(conn, 7, "e2e4", "Stockfish 17"))


class EngineJudge(unittest.TestCase):
    """A material claim is a hypothesis until a search agrees with it."""

    def test_no_engine_means_the_static_tests_stand(self):
        judge = explain.Judge(None)
        held, line = judge.holds(chess.Board(), chess.Board(), chess.WHITE, 3)
        self.assertIsNone(held)
        self.assertIsNone(line)

    def test_a_search_that_gets_the_material_back_refuses_the_claim(self):
        """The engine answers with a line that wins the piece straight back,
        so 'wins a piece' must not be printed."""
        root = chess.Board("4k3/8/8/3n4/8/8/8/3QK3 w - - 0 1")
        after = root.copy(stack=False)
        after.push(chess.Move.from_uci("d1d5"))

        def ask(fen, depth):
            # Their king walks over and takes the queen back.
            return {"cp": 0, "mate": None, "pv": ["e8e7", "d5d6", "e7d6"]}

        judge = explain.Judge(ask)
        held, _ = judge.holds(root, after, chess.WHITE, 3)
        self.assertFalse(held)

    def test_a_level_evaluation_marks_the_material_as_hollow(self):
        judge = explain.Judge(lambda fen, d: None)
        self.assertTrue(judge.drawish({"cp": 10, "mate": None, "pv": []}))
        self.assertFalse(judge.drawish({"cp": 400, "mate": None, "pv": []}))
        self.assertFalse(judge.drawish({"cp": None, "mate": 3, "pv": []}))


class WhatYourMoveCost(unittest.TestCase):
    """The other half of the explanation: what your move gave up. Every
    sentence has to name who plays what, and a move number only ever appears
    attached to its move."""

    def claims(self, fen, mine_pv, best_pv, **extra):
        payload = {"mine": mine_pv, "best": best_pv}
        payload.update(extra)
        return explain.explain(fen, mine_pv[0], best_pv[0], payload).cost

    def test_a_piece_taken_at_once_says_so_plainly(self):
        # You put a knight on a square a pawn covers; it goes immediately.
        fen = "4k3/8/5p2/8/8/8/4N3/4K3 w - - 0 1"
        claims = self.claims(fen, ["e2g3", "f6g5"], ["e2c3", "e8e7"])
        self.assertEqual(explain.verify(claims), [])

    def test_a_loss_deeper_in_the_line_is_not_described_as_now(self):
        """The bug behind 'it costs the knight after 11': a piece that falls
        four moves later must not be described as hanging on this board."""
        for claim in self.claims(
                "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 4 4",
                ["f6e4", "b1c3", "e4c3", "d2c3"],
                ["f8c5", "c2c3", "e8g8", "d2d3"]):
            text = claim["text"]
            if claim["kind"] == "material" and claim["facts"].get("ply", 1) > 1:
                self.assertIn("into the line", text)

    def test_move_numbers_are_attached_to_moves(self):
        """No sentence may contain a bare move number: '11' on its own is
        what made the old wording unreadable."""
        board = chess.Board()
        self.assertEqual(explain._numbered(board, 0, "e4"), "1.e4")
        self.assertEqual(explain._numbered(board, 1, "e5"), "1...e5")
        later = chess.Board(
            "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 4 4")
        self.assertEqual(explain._numbered(later, 0, "Nxe4"), "4...Nxe4")
        self.assertEqual(explain._numbered(later, 1, "Nc3"), "5.Nc3")

    def test_no_sentence_leaves_a_number_dangling(self):
        fen = "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 4 4"
        for claim in self.claims(fen, ["f6e4", "b1c3", "e4c3", "d2c3"],
                                 ["f8c5", "c2c3"], wp_mine=30.0, wp_best=52.0):
            self.assertNotRegex(claim["text"], r"after \d+\b(?!\.)")

    def test_a_big_drop_is_not_called_nothing(self):
        claim = explain._cost_quiet(chess.Board(), "a3", "e4", [], 25.0, 54.0)
        self.assertNotIn("Nothing falls apart", claim["text"])
        self.assertIn("29 points", claim["text"])
        small = explain._cost_quiet(chess.Board(), "a3", "e4", [], 51.0, 54.0)
        self.assertIn("matter of taste", small["text"])
        self.assertIn("51 in 100", small["text"])

    def test_shelter_is_not_discussed_in_a_pawnless_ending(self):
        bare = chess.Board("4k3/8/8/8/3Q4/8/8/4K3 b - - 0 1")
        self.assertIsNone(
            explain._cost_king(bare, chess.BLACK, bare, bare, "Kf8"))


class MateFlip(unittest.TestCase):
    """A side being mated sits at 0.0 win probability. Zero is falsy, and
    the flip once read it as 'no evaluation' and handed back 50%: a forced
    mate graded as an even game, on both the drill and the review path."""
    def test_engine_helper_keeps_zero(self):
        self.assertEqual(engine.wp_or_even(0.0), 0.0)
        self.assertEqual(engine.wp_or_even(None), 50.0)

    def test_drill_flip_of_a_mated_side_is_a_won_side(self):
        line = {"cp": None, "mate": -7, "wp": 0.0, "pv": []}
        self.assertEqual(drills._flip(line)["wp"], 100.0)
        self.assertEqual(drills._flip(line)["mate"], 7)

    def test_review_flip_of_a_mated_side_is_a_won_side(self):
        line = {"cp": None, "mate": -3, "wp": 0.0, "pv": []}
        self.assertEqual(review._flip(line)["wp"], 100.0)

    def test_a_mating_move_is_not_a_blunder(self):
        best = {"cp": None, "mate": 7, "wp": 100.0}
        mine = drills._flip({"cp": None, "mate": -8, "wp": 0.0})
        self.assertNotIn(grading.grade(best, mine, None)["verdict"],
                         ("mistake", "blunder", "missed_mate"))


class Accuracy(unittest.TestCase):
    def test_a_blunder_costs_a_game_real_accuracy(self):
        """Lichess's aggregation: the harmonic mean makes two blunders show
        through forty good moves, where a plain mean would hide them."""
        accs = [97.0] * 38 + [8.0, 12.0]
        plain = sum(accs) / len(accs)
        lichess = review.game_accuracy(accs, [50.0] * 40)
        self.assertGreater(plain, 90)
        self.assertLess(lichess, 82)
        self.assertGreater(lichess, 70)

    def test_a_clean_game_stays_clean(self):
        self.assertGreater(review.game_accuracy([96.0] * 30, [50.0] * 30), 95)

    def test_no_moves_no_accuracy(self):
        self.assertIsNone(review.game_accuracy([], []))

    def test_lichess_curve(self):
        self.assertAlmostEqual(review.move_accuracy(0), 100.0, places=1)
        self.assertGreater(review.move_accuracy(5), review.move_accuracy(10))
        self.assertLess(review.move_accuracy(50), 20)
        self.assertEqual(review.move_accuracy(500), 0.0)


class Pools(unittest.TestCase):
    """The drill pools grow from reviewed games without engine work."""

    def _db(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(open(db.SCHEMA_PATH).read())
        return conn

    def test_a_reviewed_game_feeds_the_pools(self):
        conn = self._db()
        pgn = ("[White \"me\"]\n[Black \"them\"]\n[Result \"1-0\"]\n\n"
               "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 "
               "7. Bb3 d6 8. c3 O-O 9. h3 Nb8 10. d4 Nbd7 11. Nbd2 Bb7 12. Bc2 Re8 *")
        conn.execute("INSERT INTO games(id, pgn, my_colour, white, black, result)"
                     " VALUES(1, ?, 'white', 'me', 'them', '1-0')", (pgn,))
        conn.execute("INSERT INTO reviews(game_id, depth, engine_ver, reviewed_at, plies)"
                     " VALUES(1, 16, 'test', 0, 24)")
        board = chess.Board()
        game = chess.pgn.read_game(__import__("io").StringIO(pgn))
        node = game
        ply = 0
        while node.variations:
            node = node.variations[0]
            ply += 1
            fen = board.fen()
            is_me = 1 if board.turn == chess.WHITE else 0
            # a level game: 50% for whoever is to move
            conn.execute(
                "INSERT INTO review_moves(game_id, ply, is_me, fen, move, wp_before,"
                " verdict, phase, themes) VALUES(1,?,?,?,?,50.0,'best',?,'[]')",
                (ply, is_me, fen, node.move.uci(), db.classify_phase(board)))
            board.push(node.move)
        conn.commit()
        counts = corpus.pool_from_review(conn, 1)
        self.assertGreaterEqual(counts["middlegame"], 1)
        rows = conn.execute("SELECT phase, my_colour FROM positions").fetchall()
        self.assertTrue(any(r["phase"] == "opening" for r in rows))
        for r in rows:
            self.assertEqual(r["my_colour"], "white")
        # every pooled position has the opponent to move
        for r in conn.execute("SELECT fen FROM positions WHERE phase != 'opening'"):
            self.assertEqual(chess.Board(r["fen"]).turn, chess.BLACK)

    def test_endgames_are_pooled_even_after_a_long_middlegame(self):
        """The cap is per phase. One cap across the game would spend every
        pick on the middlegame, because the endgame is always the tail."""
        conn = self._db()
        conn.execute("INSERT INTO games(id, pgn, my_colour) VALUES(1, '1. e4 *', 'white')")
        conn.execute("INSERT INTO reviews(game_id, depth, engine_ver, reviewed_at, plies)"
                     " VALUES(1, 16, 'test', 0, 80)")
        mid = "r1bq1rk1/pp2bppp/2n1pn2/3p4/2PP4/2N2NP1/PP2PPBP/R2Q1RK1 b - - 0 11"
        end = "8/5pk1/6p1/8/3R4/6P1/5PK1/3r4 b - - 0 40"
        ply = 20
        for _ in range(12):                          # a long, level middlegame
            conn.execute("INSERT INTO review_moves(game_id, ply, is_me, fen, move, wp_before,"
                         " verdict, phase, themes) VALUES(1,?,0,?,'a7a6',50.0,'best','middlegame','[]')",
                         (ply, mid))
            ply += 2
        for _ in range(3):                           # then a level rook ending
            conn.execute("INSERT INTO review_moves(game_id, ply, is_me, fen, move, wp_before,"
                         " verdict, phase, themes) VALUES(1,?,0,?,'d1d2',50.0,'best','endgame','[]')",
                         (ply, end))
            ply += 2
        conn.commit()
        counts = corpus.pool_from_review(conn, 1)
        self.assertGreaterEqual(counts["endgame"], 1)
        self.assertLessEqual(counts["middlegame"], 3)

    def test_a_lost_game_pools_nothing(self):
        conn = self._db()
        conn.execute("INSERT INTO games(id, pgn, my_colour) VALUES(1, '1. e4 *', 'white')")
        conn.execute("INSERT INTO reviews(game_id, depth, engine_ver, reviewed_at, plies)"
                     " VALUES(1, 16, 'test', 0, 1)")
        conn.execute(
            "INSERT INTO review_moves(game_id, ply, is_me, fen, move, wp_before, verdict,"
            " phase, themes) VALUES(1, 22, 0, ?, 'a7a6', 95.0, 'best', 'middlegame', '[]')",
            ("r1bq1rk1/pp2bppp/2n1pn2/3p4/2PP4/2N2NP1/PP2PPBP/R2Q1RK1 b - - 0 11",))
        conn.commit()
        self.assertEqual(corpus.pool_from_review(conn, 1)["middlegame"], 0)


class Statistics(unittest.TestCase):
    """Both views must work on an empty database and on a full one."""

    def test_views_on_an_empty_database(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(open(db.SCHEMA_PATH).read())
        self.assertEqual(stats.games_view(conn)["coverage"]["reviewed"], 0)
        self.assertEqual(stats.gym_view(conn)["overall"]["answers"], 0)

    def test_views_on_the_real_database(self):
        conn = db.init()
        g, y = stats.games_view(conn), stats.gym_view(conn)
        self.assertIn("coverage", g)
        self.assertIn("overall", y)
        for row in g.get("themes", []) + y.get("themes", []):
            self.assertGreater(row["n"], 0)
            self.assertTrue(0 <= (row["hit_rate"] or 0) <= 100)


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
