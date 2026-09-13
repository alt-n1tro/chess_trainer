# vendor/stockfish

Place a Stockfish 16+ x86-64 build with NNUE here, named exactly `stockfish`,
and make it executable:

    cp /path/to/stockfish vendor/stockfish && chmod +x vendor/stockfish

A symlink works too:

    ln -s /usr/games/stockfish vendor/stockfish

The binary is deliberately not committed. `./run doctor` verifies it answers
`uci`, records its version string, and refuses to run without an NNUE net.
The version string is part of the analysis cache key, so swapping binaries
invalidates the cache cleanly instead of mixing evaluations.

Nothing here is ever installed with sudo.
