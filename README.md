# Chess Trainer

A training tool, not a grading tool. A position is set with the opponent to
move, Stockfish plays one of its best moves — a different one each round, all
of them before any repeats — and you find the best reply. Nothing on screen
hints at the answer. When you have played, the verdict says what your move
actually was: **Best move**, **Second best**, **Third best**, or, when it costs
enough, **Inaccuracy**, **Mistake** or **Blunder**. Then it shows the engine's
move and explains what yours gave up.

Drag or click to move, as on chess.com. Clicking a piece you already picked up
puts it down again. Right-click to draw arrows and circle squares (Shift, Alt
and Shift+Alt for other colours); any left click on the board clears them.
Positions you get wrong come back more often.

Local web app, one screen, Firefox on Fedora. No accounts, no telemetry, no
outbound network except an explicitly requested chess.com fetch.

## Getting started

```sh
cp /path/to/stockfish vendor/stockfish   # or: ln -s /usr/games/stockfish vendor/stockfish
chmod +x vendor/stockfish
./run doctor                             # engine, DB, venv, port
./run import --player <your-chess.com-username>   # all your rapid games
./run review                             # grade every move; fills the gym and the statistics
./run web                                # then open http://127.0.0.1:8770/
```

The first run creates `.venv` and installs `python-chess` into it. Nothing is
ever installed with sudo; if a step would need root, the app stops and says so.

## Commands

| Command | Effect |
|---|---|
| `./run web` | Start the app on 127.0.0.1:8770. Open Firefox yourself. |
| `./run import --player <name>` | Import a chess.com player's whole history. `--time-class rapid` by default; also `blitz`, `bullet`, `daily`, a comma-separated list, or `all`. `--since YYYY-MM` skips older months. Re-running picks up only what is new. |
| `./run import <pgn-or-url>` | Import a PGN file, a chess.com game link, or an archive URL. |
| `./run review` | Engine-review your games: every move graded and tagged for the statistics, and each reviewed game's positions added to the drill pools. `--depth` is a floor (20, the same depth your drills are graded at); simple positions go much deeper within `--budget` seconds. Resumable; run it again after each import. `--redo` re-grades every game from the cached analysis, for after a grading change. |
| `./run warm` | Pre-analyse pooled positions so drills never wait. |
| `./run verify [--positions N]` | Check the app's moves against a second, deeper Stockfish process with no cache: the opponent's options, the reply it calls best, a graded reply, a chain step, and a reviewed move of yours. Prints every disagreement and how big it is. About a minute per position. |
| `./run whoami [name]` | Show or set which chess.com name is you. `import --player` sets it the first time. |
| `./run doctor` | Check the engine binary, database, venv and port. |
| `./run test` | Run the test suite. |

`./run help` prints the same from the program itself, and every command
takes `--help`. Commands that take time say what they will do and wait for a
yes; `--yes` skips the question. Everything that only reads never prompts.

The usual order is import, review, web. A game joins the drill pools when it
is reviewed.

## Keys

`Enter` next · `1`–`5` pick the opponent's nth option · `Shift+1`–`5` moves per
position · `T` statistics · `G` games played · `W` engines warming on/off ·
`O`/`B` menu · `M` cycle mode · `E` set up a position · `S` save ·
`Backspace` back · `R` reset and replay this position · `?`/`H` help ·
`Esc` close or leave set-up · `Ctrl+Z` undo while setting up ·
`←` `→` step a move in a game · `Home`/`End` jump.

## Nothing is given away

When you have answered, the panel says what your move was worth and nothing
else: no best move named, no arrow on the board, no lines to read. One button
reveals why your move was what it was, and only then does a second offer to
explain the engine's move. Pressing `X` walks the same two steps. Asking to be
shown the move, or finding it yourself, opens both at once — there is nothing
left to spoil.

The two explanations are kept apart because they answer different questions:
what your move gave up, and why the engine's move works.

Revealing the engine's move puts the board back to the position you were
asked about and draws that one move on it. Your own move is not drawn there:
an arrow starting from a square your piece has already left is worse than no
arrow at all.

Both halves follow the same rule about moves. A move number is only ever
written attached to its move — "they answer 13...Qxd5" — and a loss that
happens deeper in the line says so rather than describing a piece as hanging
on a board where it is not. "It costs the knight after 11" is exactly the
sentence this rule exists to prevent.

## Why the move is the move

Every verdict comes with an explanation of the engine's move, whether you
found it or not, and all of it is read off the board rather than asserted.
Exchanges are counted through to the end with a static exchange evaluation,
forced replies are counted by listing the legal ones, and a claim is only
made when the position can be checked for it.

Every claim is checked twice. The static tests propose it, and a search has
to agree before it is printed: over 120 random positions the engine rejected
or softened a claim in 21 of them, mostly moves whose "point" evaporates once
the opponent gets a real answer. A claim the engine contradicts is either
dropped or stated as what it is ("the exchange count says you come out a
piece up; the engine has an answer that gets it back"). Material in an ending
it cannot win with is called that, not called winning.

The reasoning for your own mistakes is worked out during `./run review`,
where there is time to search, and stored. The panel reads it back instead of
recomputing in the half second it has, and the game view shows it under any
move of yours the engine disagreed with.

What it will tell you, when it is true:

- **The tactic**, named with its pieces: what forks what, what is pinned to
  what, what is simply undefended, and what cannot be taken back.
- **Sacrifices**, as a decision rather than an accident: what the exchange
  costs on that square, and what the line gets back for it.
- **Checks**, by what they take away: the right to castle, the time to
  defend, or the number of legal answers left.
- **Prophylaxis** — the move you are stopping before it happens. "h3 takes a
  square away: without it they had Bg4, and from there the bishop would have
  pinned the knight on f3 to the queen."
- **The forced part of the line**: which replies are the only legal ones,
  which recaptures cannot be declined, and which move in the line actually
  wins the material, with the reason it cannot be held.
- **Quiet moves**, by what they buy: the seventh rank, an open file, a square
  no pawn can chase a knight off, the opposition in a pawn ending, or a piece
  that goes from three squares to eight.
- **The runner-up**, quantified: the engine's second choice, how far behind it
  is in win probability, and what happens in its line.

Accuracy is measured rather than asserted. `verify_claim()` re-derives every
claim from the position it names — pieces on those squares, on one ray, in
that order, nothing in between, the exchange worth the number quoted — and
the sweep runs clean over hundreds of engine positions. The test suite mirrors
each position, colours and ranks flipped, and requires the explanation to be
the mirror image of itself, which is what catches a perspective bug before you
see it.

## The panel

The right-hand column always reads the same way, top to bottom: which game
and position you are looking at, how far through it you are, the verdict on
your last move, then what you can do. The actions come in labelled groups —
**This position** (one primary button: find the move, or move on), **Another
position**, and a folded **Tools** for saving and hand-setting. A button that
cannot do anything here is not drawn: no disabled row to puzzle over.

## Setting up a position

`E`, or *Tools → Set up a position*. You start with nothing in hand, so a
click never drops a piece you did not ask for. Take a piece from the palette
and click squares to place it; click that piece again, or the `✕` tile, to put
it down. Clicking an occupied square clears it. **Undo** (or `Ctrl+Z`) walks
back every change, including *Clear the board*. While you are setting up, the
drill's own buttons step aside so nothing is ambiguous; *Leave set-up* or
`Esc` brings them back.

## One game at a time

**Games played** lists every game in the database: search by name, opening or
result, narrow by Elo range (`1500-1700`, or `1600` for a floor) and by date.
Each row drills that game, opens it move by move, or analyses it if it has
never been through the engine.

*Analyse* takes a chess.com link or a pasted PGN, imports it, reviews every
move and extracts the drill positions, with a progress card that counts the
moves searched and the positions found as it goes. It all lands in the
database, so it is there next time.

A finished analysis **locks** drilling to that game: a card at the top of the
panel says which game, and how many positions it holds per phase. While it is
up, *Random position* draws from that game and nothing else, and the mode
counts show only what that game has. The `✕` on the card lifts the lock and
gives you every reviewed game again. The lock survives a restart.

## The idle switch

Two Stockfish processes run: one answers you, one works ahead so the next
position is instant. The second is what keeps a core busy. **Engines warming**
in the top left turns it off — the queue is dropped and the search already
running is stopped, so the processor goes quiet at once. Your own moves are
still graded; only the guessing-ahead stops. The setting sticks.

## Statistics you can act on

`Stats` in the top bar (or `T`) opens two views.

**Your games** reads an engine review of the games you actually played
(`./run review`; about a minute a game, resumable, and the analysis it does
feeds the drill cache too). Every move you made is graded the way the gym
grades you, and the engine's best move at every turn is tagged with what it
was about: fork, pin, skewer, discovered attack, hanging piece, sacrifice,
defensive move, promotion, king attack, quiet move. From that:

- accuracy and blunder rate by phase, and by colour, with sample sizes
- a radar of **what you find and what you miss**: when the best move was a
  fork, how often you played it — the same idea as a puzzle-site radar, but
  measured on your own games rather than on puzzles
- **what your mistakes allowed**: the tactics your errors handed the opponent
- **mates you had**: every forced mate within eleven moves that you were given,
  found or missed, with *Play it out* — a drill where you must find every move
  of the mate
- your worst moments, each with a *Drill* button that puts you back in that
  position with the opponent to move
- openings by family and colour: score, accuracy, and your winning chances at
  move 12, which is where an opening leaves you
- accuracy over your recent games

Your reviewed games are in the menu (`O`) under *Your games*, with their
accuracy. Opening one shows the move list with a coloured verdict on every
move; click a move to see it, and *Drill this moment* puts you back into any
mistake of yours with the opponent to move.

**Gym** reads your answers in the trainer: best-move rate by phase, by move of
a chain (does it hold up once you have to follow a plan?), by drill-from-here
level, by theme, and by opening, plus your last fifty answers.

Accuracy is computed lichess's way: each move gets an accuracy from the win
probability it cost, and a game's accuracy is the average of a
volatility-weighted mean and the harmonic mean of those. The harmonic mean is
the part that matters — it makes two blunders show through forty good moves,
where a plain mean would hide them. Chess.com's formula is unpublished and reads
a few points lower again for the same player; compare yourself with your own
trend, not with a number from another site.

Every number shows how many moves it rests on. Radar labels turn thin under
five samples: a 40% hit rate on three forks is a hint, not a fact.

You should not have to take the engine's word for it. `./run verify` asks a
separate Stockfish — its own process, deeper search, nothing cached — the same
questions the app answered on a handful of positions, and prints where they
disagree. A few centipawns between two searches is normal; a "best" reply that
loses a piece would be printed in capitals.

There is no comparison against other players: no public dataset gives
accuracy or blunder rates by rating for the engine and depth used here, and
invented benchmarks would be worse than none.

## One move, or a plan

The chip in the top bar sets how many moves in a row a position asks for, from
one to five. Keys `1`–`5` do the same.

At one, a position is a puzzle: find the move. Above one, the opponent answers
back and you must find the next move too, and the one after that — which is a
different skill, because a move that looks strong and leads nowhere stops
scoring well once you have to follow it up.

Between your moves the opponent plays the engine's own continuation, so you are
answering best defence rather than a convenient reply. Each move is graded on
its own, the panel shows a bar per move as you go, and the position's mark is
its worst move: three good moves and a blunder is a blunder. Playing a bad move
does not end the chain — you carry on from the position you made, which is the
point.

## What the opponent plays

Anything short of a blunder. From the engine's top eight the opponent gets up
to five moves, in a random order that uses them all before repeating: the best
move, the near-equal alternatives, and the weird, inaccurate and outright
mistaken ones too, because a real opponent plays those and learning to punish
them is the point.

When the position comes from one of your games, the move your opponent really
played is one of the options too, whatever the engine thinks of it — it
happened, and the panel says so when it comes up.

What is withheld is a move that throws the game away — more than 20 points of
win probability against the opponent's own best, or a whole piece, or walking
into mate. Where one move dominates, a position asks fewer questions; the
panel says how many, as *Position 2 of 5* or *Position 1 of 1*. The thresholds
are constants at the top of `server/drills.py`.

Each reviewed game gives the pools up to three middlegame and three endgame
positions, spread across each phase, so long games still contribute their
endings.

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

The **FEN** link next to the position name copies it, so you can check any
recommendation on chess.com or lichess yourself.

**Replay** puts the same question back: the same position, the same opponent
move, your answer cleared. It never draws a different move.

**Drill from here** drills whatever is on the board. After answering, that is
the position your move reached, and the new drill hangs off the same tree one
level down. If you have clicked into one of the lines under the verdict, it is
wherever you walked to: the board stays exactly where it is and the opponent is
asked to move there. When it is your move in the position you walked to, the
drill starts one move earlier and replays that move, so you are asked the
question the line was about.

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
