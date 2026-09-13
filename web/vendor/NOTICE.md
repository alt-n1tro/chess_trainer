# Third-party code in web/vendor

## cm-chessboard 8.14.0

Board rendering, drag-and-drop and click move input, animated moves, markers,
arrows, the promotion dialog, and the right-click annotator.

- Source: https://github.com/shaack/cm-chessboard
- Copyright (c) 2017 Stefan Haack
- Licence: MIT — see `cm-chessboard/LICENSE`

Vendored rather than installed at runtime so the app stays a folder of static
files with no build step and no network access. Only the files actually used
are kept: `src/`, `assets/chessboard.css`, `assets/extensions/`, and
`assets/pieces/standard.svg`.

### Pieces

`assets/pieces/standard.svg` is a sprite of the Wikimedia standard chess
pieces, licensed **CC BY-SA 3.0**
(https://creativecommons.org/licenses/by-sa/3.0/).

The Staunty set that also ships with cm-chessboard is deliberately **not**
included: it is CC BY-NC-SA 4.0, and the non-commercial clause would restrict
what this project can be used for.
