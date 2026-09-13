"""What a position is about.

Every theme here is computed from the position, the engine's best move and
its line -- the same information the grader already has -- so a position from
one of your games and a position in the gym are tagged the same way. Nothing
is hand-labelled and nothing is guessed from move names.

The tags are deliberately the ones a coach would use, and the ones the puzzle
sites rate people on: mate, fork, pin, skewer, discovered attack, hanging
piece, sacrifice, defensive move, promotion, king attack, quiet move.
"""
from __future__ import annotations

import chess

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
          chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}
SLIDERS = (chess.BISHOP, chess.ROOK, chess.QUEEN)

# Display order and labels. Phase tags are added by the caller.
THEMES = [
    ("mate", "Checkmate"),
    ("fork", "Fork"),
    ("pin", "Pin"),
    ("skewer", "Skewer"),
    ("discovered_attack", "Discovered attack"),
    ("hanging_piece", "Hanging piece"),
    ("sacrifice", "Sacrifice"),
    ("defensive", "Defensive move"),
    ("promotion", "Promotion"),
    ("kingside_attack", "Kingside attack"),
    ("queenside_attack", "Queenside attack"),
    ("quiet", "Quiet move"),
]
LABELS = dict(THEMES)
LABELS.update({"opening": "Opening", "middlegame": "Middlegame",
               "endgame": "Endgame", "mate_in_1": "Mate in 1",
               "mate_in_2": "Mate in 2", "mate_in_3": "Mate in 3",
               "long_mate": "Long mate", "back_rank_mate": "Back-rank mate"})
TACTICAL = {"fork", "pin", "skewer", "discovered_attack", "hanging_piece",
            "sacrifice", "mate", "promotion"}


def value(piece: chess.Piece | None) -> int:
    return VALUES[piece.piece_type] if piece else 0


def material(board: chess.Board, colour: bool) -> int:
    total = 0
    for pt, val in VALUES.items():
        if pt == chess.KING:
            continue
        total += val * (len(board.pieces(pt, colour)) - len(board.pieces(pt, not colour)))
    return total


def en_prise(board: chess.Board, square: int) -> bool:
    """Is the piece on `square` attacked by something cheaper, or attacked and
    undefended? A practical reading of 'hanging'."""
    piece = board.piece_at(square)
    if piece is None or piece.piece_type == chess.KING:
        return False
    attackers = board.attackers(not piece.color, square)
    if not attackers:
        return False
    defenders = board.attackers(piece.color, square)
    cheapest = min(value(board.piece_at(a)) for a in attackers)
    if cheapest < value(piece):
        return True
    return not defenders


def _ray_between(a: int, b: int) -> list[int]:
    """Squares strictly between two squares on a line, or [] if not aligned."""
    return list(chess.SquareSet(chess.between(a, b)))


def _line_pieces(board: chess.Board, from_sq: int, colour: bool):
    """For a slider on from_sq: for each ray, the first and second pieces."""
    piece = board.piece_at(from_sq)
    if piece is None or piece.piece_type not in SLIDERS:
        return []
    out = []
    directions = []
    if piece.piece_type in (chess.ROOK, chess.QUEEN):
        directions += [(1, 0), (-1, 0), (0, 1), (0, -1)]
    if piece.piece_type in (chess.BISHOP, chess.QUEEN):
        directions += [(1, 1), (1, -1), (-1, 1), (-1, -1)]
    f0, r0 = chess.square_file(from_sq), chess.square_rank(from_sq)
    for df, dr in directions:
        first = second = None
        f, r = f0 + df, r0 + dr
        while 0 <= f <= 7 and 0 <= r <= 7:
            sq = chess.square(f, r)
            p = board.piece_at(sq)
            if p is not None:
                if first is None:
                    first = sq
                else:
                    second = sq
                    break
            f, r = f + df, r + dr
        if first is not None and second is not None:
            out.append((first, second))
    return out


def _new_targets(before: chess.Board, after: chess.Board, colour: bool,
                 exclude: int | None):
    """Enemy pieces newly attacked by our sliders (other than `exclude`)."""
    found = []
    for pt in SLIDERS:
        for sq in after.pieces(pt, colour):
            if sq == exclude:
                continue
            now = after.attacks(sq)
            was = before.attacks(sq) if before.piece_at(sq) == after.piece_at(sq) else chess.SquareSet()
            for t in now:
                p = after.piece_at(t)
                if p and p.color != colour and value(p) >= 3 and t not in was:
                    found.append((sq, t))
    return found


def tag(fen: str, best_uci: str | None, pv: list[str] | None,
        mate_in: int | None = None) -> list[str]:
    """Themes of the best move in this position, for the side to move."""
    board = chess.Board(fen)
    me = board.turn
    themes: set[str] = set()
    if not best_uci:
        return []
    try:
        best = chess.Move.from_uci(best_uci)
    except ValueError:
        return []
    if best not in board.legal_moves:
        return []
    after = board.copy(stack=False)
    after.push(best)
    moved = after.piece_at(best.to_square)
    captured = board.piece_at(best.to_square)
    in_check_before = board.is_check()

    # mate
    if mate_in is not None and mate_in > 0:
        themes.add("mate")
        if mate_in == 1:
            themes.add("mate_in_1")
            if after.is_checkmate() and _back_rank(after, not me):
                themes.add("back_rank_mate")
        elif mate_in == 2:
            themes.add("mate_in_2")
        elif mate_in == 3:
            themes.add("mate_in_3")
        else:
            themes.add("long_mate")

    # hanging piece: taking something that was not adequately defended
    if captured and captured.piece_type != chess.KING:
        defenders = board.attackers(not me, best.to_square)
        if not defenders or value(captured) > value(board.piece_at(best.from_square)):
            themes.add("hanging_piece")

    # fork: the moved piece hits two or more things worth hitting
    if moved:
        targets = 0
        for t in after.attacks(best.to_square):
            p = after.piece_at(t)
            if not p or p.color == me:
                continue
            if p.piece_type == chess.KING:
                targets += 1
            elif value(p) > value(moved) or not after.attackers(not me, t):
                if p.piece_type != chess.PAWN:
                    targets += 1
        if targets >= 2:
            themes.add("fork")

    # pin and skewer along the moved piece's rays
    if moved and moved.piece_type in SLIDERS:
        for first, second in _line_pieces(after, best.to_square, me):
            p1, p2 = after.piece_at(first), after.piece_at(second)
            if p1.color == me or p2.color == me:
                continue
            if value(p2) > value(p1) >= 3:
                themes.add("pin")           # a pinned pawn is not a theme
            elif value(p1) > value(p2) and value(p2) >= 3:
                themes.add("skewer")

    # discovered attack: moving away unmasks a slider
    for _src, target in _new_targets(board, after, me, best.to_square):
        if best.from_square in _ray_between(_src, target) or after.is_check():
            themes.add("discovered_attack")
            break

    # sacrifice: giving material on purpose
    if pv and len(pv) >= 2:
        probe = board.copy(stack=False)
        ok = True
        for uci in pv[:2]:
            try:
                mv = chess.Move.from_uci(uci)
            except ValueError:
                ok = False
                break
            if mv not in probe.legal_moves:
                ok = False
                break
            probe.push(mv)
        if ok and material(board, me) - material(probe, me) >= 2:
            themes.add("sacrifice")

    # defensive move: something of ours was hanging and now is not
    if not in_check_before:
        threatened = [sq for sq in chess.SQUARES
                      if (p := board.piece_at(sq)) and p.color == me
                      and p.piece_type != chess.PAWN and en_prise(board, sq)]
        if threatened:
            still = [sq for sq in threatened
                     if sq != best.from_square and en_prise(after, sq)]
            moved_away = best.from_square in threatened and not en_prise(after, best.to_square)
            if (not still) and (moved_away or best.from_square not in threatened):
                themes.add("defensive")

    # promotion, now or in the line
    if best.promotion:
        themes.add("promotion")
    elif pv:
        probe = board.copy(stack=False)
        for i, uci in enumerate(pv[:6]):
            try:
                mv = chess.Move.from_uci(uci)
            except ValueError:
                break
            if mv not in probe.legal_moves:
                break
            if i % 2 == 0 and mv.promotion:
                themes.add("promotion")
                break
            probe.push(mv)

    # attacking the king on its wing
    enemy_king = after.king(not me)
    if enemy_king is not None and moved and moved.piece_type != chess.KING:
        near = chess.square_distance(best.to_square, enemy_king) <= 2
        if near or after.is_check():
            if chess.square_file(enemy_king) >= 4:
                themes.add("kingside_attack")
            else:
                themes.add("queenside_attack")

    # a quiet move is one none of the above describes
    if not (themes & TACTICAL) and not captured and not after.is_check() \
            and "defensive" not in themes:
        themes.add("quiet")

    return sorted(themes)


def _back_rank(board: chess.Board, colour: bool) -> bool:
    """A mated king on its own back rank, hemmed in by its own pawns."""
    king = board.king(colour)
    if king is None:
        return False
    home = 0 if colour == chess.WHITE else 7
    if chess.square_rank(king) != home:
        return False
    step = 1 if colour == chess.WHITE else -1
    for df in (-1, 0, 1):
        f = chess.square_file(king) + df
        if 0 <= f <= 7:
            p = board.piece_at(chess.square(f, home + step))
            if p is None or p.color != colour:
                return False
    return True
