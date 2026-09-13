"""Win-probability conversion, move ranking, and verdict names.

Graded in win probability, never centipawns, shown as a percentage. Source of
truth is Stockfish's own WDL at depth 20.

A verdict names what actually happened. "Best move" is said only when you
played the engine's first choice; the second choice is called the second best;
and a move that throws the position away is called a blunder whatever its rank.
"""
from __future__ import annotations

from .engine import logistic_wp  # noqa: F401  (re-exported for cached fallbacks)

# Win-probability points lost against the engine's best move.
INACCURACY_WP = 4.0
MISTAKE_WP = 10.0
BLUNDER_WP = 20.0

# Centipawns are reported alongside a verdict but never decide it. Stockfish's
# own WDL already accounts for how little a centipawn is worth at +900 and how
# much it is worth at 0.00; a centipawn floor on top of that calls a move that
# keeps a won position "an inaccuracy" for dropping from +9 to +7.

ORDINALS = ["Best move", "Second best", "Third best", "Fourth best",
            "Fifth best"]

# verdict -> (label, tone). Tone drives colour and nothing else.
TONES = {
    "best": "best",
    "second": "good", "third": "good", "fourth": "good", "fifth": "good",
    "good": "good",
    "inaccuracy": "inaccuracy",
    "mistake": "mistake",
    "blunder": "blunder",
    "missed_mate": "blunder",
    "shown": "shown",
    # Answers recorded before verdicts were named this way. Kept so old
    # statistics stay readable rather than being rewritten.
    "playable": "good",
    "wrong": "mistake",
}
RANK_VERDICTS = ["best", "second", "third", "fourth", "fifth"]
MISSES = ("inaccuracy", "mistake", "blunder", "missed_mate", "shown", "wrong")


def rank_of(lines: list[dict], uci: str) -> int | None:
    """Where your move sits in the engine's ordered list, 1-based."""
    for i, line in enumerate(lines):
        if line.get("move") == uci:
            return i + 1
    return None


def grade(best: dict, mine: dict, rank: int | None = None) -> dict:
    """Compare your move against the engine's best, both from your side.

    `rank` is your move's place in the engine's top lines, when it is in them.
    """
    wp_best, wp_mine = best.get("wp", 50.0), mine.get("wp", 50.0)
    delta_wp = max(0.0, wp_best - wp_mine)

    cp_best, cp_mine = best.get("cp"), mine.get("cp")
    mate_best, mate_mine = best.get("mate"), mine.get("mate")
    delta_cp = None
    if cp_best is not None and cp_mine is not None:
        delta_cp = max(0.0, float(cp_best - cp_mine))

    # Mate scores are their own question: finding any mate when mate exists is
    # best, and missing one is a blunder whatever the percentages say.
    if mate_best is not None and mate_best > 0:
        if mate_mine is not None and mate_mine > 0:
            # Any mate as fast as the engine's is the move, whatever its rank
            # in the list; a slower one still wins and is not a mistake.
            if mine.get("final") == "mate":
                return _out("best", 0.0, 0.0, rank, detail="Checkmate")
            if mate_mine <= mate_best:
                return _out("best", 0.0, 0.0, rank,
                            detail="Mate in %d" % mate_mine)
            return _out("good", 0.0, 0.0, rank,
                        detail="Mate in %d; there was mate in %d"
                               % (mate_mine, mate_best))
        return _out("missed_mate", max(delta_wp, BLUNDER_WP), delta_cp, rank,
                    detail="Mate in %d was there" % mate_best)
    if mate_mine is not None and mate_mine < 0:
        return _out("blunder", max(delta_wp, BLUNDER_WP), delta_cp, rank,
                    detail="It walks into mate in %d" % abs(mate_mine))

    severity = _severity(delta_wp, delta_cp)
    if severity:
        return _out(severity, delta_wp, delta_cp, rank)
    return _out(_rank_verdict(rank, delta_wp), delta_wp, delta_cp, rank)


def _severity(delta_wp: float, delta_cp) -> str | None:
    """A move that costs this much is named for the damage, not for its rank."""
    if delta_wp >= BLUNDER_WP:
        return "blunder"
    if delta_wp >= MISTAKE_WP:
        return "mistake"
    if delta_wp >= INACCURACY_WP:
        return "inaccuracy"
    return None


def _rank_verdict(rank: int | None, delta_wp: float) -> str:
    if rank and 1 <= rank <= len(RANK_VERDICTS):
        return RANK_VERDICTS[rank - 1]
    return "good"


def _out(verdict: str, delta_wp: float, delta_cp, rank, detail: str = "") -> dict:
    return {
        "verdict": verdict,
        "label": label_for(verdict, rank),
        "tone": TONES[verdict],
        "rank": rank,
        "detail": detail,
        "delta_wp": round(delta_wp, 2),
        "delta_cp": None if delta_cp is None else round(delta_cp, 1),
    }


def label_for(verdict: str, rank: int | None = None) -> str:
    if verdict in RANK_VERDICTS:
        return ORDINALS[RANK_VERDICTS.index(verdict)]
    if verdict == "good":
        return "Good move"
    if verdict == "inaccuracy":
        return "Inaccuracy"
    if verdict == "mistake":
        return "Mistake"
    if verdict == "blunder":
        return "Blunder"
    if verdict == "playable":
        return "Good move"
    if verdict == "wrong":
        return "Mistake"
    if verdict == "missed_mate":
        return "Missed mate"
    return "Shown"


LABELS = {v: label_for(v) for v in TONES}
