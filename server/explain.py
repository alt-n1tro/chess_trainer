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
    if my_move and best_move and my_move == best_move:
        out.text = "That is the engine's move."
        return out
    if not best_sans:
        return out
    if not mine_sans:
        # Nothing to diff against: the move was shown rather than answered.
        out.text = f"{best_sans[0]} is the move."
        out.items.append({"kind": "best", "text": out.text})
        return out

    for check in CHECKS:
        item = check(root, me, mine_end, best_end, mine_boards, best_boards,
                     mine_sans, best_sans)
        if item:
            out.items.append(item)
        if len(out.items) >= MAX_ITEMS:
            break

    if best_sans:
        out.items.append(
            {"kind": "best", "text": f"{best_sans[0]} is the move."}
        )
    # Capped at two sentences: the most important structural point, then the
    # move. With nothing structural to say, the move alone is the whole of it.
    sentences, seen = [], set()
    for item in out.items[:1] + out.items[-1:]:
        if item["text"] not in seen:
            seen.add(item["text"])
            sentences.append(item["text"])
    out.text = " ".join(sentences)
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
