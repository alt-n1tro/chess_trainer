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

import json
import time
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


JUDGE_DEPTH = 20          # what a claim is checked at: the same depth as
                          # everything else, so the search that confirms a
                          # claim is the search that graded the move
JUDGE_PLIES = 10          # how far into its answer we follow the material
DRAWISH_CP = 60           # inside this, the engine is calling it level


class Judge:
    """An engine opinion, used to check what the static tests believe.

    Static exchange evaluation cannot see an in-between check, an overloaded
    defender, or a piece that is pinned two moves from now. It is a good
    guess, and a good guess stated as a fact is exactly what makes an
    explanation untrustworthy. So every material claim is put to a search
    before it is printed, and dropped or softened when the search disagrees.

    `ask` is any callable taking (fen, depth) and returning a line dict with
    cp/mate/pv, or None. The app passes one backed by its engine pool, which
    is cached, so most of these cost nothing.
    """

    def __init__(self, ask=None, depth: int = JUDGE_DEPTH):
        self.ask = ask
        self.depth = depth
        self.calls = 0

    def line(self, board: chess.Board):
        if self.ask is None:
            return None
        self.calls += 1
        try:
            return self.ask(board.fen(), self.depth)
        except Exception:
            return None

    def holds(self, root: chess.Board, after: chess.Board, me: bool,
              gain: float):
        """Does `gain` survive real play? Returns (verdict, evaluation), where
        verdict is True when the engine's own line keeps the material, False
        when it does not, and None when there is no engine to ask."""
        line = self.line(after)
        if not line:
            return None, None
        base = _material(root, me)
        board = after.copy(stack=False)
        seen = [_material(board, me)]
        for uci in (line.get("pv") or [])[:JUDGE_PLIES]:
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                break
            if move not in board.legal_moves:
                break
            board.push(move)
            seen.append(_material(board, me))
        # A line cut mid-exchange flatters whoever captured last, so take the
        # lower of the last two counts.
        settled = min(seen[-2:]) if len(seen) > 1 else seen[-1]
        return settled >= base + gain, line

    def drawish(self, line) -> bool:
        """The engine calling a position level, whatever the piece count says.
        This is what catches a rook pawn with the wrong bishop, a fortress, or
        a knight that cannot mate.

        The line always comes from the position after your move, so it is
        scored for them; your side is the other one.
        """
        if not line or line.get("mate") is not None:
            return False
        cp = line.get("cp")
        if cp is None:
            return False
        return abs(-cp) < DRAWISH_CP


@dataclass
class Explanation:
    """Two explanations, kept apart.

    `cost` is about the move you played: what it gave up. `why` is about the
    engine's move: why that one works. They answer different questions and
    the panel reveals them one at a time, so nothing gives the answer away
    before you ask for it.
    """
    text: str = ""
    items: list = field(default_factory=list)     # both, in order
    cost: list = field(default_factory=list)      # about your move
    why: list = field(default_factory=list)       # about the engine's move
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


def _cannot_mate(board: chess.Board, me: bool) -> bool:
    """Material that cannot force mate however much of it you are up: bare
    king, king and a lone minor. Saying 'a piece up' there teaches the wrong
    lesson."""
    if board.is_insufficient_material():
        return True
    pawns = board.pieces(chess.PAWN, me)
    heavy = (board.pieces(chess.QUEEN, me) or board.pieces(chess.ROOK, me))
    minors = len(board.pieces(chess.KNIGHT, me)) + len(board.pieces(chess.BISHOP, me))
    if pawns or heavy:
        return False
    if minors <= 1:
        return True
    # Two knights cannot force it either.
    return (len(board.pieces(chess.KNIGHT, me)) == 2
            and not board.pieces(chess.BISHOP, me))


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
        # Handing yourself the move must not hand you a phantom en passant
        # capture that belongs to the other side's position.
        probe.ep_square = None
        move = chess.Move(square, target)
        if move not in probe.legal_moves:
            continue
        if see(probe, move) > 0:
            out.append(f"the {NAMES[victim.piece_type]} on {chess.square_name(target)}")
    return out


def _walk_ray(board: chess.Board, square: int, df: int, dr: int):
    """Squares outward from `square` in one direction, in order. Walking the
    board explicitly is the only way to be sure which piece is in front and
    which is behind: filtering a ray by distance gets that wrong on some
    lines, and a pin named against the wrong piece is worse than no pin."""
    f, r = chess.square_file(square), chess.square_rank(square)
    while True:
        f, r = f + df, r + dr
        if not (0 <= f <= 7 and 0 <= r <= 7):
            return
        yield chess.square(f, r)


ROOK_DIRS = ((0, 1), (0, -1), (1, 0), (-1, 0))
BISHOP_DIRS = ((1, 1), (1, -1), (-1, 1), (-1, -1))


def _dirs_for(piece_type: int):
    if piece_type == chess.ROOK:
        return ROOK_DIRS
    if piece_type == chess.BISHOP:
        return BISHOP_DIRS
    if piece_type == chess.QUEEN:
        return ROOK_DIRS + BISHOP_DIRS
    return ()


def _line_pin(board: chess.Board, square: int, me: bool):
    """A pin or a skewer from `square`, as facts rather than a phrase.

    Two enemy pieces on one line out of `square`, nothing between them, and
    nothing of ours in the way. The first piece met is the front one; the
    next piece on the same line is what it is pinned or skewered to.
    """
    piece = board.piece_at(square)
    if piece is None:
        return None
    best = None
    for df, dr in _dirs_for(piece.piece_type):
        front_sq = front = None
        for sq in _walk_ray(board, square, df, dr):
            found = board.piece_at(sq)
            if found is None:
                continue
            if front is None:
                if found.color == me:
                    break                 # our own man blocks the line
                front_sq, front = sq, found
                continue
            if found.color == me:
                break                     # the thing behind is ours: no pin
            fv, bv = VALUES[front.piece_type], VALUES[found.piece_type]
            kind = None
            if found.piece_type == chess.KING:
                kind = "pins"             # even a pawn cannot move off this line
            elif bv > fv and fv >= 3:
                kind = "pins"
            elif fv > bv and bv >= 3:
                kind = "skewers"
            if kind:
                entry = {
                    # Not "kind": that is the claim's own field, and a fact
                    # named the same shadows it.
                    "pin_kind": kind,
                    "attacker": chess.square_name(square),
                    "attacker_piece": NAMES[piece.piece_type],
                    "front": NAMES[front.piece_type],
                    "front_square": chess.square_name(front_sq),
                    "behind": NAMES[found.piece_type],
                    "behind_square": chess.square_name(sq),
                }
                if best is None or entry["behind"] == "king":
                    best = entry
            break
    return best


def _pin_phrase(pin: dict) -> str:
    behind = ("the king on " + pin["behind_square"] if pin["behind"] == "king"
              else f"the {pin['behind']} on {pin['behind_square']}")
    return (f"{pin['pin_kind']} the {pin['front']} on {pin['front_square']} against"
            f" {behind}")


# --- claims ----------------------------------------------------------------
#
# Every sentence the explainer produces is built from a claim: a kind, the
# position it is about, and the facts it rests on. The prose is generated
# from the facts, never the other way round, and verify_claim() re-derives
# each one straight from the FEN. A sentence that cannot be checked against
# the board is not written.


def _claim(kind: str, text: str, board: chess.Board, **facts) -> dict:
    return {"kind": kind, "text": text, "fen": board.fen(), "facts": facts}


def _piece_word(board: chess.Board, square: int) -> str:
    piece = board.piece_at(square)
    return NAMES[piece.piece_type] if piece else "piece"


def _at(board: chess.Board, square: int) -> str:
    """'the knight on c6' -- the phrase that has to match the board."""
    return f"the {_piece_word(board, square)} on {chess.square_name(square)}"


def _after_move(san: str) -> str:
    """Claims about the position the engine's move makes must say so, or they
    read as claims about the board in front of you, which they are not."""
    return f"After {san}"


def _targets_of(board: chess.Board, square: int, me: bool) -> list[dict]:
    """Enemy men the piece on `square` attacks and could actually take: the
    king, or anything the exchange on its square comes out ahead on."""
    piece = board.piece_at(square)
    if piece is None:
        return []
    out = []
    for target in board.attacks(square):
        victim = board.piece_at(target)
        if victim is None or victim.color == me:
            continue
        if victim.piece_type == chess.KING:
            out.append({"piece": "king", "square": chess.square_name(target),
                        "gain": None})
            continue
        probe = board.copy(stack=False)
        probe.turn = me
        move = chess.Move(square, target)
        if piece.piece_type == chess.PAWN and chess.square_rank(target) in (0, 7):
            move = chess.Move(square, target, promotion=chess.QUEEN)
        if move not in probe.legal_moves:
            continue
        gain = see(probe, move)
        if gain > 0:
            out.append({"piece": NAMES[victim.piece_type],
                        "square": chess.square_name(target), "gain": gain})
    return out


def _still_winnable(board: chess.Board, square_name: str, me: bool) -> bool:
    """With `me` to move on this board, is the man on that square still there
    and still winnable by force of exchange?"""
    square = chess.parse_square(square_name)
    victim = board.piece_at(square)
    if victim is None or victim.color == me:
        return False
    if victim.piece_type == chess.KING:
        return board.is_check()
    probe = board.copy(stack=False)
    probe.turn = me
    probe.ep_square = None
    grab = cheapest_capture(probe, square)
    return grab is not None and see(probe, grab) > 0


def _they_can_save(after: chess.Board, squares: list[str], me: bool) -> bool:
    """Is there one reply of theirs that makes every one of these safe? If
    there is, the fork is not a fork and the attack is not an attack -- and
    saying otherwise is the kind of confident nonsense that makes the whole
    explanation worthless."""
    for reply in after.legal_moves:
        probe = after.copy(stack=False)
        probe.push(reply)
        if not any(_still_winnable(probe, sq, me) for sq in squares):
            return True
    return False


def _open_file(board: chess.Board, square: int, me: bool) -> bool:
    file_i = chess.square_file(square)
    return not any(
        board.piece_at(chess.square(file_i, r)) == chess.Piece(chess.PAWN, me)
        for r in range(8))


def _en_prise(board: chess.Board, square: int, me: bool) -> bool:
    """Can they simply win the man standing here, if it were their move?"""
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
    """The line as (move, san, before, after) -- everything the narration
    needs to say what happens in it, and to be checked afterwards."""
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
    return cheapest_capture(board, square) is None


def _pawn_ending(board: chess.Board) -> bool:
    return not any(board.pieces(pt, colour)
                   for pt in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT)
                   for colour in (chess.WHITE, chess.BLACK))


def _opposition(board: chess.Board, me: bool) -> bool:
    mine, theirs = board.king(me), board.king(not me)
    if mine is None or theirs is None or board.turn == me:
        return False
    same_file = chess.square_file(mine) == chess.square_file(theirs)
    same_rank = chess.square_rank(mine) == chess.square_rank(theirs)
    return (same_file or same_rank) and chess.square_distance(mine, theirs) == 2


def _passed(board: chess.Board, square: int, me: bool) -> bool:
    piece = board.piece_at(square)
    if piece is None or piece.piece_type != chess.PAWN or piece.color != me:
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


def _doubled(board: chess.Board, colour: bool, file_i: int) -> int:
    return sum(1 for r in range(8)
               if board.piece_at(chess.square(file_i, r)) ==
               chess.Piece(chess.PAWN, colour))


# --- the claims themselves -------------------------------------------------

def _claim_check(root, move, san, after, me, gain, where):
    if not root.is_check():
        return None
    outs = list(root.legal_moves)
    if len(outs) == 1:
        return _claim("check", f"You are in check, and {san} is the only legal"
                      f" move on the board. There is nothing to choose here.",
                      root, legal_answers=1, chosen=san)
    lead = (f"You are in check. Of the {len(outs)} legal answers, {san} is the"
            f" one the engine wants")
    if root.is_capture(move) and gain >= 0:
        text = (lead + f", because it answers the check by taking the piece that"
                f" gave it, and the exchange on {where} comes out in your"
                f" favour.")
    elif root.is_capture(move):
        text = (lead + f", because it takes the checking piece; the material it"
                f" costs on {where} is the price of ending the check on your"
                f" own terms.")
    else:
        text = lead + _check_reason(root, move, me)
    return _claim("check", text, root, legal_answers=len(outs), chosen=san)


def _check_reason(root: chess.Board, move: chess.Move, me: bool) -> str:
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
            return (". Every other legal answer is mate next move, and this one"
                    " is not.")
        return (f". Every other legal answer hands them at least"
                f" {_worth(worst)} straight back, and this one does not.")
    if losing:
        return (f". {len(losing)} of the other {len(others)} answers lose"
                f" material on the spot; this one keeps everything.")
    return (". The others hold too, so this is about where the king stands"
            " once the check is over, not about surviving it.")


def _claim_rescue(root, move, san, after, me, name):
    if root.is_capture(move) or root.is_check():
        return None
    if not _en_prise(root, move.from_square, me):
        return None
    if _en_prise(after, move.to_square, me):
        return None
    from_sq = chess.square_name(move.from_square)
    return _claim(
        "rescue",
        f"The {name} on {from_sq} was attacked and could not stay: they were"
        f" winning it for nothing. {san} puts it on"
        f" {chess.square_name(move.to_square)}, where they cannot reach it.",
        root, piece=name, square=from_sq,
        to=chess.square_name(move.to_square))


def _claim_capture(root, move, san, after, me, gain, where, name, outcome,
                   judge=None):
    if not root.is_capture(move):
        return None
    victim = ("pawn" if root.is_en_passant(move)
              else NAMES[root.piece_at(move.to_square).piece_type])
    free = _undefended(after, move.to_square)
    if gain > 0:
        judge = judge or Judge()
        held, line = judge.holds(root, after, me, gain)
        if held is False:
            # The exchange count says this wins material and the engine says
            # it does not: something static evaluation cannot see answers it.
            # Say that, rather than the thing that is not true.
            return _claim(
                "capture",
                f"{san} takes the {victim} on {where}, and counting the"
                f" exchange on that square alone says you come out"
                f" {_worth(gain)} up. The engine does not agree: it has an"
                f" answer that gets the material back, and the line below is"
                f" where to look for it.",
                root, move=move.uci(), victim=victim, square=where, gain=gain,
                engine_confirmed=False)
        drawn = judge.drawish(line) or _cannot_mate(after, me)
        if free:
            text = (f"{san} simply wins the {victim} on {where}: after the"
                    f" capture nothing of theirs attacks {where}, so there is"
                    f" no recapture to worry about.")
        else:
            text = (f"{san} wins {_worth(gain)} on {where}. They can take back,"
                    f" but counting the whole exchange through to the end"
                    f" leaves you ahead whichever way they do it.")
        if drawn:
            text += (" The engine still calls the position level, so the extra"
                     " material is not the same as a win here.")
        return _claim("capture", text, root, move=move.uci(), victim=victim,
                      square=where, gain=gain, undefended=free,
                      engine_confirmed=bool(held), drawish=drawn)
    if gain < 0:
        back = ""
        if outcome is not None and outcome > 0:
            back = (f" Follow the line: it comes back with {_worth(outcome)}"
                    f" more than you started with.")
        elif outcome == 0:
            back = (" Follow the line: the material all comes back, and what"
                    " you keep is the better position.")
        return _claim(
            "sacrifice",
            f"{san} gives the {name} for the {victim} on {where}, which is"
            f" {-gain:.0f} points down if you stop counting there. Counting"
            f" there is the mistake: the move is played for what the line does"
            f" next.{back}",
            root, move=move.uci(), victim=victim, square=where, gain=gain)
    extra = _trade_gain(root, move, after, me)
    return _claim(
        "trade",
        f"{san} trades the {name} for the {victim} on {where}. The count comes"
        f" out level, so this is a decision about which pieces stay on the"
        f" board rather than a way of winning material{extra}.",
        root, move=move.uci(), victim=victim, square=where, gain=0)


def _trade_gain(root, move, after, me) -> str:
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
        return (", and the piece it takes was standing where no pawn of yours"
                " could ever have chased it off")
    return ""


def _engine_agrees(judge, root, after, me, gain: float) -> bool:
    """A tactic is only worth saying when a search also collects the
    material. With no engine to ask, the static tests stand on their own."""
    if judge is None:
        return True
    held, _ = judge.holds(root, after, me, gain)
    return held is not False


def _claim_tactic(root, move, san, after, me, where, name,
                  include_attack: bool = True, judge=None):
    """Fork, pin, skewer -- all of them claims about the position *after* the
    move, which is why every sentence here says so."""
    if _en_prise(after, move.to_square, me):
        return None                    # a hanging piece forks nothing
    hits = [t for t in _targets_of(after, move.to_square, me)
            if t["piece"] != "king"]
    pin = _line_pin(after, move.to_square, me)
    checking = after.is_check()
    if len(hits) >= 2 and not _they_can_save(
            after, [hits[0]["square"], hits[1]["square"]], me) \
            and _engine_agrees(judge, root, after, me,
                               min(h["gain"] or 1 for h in hits[:2])):
        return _claim(
            "fork",
            f"{_after_move(san)} the {name} on {where} attacks both the"
            f" {hits[0]['piece']} on {hits[0]['square']} and the"
            f" {hits[1]['piece']} on {hits[1]['square']}. They can defend one"
            f" of them and not the other, and that is the whole point of the"
            f" square.",
            after, square=where, targets=hits[:2])
    if checking and hits and not _they_can_save(after, [hits[0]["square"]], me):
        return _claim(
            "double",
            f"{san} checks the king and, in the same move, attacks the"
            f" {hits[0]['piece']} on {hits[0]['square']}. The check has to be"
            f" answered first, and the second target is still there afterwards.",
            after, square=where, targets=hits[:1])
    if pin:
        behind = ("their king" if pin["behind"] == "king"
                  else f"the {pin['behind']} on {pin['behind_square']}")
        consequence = ("cannot legally move at all while the line is open"
                       if pin["behind"] == "king" else
                       f"cannot move without losing {behind}")
        return _claim(
            "pin",
            f"{_after_move(san)} the {name} on {where} {pin['pin_kind']} the"
            f" {pin['front']} on {pin['front_square']} against {behind}: the"
            f" {pin['front']} {consequence}.",
            after, **pin)
    if hits and include_attack:
        held = not _they_can_save(after, [hits[0]["square"]], me)
        if not held:
            return None        # they answer it in one move: not worth saying
        if not _engine_agrees(judge, root, after, me, hits[0]["gain"] or 1):
            return None        # the static count says it falls; the engine does not
        return _claim(
            "attack",
            f"{_after_move(san)} the {name} on {where} attacks the"
            f" {hits[0]['piece']} on {hits[0]['square']}, and they have no"
            f" single move that saves it: defending it is not enough, and"
            f" moving it away costs them something else.",
            after, square=where, targets=hits[:1], unanswerable=True)
    if checking:
        rights = (root.has_castling_rights(not me)
                  and not after.has_castling_rights(not me))
        answers = len(list(after.legal_moves))
        if rights:
            text = (f"{san} comes with check, and answering it costs them the"
                    f" right to castle: the king has to move itself. It stays"
                    f" in the middle after that, which is worth more than the"
                    f" check.")
        else:
            text = (f"{san} comes with check, so they get no say in what"
                    f" happens next: {answers} legal answer"
                    f"{'' if answers == 1 else 's'}, all of them in the line"
                    f" below.")
        return _claim("check_given", text, after, legal_answers=answers,
                      castling_lost=rights)
    return None


def _claim_positional(root, move, san, after, me, where, name):
    piece = root.piece_at(move.from_square)
    if piece is None:
        return None
    seventh = 6 if me == chess.WHITE else 1
    if piece.piece_type == chess.ROOK \
            and chess.square_rank(move.to_square) == seventh:
        hits = [t for t in _targets_of(after, move.to_square, me)
                if t["piece"] != "king"]
        first = (f" It already attacks the {hits[0]['piece']} on"
                 f" {hits[0]['square']}." if hits else "")
        return _claim(
            "seventh",
            f"{san} puts the rook on the seventh rank, where their pawns sit"
            f" and cannot turn round to defend each other, and where it keeps"
            f" their king on the back rank.{first}",
            after, square=where, targets=hits[:1])
    if piece.piece_type == chess.ROOK and _open_file(after, move.to_square, me) \
            and not _open_file(root, move.from_square, me):
        file_name = where[0]
        return _claim(
            "file",
            f"{san} takes the open {file_name}-file. With no pawn of your own"
            f" in front of it the rook sees the whole board that way, which it"
            f" did not from {chess.square_name(move.from_square)}.",
            after, square=where, file=file_name)
    if piece.piece_type in (chess.KNIGHT, chess.BISHOP) \
            and _is_outpost(after, move.to_square, me):
        return _claim(
            "outpost",
            f"{san} puts the {name} on {where} for good. No pawn of theirs can"
            f" ever attack that square again, so the piece cannot be chased"
            f" off it by anything cheaper than itself.",
            after, square=where)
    if piece.piece_type == chess.PAWN and _passed(after, move.to_square, me):
        return _claim(
            "passed",
            f"{san} makes a passed pawn: no pawn of theirs stands on"
            f" {where[0]} or either file beside it ahead of the pawn, so"
            f" nothing but a piece can stop it, and every trade makes it"
            f" stronger.",
            after, square=where)
    if piece.piece_type == chess.KING and _pawn_ending(after) \
            and _opposition(after, me):
        return _claim(
            "opposition",
            f"{san} takes the opposition: the kings stand two squares apart"
            f" with them to move, so they have to give way and let your king"
            f" in. In a pawn ending that is usually the whole game.",
            after, square=where)
    if root.is_castling(move) and _king_exposed(root, me):
        return _claim(
            "castle",
            f"{san} gets the king off an open file — the file their rook wants"
            f" — and puts a rook where the middle of the board is about to"
            f" open.",
            after, square=where)
    return None


def _claim_prevented(root, move, san, after, me):
    stop = _prevented(root, move, after, me)
    if not stop:
        return None
    if stop["pin"]:
        pin = stop["pin"]
        behind = ("your king" if pin["behind"] == "king"
                  else f"your {pin['behind']} on {pin['behind_square']}")
        would = (f"{pin['pin_kind'][:-1]}ned your {pin['front']} on"
                 f" {pin['front_square']} against {behind}")
    else:
        would = f"attacked your {stop['hits'][0]['piece']} on {stop['hits'][0]['square']}"
    price = (f"now going there anyway costs them {_worth(stop['cost'])}"
             if stop["cost"] else "now the square is not available at all")
    return _claim(
        "prevent",
        f"{san} takes a square away before they can use it. Without it they"
        f" had {stop['san']}, and from {stop['square']} the {stop['piece']}"
        f" would have {would}. That is the move you are stopping, and"
        f" {price}.",
        root, their_move=stop["san"], square=stop["square"])


def _prevented(root, move, after, me):
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
        grab = cheapest_capture(landed, theirs.to_square)
        if grab is not None and see(landed, grab) > 0:
            continue                      # it was not safe there anyway
        pin = _line_pin(landed, theirs.to_square, not me)
        hits = [t for t in _targets_of(landed, theirs.to_square, not me)
                if t["piece"] != "king"]
        if not pin and not hits:
            continue
        cost = None
        if theirs in after.legal_moves:
            probe_after = after.copy(stack=False)
            probe_after.push(theirs)
            grab = cheapest_capture(probe_after, theirs.to_square)
            if grab is None or see(probe_after, grab) <= 0:
                continue                  # the square is still fine for them
            cost = see(probe_after, grab)
        entry = {"san": probe.san(theirs), "pin": pin, "hits": hits,
                 "cost": cost, "piece": NAMES[piece.piece_type],
                 "square": chess.square_name(theirs.to_square)}
        if best is None or (pin and not best["pin"]):
            best = entry
    return best


def _claim_stops(root, move, san, after, me, theirs):
    if not theirs:
        return None
    now = best_shot(after, not me)
    if now is not None and now["gain"] >= theirs["gain"]:
        return None
    if theirs["mate"]:
        text = (f"What they were threatening was {theirs['san']}, which is"
                f" mate. {san} is the move that takes it away.")
    else:
        text = (f"Their idea was {theirs['san']}, winning"
                f" {_worth(theirs['gain'])}. {san} deals with that before"
                f" anything else.")
    return _claim("stops", text, root, their_move=theirs["san"],
                  gain=theirs["gain"])


def _narrate(root: chess.Board, steps, me: bool) -> list[dict]:
    """What happens in the engine's line, in order, keeping only the moves
    that carry the point. Each sentence names the move it is about, so it can
    be checked against that ply and no other."""
    out = []
    base = _material(root, me)
    if len(steps) < 2:
        return out

    move, san, before, after = steps[1]
    replies = list(before.legal_moves)
    we_took = root.is_capture(steps[0][0])
    same_square = move.to_square == steps[0][0].to_square
    if len(replies) == 1:
        out.append(_claim("line", f"They have one legal answer, {san}, and it"
                          f" does not change anything.", before,
                          ply=1, san=san, only=True))
    elif before.is_check():
        out.append(_claim("line", f"They are in check and have to answer it;"
                          f" {san} is the best of the ways out.", before,
                          ply=1, san=san, in_check=True))
    elif we_took and same_square and before.is_capture(move):
        lost = _material(before, me) - base
        out.append(_claim("line", f"They have to take back with {san};"
                          f" leaving it is {_worth(abs(lost))} for nothing.",
                          before, ply=1, san=san, recapture=True))
    elif same_square and before.is_capture(move):
        out.append(_claim("line", f"They take with {san} — that is the move"
                          f" you have to have seen, and the rest of the line"
                          f" is the answer to it.", before,
                          ply=1, san=san, capture=True))

    for i in range(2, len(steps)):
        move, san, before, after_i = steps[i]
        if i % 2:
            continue
        if _material(after_i, me) <= max(base, _material(before, me)):
            continue
        victim = before.piece_at(move.to_square)
        if victim is None:
            continue
        why = _why_it_falls(before, move, after_i, me)
        out.append(_claim(
            "line",
            f"Then {_move_number(root, i)} {san} takes the"
            f" {NAMES[victim.piece_type]} on {chess.square_name(move.to_square)}"
            + (f", {why}." if why else "."),
            before, ply=i, san=san,
            victim=NAMES[victim.piece_type],
            square=chess.square_name(move.to_square)))
        break

    end = steps[-1][3]
    swing = _material(end, me) - base
    if swing == 0 and len(out) < 3:
        gained = _structure_gain(root, end, me)
        if gained:
            out.append(_claim("line", gained, end, structural=True))
    if swing > 0 and len(out) < 3:
        moves = (len(steps) + 1) // 2
        out.append(_claim("line", f"{moves} moves into the line the engine"
                          f" shows, the count is {_worth(swing)} in your"
                          f" favour. That is as far as it was searched, not a"
                          f" promise about the rest of the game.", end,
                          swing=swing, horizon=len(steps)))
    return out


def _why_it_falls(before, move, after, me) -> str:
    if after.is_check():
        return "and it comes with check, so there is no time to save anything"
    if _undefended(after, move.to_square):
        return "and nothing defends it"
    if see(before, move) > 0:
        return "and taking back costs them more than it gains"
    return ""


def _structure_gain(root, end, me) -> str:
    pair_before = len(root.pieces(chess.BISHOP, me)) - len(root.pieces(chess.BISHOP, not me))
    pair_after = len(end.pieces(chess.BISHOP, me)) - len(end.pieces(chess.BISHOP, not me))
    if pair_after > pair_before and len(end.pieces(chess.BISHOP, me)) == 2:
        return ("The material comes out level, but at the end of the line the"
                " engine shows, you have both bishops and they do not.")
    shield_before = _shield(root, not me)
    shield_after = _shield(end, not me)
    if shield_after < shield_before:
        return (f"Material stays level, but at the end of the line the engine"
                f" shows, their king has {shield_after} pawn"
                f"{'' if shield_after == 1 else 's'} in front of it instead of"
                f" {shield_before}.")
    return ""


def _plan(root, steps, me) -> dict | None:
    mine = [san for i, (_, san, _, _) in enumerate(steps) if i % 2 == 0]
    if len(mine) < 2:
        return None
    end = steps[-1][3]
    swing = _material(end, me) - _material(root, me)
    if swing > 0:
        close = (f" and by the end of it you have won {_worth(swing)} without"
                 f" anything being forced")
    else:
        opened = _open_file_gain(root, end, me)
        close = (f" and {opened}" if opened else
                 " and the gain is position rather than material")
    return _claim("line",
                  f"Nothing here is forced, so this is a plan rather than a"
                  f" combination. The engine plays {', then '.join(mine[:3])},"
                  f"{close}.", root, plan=mine[:3])


def _open_file_gain(root, end, me) -> str:
    for sq in end.pieces(chess.ROOK, me):
        f = chess.square_file(sq)
        if any((end.piece_at(chess.square(f, r)) or chess.Piece(chess.KING, me)).piece_type == chess.PAWN
               for r in range(8)):
            continue
        if any((root.piece_at(chess.square(f, r)) or chess.Piece(chess.KING, me)).piece_type == chess.PAWN
               for r in range(8)):
            return (f"the {chess.square_name(sq)[0]}-file opens for the rook on"
                    f" the way")
    return ""


def _king_exposed(board: chess.Board, colour: bool) -> bool:
    king = board.king(colour)
    if king is None:
        return False
    f = chess.square_file(king)
    return not any(
        board.piece_at(chess.square(f, r)) == chess.Piece(chess.PAWN, colour)
        for r in range(8))


SPELLED = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def _spell(n: int) -> str:
    return SPELLED.get(n, str(n))


def _payoff(root: chess.Board, steps, me: bool):
    """Where the engine's line actually pays off: the first move of ours that
    ends up ahead of where the position started, or mate. That is what the
    first move is for; everything before it is preparation."""
    base = _material(root, me)
    for i in range(0, len(steps)):
        move, san, before, after = steps[i]
        if i % 2:
            continue
        if after.is_checkmate():
            return {"ply": i, "move": move, "san": san, "before": before,
                    "after": after, "mate": True, "swing": None}
        if i and _material(after, me) > max(base, _material(before, me)):
            return {"ply": i, "move": move, "san": san, "before": before,
                    "after": after, "mate": False,
                    "swing": _material(after, me) - base}
    return None


def _guard_story(root: chess.Board, first: chess.Move, after_first: chess.Board,
                 payoff: dict, steps, me: bool):
    """Why the payoff was not available at move one, and what the first move
    did about it. This is the part that makes a move a plan rather than a
    reflex, and it is all read off the two positions."""
    square = payoff["move"].to_square
    executor = payoff["before"].piece_at(payoff["move"].from_square)
    if executor is None:
        return None
    guards_then = set(root.attackers(not me, square))
    guards_at_payoff = set(payoff["before"].attackers(not me, square))
    gone = guards_then - guards_at_payoff
    # 1. The first move took the defender off the board.
    if root.is_capture(first) and first.to_square in guards_then:
        taken = root.piece_at(first.to_square)
        if taken is not None:
            return {
                "why": "removes the guard",
                "text": (f"the {NAMES[taken.piece_type]} it takes on"
                         f" {chess.square_name(first.to_square)} is one of the"
                         f" pieces holding {chess.square_name(square)}"),
                "square": chess.square_name(square),
                "guard": chess.square_name(first.to_square),
            }
    # 2. A defender moved during the line. Which of their moves did it, and
    #    where it went, decide whether that is a deflection or a piece
    #    walking into the capture.
    for guard in gone:
        piece = root.piece_at(guard)
        if piece is None:
            continue
        their_move = next((st for j, st in enumerate(steps)
                           if j % 2 and j < payoff["ply"]
                           and st[0].from_square == guard), None)
        if their_move is None:
            continue
        went = their_move[0].to_square
        if went == square:
            return {
                "why": "walks in",
                "text": (f"their {NAMES[piece.piece_type]} goes to"
                         f" {chess.square_name(square)} itself in the line, and"
                         f" that is what you take"),
                "square": chess.square_name(square),
                "guard": chess.square_name(guard),
            }
        return {
            "why": "deflects the guard",
            "text": (f"their {NAMES[piece.piece_type]} on"
                     f" {chess.square_name(guard)} is holding"
                     f" {chess.square_name(square)} right now, and"
                     f" {their_move[1]} in the line takes it away from that job"),
            "square": chess.square_name(square),
            "guard": chess.square_name(guard),
        }
    # 3. The piece that lands the blow has to get there first.
    if payoff["move"].from_square != first.to_square \
            and root.piece_at(payoff["move"].from_square) is None:
        return {
            "why": "brings the piece",
            "text": (f"the {NAMES[executor.piece_type]} that lands on"
                     f" {chess.square_name(square)} is not there yet; the line"
                     f" is how it arrives"),
            "square": chess.square_name(square),
            "guard": None,
        }
    # 4. The blow is by the piece this move just played.
    if payoff["move"].from_square == first.to_square:
        return {
            "why": "same piece",
            "text": (f"it is the same {NAMES[executor.piece_type]} that comes"
                     f" back to take on {chess.square_name(square)}"),
            "square": chess.square_name(square),
            "guard": None,
        }
    return None


def _claim_purpose(root: chess.Board, move: chess.Move, san: str,
                   after: chess.Board, steps, me: bool, judge=None):
    """What the move is for, several moves out. Nothing here is asserted
    about the board in front of you: it is about the line, and it names the
    ply it is about."""
    payoff = _payoff(root, steps, me)
    if payoff is None or payoff["ply"] == 0:
        return None
    moves_away = (payoff["ply"] + 2) // 2
    story = _guard_story(root, move, after, payoff, steps, me)
    swing = payoff["swing"] or 0
    strong = story and story["why"] in ("removes the guard", "deflects the guard",
                                        "brings the piece")
    if not payoff["mate"]:
        # "The point is that they walk a pawn onto a square four moves from
        # now" is not the point of anything. A purpose is worth stating when
        # this move made the payoff possible, or when the payoff is big.
        if not strong and swing < 2:
            return None
        if story and story["why"] == "walks in" and swing < 3:
            return None
        if not _engine_agrees(judge, root, after, me, min(swing, 2)):
            return None
    count = _spell(moves_away)
    if payoff["mate"]:
        head = (f"The point of {san} is mate {count} move"
                f"{'' if moves_away == 1 else 's'} from here, with"
                f" {payoff['san']}.")
    else:
        victim = payoff["before"].piece_at(payoff["move"].to_square)
        what = (f"{payoff['san']} takes the {NAMES[victim.piece_type]} on"
                f" {chess.square_name(payoff['move'].to_square)}"
                if victim is not None else f"{payoff['san']} lands")
        head = (f"The point of {san} is {count} move"
                f"{'' if moves_away == 1 else 's'} further on: {what}, and the"
                f" count ends {_worth(payoff['swing'])} in your favour.")
    tail = f" What makes it work is that {story['text']}." if story else ""
    return _claim("purpose", head + tail, root,
                  payoff_ply=payoff["ply"], payoff_san=payoff["san"],
                  swing=payoff["swing"], why=(story or {}).get("why"))


def why_best(root: chess.Board, move: chess.Move, sans: list[str],
             boards: list[chess.Board], me: bool, line: list[str] | None = None,
             alts: list[dict] | None = None, judge: "Judge | None" = None) -> list[dict]:
    """Why the engine's move is the engine's move.

    Claims are made in one order only: mate, then the check you are in, then
    what the move does to their position, then what it stops, then what the
    line does with it. Each carries the position it is about.
    """
    after = root.copy(stack=False)
    after.push(move)
    san = sans[0] if sans else root.san(move)
    steps = _steps(root.fen(), line or [], PV_PLIES)
    where = chess.square_name(move.to_square)
    piece = root.piece_at(move.from_square)
    name = NAMES[piece.piece_type] if piece else "piece"
    gain = see(root, move) if root.is_capture(move) else 0.0
    outcome = (_material(steps[-1][3], me) - _material(root, me)) if steps else None

    if after.is_checkmate():
        return [_claim("why", f"{san} is mate. Nothing else matters.", after,
                       mate_in=0)]
    if boards and boards[-1].is_checkmate():
        n = (len(boards) + 1) // 2
        items = [_claim("why", f"{san} forces mate in {n}. Every answer they"
                        f" have is in the line below, and none of them change"
                        f" the ending.", root, mate_in=n)]
        items += _narrate(root, steps, me)[:1]
        return items

    items = []
    judge = judge or Judge()
    head = (_claim_check(root, move, san, after, me, gain, where)
            or _claim_capture(root, move, san, after, me, gain, where, name,
                              outcome, judge)
            or _claim_rescue(root, move, san, after, me, name)
            # A fork, a pin or a check is the point of the move; a rook on the
            # seventh is the point of the move; "it attacks something" only
            # is the point when nothing better describes it.
            or _claim_tactic(root, move, san, after, me, where, name, False, judge)
            or _claim_positional(root, move, san, after, me, where, name)
            or _claim_tactic(root, move, san, after, me, where, name, True, judge)
            or _claim_prevented(root, move, san, after, me))
    if head:
        items.append(head)
        # A capture that also forks is worth both sentences.
        if head["kind"] in ("capture", "trade", "sacrifice", "check"):
            second = _claim_tactic(root, move, san, after, me, where, name,
                                   True, judge)
            if second:
                items.append(second)
    # What the move is actually for, when the payoff is further down the line
    # than the move itself. This is the sentence worth reading.
    purpose = _claim_purpose(root, move, san, after, steps, me, judge)
    if purpose:
        items.insert(0 if not items else 1, purpose)

    theirs = best_shot(root, not me)
    stops = _claim_stops(root, move, san, after, me, theirs)
    if not items and stops:
        items.append(stops)
        stops = None

    for claim in _narrate(root, steps, me):
        if len(items) >= 3:
            break
        items.append(claim)

    if stops and len(items) < 4:
        items.append(stops)
    if not items:
        plan = _plan(root, steps, me)
        if plan:
            items.append(plan)
    if not items:
        items.append(_claim("why", f"{san} keeps everything defended and"
                            f" improves the worst-placed piece. There is no"
                            f" tactic in the position to find.", root))
    if alts:
        alt = _runner_up(root, alts, me)
        if alt:
            items.append(alt)
    return items


def _runner_up(root: chess.Board, alts: list[dict], me: bool):
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
            return _claim("alt", f"The engine's other choice, {san}, is worth"
                          f" the same here to within a rounding error: either"
                          f" move keeps everything you had.", root,
                          san=san, gap=gap)
        if gap < 2:
            return _claim("alt", f"The engine's second choice, {san}, is only"
                          f" {gap:.1f} points of win probability behind, so"
                          f" this was a choice between two reasonable moves"
                          f" rather than a test you failed.", root,
                          san=san, gap=gap)
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
                tail = f": they answer {reply} and you end up {_worth(-swing)} down."
            elif swing == 0:
                tail = (f": they answer {reply}, the material is unchanged, and"
                        f" the position has stopped being about anything.")
            else:
                tail = (f". That move wins material too, so the difference is"
                        f" not the count: the position this one leaves behind"
                        f" is worth more than the extra material.")
        return _claim("alt", f"The engine's second choice was {san}, and it"
                      f" puts that {gap:.0f} points of win probability behind"
                      f" this one{tail}", root, san=san, gap=gap)
    return None


# --- checking the claims against the board ---------------------------------

def verify_claim(claim: dict, judge=None) -> list[str]:
    """Re-derive a claim from its own FEN, with plain board lookups rather
    than the code that produced it. Anything it cannot confirm is returned as
    a complaint, and a complaint is a bug."""
    problems = []
    facts = claim.get("facts") or {}
    board = chess.Board(claim["fen"])

    def piece_on(name: str, square_name: str, colour=None):
        square = chess.parse_square(square_name)
        found = board.piece_at(square)
        if found is None:
            problems.append(f"{claim['kind']}: nothing on {square_name},"
                            f" claimed {name}")
            return None
        if NAMES[found.piece_type] != name:
            problems.append(f"{claim['kind']}: {square_name} holds a"
                            f" {NAMES[found.piece_type]}, claimed {name}")
        if colour is not None and found.color != colour:
            problems.append(f"{claim['kind']}: {square_name} is the wrong"
                            f" colour for the claim")
        return found

    kind = claim["kind"]
    if kind in ("fork", "double", "attack"):
        squares = [t["square"] for t in facts.get("targets", [])]
        mover = board.piece_at(chess.parse_square(facts["square"])) if facts.get("square") else None
        if squares and mover is not None and _they_can_save(board, squares, mover.color):
            problems.append(f"{kind}: one reply of theirs saves everything the"
                            f" claim says is hanging")
        square = chess.parse_square(facts["square"])
        attacker = board.piece_at(square)
        if attacker is None:
            problems.append(f"{kind}: nothing stands on {facts['square']}")
        for target in facts.get("targets", []):
            victim = piece_on(target["piece"], target["square"])
            if victim is not None and attacker is not None:
                if victim.color == attacker.color:
                    problems.append(f"{kind}: {target['square']} is their own"
                                    f" piece")
                if chess.parse_square(target["square"]) not in board.attacks(square):
                    problems.append(f"{kind}: {facts['square']} does not"
                                    f" attack {target['square']}")
    elif kind == "pin":
        attacker = chess.parse_square(facts["attacker"])
        front = chess.parse_square(facts["front_square"])
        behind = chess.parse_square(facts["behind_square"])
        piece_on(facts["attacker_piece"], facts["attacker"])
        piece_on(facts["front"], facts["front_square"])
        piece_on(facts["behind"], facts["behind_square"])
        if front not in board.attacks(attacker):
            problems.append("pin: the pinning piece does not attack the front"
                            " piece")
        ray = chess.ray(attacker, front)
        if not ray or behind not in chess.SquareSet(ray):
            problems.append("pin: the piece behind is not on the same line")
        between = chess.SquareSet(chess.between(front, behind)) & board.occupied
        if between:
            problems.append("pin: something stands between the two pieces")
        a_piece, f_piece, b_piece = (board.piece_at(attacker),
                                     board.piece_at(front),
                                     board.piece_at(behind))
        if a_piece and f_piece and a_piece.color == f_piece.color:
            problems.append("pin: the front piece is our own")
        if f_piece and b_piece and f_piece.color != b_piece.color:
            problems.append("pin: the two pinned pieces are not the same side")
        if f_piece and b_piece and facts.get("pin_kind") == "pins" \
                and b_piece.piece_type != chess.KING \
                and VALUES[b_piece.piece_type] <= VALUES[f_piece.piece_type]:
            problems.append("pin: what is behind is not worth more")
    elif kind in ("capture", "trade", "sacrifice"):
        move = chess.Move.from_uci(facts["move"])
        if move not in board.legal_moves:
            problems.append(f"{kind}: {facts['move']} is not legal here")
        elif not board.is_capture(move):
            problems.append(f"{kind}: {facts['move']} captures nothing")
        else:
            got = see(board, move)
            if abs(got - facts["gain"]) > 0.01:
                problems.append(f"{kind}: exchange is {got}, claimed"
                                f" {facts['gain']}")
            if not board.is_en_passant(move):
                piece_on(facts["victim"], facts["square"])
            # The independent half: a search, not the same static count that
            # wrote the claim in the first place.
            if judge is not None and facts.get("engine_confirmed"):
                after = board.copy(stack=False)
                after.push(move)
                held, _ = judge.holds(board, after, board.turn, facts["gain"])
                if held is False:
                    problems.append(f"{kind}: the engine gets the material"
                                    f" back; the claim says it does not")
    elif kind == "rescue":
        piece_on(facts["piece"], facts["square"])
    elif kind == "seventh":
        square = chess.parse_square(facts["square"])
        rook = board.piece_at(square)
        if rook is None or rook.piece_type != chess.ROOK:
            problems.append("seventh: no rook there")
        elif chess.square_rank(square) != (6 if rook.color == chess.WHITE else 1):
            problems.append("seventh: not the seventh rank")
    elif kind == "outpost":
        square = chess.parse_square(facts["square"])
        piece = board.piece_at(square)
        if piece is None:
            problems.append("outpost: nothing on the square")
        elif not _is_outpost(board, square, piece.color):
            problems.append("outpost: a pawn can still challenge that square")
    elif kind == "passed":
        square = chess.parse_square(facts["square"])
        piece = board.piece_at(square)
        if piece is None or piece.piece_type != chess.PAWN:
            problems.append("passed: no pawn there")
        elif not _passed(board, square, piece.color):
            problems.append("passed: the pawn is not passed")
    elif kind == "file":
        square = chess.parse_square(facts["square"])
        rook = board.piece_at(square)
        if rook is None or rook.piece_type != chess.ROOK:
            problems.append("file: no rook there")
        elif not _open_file(board, square, rook.color):
            problems.append("file: a pawn of ours is still on it")
    elif kind == "check":
        if not board.is_check():
            problems.append("check: the side to move is not in check")
        if facts.get("legal_answers") != len(list(board.legal_moves)):
            problems.append("check: wrong count of legal answers")
    elif kind == "check_given":
        if not board.is_check():
            problems.append("check_given: the position is not a check")
    elif kind == "stops":
        # The threat is what they would play if handed the move.
        probe = board.copy(stack=False)
        if not probe.is_check():
            probe.push(chess.Move.null())
            if facts.get("their_move") not in {probe.san(m) for m in probe.legal_moves}:
                problems.append(f"stops: {facts.get('their_move')} is not a"
                                f" move they have")
    elif kind == "prevent":
        probe = board.copy(stack=False)
        if not probe.is_check():
            probe.push(chess.Move.null())
            if facts.get("their_move") not in {probe.san(m) for m in probe.legal_moves}:
                problems.append(f"prevent: {facts.get('their_move')} is not a"
                                f" move they had")
    elif kind == "line":
        san = facts.get("san")
        if san and san not in {board.san(m) for m in board.legal_moves}:
            problems.append(f"line: {san} is not legal in the position quoted")
    elif kind == "alt":
        san = facts.get("san")
        if san and san not in {board.san(m) for m in board.legal_moves}:
            problems.append(f"alt: {san} is not a legal move here")
    return problems


def verify(items: list[dict], judge=None) -> list[str]:
    out = []
    for item in items:
        if item.get("fen"):
            out.extend(verify_claim(item, judge))
    return out


CHECKS = (
    _material_swing, _trapped_piece, _square_control,
    _king_safety, _activity, _tempo,
)


def explain(fen: str, my_move: str, best_move: str, pvs: dict,
            judge: "Judge | None" = None,
            reasons: list[dict] | None = None) -> Explanation:
    """Why the move is the move.

    `pvs` is {"mine": [uci, ...], "best": [uci, ...]} -- the principal
    variations, each beginning with the move it belongs to. `judge` is an
    engine opinion every material claim has to survive; `reasons` is a set of
    claims worked out earlier (during review, at more depth) for this same
    position and move, used instead of computing them again.
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
    if reasons is None:
        reasons = why_best(root, best_first, best_sans, best_boards, me,
                           line=best_line, alts=pvs.get("alts"), judge=judge)
    # Set as a plain attribute, not a dataclass field: the client already has
    # these inside `items` and does not need them twice.
    out.why_claims = [dict(r) for r in reasons]

    if my_move and best_move and my_move == best_move:
        out.items = [dict(r) for r in reasons]
        out.why = [dict(r) for r in reasons]
        out.text = " ".join(r["text"] for r in reasons[:2])
        return out

    if not mine_sans:
        # Shown rather than answered: there is nothing of yours to compare.
        out.items = [dict(r) for r in reasons]
        out.why = [dict(r) for r in reasons]
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
    out.cost = [dict(c) for c in cost]
    out.why = [dict(r) for r in reasons]
    head = cost[0]["text"] if cost else ""
    out.text = " ".join([t for t in [head, reasons[0]["text"]] if t])
    return out


# --- explanations worked out ahead of time ---------------------------------

def store(conn, pos_hash: int, best_move: str, depth: int, engine_ver: str,
          items: list[dict]) -> None:
    """Keep an explanation computed during review, where there was time to
    check it against a real search."""
    conn.execute(
        "INSERT OR REPLACE INTO explanations(pos_hash, best_move, depth,"
        " engine_ver, items, computed_at) VALUES(?,?,?,?,?,?)",
        (pos_hash, best_move, depth, engine_ver, json.dumps(items),
         int(time.time())),
    )


def stored(conn, pos_hash: int, best_move: str, engine_ver: str,
           min_depth: int = 0):
    """The explanation for this position and move, if one was worked out
    deeply enough. A shallower one is no better than computing it now."""
    row = conn.execute(
        "SELECT items FROM explanations WHERE pos_hash=? AND best_move=?"
        " AND engine_ver=? AND depth>=?",
        (pos_hash, best_move, engine_ver, min_depth)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["items"])
    except (ValueError, TypeError):
        return None


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
