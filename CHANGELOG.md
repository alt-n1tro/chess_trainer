# Changelog

Semantic versioning. The number lives here, at the top of this file, and
`server/version.py` reads it; nothing else stores it.

- **MAJOR** — the app works differently than you remember. A schema change
  that rewrites what is stored, a workflow that moved, anything that makes an
  old habit wrong.
- **MINOR** — something new you can use: a screen, a command, a kind of drill,
  a new class of explanation.
- **PATCH** — a fix or a polish. Same app, less wrong.

Every change lands with an entry here and the version bumped to match.

## 1.0.1 — 2026-09-17

### Fixed
- "Replay move" put the whole round back, which in a chain threw away the
  moves you had already found and re-asked the first one. It now puts back
  exactly the question you just answered, keeping the rest of the chain
  played and scored. "Replay drill" is still the whole drill.

## 1.0.0 — 2026-09-17

First numbered build. Everything below was already here; the version counter
is what is new, so the history before this point is summarised rather than
split into releases it never had.

### Drilling
- Positions from your own reviewed games, drawn by theme, colour and phase.
- Chains: find one move, or several in a row.
- Step back and forward through a round with the arrow keys or the buttons,
  replay a single move or the whole drill.
- A lock on one game, so Random draws only from it, with the library
  ("Games played") to search your games by name, Elo range or date.
- Position editor with an eraser and an undo stack.

### Explanations
- Every claim carries the position it was made in plus the facts it rests on,
  and is re-derived before it is shown.
- Claims are confirmed by a search before they are said, so a tactic that does
  not hold is never claimed.
- What the move is for down the line, what your move cost, and why the
  opponent's move was good or bad.
- Verdicts are graded in win probability at depth 20, and the played move's
  square is coloured by the verdict.

### Engine and storage
- Stockfish through two persistent slots, foreground and background.
- Zobrist-keyed analysis cache: a position reached again is not analysed
  again, in drills or in review.
- Depth 20 everywhere — drills, review, grading and judging.
- Explanations computed during review and during drills are stored and reused.
- A power switch in the top bar that stops all background work at once.
