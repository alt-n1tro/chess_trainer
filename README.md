# Chess Trainer

A training tool, not a grading tool. A position is set with the opponent to
move, Stockfish plays one of its five best moves — a different one each round,
all five before any repeats — and you find the best reply. Nothing on screen
hints at the answer. When you have played, the verdict says what your move
actually was: **Best move**, **Second best**, **Third best**, or, when it costs
enough, **Inaccuracy**, **Mistake** or **Blunder**. Then it shows the engine's
move and explains what yours gave up.

Drag or click to move, as on chess.com. Right-click to draw arrows and circle
squares (Shift, Alt and Shift+Alt for other colours). Positions you get wrong
come back more often.

Local web app, one screen, Firefox on Fedora. No accounts, no telemetry, no
outbound network except an explicitly requested chess.com fetch.

## Getting started

```sh
cp /path/to/stockfish vendor/stockfish   # or: ln -s /usr/games/stockfish vendor/stockfish
chmod +x vendor/stockfish
./run doctor                             # engine, DB, venv, port
./run import --player <your-chess.com-username>   # all your rapid games
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
| `./run import --player <name>` | Import a chess.com player's whole history. `--time-class rapid` by default; also `blitz`, `bullet`, `daily`, a comma-separated list, or `all`. `--since YYYY-MM` skips older months. |
| `./run phases --build` | Rebuild the middlegame and endgame position pools. |
| `./run phases` | List the current pools with their evals. |
| `./run tree` | Print recorded answers: per position, per depth, per opponent move. |
| `./run leaks` | Print the current leak list. |
| `./run warm` | Pre-analyse everything not yet cached. Long-running. |
| `./run doctor` | Check engine binary, DB integrity, venv, port availability. |
| `./run test` | Run the test suite. |
| `./run whoami <name>` | Record which chess.com name is you, and re-tag stored games. |

`--player` walks every monthly archive oldest first and skips games already
stored, so re-running it later picks up only what is new. It sets your username
the first time, so `whoami` is only needed to change it.

`warm` and `phases --build` state what they will do and require a yes; `--yes`
skips the gate for scripted use. Everything read-only takes flags and never
prompts.

## Keys

`Enter` next · `O`/`B` menu · `M` cycle mode · `E` set up a position ·
`S` save · `Backspace` back · `R` reset and replay this position · `?`/`H` help ·
`Esc` close · `←` `→` step a move in a game · `Home`/`End` jump · `1`–`5` pick
the nth opponent option.

## Verdicts

Graded in win probability, converted from the engine's evaluation with the
Lichess logistic so that the same centipawn loss means the same thing on move 2
and on move 40. Your move is looked up in the engine's own ordered list:

| What you played | Verdict |
|---|---|
| The engine's first choice | Best move |
| Its second to fifth choice | Second best … Fifth best |
| Anything else that costs little | Good move |
| 4 or more points of win probability | Inaccuracy |
| 10 or more | Mistake |
| 20 or more, or a mate missed | Blunder |

Damage outranks position in the list: the engine's second choice is still called
a blunder if it throws the game away.

Both your move and the engine's are measured the same way — by the position
each one leads to, searched to the same depth — so playing the engine's move
always scores exactly zero, and a move outside its top five is not judged by a
different yardstick than one inside it.

**Replay** puts the same question back: the same position, the same opponent
move, your answer cleared. It never draws a different move. **Drill from here**
takes the position you just reached and asks the same question one level
deeper, with the opponent to move.

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
- `web/` — plain HTML, CSS and ES modules, served as static files. No build
  step. The front end holds no chess logic: the server says what is on the
  board and which moves are legal.
- `web/vendor/cm-chessboard` — the board itself: rendering, dragging, animated
  moves, markers, arrows, the promotion dialog and the right-click annotator.
  MIT licensed, vendored rather than installed so the app stays a folder of
  static files. Pieces are the Wikimedia standard set (CC BY-SA 3.0). See
  `web/vendor/NOTICE.md`.

Everything lives in `data/trainer.db`.
