# -*- coding: utf-8 -*-
"""中文记谱（纵线：红方汉字一~九自右向左，黑方阿拉伯 1~9 自左向右）。"""
from __future__ import annotations

from .board import Board, Move
from .constants import Side, PieceType, CN_DIGITS, piece_name
from .rules import STRAIGHT_TYPES


def _file_char(side, col):
    return CN_DIGITS[8 - col] if Side(side) == Side.RED else str(col + 1)


def _step_char(side, n):
    if Side(side) == Side.RED and 1 <= n <= 9:
        return CN_DIGITS[n - 1]
    return str(n)


def _prefix(board: Board, side, col, row, eff_type, is_dark):
    """同色同列有同类型棋子时用 前/中/后 区分，否则用纵线数字。"""
    rows = []
    for c, r, p in board.pieces(side):
        if c == col and p.dark == is_dark and \
                board.effective_type(c, r, p) == eff_type:
            rows.append(r)
    if len(rows) <= 1:
        return _file_char(side, col)
    ordered = sorted(rows) if Side(side) == Side.RED else sorted(rows, reverse=True)
    idx = ordered.index(row) if row in ordered else 0
    if len(ordered) == 2:
        return "前" if idx == 0 else "后"
    labels = ["前", "中", "后", "四", "五", "六", "七", "八", "九"]
    return labels[min(idx, len(labels) - 1)]


def describe(board: Board, move: Move, traditional: bool = False) -> str:
    """生成一步棋的中文记谱；揭棋暗子额外附注翻出的真实身份。"""
    sc, sr = move.src
    dc, dr = move.dst
    p = board.get(sc, sr)
    if p is None:
        return "??"
    side = Side(p.side)
    eff = board.effective_type(sc, sr, p)
    name = ("暗" if p.dark else "") + piece_name(side, eff, traditional)
    prefix = _prefix(board, side, sc, sr, eff, p.dark)

    dy = dr - sr
    if dy == 0:
        action, target = "平", _file_char(side, dc)
    else:
        action = "进" if dy * side.forward > 0 else "退"
        if PieceType(eff) in STRAIGHT_TYPES:
            target = _step_char(side, abs(dy))
        else:
            target = _file_char(side, dc)
    text = name + prefix + action + target
    if p.dark:
        text += "〔翻:" + piece_name(side, p.ptype, traditional) + "〕"
    return text


def uci_to_move(text: str, rank_flip: bool = True):
    """UCI/UCCI 着法（如 h2e2）转内部 Move，失败返回 None。"""
    s = text.strip().lower()
    if len(s) < 4 or not s[0].isalpha() or not s[2].isalpha():
        return None
    try:
        c1 = ord(s[0]) - ord("a")
        c2 = ord(s[2]) - ord("a")
        r1 = int(s[1])
        r2 = int(s[3])
    except ValueError:
        return None
    if rank_flip:
        r1, r2 = 9 - r1, 9 - r2
    from .constants import in_board
    if in_board(c1, r1) and in_board(c2, r2):
        return Move((c1, r1), (c2, r2))
    return None


def move_to_uci(move: Move, rank_flip: bool = True) -> str:
    (c1, r1), (c2, r2) = move.src, move.dst
    if rank_flip:
        r1, r2 = 9 - r1, 9 - r2
    return f"{chr(ord('a') + c1)}{r1}{chr(ord('a') + c2)}{r2}"
