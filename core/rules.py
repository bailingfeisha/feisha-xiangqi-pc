# -*- coding: utf-8 -*-
"""走法生成与胜负判定（普通象棋 / 揭棋共用）。

揭棋差异：
* 暗子按其**所在开局格位**对应的棋子走第一步，走后必须揭开；
* 揭棋明子的士可出九宫、象可过河（可在设置中关闭）；
* 其余（车/马/炮/兵/将帅、蹩马腿、塞象眼、炮架、九宫、飞将）同象棋。
"""
from __future__ import annotations

from dataclasses import dataclass

from .board import Board, Move
from .constants import (
    Side, Mode, PieceType, in_board, in_palace, on_own_half, crossed_river,
)
from .options import Options

ORTHO = [(0, -1), (0, 1), (-1, 0), (1, 0)]
DIAG = [(-1, -1), (1, -1), (-1, 1), (1, 1)]
HORSE_STEPS = [
    ((0, -1), (-1, -2)), ((0, -1), (1, -2)),
    ((0, 1), (-1, 2)), ((0, 1), (1, 2)),
    ((-1, 0), (-2, -1)), ((-1, 0), (-2, 1)),
    ((1, 0), (2, -1)), ((1, 0), (2, 1)),
]
ELEPHANT_STEPS = [
    ((-1, -1), (-2, -2)), ((1, -1), (2, -2)),
    ((-1, 1), (-2, 2)), ((1, 1), (2, 2)),
]
STRAIGHT_TYPES = {
    PieceType.CHARIOT, PieceType.CANNON,
    PieceType.SOLDIER, PieceType.GENERAL,
}


@dataclass
class GameResult:
    over: bool = True
    winner: Side | None = None      # None 表示和棋
    reason: str = ""
    kind: str = ""

    def to_dict(self):
        return {"over": self.over,
                "winner": None if self.winner is None else int(self.winner),
                "reason": self.reason, "kind": self.kind}

    @classmethod
    def from_dict(cls, d):
        if not d:
            return None
        return cls(bool(d.get("over", True)),
                   None if d.get("winner") is None else Side(int(d["winner"])),
                   d.get("reason", ""), d.get("kind", ""))

    @property
    def text(self) -> str:
        if self.winner is None:
            return f"和棋 · {self.reason}"
        return f"{self.winner.label}胜 · {self.reason}"


def _can_land(board: Board, side, col, row, options: Options) -> bool:
    p = board.get(col, row)
    if p is None:
        return True
    if p.side == side:
        return False
    if p.dark and not options.dark_can_be_captured:
        return False
    return True


def _sliding(board, side, col, row, options, cannon=False):
    result = []
    for dc, dr in ORTHO:
        x, y = col + dc, row + dr
        jumped = False
        while in_board(x, y):
            t = board.get(x, y)
            if not cannon:
                if not _can_land(board, side, x, y, options):
                    break
                result.append((x, y))
                if t is not None:
                    break
            else:
                if not jumped:
                    if t is None:
                        result.append((x, y))
                    else:
                        jumped = True
                else:
                    if t is not None:
                        if _can_land(board, side, x, y, options):
                            result.append((x, y))
                        break
            x += dc
            y += dr
    return result


def _targets(board, side, col, row, eff_type, is_dark, options):
    # 揭棋明子才享有士出九宫 / 象过河的特权；暗子按原始象棋规则走第一步
    cross = (board.mode == Mode.JIEQI and not is_dark)
    targets = []
    t = PieceType(eff_type)

    if t == PieceType.CHARIOT:
        targets = _sliding(board, side, col, row, options)
    elif t == PieceType.CANNON:
        targets = _sliding(board, side, col, row, options, cannon=True)
    elif t == PieceType.HORSE:
        for (lc, lr), (tc, tr) in HORSE_STEPS:
            lx, ly = col + lc, row + lr
            if not in_board(lx, ly) or board.get(lx, ly) is not None:
                continue
            x, y = col + tc, row + tr
            if in_board(x, y) and _can_land(board, side, x, y, options):
                targets.append((x, y))
    elif t == PieceType.ELEPHANT:
        for (ec, er), (tc, tr) in ELEPHANT_STEPS:
            ex, ey = col + ec, row + er
            if not in_board(ex, ey) or board.get(ex, ey) is not None:
                continue
            x, y = col + tc, row + tr
            if not in_board(x, y):
                continue
            if not (cross and options.jieqi_elephant_cross):
                if not on_own_half(side, y):
                    continue
            if _can_land(board, side, x, y, options):
                targets.append((x, y))
    elif t == PieceType.ADVISOR:
        for dc, dr in DIAG:
            x, y = col + dc, row + dr
            if not in_board(x, y):
                continue
            if not (cross and options.jieqi_advisor_cross):
                if not in_palace(side, x, y):
                    continue
            if _can_land(board, side, x, y, options):
                targets.append((x, y))
    elif t == PieceType.SOLDIER:
        fwd = Side(side).forward
        cands = [(col, row + fwd)]
        if crossed_river(side, row):
            cands += [(col - 1, row), (col + 1, row)]
        for x, y in cands:
            if in_board(x, y) and _can_land(board, side, x, y, options):
                targets.append((x, y))
    elif t == PieceType.GENERAL:
        for dc, dr in ORTHO:
            x, y = col + dc, row + dr
            if in_palace(side, x, y) and _can_land(board, side, x, y, options):
                targets.append((x, y))
    return targets


def pseudo_moves(board: Board, side, options: Options | None = None,
                 from_pos=None):
    """伪合法着法（未过滤送将 / 飞将）。"""
    options = options or board.options
    side = Side(side)
    result = []
    for c, r, p in board.pieces(side):
        if from_pos is not None and (c, r) != from_pos:
            continue
        eff = board.effective_type(c, r, p)
        for (x, y) in _targets(board, side, c, r, eff, p.dark, options):
            result.append(Move((c, r), (x, y)))
    return result


def generals_facing(board: Board) -> bool:
    """将帅照面（同一列且中间无子）——非法局面。"""
    rg = board.general_pos(Side.RED)
    bg = board.general_pos(Side.BLACK)
    if rg is None or bg is None or rg[0] != bg[0]:
        return False
    lo, hi = sorted((rg[1], bg[1]))
    for r in range(lo + 1, hi):
        if board.get(rg[0], r) is not None:
            return False
    return True


def is_in_check(board: Board, side, options: Options | None = None) -> bool:
    pos = board.general_pos(side)
    if pos is None:
        return True
    for m in pseudo_moves(board, Side(side).opponent, options):
        if m.dst == pos:
            return True
    return False


def legal_moves(board: Board, side, options: Options | None = None,
                from_pos=None):
    options = options or board.options
    side = Side(side)
    legal = []
    for m in pseudo_moves(board, side, options, from_pos):
        res = board.apply(m)
        bad = generals_facing(board) or is_in_check(board, side, options)
        board.undo(res)
        if not bad:
            legal.append(m)
    return legal


def judge(board: Board, side_to_move, options: Options | None = None,
          halfmove_clock=0, fullmove=1, repetition=1) -> GameResult | None:
    options = options or board.options
    side_to_move = Side(side_to_move)

    if board.general_pos(Side.RED) is None:
        return GameResult(True, Side.BLACK, "红帅被吃", "general")
    if board.general_pos(Side.BLACK) is None:
        return GameResult(True, Side.RED, "黑将被吃", "general")

    if not legal_moves(board, side_to_move, options):
        checked = is_in_check(board, side_to_move, options)
        return GameResult(True, side_to_move.opponent,
                          "将死" if checked else "困毙",
                          "checkmate" if checked else "stalemate")

    if options.repetition_draw and repetition >= 3:
        return GameResult(True, None, "三次重复局面", "repetition")
    if options.no_capture_draw > 0 and halfmove_clock >= options.no_capture_draw:
        return GameResult(True, None,
                          f"连续 {options.no_capture_draw} 半回合无吃子", "nocapture")
    if options.max_moves > 0 and fullmove > options.max_moves:
        return GameResult(True, None,
                          f"超过 {options.max_moves} 回合", "maxmoves")
    return None
