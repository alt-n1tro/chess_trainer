"""Statistics you can act on.

Two views. "Your games" reads the engine review of the games you actually
played. "Gym" reads your answers in the trainer. Both break down by phase and
by theme, because "weak in the middlegame" is not something you can work on
and "you miss forks" is.

Every number carries its sample size. A 40% hit rate on three forks is a
hint, not a fact, and the page says so.
"""
from __future__ import annotations

import json
import time

import chess

from . import db, grading, review, themes
from .engine import DEPTH_GRADE

PHASES = ("opening", "middlegame", "endgame")
MISS_SQL = "(" + ", ".join(f"'{v}'" for v in grading.MISSES) + ")"
BAD_SQL = "('mistake', 'blunder', 'missed_mate', 'wrong')"


def _pct(num, den) -> float | None:
    return round(100.0 * num / den, 1) if den else None


# --- your games --------------------------------------------------------------

def games_view(conn) -> dict:
    cov = review.coverage(conn)
    out = {"coverage": cov}
    if not cov["reviewed"]:
        return out

    g = conn.execute(
        "SELECT COUNT(*) games, ROUND(AVG(r.accuracy), 1) accuracy"
        " FROM reviews r").fetchone()
    per_game = conn.execute(
        "SELECT ROUND(1.0 * SUM(verdict IN ('blunder','missed_mate')) / COUNT(DISTINCT game_id), 2) blunders,"
        " ROUND(1.0 * SUM(verdict='mistake') / COUNT(DISTINCT game_id), 2) mistakes,"
        " ROUND(1.0 * SUM(verdict='inaccuracy') / COUNT(DISTINCT game_id), 2) inaccuracies,"
        " COUNT(*) moves FROM review_moves WHERE is_me=1").fetchone()
    out["overall"] = {"games": g["games"], "accuracy": g["accuracy"],
                      "moves": per_game["moves"],
                      "blunders_per_game": per_game["blunders"],
                      "mistakes_per_game": per_game["mistakes"],
                      "inaccuracies_per_game": per_game["inaccuracies"]}

    # score and accuracy by colour
    out["by_colour"] = []
    for colour in ("white", "black"):
        row = conn.execute(
            "SELECT COUNT(*) games, ROUND(AVG(r.accuracy),1) accuracy,"
            " SUM(CASE WHEN (g.my_colour='white' AND g.result='1-0') OR"
            "               (g.my_colour='black' AND g.result='0-1') THEN 1 ELSE 0 END) wins,"
            " SUM(CASE WHEN g.result='1/2-1/2' THEN 1 ELSE 0 END) draws"
            " FROM reviews r JOIN games g ON g.id=r.game_id WHERE g.my_colour=?",
            (colour,)).fetchone()
        if row["games"]:
            out["by_colour"].append({
                "colour": colour, "games": row["games"], "accuracy": row["accuracy"],
                "score": _pct((row["wins"] or 0) + 0.5 * (row["draws"] or 0), row["games"])})

    # by phase: the basic graph you asked for
    out["by_phase"] = []
    for phase in PHASES:
        row = conn.execute(
            "SELECT COUNT(*) moves, ROUND(AVG(accuracy),1) accuracy,"
            " SUM(verdict IN ('blunder','missed_mate')) blunders,"
            " SUM(verdict='mistake') mistakes, SUM(verdict='best') best"
            " FROM review_moves WHERE is_me=1 AND phase=?", (phase,)).fetchone()
        out["by_phase"].append({
            "phase": phase, "moves": row["moves"], "accuracy": row["accuracy"],
            "best_rate": _pct(row["best"] or 0, row["moves"]),
            "blunder_rate": _pct(row["blunders"] or 0, row["moves"]),
            "mistake_rate": _pct(row["mistakes"] or 0, row["moves"])})

    # by theme: when the best move was about X, how often did you find it,
    # and when you went wrong, what did it let the opponent do
    found: dict = {}
    allowed: dict = {}
    for row in conn.execute(
            "SELECT verdict, themes, allowed, phase FROM review_moves WHERE is_me=1"):
        for t in json.loads(row["themes"]):
            if t in PHASES:
                continue
            f = found.setdefault(t, {"n": 0, "hit": 0, "bad": 0})
            f["n"] += 1
            f["hit"] += row["verdict"] == "best"
            f["bad"] += row["verdict"] in ("mistake", "blunder", "missed_mate")
        if row["allowed"]:
            for t in json.loads(row["allowed"]):
                if t in PHASES:
                    continue
                allowed[t] = allowed.get(t, 0) + 1
    out["themes"] = _theme_rows(found)
    out["allowed"] = sorted(
        ({"theme": t, "label": themes.LABELS.get(t, t), "n": n} for t, n in allowed.items()),
        key=lambda r: -r["n"])

    # openings: where you come out of the opening, and how it goes
    out["openings"] = [dict(r) for r in conn.execute(
        "SELECT p.name, g.my_colour colour, COUNT(DISTINCT g.id) games,"
        " ROUND(AVG(r.accuracy),1) accuracy,"
        " ROUND(100.0 * SUM(CASE WHEN (g.my_colour='white' AND g.result='1-0') OR"
        "   (g.my_colour='black' AND g.result='0-1') THEN 1"
        "   WHEN g.result='1/2-1/2' THEN 0.5 ELSE 0 END) / COUNT(DISTINCT g.id), 0) score,"
        " ROUND(AVG(m.wp_before), 0) wp_at_12"
        " FROM reviews r JOIN games g ON g.id=r.game_id"
        " JOIN positions p ON p.source_game=g.id AND p.phase='opening'"
        " LEFT JOIN review_moves m ON m.game_id=g.id AND m.is_me=1"
        "   AND m.ply BETWEEN 23 AND 26"
        " GROUP BY p.name, g.my_colour HAVING games >= 1"
        " ORDER BY games DESC, score ASC LIMIT 20")]

    # mates you had
    out["mates"] = [dict(r) for r in conn.execute(
        "SELECT m.id, m.game_id, m.ply, m.mate_in, m.kept_mate, m.fen, m.move, m.best,"
        " g.white, g.black, g.my_colour FROM review_moves m JOIN games g ON g.id=m.game_id"
        " WHERE m.is_me=1 AND m.mate_in IS NOT NULL AND m.mate_in <= ?"
        " ORDER BY m.kept_mate ASC, m.mate_in ASC, m.game_id DESC LIMIT 40",
        (review.MATE_HORIZON,))]
    for m in out["mates"]:
        m["san"] = _san(m["fen"], m["move"])
        m["best_san"] = _san(m["fen"], m["best"])
    out["mates_summary"] = conn.execute(
        "SELECT COUNT(*) had, SUM(kept_mate) found FROM review_moves"
        " WHERE is_me=1 AND mate_in IS NOT NULL AND mate_in <= ?",
        (review.MATE_HORIZON,)).fetchone()
    out["mates_summary"] = dict(out["mates_summary"])

    # the worst moments, each drillable
    out["worst"] = [dict(r) for r in conn.execute(
        "SELECT m.id, m.game_id, m.ply, m.fen, m.move, m.best, m.delta_wp, m.verdict,"
        " m.themes, m.phase, g.white, g.black, g.my_colour"
        " FROM review_moves m JOIN games g ON g.id=m.game_id"
        " WHERE m.is_me=1 AND m.verdict IN ('blunder','missed_mate')"
        " ORDER BY m.delta_wp DESC LIMIT 15")]
    for w in out["worst"]:
        w["san"] = _san(w["fen"], w["move"])
        w["best_san"] = _san(w["fen"], w["best"])
        w["themes"] = [themes.LABELS.get(t, t) for t in json.loads(w["themes"])
                       if t not in PHASES]

    # trend, oldest to newest
    out["trend"] = [dict(r) for r in conn.execute(
        "SELECT r.accuracy, g.played_at, g.result, g.my_colour FROM reviews r"
        " JOIN games g ON g.id=r.game_id ORDER BY g.played_at ASC, g.id ASC")][-40:]
    return out


def _theme_rows(found: dict) -> list[dict]:
    rows = []
    for t, f in found.items():
        rows.append({"theme": t, "label": themes.LABELS.get(t, t), "n": f["n"],
                     "hit_rate": _pct(f["hit"], f["n"]),
                     "bad_rate": _pct(f["bad"], f["n"])})
    order = [t for t, _ in themes.THEMES] + ["mate_in_1", "mate_in_2", "mate_in_3",
                                              "long_mate", "back_rank_mate"]
    rows.sort(key=lambda r: (order.index(r["theme"]) if r["theme"] in order else 99))
    return rows


def _san(fen, uci):
    if not uci:
        return None
    try:
        board = chess.Board(fen)
        return board.san(chess.Move.from_uci(uci))
    except (ValueError, AssertionError):
        return uci


# --- the gym -----------------------------------------------------------------

def gym_view(conn, pool=None) -> dict:
    out: dict = {}
    total = conn.execute(
        "SELECT COUNT(*) answers, SUM(verdict='best') best,"
        " SUM(verdict IN ('second','third','fourth','fifth','good','playable')) good,"
        " SUM(verdict='inaccuracy') inaccuracy, SUM(verdict='mistake') mistake,"
        " SUM(verdict IN ('blunder','missed_mate','wrong')) blunder,"
        " SUM(verdict='shown') shown FROM answers").fetchone()
    out["overall"] = dict(total)
    if not total["answers"]:
        return out

    def breakdown(sql, args=()):
        rows = []
        for r in conn.execute(sql, args):
            rows.append({**dict(r), "best_rate": _pct(r["best"] or 0, r["n"]),
                         "miss_rate": _pct(r["misses"] or 0, r["n"])})
        return rows

    out["by_phase"] = breakdown(
        "SELECT p.phase key, COUNT(*) n, SUM(a.verdict='best') best,"
        f" SUM(a.verdict IN {MISS_SQL}) misses"
        " FROM answers a JOIN positions p ON p.pos_hash=a.root_hash"
        " GROUP BY p.phase")
    out["by_level"] = breakdown(
        "SELECT depth_level key, COUNT(*) n, SUM(verdict='best') best,"
        f" SUM(verdict IN {MISS_SQL}) misses FROM answers GROUP BY depth_level")
    out["by_step"] = breakdown(
        "SELECT step key, COUNT(*) n, SUM(verdict='best') best,"
        f" SUM(verdict IN {MISS_SQL}) misses FROM answers GROUP BY step")
    out["by_opening"] = breakdown(
        "SELECT p.name key, COUNT(*) n, SUM(a.verdict='best') best,"
        f" SUM(a.verdict IN {MISS_SQL}) misses"
        " FROM answers a JOIN positions p ON p.pos_hash=a.root_hash"
        " GROUP BY p.name ORDER BY n DESC LIMIT 15")

    # themes of the questions you have answered
    found: dict = {}
    for row in conn.execute(
            "SELECT a.verdict, t.themes FROM answers a"
            " JOIN position_themes t ON t.pos_hash=a.pos_hash"):
        for t in json.loads(row["themes"]):
            if t in PHASES:
                continue
            f = found.setdefault(t, {"n": 0, "hit": 0, "bad": 0})
            f["n"] += 1
            f["hit"] += row["verdict"] == "best"
            f["bad"] += row["verdict"] in grading.MISSES
    out["themes"] = _theme_rows(found)
    untagged = conn.execute(
        "SELECT COUNT(DISTINCT a.pos_hash) n FROM answers a"
        " LEFT JOIN position_themes t ON t.pos_hash=a.pos_hash WHERE t.pos_hash IS NULL"
    ).fetchone()["n"]
    out["untagged"] = untagged

    # recent form: last 50 answers
    out["recent"] = [r["verdict"] for r in conn.execute(
        "SELECT verdict FROM answers ORDER BY id DESC LIMIT 50")][::-1]
    return out


def tag_position(conn, pos_hash: int, fen: str, lines: list[dict]) -> list[str]:
    """Record what a drilled position is about. Cheap: it reads the analysis
    grading already did."""
    if not lines:
        return []
    best = lines[0]
    tags = themes.tag(fen, best.get("move"), best.get("pv"),
                      best.get("mate") if (best.get("mate") or 0) > 0 else None)
    tags.append(db.classify_phase(chess.Board(fen)))
    conn.execute(
        "INSERT OR REPLACE INTO position_themes(pos_hash, themes, computed_at)"
        " VALUES(?,?,?)", (pos_hash, json.dumps(tags), int(time.time())))
    conn.commit()
    return tags


def backfill_tags(conn, limit: int = 500) -> int:
    """Tag answered positions from cached analysis, for answers recorded
    before tagging existed. Needs no engine."""
    rows = conn.execute(
        "SELECT DISTINCT a.pos_hash, n.fen FROM answers a"
        " JOIN tree_nodes n ON n.pos_hash=a.pos_hash"
        " LEFT JOIN position_themes t ON t.pos_hash=a.pos_hash"
        " WHERE t.pos_hash IS NULL LIMIT ?", (limit,)).fetchall()
    done = 0
    for r in rows:
        hit = conn.execute(
            "SELECT lines FROM analysis WHERE pos_hash=? AND depth>=? AND multipv>=1"
            " ORDER BY multipv DESC, depth DESC LIMIT 1",
            (r["pos_hash"], DEPTH_GRADE)).fetchone()
        if not hit:
            continue
        from .engine import with_wp
        tag_position(conn, r["pos_hash"], r["fen"], with_wp(json.loads(hit["lines"])))
        done += 1
    return done
