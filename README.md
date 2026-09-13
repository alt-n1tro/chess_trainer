# Chess Trainer

A training tool, not a grading tool. A position is set with the opponent to
move, Stockfish plays one of its five best moves, and you find the best reply.
Every answer comes back with a verdict in win probability, a structural
explanation of what your move gave up, and the position returns later if you
got it wrong.

Local web app, one screen, Firefox on Fedora. No accounts, no telemetry, no
outbound network except an explicitly requested chess.com fetch.

## Getting started

```sh
cp /path/to/stockfish vendor/stockfish   # or: ln -s /usr/games/stockfish vendor/stockfish
chmod +x vendor/stockfish
./run doctor                             # engine, DB, venv, port
./run whoami <your-chess.com-username>   # so the importer knows which side you are
./run import <pgn-file-or-url>           # a PGN, a chess.com game, or a monthly archive
./run phases --build                     # build the middlegame and endgame pools
./run web                                # then open http://127.0.0.1:8770/
```

The first run creates `.venv` and installs `python-chess` into it. Nothing is
ever installed with sudo; if a step would need root, the app stops and says so.

## Commands

| Command | Effect |
|---|---|
| `./run web` | Start the server on 8770. Open Firefox yourself. |
| `./run import <pgn-or-url>` | Import a game or a PGN archive into the corpus. |
| `./run phases --build` | Rebuild the middlegame and endgame position pools. |
| `./run phases` | List the current pools with their evals. |
| `./run tree` | Print recorded answers: per position, per depth, per opponent move. |
| `./run leaks` | Print the current leak list. |
| `./run warm` | Pre-analyse everything not yet cached. Long-running. |
| `./run doctor` | Check engine binary, DB integrity, venv, port availability. |
| `./run whoami <name>` | Record which chess.com name is you, and re-tag stored games. |

`warm` and `phases --build` state what they will do and require a yes; `--yes`
skips the gate for scripted use. Everything read-only takes flags and never
prompts.

## Keys

`Enter` next · `O`/`B` menu · `M` cycle mode · `E` set up a position ·
`S` save · `Backspace` back · `R` reset and replay this position · `?`/`H` help ·
`Esc` close · `←` `→` step a move in a game · `Home`/`End` jump · `1`–`5` pick
the nth opponent option.

## How it is put together

- `server/engine.py` — two persistent Stockfish processes: one foreground, one
  warmer. Analysis is cached by `(position, depth, MultiPV, engine version)`,
  so a deeper request never silently serves a shallow result.
- `server/grading.py` — win probability from Stockfish's own WDL, with bands at
  2 and 6 points and a hard 80cp clamp so a blunder in a winning position
  cannot grade as perfect.
- `server/explain.py` — the structural explainer. It diffs the two principal
  variations and reports material, trapped pieces, square control, king safety,
  activity and tempo. No language model, no network. `explain()` is the seam
  where a richer explainer would plug in.
- `server/drills.py` — rounds, the path-keyed tree, weighted replay, leaks.
- `server/corpus.py` — PGN import, chess.com fetch, phase extraction.
- `web/` — plain HTML, CSS and modules. The front end holds no chess logic: the
  server says what is on the board and what is clickable.

Everything lives in `data/trainer.db`.
