# -*- coding: utf-8 -*-
"""飞鲨象棋 PC 版 —— 棋盘常量、棋子与初始布局定义。

坐标约定：``(col, row)``，col 0..8 自左向右，row 0..9 自上向下。
黑方位于上半场 row 0..4，红方位于下半场 row 5..9；红方先行。
"""
from __future__ import annotations

from enum import IntEnum

BOARD_COLS = 9
BOARD_ROWS = 10


class Side(IntEnum):
    RED = 0
    BLACK = 1

    @property
    def opponent(self) -> "Side":
        return Side.BLACK if self == Side.RED else Side.RED

    @property
    def label(self) -> str:
        return "红方" if self == Side.RED else "黑方"

    @property
    def short(self) -> str:
        return "红" if self == Side.RED else "黑"

    @property
    def forward(self) -> int:
        """前进方向 row 增量（红方向上为 -1，黑方向下为 +1）。"""
        return -1 if self == Side.RED else 1


class Mode(IntEnum):
    XIANGQI = 0
    JIEQI = 1

    @property
    def label(self) -> str:
        return "普通象棋" if self == Mode.XIANGQI else "揭棋"


class PieceType(IntEnum):
    GENERAL = 0
    ADVISOR = 1
    ELEPHANT = 2
    HORSE = 3
    CHARIOT = 4
    CANNON = 5
    SOLDIER = 6


# FEN / UCI 用字母：k=将帅 a=士仕 e=象相 h=马 r=车 c=炮 p=卒兵
TYPE_CODE = {
    PieceType.GENERAL: "k",
    PieceType.ADVISOR: "a",
    PieceType.ELEPHANT: "e",
    PieceType.HORSE: "h",
    PieceType.CHARIOT: "r",
    PieceType.CANNON: "c",
    PieceType.SOLDIER: "p",
}
CODE_TYPE = {v: k for k, v in TYPE_CODE.items()}

TYPE_LABEL = {
    PieceType.GENERAL: "将/帅",
    PieceType.ADVISOR: "士/仕",
    PieceType.ELEPHANT: "象/相",
    PieceType.HORSE: "马",
    PieceType.CHARIOT: "车",
    PieceType.CANNON: "炮",
    PieceType.SOLDIER: "卒/兵",
}

NAMES_SIMPLIFIED = {
    Side.RED: {
        PieceType.GENERAL: "帅", PieceType.ADVISOR: "仕",
        PieceType.ELEPHANT: "相", PieceType.HORSE: "马",
        PieceType.CHARIOT: "车", PieceType.CANNON: "炮",
        PieceType.SOLDIER: "兵",
    },
    Side.BLACK: {
        PieceType.GENERAL: "将", PieceType.ADVISOR: "士",
        PieceType.ELEPHANT: "象", PieceType.HORSE: "马",
        PieceType.CHARIOT: "车", PieceType.CANNON: "砲",
        PieceType.SOLDIER: "卒",
    },
}

NAMES_TRADITIONAL = {
    Side.RED: {
        PieceType.GENERAL: "帥", PieceType.ADVISOR: "仕",
        PieceType.ELEPHANT: "相", PieceType.HORSE: "傌",
        PieceType.CHARIOT: "俥", PieceType.CANNON: "炮",
        PieceType.SOLDIER: "兵",
    },
    Side.BLACK: {
        PieceType.GENERAL: "將", PieceType.ADVISOR: "士",
        PieceType.ELEPHANT: "象", PieceType.HORSE: "馬",
        PieceType.CHARIOT: "車", PieceType.CANNON: "砲",
        PieceType.SOLDIER: "卒",
    },
}


def piece_name(side, ptype, traditional: bool = False) -> str:
    table = NAMES_TRADITIONAL if traditional else NAMES_SIMPLIFIED
    return table[Side(side)][PieceType(ptype)]


BACK_RANK = [
    PieceType.CHARIOT, PieceType.HORSE, PieceType.ELEPHANT,
    PieceType.ADVISOR, PieceType.GENERAL, PieceType.ADVISOR,
    PieceType.ELEPHANT, PieceType.HORSE, PieceType.CHARIOT,
]
BACK_ROW = {Side.BLACK: 0, Side.RED: 9}
CANNON_ROW = {Side.BLACK: 2, Side.RED: 7}
SOLDIER_ROW = {Side.BLACK: 3, Side.RED: 6}
CANNON_COLS = (1, 7)
SOLDIER_COLS = (0, 2, 4, 6, 8)
GENERAL_POS = {Side.RED: (4, 9), Side.BLACK: (4, 0)}

CN_DIGITS = "一二三四五六七八九"


def side_of_row(row: int) -> Side:
    return Side.BLACK if row <= 4 else Side.RED


def in_board(col: int, row: int) -> bool:
    return 0 <= col < BOARD_COLS and 0 <= row < BOARD_ROWS


def in_palace(side, col: int, row: int) -> bool:
    if not (3 <= col <= 5):
        return False
    return (0 <= row <= 2) if Side(side) == Side.BLACK else (7 <= row <= 9)


def on_own_half(side, row: int) -> bool:
    return row >= 5 if Side(side) == Side.RED else row <= 4


def crossed_river(side, row: int) -> bool:
    return row <= 4 if Side(side) == Side.RED else row >= 5


def format_clock(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def build_initial_types():
    """标准象棋开局每个格位对应的棋子种类（揭棋暗子的"位置身份"查表）。"""
    layout = {}
    for side in (Side.BLACK, Side.RED):
        row = BACK_ROW[side]
        for col, ptype in enumerate(BACK_RANK):
            layout[(col, row)] = ptype
        for col in CANNON_COLS:
            layout[(col, CANNON_ROW[side])] = PieceType.CANNON
        for col in SOLDIER_COLS:
            layout[(col, SOLDIER_ROW[side])] = PieceType.SOLDIER
    return layout


INITIAL_TYPES = build_initial_types()

# 揭棋：除将帅外的 15 个开局格位，用于随机摆放暗子
DARK_POSITIONS = {
    side: [pos for pos in INITIAL_TYPES
           if side_of_row(pos[1]) == side and pos != GENERAL_POS[side]]
    for side in (Side.RED, Side.BLACK)
}

# 每方 15 枚暗子（将帅明放，不参与洗牌）
DARK_POOL = (
    [PieceType.CHARIOT] * 2 + [PieceType.HORSE] * 2
    + [PieceType.ELEPHANT] * 2 + [PieceType.ADVISOR] * 2
    + [PieceType.CANNON] * 2 + [PieceType.SOLDIER] * 5
)
