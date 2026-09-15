"""Structural explanation generator.

Everything here is computed by diffing the principal variation after your move
against the principal variation after the best move. No language model, no
network, no invented commentary.

The seam for a richer explainer is the single entry point:

    explain(fen, my_move, best_move, pvs) -> Explanation

A future implementation may call out to a language model with the same
signature. It is not wired up, needs no key, and the app is fully functional
without it.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import chess

PV_PLIES = 8          # how far into each line we look
PREVIEW_MOVES = 4     # first four moves of both PVs, shown clickable
MAX_ITEMS = 3

VALUES = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0,
}
NAMES = {
    chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
    chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king",
}


@dataclass
class Explanation:
    text: str = ""
    items: list = field(default_factory=list)
    my_pv: list = field(default_factory=list)     # [{uci, san}, ...]
    best_pv: list = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


# --- line walking ----------------------------------------------------------

def _walk(fen: str, ucis: list[str], plies: int = PV_PLIES):
    """Play a line out and return (end board, san list, boards per ply)."""
    board = chess.Board(fen)
    sans, boards = [], []
    for uci in ucis[:plies]:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        sans.append(board.san(move))
        board.push(move)
        boards.append(board.copy(stack=False))
    return board, sans, boards


def _material(board: chess.Board, me: bool) -> int:
    total = 0
    for pt, val in VALUES.items():
        total += val * (
            len(board.pieces(pt, me)) - len(board.pieces(pt, not me))
        )
    return total


def _move_number(board: chess.Board, idx: int) -> str:
    """Human move number for ply index `idx` of a line starting at `board`."""
    ply = board.ply() + idx
    return f"{ply // 2 + 1}{'.' if ply % 2 == 0 else '...'}"


def _safe_squares(board: chess.Board, square: int, me: bool) -> int:
    """Destinations for the piece on `square` that the opponent does not simply
    win it on. A practical proxy, not a search."""
    probe = board.copy(stack=False)
    probe.turn = me
    count = 0
    for move in probe.legal_moves:
        if move.from_square != square:
            continue
        after = probe.copy(stack=False)
        after.push(move)
        attackers = after.attackers(not me, move.to_square)
        defenders = after.attackers(me, move.to_square)
        if not attackers or len(defenders) >= len(attackers):
            count += 1
    return count


def _mobility(board: chess.Board, square: int, me: bool) -> int:
    probe = board.copy(stack=False)
    probe.turn = me
    return sum(1 for m in probe.legal_moves if m.from_square == square)


def _is_outpost(board: chess.Board, square: int, colour: bool) -> bool:
    """A square no enemy pawn can ever challenge."""
    file_i, rank_i = chess.square_file(square), chess.square_rank(square)
    for df in (-1, 1):
        f = file_i + df
        if not 0 <= f <= 7:
            continue
        ranks = range(rank_i + 1, 8) if colour == chess.WHITE else range(0, rank_i)
        for r in ranks:
            sq = chess.square(f, r)
            piece = board.piece_at(sq)
            if piece and piece.piece_type == chess.PAWN and piece.color != colour:
                return False
    return True


def _shield(board: chess.Board, colour: bool) -> int:
    king = board.king(colour)
    if king is None:
        return 0
    kf, kr = chess.square_file(king), chess.square_rank(king)
    step = 1 if colour == chess.WHITE else -1
    count = 0
    for df in (-1, 0, 1):
        f = kf + df
        if not 0 <= f <= 7:
            continue
        for dr in (1, 2):
            r = kr + step * dr
            if not 0 <= r <= 7:
                continue
            piece = board.piece_at(chess.square(f, r))
            if piece and piece.piece_type == chess.PAWN and piece.color == colour:
                count += 1
    return count


def _open_files_at_king(board: chess.Board, colour: bool) -> int:
    king = board.king(colour)
    if king is None:
        return 0
    kf = chess.square_file(king)
    n = 0
    for df in (-1, 0, 1):
        f = kf + df
        if not 0 <= f <= 7:
            continue
        if not any(
            board.piece_at(chess.square(f, r)) == chess.Piece(chess.PAWN, colour)
            for r in range(8)
        ):
            n += 1
    return n


# --- the six checks, in priority order -------------------------------------

def _material_swing(root, me, mine_end, best_end, mine_boards, best_boards,
                    mine_sans, best_sans):
    m_mine, m_best = _material(mine_end, me), _material(best_end, me)
    if m_mine >= m_best:
        return None
    # A line can end mid-exchange, one ply before the recapture, which makes
    # an even trade look like a loss. The deficit has to hold a ply earlier
    # too before it is worth saying out loud.
    if len(mine_boards) >= 2 and len(best_boards) >= 2:
        if _material(mine_boards[-2], me) >= _material(best_boards[-2], me):
            return None
    # Find the ply at which your line first falls behind the best line.
    idx = None
    base = _material(root, me)
    for i, b in enumerate(mine_boards):
        if _material(b, me) < base:
            idx = i
            break
    detail = ""
    if idx is not None and idx < len(mine_sans):
        san = mine_sans[idx]
        detail = f" after {_move_number(root, idx)} {san}"
    swing = m_best - m_mine
    piece = ""
    for pt in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN):
        if len(mine_end.pieces(pt, me)) < len(best_end.pieces(pt, me)):
            piece = f" the {NAMES[pt]}"
            break
    return {
        "kind": "material",
        "text": f"It costs{piece or ' material'}{detail}"
                f" — {swing} point{'s' if swing != 1 else ''} down on the best line.",
    }


def _trapped_piece(root, me, mine_end, best_end, *_):
    for pt in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT):
        for sq in mine_end.pieces(pt, me):
            if _safe_squares(mine_end, sq, me) > 0:
                continue
            # A piece that was already boxed in at the root is not news: the
            # line has to be what took its squares away.
            if sq in root.pieces(pt, me) and _safe_squares(root, sq, me) == 0:
                continue
            better = any(
                _safe_squares(best_end, s, me) > 0 for s in best_end.pieces(pt, me)
            )
            if better:
                return {
                    "kind": "trapped",
                    "text": f"Your {NAMES[pt]} on {chess.square_name(sq)} ends up"
                            f" with nowhere safe to go.",
                }
    return None


def _square_control(root, me, mine_end, best_end, *_):
    for sq in mine_end.pieces(chess.KNIGHT, not me):
        if sq in best_end.pieces(chess.KNIGHT, not me):
            continue
        if _is_outpost(mine_end, sq, not me):
            return {
                "kind": "square",
                "text": f"It lets a knight settle on {chess.square_name(sq)},"
                        f" where no pawn can challenge it.",
            }
    for sq in best_end.pieces(chess.KNIGHT, me):
        if sq not in mine_end.pieces(chess.KNIGHT, me) and _is_outpost(best_end, sq, me):
            return {
                "kind": "square",
                "text": f"The best move claims {chess.square_name(sq)} for a knight"
                        f" instead, permanently.",
            }
    return None


def _king_safety(root, me, mine_end, best_end, *_):
    s_mine, s_best = _shield(mine_end, me), _shield(best_end, me)
    if s_mine < s_best:
        return {
            "kind": "king",
            "text": "It strips pawns from in front of your king that the best"
                    " move keeps.",
        }
    o_mine, o_best = _open_files_at_king(mine_end, me), _open_files_at_king(best_end, me)
    if o_mine > o_best:
        return {
            "kind": "king",
            "text": "It opens a file towards your king.",
        }
    if _open_files_at_king(best_end, not me) > _open_files_at_king(mine_end, not me):
        return {
            "kind": "king",
            "text": "The best move opens a file against their king; yours does not.",
        }
    return None


def _activity(root, me, mine_end, best_end, *_):
    """A piece whose legal move count collapses in one line: the practical
    proxy for 'your bishop has no future'."""
    for pt in (chess.BISHOP, chess.KNIGHT, chess.ROOK, chess.QUEEN):
        mine_sqs, best_sqs = mine_end.pieces(pt, me), best_end.pieces(pt, me)
        if not mine_sqs or not best_sqs:
            continue
        m = min(_mobility(mine_end, s, me) for s in mine_sqs)
        b = max(_mobility(best_end, s, me) for s in best_sqs)
        worst = min(mine_sqs, key=lambda s: _mobility(mine_end, s, me))
        root_mob = (
            _mobility(root, worst, me) if worst in root.pieces(pt, me) else None
        )
        if m <= 1 and b >= m + 3 and (root_mob is None or root_mob > m):
            return {
                "kind": "activity",
                "text": f"Your {NAMES[pt]} on {chess.square_name(worst)} runs out of"
                        f" squares; on the best line it stays active.",
            }
    return None


def _tempo(root, me, mine_end, best_end, mine_boards, best_boards, mine_sans,
           best_sans):
    def forcing(sans):
        return sum(1 for i, s in enumerate(sans) if i % 2 == 1 and ("+" in s or "x" in s))

    f_mine, f_best = forcing(mine_sans), forcing(best_sans)
    if f_mine > f_best and f_mine >= 2:
        first = next(
            (s for i, s in enumerate(mine_sans) if i % 2 == 1 and ("+" in s or "x" in s)),
            None,
        )
        if first:
            return {
                "kind": "tempo",
                "text": f"It hands them a forcing sequence starting with {first};"
                        f" the best move takes that away.",
            }
    return None


# --- why the engine's move works -------------------------------------------
#
# Everything below is read off the board, never guessed: a claim is made only
# when the position can be checked for it. Static exchange evaluation stands
# in for a search, so these are the tactics a human sees at a glance, which
# is the point -- they are the ones worth explaining.

def cheapest_capture(board: chess.Board, square: int):
    """The least valuable legal capture of whatever stands on `square`."""
    best = None
    for move in board.legal_moves:
        if move.to_square != square or not board.is_capture(move):
            continue
        piece = board.piece_at(move.from_square)
        if piece is None:
            continue
        value = VALUES[piece.piece_type]
        if best is None or value < best[0]:
            best = (value, move)
    return best[1] if best else None


def _swap(board: chess.Board, square: int) -> float:
    """What the side to move wins by taking on `square`, both sides
    recapturing with their cheapest piece and either free to stop."""
    piece = board.piece_at(square)
    if piece is None:
        return 0.0
    move = cheapest_capture(board, square)
    if move is None:
        return 0.0
    after = board.copy(stack=False)
    after.push(move)
    # Nobody is forced to continue an exchange that loses material.
    return max(0.0, VALUES[piece.piece_type] - _swap(after, square))


def see(board: chess.Board, move: chess.Move) -> float:
    """Static exchange evaluation, in pawns: what this capture is worth once
    both sides have taken on the square as cheaply as they can. It knows
    nothing of in-between moves, so it is a strong hint, not a proof."""
    if not board.is_capture(move):
        return 0.0
    gain = VALUES[chess.PAWN] if board.is_en_passant(move) else \
        VALUES[board.piece_at(move.to_square).piece_type]
    if move.promotion:
        gain += VALUES[move.promotion] - VALUES[chess.PAWN]
    after = board.copy(stack=False)
    after.push(move)
    return gain - _swap(after, move.to_square)


def best_shot(board: chess.Board, colour: bool):
    """What `colour` would play if handed the move: mate in one, else the
    capture that wins most on the exchange. Returns None when there is
    nothing there."""
    probe = board.copy(stack=False)
    if probe.turn != colour:
        if probe.is_check():
            return None              # you cannot hand over the move in check
        probe.push(chess.Move.null())
    best = None
    for move in probe.legal_moves:
        after = probe.copy(stack=False)
        after.push(move)
        if after.is_checkmate():
            return {"san": probe.san(move), "gain": 99.0, "mate": True}
        if not probe.is_capture(move):
            continue
        gain = see(probe, move)
        if gain > 0 and (best is None or gain > best["gain"]):
            best = {"san": probe.san(move), "gain": gain, "mate": False}
    return best


def _worth(points: float) -> str:
    """What a material swing is worth, said the way a player would say it."""
    if points >= 8:
        return "a queen"
    if points >= 5:
        return "a rook"
    if points >= 3:
        return "a piece"
    if points >= 2:
        return f"{points:.0f} pawns"
    return "a pawn"


def _targets(board: chess.Board, square: int, me: bool) -> list[str]:
    """Enemy pieces the man on `square` now attacks and could actually win:
    the king, anything worth more than it, or anything undefended."""
    piece = board.piece_at(square)
    if piece is None:
        return []
    out = []
    for target in board.attacks(square):
        victim = board.piece_at(target)
        if victim is None or victim.color == me:
            continue
        if victim.piece_type == chess.KING:
            out.append("the king")
            continue
        probe = board.copy(stack=False)
        probe.turn = me
        move = chess.Move(square, target)
        if move not in probe.legal_moves:
            continue
        if see(probe, move) > 0:
            out.append(f"the {NAMES[victim.piece_type]} on {chess.square_name(target)}")
    return out


def _line_pin(board: chess.Board, square: int, me: bool):
    """A pin or a skewer from `square`: two enemy pieces on one ray, nothing
    between them, the front one worth less (a pin) or more (a skewer)."""
    piece = board.piece_at(square)
    if piece is None or piece.piece_type not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
        return None
    for target in board.attacks(square):
        front = board.piece_at(target)
        if front is None or front.color == me:
            continue
        ray = chess.ray(square, target)
        if not ray:
            continue
        beyond = [sq for sq in chess.SquareSet(ray)
                  if _further(square, target, sq)]
        beyond.sort(key=lambda sq: chess.square_distance(target, sq))
        for sq in beyond:
            behind = board.piece_at(sq)
            if behind is None:
                continue
            if behind.color == me:
                break
            fv, bv = VALUES[front.piece_type], VALUES[behind.piece_type]
            kind = None
            # A pawn pinned to a queen is not worth a sentence; a pawn pinned
            # to the king is, because it cannot move at all.
            if behind.piece_type == chess.KING:
                kind = "pins"
            elif bv > fv and fv >= 3:
                kind = "pins"
            elif fv > bv and bv >= 3:
                kind = "skewers"
            if kind:
                behind_name = ("the king" if behind.piece_type == chess.KING
                               else f"the {NAMES[behind.piece_type]} on {chess.square_name(sq)}")
                return (f"{kind} the {NAMES[front.piece_type]} on"
                        f" {chess.square_name(target)} to {behind_name}")
            break
    return None


def _further(origin: int, near: int, far: int) -> bool:
    """Is `far` past `near`, seen from `origin`, on their shared line?"""
    if far in (origin, near):
        return False
    return (chess.square_distance(origin, far) > chess.square_distance(origin, near)
            and chess.square_distance(near, far) < chess.square_distance(origin, far))


def _open_file(board: chess.Board, square: int, me: bool) -> bool:
    file_i = chess.square_file(square)
    return not any(
        board.piece_at(chess.square(file_i, r)) == chess.Piece(chess.PAWN, me)
        for r in range(8))


def _en_prise(board: chess.Board, square: int, me: bool) -> bool:
    """Can the enemy simply win the man standing here?"""
    piece = board.piece_at(square)
    if piece is None or piece.color != me:
        return False
    probe = board.copy(stack=False)
    if probe.turn == me:
        if probe.is_check():
            return False
        probe.push(chess.Move.null())
    grab = cheapest_capture(probe, square)
    return grab is not None and see(probe, grab) > 0


def _steps(fen: str, ucis: list[str], plies: int = PV_PLIES):
    """The line as a list of (move, san, before, after) — everything the
    narration needs to say what is actually happening in it."""
    board = chess.Board(fen)
    out = []
    for uci in ucis[:plies]:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        before = board.copy(stack=False)
        san = board.san(move)
        board.push(move)
        out.append((move, san, before, board.copy(stack=False)))
    return out


def _undefended(board: chess.Board, square: int) -> bool:
    """Nothing can take back on this square."""
    return cheapest_capture(board, square) is None


def _why_it_falls(before: chess.Board, move: chess.Move, after: chess.Board,
                  me: bool) -> str:
    """The reason a capture in the line cannot be answered — the part that
    makes a combination a combination rather than a swap."""
    if after.is_check():
        return "and it comes with check, so there is no time to save anything"
    if _undefended(after, move.to_square):
        return "and nothing defends it"
    if see(before, move) > 0:
        return "and taking back costs them more than it gains"
    return ""


def _narrate(root: chess.Board, steps, me: bool) -> list[str]:
    """What happens in the engine's line, in the order it happens, keeping
    only the moves that carry the point."""
    said = []
    base = _material(root, me)
    if len(steps) < 2:
        return said

    # Their reply. Worth a sentence only when they have no real choice: that
    # is what makes the first move work.
    move, san, before, after = steps[1]
    replies = list(before.legal_moves)
    we_took = root.is_capture(steps[0][0])
    same_square = move.to_square == steps[0][0].to_square
    if len(replies) == 1:
        said.append(f"They have one legal answer, {san}, and it does not help.")
    elif before.is_check():
        said.append(f"They have to deal with the check, and {san} is the best"
                    f" of the ways out.")
    elif we_took and same_square and before.is_capture(move):
        lost = _material(before, me) - base
        said.append(f"They have to take back with {san}; leaving the position"
                    f" as it stands is {_worth(abs(lost))} for nothing.")
    elif same_square and before.is_capture(move):
        said.append(f"They take with {san} — that is the move you have to have"
                    f" seen, and the line below is the answer to it.")

    # The move in the line that actually wins the material, and why the
    # material cannot be held.
    for i in range(2, len(steps)):
        move, san, before, after = steps[i]
        if i % 2:
            continue                      # their move: not our point to make
        # Taking a pawn back after giving one up is not "winning" anything.
        # Only a line that ends ahead of where it started is worth narrating.
        if _material(after, me) <= max(base, _material(before, me)):
            continue
        victim = before.piece_at(move.to_square)
        if victim is None:
            continue
        why = _why_it_falls(before, move, after, me)
        where = chess.square_name(move.to_square)
        said.append(f"Then {_move_number(root, i)} {san} takes the"
                    f" {NAMES[victim.piece_type]} on {where}"
                    + (f", {why}." if why else "."))
        break

    end = steps[-1][3]
    swing = _material(end, me) - base
    if swing == 0 and len(said) < 3:
        gained = _structure_gain(root, end, me)
        if gained:
            said.append(gained)
    if swing > 0 and len(said) < 3:
        moves = (len(steps) + 1) // 2
        said.append(f"{moves} moves on, the count is {_worth(swing)} in your"
                    f" favour, and it is not going back.")
    return said


def _structure_gain(root: chess.Board, end: chess.Board, me: bool) -> str:
    """When the line comes out level on material, what did it buy? Only
    things that can be counted on the board are claimed."""
    pair_before = len(root.pieces(chess.BISHOP, me)) - len(root.pieces(chess.BISHOP, not me))
    pair_after = len(end.pieces(chess.BISHOP, me)) - len(end.pieces(chess.BISHOP, not me))
    if pair_after > pair_before and len(end.pieces(chess.BISHOP, me)) == 2:
        return ("The material comes out level, but you keep both bishops and"
                " they do not, and that pair is worth having in an open"
                " position.")
    shield_before = _shield(root, not me)
    shield_after = _shield(end, not me)
    if shield_after < shield_before:
        return (f"Material stays level, but their king finishes with"
                f" {shield_after} pawn{'' if shield_after == 1 else 's'} in"
                f" front of it instead of {shield_before}, and that is what"
                f" the line was really about.")
    return ""


def _plan(root: chess.Board, steps, me: bool) -> str:
    """A line with no tactic in it is still going somewhere. Say where, in
    the engine's own moves, and say what it is worth at the end."""
    mine = [san for i, (_, san, _, _) in enumerate(steps) if i % 2 == 0]
    if len(mine) < 2:
        return ""
    end = steps[-1][3]
    swing = _material(end, me) - _material(root, me)
    if swing > 0:
        close = (f" and by the end of it you have won {_worth(swing)} without"
                 f" anything being forced")
    else:
        opened = _open_file_gain(root, end, me)
        close = (f" and {opened}" if opened else
                 " and the gain is position rather than material: better"
                 " squares, and their pieces with less to do")
    return (f"Nothing here is forced, so this is a plan rather than a"
            f" combination. The engine plays {', then '.join(mine[:3])},"
            f"{close}.")


def _open_file_gain(root: chess.Board, end: chess.Board, me: bool) -> str:
    """A rook that ends the line on a file with no pawns in the way."""
    for sq in end.pieces(chess.ROOK, me):
        f = chess.square_file(sq)
        if any(end.piece_at(chess.square(f, r)) and
               end.piece_at(chess.square(f, r)).piece_type == chess.PAWN
               for r in range(8)):
            continue
        if any(root.piece_at(chess.square(f, r)) and
               root.piece_at(chess.square(f, r)).piece_type == chess.PAWN
               for r in range(8)):
            return (f"the {chess.square_name(sq)[0]}-file opens up for the rook"
                    f" on the way")
    return ""


def _king_exposed(board: chess.Board, colour: bool) -> bool:
    """Is their king on a file with no pawn in front of it?"""
    king = board.king(colour)
    if king is None:
        return False
    f = chess.square_file(king)
    return not any(
        board.piece_at(chess.square(f, r)) == chess.Piece(chess.PAWN, colour)
        for r in range(8))


def _headline(root: chess.Board, move: chess.Move, san: str, after: chess.Board,
              me: bool, outcome: float | None = None) -> str:
    """One full sentence for what the move does and why that works here."""
    piece = root.piece_at(move.from_square)
    name = NAMES[piece.piece_type] if piece else "piece"
    where = chess.square_name(move.to_square)
    gain = see(root, move) if root.is_capture(move) else 0.0
    loose = _en_prise(after, move.to_square, me)
    checking = after.is_check()
    hits = [] if loose else [t for t in _targets(after, move.to_square, me)
                             if t != "the king"]
    pin = None if loose else _line_pin(after, move.to_square, me)

    if root.is_check():
        outs = list(root.legal_moves)
        if len(outs) == 1:
            return (f"You are in check and {san} is the only legal move on the"
                    f" board. There is nothing to choose here — the position"
                    f" chose for you.")
        lead = (f"You are in check. Of the {len(outs)} legal answers, {san} is"
                f" the one the engine wants")
        if root.is_capture(move) and gain >= 0:
            return (lead + f", because it answers the check by taking the piece"
                    f" that gave it, and the exchange on {where} comes out in"
                    f" your favour.")
        if root.is_capture(move):
            return (lead + f", because it takes the checking piece; the"
                    f" material it costs on {where} is the price of ending the"
                    f" check on your own terms.")
        return lead + _check_reason(root, move, me)

    was_hanging = _en_prise(root, move.from_square, me)
    if was_hanging and not _en_prise(after, move.to_square, me) \
            and not root.is_capture(move):
        return (f"The {name} on {chess.square_name(move.from_square)} was"
                f" attacked and could not stay: they were winning it for"
                f" nothing. {san} moves it somewhere they cannot reach it,"
                f" which is the whole of the move.")

    if root.is_capture(move) and gain > 0:
        victim = ("the pawn" if root.is_en_passant(move)
                  else NAMES[root.piece_at(move.to_square).piece_type])
        if _undefended(after, move.to_square):
            return (f"{san} simply wins the {victim}: nothing on the board"
                    f" defends {where}, so there is nothing to take back with.")
        return (f"{san} wins {_worth(gain)} on {where}. Count the exchange"
                f" through to the end and you come out ahead, whichever way"
                f" they recapture.")
    if root.is_capture(move) and gain < 0:
        back = ""
        if outcome is not None:
            if outcome > 0:
                back = (f" Follow the line: it comes back with"
                        f" {_worth(outcome)} more than you started with, which"
                        f" is why the count on {where} is not the point.")
            elif outcome == 0:
                back = (" Follow the line: every pawn of it comes back, and"
                        " what you keep is the better position.")
        taken = NAMES[root.piece_at(move.to_square).piece_type]
        return (f"{san} gives the {name} for the {taken} on {where}, which is"
                f" {-gain:.0f} points down if you stop counting there. Counting"
                f" there is the mistake: the move is played for what happens"
                f" next, not for the exchange on {where}.{back}")
    if root.is_capture(move) and gain == 0:
        taken = NAMES[root.piece_at(move.to_square).piece_type]
        extra = _trade_gain(root, move, after, me)
        return (f"{san} trades the {name} for the {taken} on {where}. The count"
                f" comes out level, so the move is a decision about which"
                f" pieces stay on the board rather than a way of winning"
                f" material{extra}.")
    if move.promotion:
        return (f"{san} promotes. From here the new"
                f" {NAMES[move.promotion]} is the whole game.")
    if len(hits) >= 2:
        return (f"{san} hits {hits[0]} and {hits[1]} at once. They cannot"
                f" answer both, which is why the {name} lands on {where}"
                f" rather than anywhere else.")
    if checking and hits:
        return (f"{san} checks the king and attacks {hits[0]} in the same"
                f" move. The check has to be answered first, and then the"
                f" second target is still hanging there.")
    if pin:
        return (f"{san} {pin}. Until that is broken, the piece in front"
                f" cannot move without losing what is behind it.")
    seventh_rank = 6 if me == chess.WHITE else 1
    on_seventh = (piece and piece.piece_type == chess.ROOK
                  and chess.square_rank(move.to_square) == seventh_rank)
    if on_seventh:
        target = f", where it already hits {hits[0]}" if hits else ""
        return (f"{san} puts the rook on the seventh{target}. Pawns cannot"
                f" turn round to defend each other, and their king is shut on"
                f" the back rank while the rook eats along the rank.")
    if hits:
        return (f"{san} goes after {hits[0]}, and it cannot be held where it"
                f" stands.")
    if loose and not root.is_capture(move):
        return (f"{san} offers the {name} on {where}. The point is not the"
                f" square, it is the line that follows if they take.")
    if checking:
        rights = (root.has_castling_rights(not me)
                  and not after.has_castling_rights(not me))
        forced = len(list(after.legal_moves))
        if rights:
            return (f"{san} comes with check, and answering it costs them the"
                    f" right to castle: the king has to step out itself. After"
                    f" that it stays in the middle for the rest of the game,"
                    f" which is worth more than it looks.")
        return (f"{san} comes with check, so they get no say in what happens"
                f" next — they have {forced} legal answer"
                f"{'' if forced == 1 else 's'} and every one of them is in the"
                f" line below.")

    # Nothing forcing. What is left is position, and position can still be
    # pointed at: a rank, a file, a square no pawn can take back.
    seventh = 6 if me == chess.WHITE else 1
    if piece and piece.piece_type == chess.ROOK \
            and chess.square_rank(move.to_square) == seventh:
        return (f"{san} puts the rook on the seventh. From behind, it hits the"
                f" pawns that cannot turn round to defend themselves, and their"
                f" king stays shut on the back rank.")
    if piece and piece.piece_type == chess.ROOK \
            and _open_file(after, move.to_square, me) \
            and not _open_file(root, move.from_square, me):
        file_name = chess.square_name(move.to_square)[0]
        return (f"{san} takes the open {file_name}-file. A rook on a file with"
                f" no pawn of yours in the way is worth more than one behind"
                f" its own pawns, and there is no way for them to block it"
                f" cheaply.")
    if piece and piece.piece_type in (chess.KNIGHT, chess.BISHOP) \
            and _is_outpost(after, move.to_square, me):
        return (f"{san} plants the {name} on {where}. No pawn of theirs can"
                f" ever come and chase it off that square, so it sits there"
                f" for the rest of the game.")
    if piece and piece.piece_type == chess.PAWN and _passed(after, move.to_square, me):
        return (f"{san} makes a passed pawn. Nothing of theirs on either side"
                f" of it can stop it going, so every trade from here makes it"
                f" stronger.")
    if root.is_castling(move) and _king_exposed(root, me):
        return (f"{san} gets the king off an open file. That is the file their"
                f" rook wants, and once the king has left it there is nothing"
                f" to aim at.")
    if piece and piece.piece_type == chess.KING and _pawn_ending(after) \
            and _opposition(after, me):
        return (f"{san} takes the opposition. With the kings facing each other"
                f" and them to move, they have to step aside and let your king"
                f" in — in a pawn ending that is usually the whole game.")
    stop = _prevented(root, move, after, me)
    if stop:
        what = (stop["pin"].replace("pins", "would have pinned")
                          .replace("skewers", "would have skewered")
                if stop["pin"] else f"would have hit {stop['hits'][0]}")
        price = (f"now it costs them {_worth(stop['cost'])} to go there anyway"
                 if stop["cost"] else "now the square is simply not available")
        return (f"{san} takes a square away before they can use it. Without"
                f" it they had {stop['san']}, and from there the"
                f" {stop['piece']} {what}. That is the move you are stopping,"
                f" and {price}.")

    before_n = _mobility(root, move.from_square, me)
    after_n = _mobility(after, move.to_square, me)
    if after_n >= before_n + 4:
        return (f"{san} is about the {name}'s scope: {before_n} squares where"
                f" it stood, {after_n} from {where}. Nothing forcing, but every"
                f" line after this runs through the better-placed piece.")
    return ""


def _doubled(board: chess.Board, colour: bool, file_i: int) -> int:
    return sum(1 for r in range(8)
               if board.piece_at(chess.square(file_i, r)) ==
               chess.Piece(chess.PAWN, colour))


def _trade_gain(root: chess.Board, move: chess.Move, after: chess.Board,
                me: bool) -> str:
    """What an even trade buys, when the board can show it: a recapture that
    doubles their pawns, or a piece removed from a square they could never
    be chased off."""
    recapture = cheapest_capture(after, move.to_square)
    if recapture is not None:
        taker = after.piece_at(recapture.from_square)
        landed = after.copy(stack=False)
        landed.push(recapture)
        f = chess.square_file(move.to_square)
        if taker and taker.piece_type == chess.PAWN \
                and _doubled(landed, not me, f) > _doubled(after, not me, f):
            return (f", and the only way to take back doubles their pawns on"
                    f" the {chess.square_name(move.to_square)[0]}-file")
    victim = root.piece_at(move.to_square)
    if victim and victim.piece_type in (chess.KNIGHT, chess.BISHOP) \
            and _is_outpost(root, move.to_square, not me):
        return (f", and the piece it removes was sitting on a square no pawn"
                f" of yours could ever chase it off")
    return ""


def _check_reason(root: chess.Board, move: chess.Move, me: bool) -> str:
    """Why this way out of check and not the others: what each of the others
    hands them, counted rather than asserted."""
    def cost(candidate: chess.Move) -> float:
        probe = root.copy(stack=False)
        probe.push(candidate)
        if probe.is_checkmate():
            return 99.0
        shot = best_shot(probe, not me)
        return shot["gain"] if shot else 0.0

    ours = cost(move)
    others = [(m, cost(m)) for m in root.legal_moves if m != move]
    if not others:
        return "."
    losing = [c for _, c in others if c > ours]
    if len(losing) == len(others):
        worst = min(losing)
        if worst >= 99:
            return (". Every other legal answer is mate next move; this one is"
                    " the only square that is not.")
        return (f". Every other legal answer hands them at least"
                f" {_worth(worst)} straight back — this is the only one that"
                f" does not.")
    if losing:
        return (f". {len(losing)} of the other {len(others)} answers lose"
                f" material on the spot; this one keeps everything, and the"
                f" line below is what the engine does with it.")
    return (". The others hold too, so this is about where the king is best"
            " placed once the check is over, not about surviving it.")


def _prevented(root: chess.Board, move: chess.Move, after: chess.Board,
               me: bool):
    """The enemy move this one takes away before it happens.

    A square is worth taking away when their piece could have gone there
    safely, and would have hit something of yours from it. That is what a
    move like h3 is for, and it is invisible unless somebody names it.
    """
    probe = root.copy(stack=False)
    if probe.turn == me:
        if probe.is_check():
            return None
        probe.push(chess.Move.null())
    gained = set(after.attacks(move.to_square)) - set(root.attacks(move.from_square))
    best = None
    for theirs in probe.legal_moves:
        if theirs.to_square not in gained:
            continue
        piece = probe.piece_at(theirs.from_square)
        if piece is None or piece.piece_type in (chess.KING, chess.PAWN):
            continue
        landed = probe.copy(stack=False)
        landed.push(theirs)
        # Would it have been safe there, before this move took the square?
        grab = cheapest_capture(landed, theirs.to_square)
        if grab is not None and see(landed, grab) > 0:
            continue
        pin = _line_pin(landed, theirs.to_square, not me)
        hits = [t for t in _targets(landed, theirs.to_square, not me)
                if t != "the king"]
        if not pin and not hits:
            continue
        # After our move the same square costs them material, or it is gone.
        cost = None
        if theirs in after.legal_moves:
            probe_after = after.copy(stack=False)
            probe_after.push(theirs)
            grab = cheapest_capture(probe_after, theirs.to_square)
            if grab is None or see(probe_after, grab) <= 0:
                continue
            cost = see(probe_after, grab)
        entry = {"san": probe.san(theirs), "pin": pin,
                 "hits": hits, "cost": cost,
                 "piece": NAMES[piece.piece_type]}
        if best is None or (pin and not best["pin"]):
            best = entry
    return best


def _pawn_ending(board: chess.Board) -> bool:
    return not any(board.pieces(pt, colour)
                   for pt in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT)
                   for colour in (chess.WHITE, chess.BLACK))


def _opposition(board: chess.Board, me: bool) -> bool:
    """Kings facing each other with one square between, and the other side
    to move."""
    mine, theirs = board.king(me), board.king(not me)
    if mine is None or theirs is None or board.turn == me:
        return False
    same_file = chess.square_file(mine) == chess.square_file(theirs)
    same_rank = chess.square_rank(mine) == chess.square_rank(theirs)
    return (same_file or same_rank) and chess.square_distance(mine, theirs) == 2


def _passed(board: chess.Board, square: int, me: bool) -> bool:
    """A pawn with no enemy pawn in front of it, on its file or either side."""
    piece = board.piece_at(square)
    if piece is None or piece.piece_type != chess.PAWN:
        return False
    f, r = chess.square_file(square), chess.square_rank(square)
    ranks = range(r + 1, 8) if me == chess.WHITE else range(0, r)
    for df in (-1, 0, 1):
        file_i = f + df
        if not 0 <= file_i <= 7:
            continue
        for rank_i in ranks:
            other = board.piece_at(chess.square(file_i, rank_i))
            if other and other.piece_type == chess.PAWN and other.color != me:
                return False
    return True


def _defensive(root: chess.Board, move: chess.Move, san: str,
               after: chess.Board, me: bool, theirs) -> str:
    """What the move takes away from them, stated as their idea, not yours:
    the threat you did not see is the thing worth learning."""
    if not theirs:
        return ""
    now = best_shot(after, not me)
    if now is not None and now["gain"] >= theirs["gain"]:
        return ""
    if theirs["mate"]:
        return (f"What they were threatening was {theirs['san']} — mate. {san}"
                f" is the move that takes it away.")
    return (f"Their idea was {theirs['san']}, winning {_worth(theirs['gain'])}."
            f" {san} deals with it before anything else.")


def why_best(root: chess.Board, move: chess.Move, sans: list[str],
             boards: list[chess.Board], me: bool, line: list[str] | None = None,
             alts: list[dict] | None = None) -> list[dict]:
    """Why the engine's move is the engine's move, in sentences a player can
    argue with. Every claim is read off the board: the exchanges are counted,
    the forced replies are counted, and nothing is asserted that the position
    does not show."""
    items = []
    after = root.copy(stack=False)
    after.push(move)
    san = sans[0] if sans else root.san(move)
    steps = _steps(root.fen(), line or [], PV_PLIES)

    if after.is_checkmate():
        return [{"kind": "why", "text": f"{san} is mate. Nothing else matters."}]
    if boards and boards[-1].is_checkmate():
        n = (len(boards) + 1) // 2
        items.append({"kind": "why", "text":
                      f"{san} forces mate in {n}. Every answer they have is in"
                      f" the line below, and none of them change the ending."})
        for sentence in _narrate(root, steps, me)[:1]:
            items.append({"kind": "line", "text": sentence})
        return items

    theirs = best_shot(root, not me)
    # Where the engine's own line ends up on material, so a sacrifice can be
    # explained by what it buys rather than by what it costs.
    outcome = None
    if steps:
        outcome = _material(steps[-1][3], me) - _material(root, me)
    head = _headline(root, move, san, after, me, outcome)
    stops = _defensive(root, move, san, after, me, theirs)

    if head:
        items.append({"kind": "why", "text": head})
    elif stops:
        items.append({"kind": "stops", "text": stops})
        stops = ""

    for sentence in _narrate(root, steps, me):
        items.append({"kind": "line", "text": sentence})
        if len(items) >= 3:
            break

    if stops and len(items) < 4:
        items.append({"kind": "stops", "text": stops})

    if not items:
        plan = _plan(root, steps, me)
        if plan:
            items.append({"kind": "line", "text": plan})
    if not items:
        items.append({"kind": "why", "text":
                      f"{san} keeps everything defended and improves the worst"
                      f" piece. There is no tactic to find here."})
    if alts:
        alt = _runner_up(root, alts, me)
        if alt:
            items.append(alt)
    return items


def _runner_up(root: chess.Board, alts: list[dict], me: bool):
    """What separates the move from the one most people would play. The
    engine's own numbers, so the size of the difference is not a guess."""
    for alt in alts[:1]:
        uci = alt.get("move") or alt.get("uci")
        if not uci:
            continue
        try:
            mv = chess.Move.from_uci(uci)
        except ValueError:
            continue
        if mv not in root.legal_moves:
            continue
        gap = alt.get("gap")
        if gap is None:
            continue
        san = root.san(mv)
        if gap < 0.5:
            return {"kind": "alt", "text":
                    f"The engine's other choice, {san}, is worth the same here"
                    f" to within a rounding error, so there was nothing to"
                    f" find: either move keeps everything you had."}
        if gap < 2:
            return {"kind": "alt", "text":
                    f"The engine's second choice, {san}, is only {gap:.1f}"
                    f" points of win probability behind, so this was a choice"
                    f" between two reasonable moves rather than a test you"
                    f" failed."}
        # A stored line's pv already starts with the move itself.
        walk = list(alt.get("pv") or [])
        if not walk or walk[0] != uci:
            walk = [uci] + walk
        steps = _steps(root.fen(), walk, PV_PLIES)
        tail = "."
        if len(steps) >= 2:
            end = steps[-1][3]
            swing = _material(end, me) - _material(root, me)
            reply = steps[1][1]
            if swing < 0:
                tail = (f": they answer {reply} and you end up {_worth(-swing)}"
                        f" down.")
            elif swing == 0:
                tail = (f": they answer {reply}, the material is unchanged, and"
                        f" the position has stopped being about anything.")
            else:
                tail = (f". That move wins material too, so the difference is"
                        f" not the count: after {reply} the engine still likes"
                        f" this one better, which means the position it leaves"
                        f" behind is worth more than the extra material.")
        return {"kind": "alt", "text":
                f"The engine's second choice was {san}, and it puts that"
                f" {gap:.0f} points of win probability behind this one{tail}"}
    return None


CHECKS = (
    _material_swing, _trapped_piece, _square_control,
    _king_safety, _activity, _tempo,
)


def explain(fen: str, my_move: str, best_move: str, pvs: dict) -> Explanation:
    """Why the move is the move.

    `pvs` is {"mine": [uci, ...], "best": [uci, ...]} -- the principal
    variations, each beginning with the move it belongs to.
    """
    root = chess.Board(fen)
    mine_line = list(pvs.get("mine") or ([my_move] if my_move else []))
    best_line = list(pvs.get("best") or ([best_move] if best_move else []))
    me = root.turn

    # Both lines are walked to the same depth, so a difference between the end
    # positions is a difference between the moves and not between PV lengths.
    plies = min(len(mine_line), len(best_line), PV_PLIES)
    plies -= plies % 2          # end with the opponent having replied
    plies = max(plies, 1)
    mine_end, mine_sans, mine_boards = _walk(fen, mine_line, plies)
    best_end, best_sans, best_boards = _walk(fen, best_line, plies)

    out = Explanation(
        my_pv=_preview(fen, mine_line),
        best_pv=_preview(fen, best_line),
    )
    if not best_sans:
        return out

    # Why the engine's move is the engine's move. This is the half you are
    # actually trying to learn, so it is said whether you found it or not.
    best_first = chess.Move.from_uci(best_line[0])
    reasons = why_best(root, best_first, best_sans, best_boards, me,
                       line=best_line, alts=pvs.get("alts"))

    if my_move and best_move and my_move == best_move:
        out.items = [dict(r) for r in reasons]
        out.text = " ".join(r["text"] for r in reasons[:2])
        return out

    if not mine_sans:
        # Shown rather than answered: there is nothing of yours to compare.
        out.items = [dict(r) for r in reasons]
        out.text = " ".join(r["text"] for r in reasons[:2])
        return out

    cost = []
    for check in CHECKS:
        item = check(root, me, mine_end, best_end, mine_boards, best_boards,
                     mine_sans, best_sans)
        if item:
            cost.append(item)
        if len(cost) >= MAX_ITEMS:
            break

    # What yours cost first -- it is the answer to "what did I miss" -- then
    # why theirs works.
    out.items = cost + [dict(r) for r in reasons]
    head = cost[0]["text"] if cost else ""
    out.text = " ".join([t for t in [head, reasons[0]["text"]] if t])
    return out


def _preview(fen: str, ucis: list[str]) -> list[dict]:
    """The first four moves of both PVs, clickable, so you can walk the
    refutation on the board rather than read about it. Each entry carries the
    rendered position, because the client is told the board and never computes
    one."""
    board = chess.Board(fen)
    out = []
    for uci in ucis[: PREVIEW_MOVES * 2]:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        entry = {"uci": uci, "san": board.san(move), "fen_before": board.fen()}
        board.push(move)
        entry["fen_after"] = board.fen()
        entry["board"] = _squares(board)
        out.append(entry)
    return out


def _squares(board: chess.Board) -> list[dict]:
    return [
        {"square": chess.square_name(sq), "piece": board.piece_at(sq).symbol()}
        for sq in chess.SQUARES
        if board.piece_at(sq)
    ]
