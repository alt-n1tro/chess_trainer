"""Win-probability conversion and verdict bands.

Graded in win probability, never centipawns, shown as a percentage. Source of
truth is Stockfish's own WDL at depth 20.
"""
from __future__ import annotations

from .engine import logistic_wp  # noqa: F401  (re-exported for cached fallbacks)

BEST_WP = 2.0       # <= 2.0 points lost: the move
PLAYABLE_WP = 6.0   # <= 6.0 points lost: playable, not best

# Hard clamp. The logistic is flat at extreme evaluations, so at +600cp a
# 2-point band spans well over 100cp -- a genuine blunder would grade as
# perfect exactly where conversion matters most. Bands are therefore never
# wider than 80cp equivalent, scaled proportionally for the inner band.
CLAMP_CP = 80.0
BEST_CP = CLAMP_CP * BEST_WP / PLAYABLE_WP   # 26.7cp


def _mate_rank(line: dict) -> int | None:
    """Lower is better. None when the line is not a mate score."""
    if line.get("mate") is None:
        return None
    m = line["mate"]
    return m if m > 0 else 10_000 - m


def grade(best: dict, mine: dict) -> dict:
    """Compare your move against the engine's best, both from your side.

    `best` and `mine` are cached analysis lines scored from your point of view.
    Returns verdict, the win-probability loss, and the centipawn loss.
    """
    wp_best, wp_mine = best.get("wp", 50.0), mine.get("wp", 50.0)
    delta_wp = max(0.0, wp_best - wp_mine)

    cp_best, cp_mine = best.get("cp"), mine.get("cp")
    mate_best, mate_mine = best.get("mate"), mine.get("mate")

    # Mate scores are handled separately: finding any mate when mate exists is
    # best, missing a mate is wrong whatever the percentages say.
    if mate_best is not None and mate_best > 0:
        if mate_mine is not None and mate_mine > 0:
            verdict = "best" if mate_mine <= mate_best else "playable"
            return _out(verdict, delta_wp, 0.0)
        return _out("wrong", max(delta_wp, PLAYABLE_WP + 1), None)
    if mate_mine is not None and mate_mine < 0:
        # Your move walks into a forced mate.
        return _out("wrong", max(delta_wp, PLAYABLE_WP + 1), None)

    if cp_best is not None and cp_mine is not None:
        delta_cp = max(0.0, float(cp_best - cp_mine))
    else:
        delta_cp = None

    if delta_wp <= BEST_WP and (delta_cp is None or delta_cp <= BEST_CP):
        verdict = "best"
    elif delta_wp <= PLAYABLE_WP and (delta_cp is None or delta_cp <= CLAMP_CP):
        verdict = "playable"
    else:
        verdict = "wrong"
    return _out(verdict, delta_wp, delta_cp)


def _out(verdict: str, delta_wp: float, delta_cp) -> dict:
    return {
        "verdict": verdict,
        "delta_wp": round(delta_wp, 2),
        "delta_cp": None if delta_cp is None else round(delta_cp, 1),
        "label": LABELS[verdict],
    }


LABELS = {
    "best": "Best — the move",
    "playable": "Playable, not best",
    "wrong": "Wrong",
    "shown": "Shown",
}
