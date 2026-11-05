# -*- coding: utf-8 -*-
"""棋盘状态：棋子、着法、FEN/JSON 序列化。

揭棋要点：
* 每枚暗子持有"真实身份"(ptype)，但走第一步时按其**所在格位的开局身份**
  (INITIAL_TYPES) 行动；走子后自动揭开，此后按真实身份行动。
* 暗子被吃后保持背面朝上（dark=True），不公开身份。
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .constants import (
    BOARD_COLS, BOARD_ROWS, Side, Mode, PieceType, TYPE_CODE, CODE_TYPE,
    DARK_POOL, DARK_POSITIONS, GENERAL_POS, INITIAL_TYPES,
    in_board, side_of_row,
)
from .options import Options


@dataclass
class Piece:
    side: int
    ptype: int
    dark: bool = False
    uid: int = 0

    def clone(self) -> "Piece":
        return Piece(self.side, self.ptype, self.dark, self.uid)

    def to_dict(self) -> dict:
        return {"s": int(self.side), "t": int(self.ptype),
                "d": bool(self.dark), "u": self.uid}

    @classmethod
    def from_dict(cls, d: dict) -> "Piece":
        return cls(int(d["s"]), int(d["t"]), bool(d["d"]), int(d.get("u", 0)))


@dataclass(frozen=True)
class Move:
    src: tuple
    dst: tuple

    def to_list(self):
        return [self.src[0], self.src[1], self.dst[0], self.dst[1]]

    @classmethod
    def from_list(cls, seq):
        return cls((int(seq[0]), int(seq[1])), (int(seq[2]), int(seq[3])))


@dataclass
class MoveResult:
    move: Move
    moved: Piece
    captured: Piece | None
    was_dark: bool


class Board:
    def __init__(self, mode=Mode.XIANGQI, options: Options | None = None,
                 seed=None, fen: str | None = None):
        self.mode = Mode(mode)
        self.options = options or Options()
        self.squares = [[None] * BOARD_COLS for _ in range(BOARD_ROWS)]
        self.captured = {int(Side.RED): [], int(Side.BLACK): []}
        self._next_uid = 1
        self.seed = seed
        self._fen_side = Side.RED
        if fen:
            self.from_fen(fen)
        else:
            self.setup(seed)

    # ---------- 基础存取 ----------
    def _mk(self, side, ptype, dark=False) -> Piece:
        p = Piece(int(side), int(ptype), bool(dark), self._next_uid)
        self._next_uid += 1
        return p

    def get(self, col, row):
        return self.squares[row][col] if in_board(col, row) else None

    def put(self, col, row, piece: Piece | None):
        if in_board(col, row):
            self.squares[row][col] = piece

    def pieces(self, side=None):
        for r in range(BOARD_ROWS):
            for c in range(BOARD_COLS):
                p = self.squares[r][c]
                if p is not None and (side is None or p.side == side):
                    yield c, r, p

    def general_pos(self, side):
        for c, r, p in self.pieces(side):
            if p.ptype == PieceType.GENERAL and not p.dark:
                return c, r
        return None

    def effective_type(self, col, row, piece: Piece) -> int:
        """暗子按其所在开局格位的身份行动；明子按真实身份。"""
        if piece.dark:
            return int(INITIAL_TYPES.get((col, row), piece.ptype))
        return int(piece.ptype)

    def setup(self, seed=None):
        self.captured = {int(Side.RED): [], int(Side.BLACK): []}
        self.squares = [[None] * BOARD_COLS for _ in range(BOARD_ROWS)]
        self._next_uid = 1
        self.seed = seed
        if seed in (None, ""):
            rng = random.Random()
        else:
            rng = random.Random(seed)

        if self.mode == Mode.XIANGQI:
            for (c, r), ptype in INITIAL_TYPES.items():
                self.squares[r][c] = self._mk(side_of_row(r), ptype)
        else:
            # 将 / 帅 明放于九宫原点
            for side, (c, r) in GENERAL_POS.items():
                self.squares[r][c] = self._mk(side, PieceType.GENERAL)
            # 其余 15 子洗牌后暗放在开局格位
            for side in (Side.RED, Side.BLACK):
                pool = list(DARK_POOL)
                rng.shuffle(pool)
                for (c, r), ptype in zip(DARK_POSITIONS[side], pool):
                    self.squares[r][c] = self._mk(side, ptype, dark=True)

    # ---------- 着法 ----------
    def apply(self, move: Move) -> MoveResult:
        sc, sr = move.src
        dc, dr = move.dst
        p = self.get(sc, sr)
        if p is None:
            raise ValueError("起点无棋子")
        captured = self.get(dc, dr)
        if captured is not None:
            self.captured[int(p.side)].append(captured)
        self.squares[sr][sc] = None
        was_dark = p.dark
        p.dark = False
        self.squares[dr][dc] = p
        return MoveResult(move, p, captured, was_dark)

    def undo(self, res: MoveResult):
        sc, sr = res.move.src
        dc, dr = res.move.dst
        res.moved.dark = res.was_dark
        self.squares[sr][sc] = res.moved
        self.squares[dr][dc] = res.captured
        if res.captured is not None:
            self.captured[int(res.moved.side)].pop()

    def clone(self) -> "Board":
        b = Board.__new__(Board)
        b.mode = self.mode
        b.options = self.options
        b.squares = [[p.clone() if p else None for p in row]
                     for row in self.squares]
        b.captured = {k: [p.clone() for p in v]
                      for k, v in self.captured.items()}
        b._next_uid = self._next_uid
        b.seed = self.seed
        b._fen_side = self._fen_side
        return b

    def hash_key(self, side_to_move=Side.RED) -> str:
        parts = []
        for r in range(BOARD_ROWS):
            for c in range(BOARD_COLS):
                p = self.squares[r][c]
                if p is None:
                    parts.append(".")
                else:
                    parts.append(("D" if p.dark else "") +
                                 ("R" if p.side == Side.RED else "B") +
                                 TYPE_CODE[PieceType(p.ptype)])
        parts.append("w" if side_to_move == Side.RED else "b")
        return "".join(parts)

    # ---------- FEN：AB-JChess / UCI 揭棋引擎原生格式 ----------
    # 明子 R=车 N=马 B=象 A=士 K=将帅 C=炮 P=卒兵（大写红 / 小写黑）
    # 暗子 X / x；第 3 段是"暗子身份池"（各身份剩余数量），'-' 表示无暗子
    # 例：xxxxkxxxx/9/.../XXXXKXXXX w R2A2C2P5N2B2r2a2c2p5n2b2 0 1
    AB_CODE = {
        PieceType.GENERAL: "K", PieceType.ADVISOR: "A",
        PieceType.ELEPHANT: "B", PieceType.HORSE: "N",
        PieceType.CHARIOT: "R", PieceType.CANNON: "C",
        PieceType.SOLDIER: "P",
    }
    AB_DECODE = {
        "k": PieceType.GENERAL, "a": PieceType.ADVISOR,
        "b": PieceType.ELEPHANT, "e": PieceType.ELEPHANT,
        "n": PieceType.HORSE, "h": PieceType.HORSE,
        "r": PieceType.CHARIOT, "c": PieceType.CANNON,
        "p": PieceType.SOLDIER,
    }
    POOL_ORDER = [PieceType.CHARIOT, PieceType.ADVISOR, PieceType.CANNON,
                  PieceType.SOLDIER, PieceType.HORSE, PieceType.ELEPHANT]

    def to_fen(self, side_to_move=Side.RED, halfmove=0, fullmove=1) -> str:
        rows = []
        for r in range(BOARD_ROWS):
            s, empty = "", 0
            for c in range(BOARD_COLS):
                p = self.squares[r][c]
                if p is None:
                    empty += 1
                    continue
                if empty:
                    s += str(empty)
                    empty = 0
                if p.dark:
                    s += "X" if p.side == Side.RED else "x"
                else:
                    ch = self.AB_CODE[PieceType(p.ptype)]
                    s += ch if p.side == Side.RED else ch.lower()
            if empty:
                s += str(empty)
            rows.append(s)
        # 池只暴露"身份数量分布"，不暴露每枚暗子的真实身份
        counts = {}
        for _, _, p in self.pieces():
            if p.dark:
                counts[(p.side, p.ptype)] = counts.get((p.side, p.ptype), 0) + 1
        pool = ""
        for side in (Side.RED, Side.BLACK):
            for ptype in self.POOL_ORDER:
                n = counts.get((side, ptype), 0)
                if n:
                    ch = self.AB_CODE[ptype]
                    pool += (ch if side == Side.RED else ch.lower()) + str(n)
        side_char = "w" if side_to_move == Side.RED else "b"
        return "/".join(rows) + f" {side_char} {pool or '-'} {halfmove} {fullmove}"

    def from_fen(self, fen: str):
        parts = fen.strip().split()
        board_part = parts[0] if parts else ""
        self._fen_side = Side.BLACK if (len(parts) > 1 and
                                       parts[1].lower() in ("b", "black")) else Side.RED
        pool_str = parts[2] if len(parts) > 2 else "-"
        self._fen_halfmove = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        self._fen_fullmove = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 1
        self.squares = [[None] * BOARD_COLS for _ in range(BOARD_ROWS)]
        self.captured = {int(Side.RED): [], int(Side.BLACK): []}
        self._next_uid = 1
        dark_marks = []  # (c, r, side)
        for r, rowstr in enumerate(board_part.split("/")[:BOARD_ROWS]):
            c = 0
            for ch in rowstr:
                if ch.isdigit():
                    c += int(ch)
                elif ch in ("X", "x", "D", "d", "*"):
                    s = (Side.RED if ch in ("X", "D") else
                         Side.BLACK if ch in ("x", "d") else side_of_row(r))
                    dark_marks.append((c, r, s))
                    c += 1
                else:
                    ptype = self.AB_DECODE.get(ch.lower())
                    if ptype is not None and c < BOARD_COLS:
                        s = Side.RED if ch.isupper() else Side.BLACK
                        self.squares[r][c] = self._mk(s, ptype)
                    c += 1
        # 解析暗子身份池
        pools = {Side.RED: [], Side.BLACK: []}
        if pool_str not in ("", "-"):
            i = 0
            while i < len(pool_str):
                ch = pool_str[i]
                i += 1
                ptype = self.AB_DECODE.get(ch.lower())
                if ptype is None:
                    continue
                j = i
                while j < len(pool_str) and pool_str[j].isdigit():
                    j += 1
                n = int(pool_str[i:j]) if j > i else 1
                i = j
                pools[Side.RED if ch.isupper() else Side.BLACK] += [ptype] * n
        # 暗子真实身份属隐藏信息：按池随机分配（数量不足时用开局池补齐）
        for side in (Side.RED, Side.BLACK):
            marks = [(c, r) for (c, r, s) in dark_marks if s == side]
            pool = list(pools[side])
            if len(pool) < len(marks):
                pool += list(DARK_POOL)[:len(marks) - len(pool)]
            random.shuffle(pool)
            for (c, r), ptype in zip(marks, pool):
                self.squares[r][c] = self._mk(side, ptype, dark=True)
        return self._fen_side

    # ---------- JSON ----------
    def to_dict(self) -> dict:
        return {
            "mode": int(self.mode),
            "seed": self.seed,
            "squares": [[p.to_dict() if p else None for p in row]
                        for row in self.squares],
            "captured": {str(k): [p.to_dict() for p in v]
                         for k, v in self.captured.items()},
            "next_uid": self._next_uid,
            "fen_side": int(self._fen_side),
        }

    @classmethod
    def from_dict(cls, data: dict, options: Options | None = None) -> "Board":
        b = cls.__new__(cls)
        b.mode = Mode(int(data.get("mode", 0)))
        b.options = options or Options()
        b.seed = data.get("seed")
        b.squares = [[Piece.from_dict(x) if x else None for x in row]
                     for row in data["squares"]]
        b.captured = {int(k): [Piece.from_dict(x) for x in v]
                      for k, v in data.get("captured", {}).items()}
        b.captured.setdefault(int(Side.RED), [])
        b.captured.setdefault(int(Side.BLACK), [])
        b._next_uid = int(data.get("next_uid", 1))
        b._fen_side = Side(int(data.get("fen_side", 0)))
        return b

    def ascii(self) -> str:
        """调试用文本棋盘。"""
        lines = []
        for r in range(BOARD_ROWS):
            line = ""
            for c in range(BOARD_COLS):
                p = self.squares[r][c]
                if p is None:
                    line += " . "
                elif p.dark:
                    line += " # "
                else:
                    from .constants import piece_name
                    line += " " + piece_name(p.side, p.ptype) + " "
            lines.append(f"{r:2d} {line}")
        lines.append("    " + " ".join(f"{c:2d}" for c in range(BOARD_COLS)))
        return "\n".join(lines)
