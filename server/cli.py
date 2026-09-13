"""./run subcommands: the command line side of the app."""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

import chess
import chess.pgn

from . import app, corpus, db, engine, review


class Progress:
    """A bar with a percentage, the stage, and an ETA once enough samples
    exist. Updates at most 10x/s and only on a change, so rendering never
    measurably slows the work."""

    def __init__(self, total: int, stage: str = ""):
        self.total = max(1, total)
        self.stage = stage
        self.n = 0
        self.started = time.time()
        self.last_draw = 0.0
        self.last_text = ""
        self.enabled = sys.stderr.isatty()

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self.draw(force=True)

    def step(self, n: int = 1, total: int | None = None) -> None:
        self.n += n
        if total is not None:
            self.total = max(1, total)
        self.draw()

    def draw(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_draw < 0.1:
            return
        frac = min(1.0, self.n / self.total)
        width = 28
        filled = int(frac * width)
        eta = ""
        elapsed = now - self.started
        if self.n >= 5 and frac > 0:
            remaining = elapsed / frac - elapsed
            eta = f"  eta {_dur(remaining)}"
        text = (f"[{'#' * filled}{'.' * (width - filled)}] {frac * 100:5.1f}%  "
                f"{self.stage} {self.n}/{self.total}{eta}")
        if text == self.last_text:
            return
        self.last_text = text
        self.last_draw = now
        if self.enabled:
            sys.stderr.write("\r" + text[:110].ljust(110))
            sys.stderr.flush()
        elif force:
            print(text, file=sys.stderr)

    def done(self) -> None:
        if self.enabled:
            sys.stderr.write("\r" + " " * 110 + "\r")
            sys.stderr.flush()


def _dur(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def confirm(summary: str, assume_yes: bool) -> bool:
    """State what will happen and require an explicit yes."""
    print(summary)
    if assume_yes:
        print("--yes given; starting.")
        return True
    if not sys.stdin.isatty():
        print("Not a terminal and no --yes; refusing to start.", file=sys.stderr)
        return False
    try:
        reply = input("Go ahead? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return reply in ("y", "yes")


# --- commands -------------------------------------------------------------

def cmd_web(args) -> int:
    info = engine.probe()
    if not info["ok"]:
        print(f"Engine not usable: {info.get('error')}\nSee vendor/README.md.",
              file=sys.stderr)
        return 1
    app.serve(args.host, args.port)
    return 0


def cmd_import(args) -> int:
    conn = db.init()
    if args.player:
        return _import_player(conn, args)
    if not args.source:
        print("Give a PGN file, a URL, or --player <username>.", file=sys.stderr)
        return 2
    bar = None

    def progress(n):
        nonlocal bar
        if n == 21 and bar is None:
            bar = Progress(n, "parsing")
        if bar:
            bar.step(0, total=max(n, bar.total))
            bar.n = n
            bar.draw()

    try:
        ids = corpus.import_source(conn, args.source, progress=progress)
    except (LookupError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if bar:
        bar.done()
    print(f"Imported {len(ids)} game(s).")
    mine = sum(
        1 for i in ids
        if conn.execute("SELECT my_colour FROM games WHERE id=?", (i,)).fetchone()["my_colour"]
    )
    if ids and not mine and not corpus.my_username(conn):
        print("None of these are marked as yours: run ./run whoami <username>"
              " and re-import, or they stay out of the phase pools.")
    print("Next: ./run phases --build")
    return 0


def _import_player(conn, args) -> int:
    """Import a whole chess.com history, one time class at a time."""
    user = args.player
    classes = None if args.time_class == "all" else tuple(
        c.strip() for c in args.time_class.split(",") if c.strip()
    )
    if classes:
        unknown = [c for c in classes if c not in corpus.TIME_CLASSES]
        if unknown:
            print(f"Unknown time class {', '.join(unknown)}. Known:"
                  f" {', '.join(corpus.TIME_CLASSES)}, or all.", file=sys.stderr)
            return 2
    if not corpus.my_username(conn):
        db.meta_set(conn, "username", user)
        print(f"You are {user}.")

    bar = Progress(1, "fetching archive list")
    bar.draw(force=True)

    def progress(done, total, month, counts):
        bar.total = max(1, total)
        bar.n = done
        bar.stage = (f"{month}  {counts['kept']} kept of {counts['seen']} seen")
        bar.draw()

    try:
        counts = corpus.import_player(conn, user, classes, args.since, progress)
    except (LookupError, ValueError) as exc:
        bar.done()
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        bar.done()

    label = "every time class" if classes is None else ", ".join(classes)
    print(f"{counts['months']} month(s), {counts['seen']} game(s) on chess.com,"
          f" {counts['kept']} in {label}, {counts['new']} new to the corpus.")
    mine = conn.execute(
        "SELECT COUNT(*) n FROM games WHERE my_colour IS NOT NULL"
    ).fetchone()["n"]
    print(f"{mine} game(s) are yours and will feed the pools.")
    print("Next: ./run phases --build")
    return 0


def cmd_whoami(args) -> int:
    conn = db.init()
    if not args.username:
        print(corpus.my_username(conn) or "(not set)")
        return 0
    db.meta_set(conn, "username", args.username)
    n = conn.execute(
        "UPDATE games SET my_colour = CASE"
        " WHEN lower(white)=lower(?) THEN 'white'"
        " WHEN lower(black)=lower(?) THEN 'black' ELSE NULL END",
        (args.username, args.username),
    ).rowcount
    conn.commit()
    print(f"You are {args.username}. Re-tagged {n} stored game(s).")
    return 0


def cmd_phases(args) -> int:
    """List the drill pools, or add games to them."""
    conn = db.init()
    if not args.build and not args.rebuild:
        rows = conn.execute(
            "SELECT phase, name, my_colour, eval_cp, tail_san FROM positions"
            " ORDER BY phase, name, eval_cp"
        ).fetchall()
        if not rows:
            print("No pools yet. ./run phases --build")
            return 0
        for phase in ("opening", "middlegame", "endgame"):
            group = [r for r in rows if r["phase"] == phase]
            print(f"\n{phase}  ({len(group)})")
            for r in group:
                ev = "    -" if r["eval_cp"] is None else f"{r['eval_cp']:+5d}"
                print(f"  {ev}cp  {r['my_colour'][:1].upper()}  "
                      f"{(r['name'] or '')[:28]:28s}  {r['tail_san'] or ''}")
        return 0

    games = conn.execute(
        "SELECT COUNT(*) n FROM games WHERE my_colour IS NOT NULL"
    ).fetchone()["n"]
    if not games:
        print("No games of yours are imported. ./run import --player <name>"
              " first.", file=sys.stderr)
        return 1
    existing = conn.execute("SELECT COUNT(*) n FROM positions").fetchone()["n"]
    if args.rebuild:
        pending = games
    else:
        pending = conn.execute(
            "SELECT COUNT(*) n FROM games g WHERE g.my_colour IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM positions p WHERE p.source_game=g.id)"
        ).fetchone()["n"]
    if not pending:
        print(f"All {games} game(s) are pooled already ({existing} positions)."
              " --rebuild starts over.")
        return 0
    unreviewed = conn.execute(
        "SELECT COUNT(*) n FROM games g WHERE g.my_colour IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.game_id=g.id)"
        + ("" if args.rebuild else
           " AND NOT EXISTS (SELECT 1 FROM positions p WHERE p.source_game=g.id)")
    ).fetchone()["n"]

    lines = [
        (f"Rebuild the pools from all {games} game(s), replacing the current"
         f" {existing} position(s).") if args.rebuild else
        (f"Add {pending} game(s) to the pools ({existing} positions already)."),
        f"  - {pending - unreviewed} reviewed game(s) are pooled from their"
        f" review: no engine time",
    ]
    if unreviewed:
        lines.append(
            f"  - {unreviewed} unreviewed game(s) are searched at depth"
            f" {engine.DEPTH_FILTER}, survivors at {engine.DEPTH_GRADE}:"
            f" roughly {_dur(unreviewed * 12)}")
    if not confirm("\n".join(lines) + "\n", args.yes):
        return 1

    pool = None
    if unreviewed:
        info = engine.probe()
        if not info["ok"]:
            print(f"Engine not usable: {info.get('error')}", file=sys.stderr)
            return 1
        pool = engine.Pool()
    bar = Progress(pending, "pooling")
    try:
        def progress(gi, total, examined):
            bar.total = max(1, total)
            bar.n = gi
            bar.stage = f"game {gi + 1}/{total}, {examined} positions searched"
            bar.draw()

        counts = corpus.build_phases(conn, pool, progress=progress,
                                     rebuild=args.rebuild)
    finally:
        bar.done()
        if pool:
            pool.close()
    print(f"openings {counts['opening']}, middlegame {counts['middlegame']},"
          f" endgame {counts['endgame']}  (from {counts['games']} game(s))")
    return 0


def cmd_warm(args) -> int:
    conn = db.init()
    info = engine.probe()
    if not info["ok"]:
        print(f"Engine not usable: {info.get('error')}", file=sys.stderr)
        return 1
    rows = conn.execute("SELECT fen, phase FROM positions").fetchall()
    todo = []
    pool_ver = info["version"]
    for r in rows:
        for depth, multipv in ((engine.DEPTH_CANDIDATES, engine.MULTIPV_CANDIDATES),
                               (engine.DEPTH_GRADE, 1)):
            hit = conn.execute(
                "SELECT 1 FROM analysis WHERE pos_hash=? AND depth=? AND"
                " multipv=? AND engine_ver=?",
                (db.pos_hash(chess.Board(r["fen"]), r["phase"]), depth, multipv,
                 pool_ver),
            ).fetchone()
            if not hit:
                todo.append((r["fen"], r["phase"], depth, multipv))
    if not todo:
        print("Everything in the pools is already cached.")
        return 0
    if not confirm(
        f"Pre-analyse {len(todo)} missing analysis job(s) over"
        f" {len(rows)} pooled position(s).\n"
        f"  - writes to the analysis cache only; nothing is overwritten\n"
        f"  - rough estimate {_dur(len(todo) * 2.5)}, interruptible with ^C\n",
        args.yes,
    ):
        return 1
    pool = engine.Pool()
    bar = Progress(len(todo), "analysing")
    done = 0
    try:
        for fen, phase, depth, multipv in todo:
            pool.analyse(chess.Board(fen), depth, multipv, phase=phase)
            done += 1
            bar.step()
    except KeyboardInterrupt:
        bar.done()
        print(f"\nStopped. {done} of {len(todo)} done; the rest stays for next time.")
        return 130
    finally:
        bar.done()
        pool.close()
    print(f"Cached {done} analysis job(s).")
    return 0


def cmd_review(args) -> int:
    """Engine-review your games, newest first. Resumable: already reviewed
    games are skipped, so this can be run again after every import."""
    conn = db.init()
    todo = review.pending(conn, args.depth, args.limit)
    cov = review.coverage(conn)
    if not todo:
        print(f"All {cov['reviewed']} of your games are reviewed at depth"
              f" {args.depth} or deeper.")
        return 0
    plies = sum(
        len(r["pgn"].split()) // 2 for r in todo)      # rough: tokens / 2
    if not confirm(
        f"Review {len(todo)} game(s) at depth {args.depth}"
        f" ({cov['reviewed']} of {cov['games']} already done).\n"
        f"  - roughly {plies} positions to search; estimate {_dur(plies * 0.7)}\n"
        f"  - writes review rows and fills the analysis cache; nothing is lost\n"
        f"  - interruptible with ^C; finished games stay finished\n", args.yes,
    ):
        return 1
    info = engine.probe()
    if not info["ok"]:
        print(f"Engine not usable: {info.get('error')}", file=sys.stderr)
        return 1
    pool = engine.Pool()
    bar = Progress(len(todo), "reviewing")
    done = 0
    try:
        for gi, row in enumerate(todo):
            label = f"{row['white']} vs {row['black']}"[:34]

            def progress(i, n, label=label, gi=gi):
                bar.n = gi
                bar.stage = f"{label}  {i}/{n}"
                bar.draw()

            summary = review.review_game(conn, pool, row, args.depth, progress,
                                         budget=args.budget)
            done += 1
            if summary and summary["accuracy"] is not None and not bar.enabled:
                print(f"  {label}: accuracy {summary['accuracy']}")
    except KeyboardInterrupt:
        bar.done()
        print(f"\nStopped. {done} of {len(todo)} reviewed; the rest waits for"
              f" next time.")
        return 130
    finally:
        bar.done()
        pool.close()
    cov = review.coverage(conn)
    print(f"Reviewed {done} game(s). {cov['reviewed']} of {cov['games']} done.")
    print("Open the statistics with ? in the app.")
    return 0


def cmd_doctor(args) -> int:
    ok = True
    print(f"python      {sys.version.split()[0]}  ({sys.executable})")
    try:
        import chess as c
        print(f"python-chess {c.__version__}")
    except ImportError:
        print("python-chess MISSING")
        ok = False

    info = engine.probe()
    if info["ok"]:
        print(f"engine      {info['version']}  NNUE yes  "
              f"Threads {info['threads']} x {info['pool_size']} processes")
        if info["threads"] == 1:
            print(f"            (fewer than 6 cores: {os.cpu_count()},"
                  f" so Threads 1)")
    else:
        print(f"engine      NOT USABLE: {info.get('error')}  ({info['path']})")
        ok = False

    try:
        conn = db.init()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {
            t: conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
            for t in ("games", "positions", "analysis", "answers", "tree_nodes",
                      "saved")
        }
        print(f"database    {db.DB_PATH}  integrity {integrity}"
              f"  schema v{db.meta_get(conn, 'schema_version')}")
        print("            " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        if integrity != "ok":
            ok = False
    except Exception as exc:
        print(f"database    FAILED: {exc}")
        ok = False

    sock = socket.socket()
    try:
        sock.bind((app.HOST, app.PORT))
        print(f"port        {app.PORT} free")
    except OSError as exc:
        print(f"port        {app.PORT} NOT AVAILABLE: {exc}")
        ok = False
    finally:
        sock.close()

    print("ok" if ok else "not ready")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="./run",
        description="Chess Trainer. A gym built from your own games.",
        epilog="Commands that take time confirm before starting; --yes skips"
               " the question. Everything that only reads never prompts.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="command")

    p = sub.add_parser("web", help="start the app on 127.0.0.1:8770")
    p.add_argument("--host", default=app.HOST)
    p.add_argument("--port", type=int, default=app.PORT)
    p.set_defaults(func=cmd_web)

    p = sub.add_parser(
        "import", help="import games: a PGN file, a chess.com link, or a player",
        description="Import games. Give a PGN file or a chess.com game link, or"
                    " use --player to fetch a chess.com player's whole history.")
    p.add_argument("source", nargs="?",
                   help="a PGN file, a chess.com game link, or an archive URL")
    p.add_argument("--player", metavar="USERNAME",
                   help="fetch every game of this chess.com user; re-running"
                        " picks up only what is new")
    p.add_argument("--time-class", default="rapid",
                   help="with --player: rapid (default), blitz, bullet, daily,"
                        " a comma-separated list, or all")
    p.add_argument("--since", metavar="YYYY-MM",
                   help="with --player: skip months before this one")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser(
        "review", help="engine-review your games; feeds statistics and pools",
        description="Engine-review your games, newest first. Every move is"
                    " graded and tagged, and each reviewed game's positions"
                    " join the drill pools. Resumable: reviewed games are"
                    " skipped, so run it again after each import.")
    p.add_argument("--depth", type=int, default=review.REVIEW_DEPTH,
                   help=f"depth floor per position (default {review.REVIEW_DEPTH});"
                        " simple positions go much deeper within --budget")
    p.add_argument("--budget", type=float, default=review.BUDGET,
                   help=f"seconds of extra search per position (default {review.BUDGET})")
    p.add_argument("--limit", type=int, metavar="N",
                   help="review at most N games this run")
    p.add_argument("--yes", action="store_true", help="skip the confirmation")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser(
        "phases", help="list the drill pools, or --build to add games to them",
        description="Without flags, list the pooled positions. --build adds"
                    " games not yet pooled: reviewed ones instantly, the rest"
                    " with the engine. --rebuild starts over.")
    p.add_argument("--build", action="store_true", help="add unpooled games")
    p.add_argument("--rebuild", action="store_true",
                   help="delete the pools and build them again from every game")
    p.add_argument("--yes", action="store_true", help="skip the confirmation")
    p.set_defaults(func=cmd_phases)

    p = sub.add_parser(
        "warm", help="pre-analyse pooled positions so drills never wait",
        description="Analyse every pooled position that is not yet cached, at"
                    " the depths the drills use. Long-running and interruptible.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation")
    p.set_defaults(func=cmd_warm)

    p = sub.add_parser("whoami", help="show or set which chess.com name is you")
    p.add_argument("username", nargs="?",
                   help="set it and re-tag stored games; omit to show")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("doctor", help="check the engine, database, venv and port")
    p.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
