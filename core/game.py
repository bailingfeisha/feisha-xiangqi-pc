# -*- coding: utf-8 -*-
"""对局管理：分支棋谱树、悔棋/跳步、计时、存读档、胜负状态。

棋谱采用树结构（参考鹏飞象棋的分支棋谱）：从任意历史节点走出新着时
不覆盖原主线，而是新增一条变例；悔棋、拆棋的招数都完整保留。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .board import Board, Move, Piece
from .constants import Side, Mode
from .notation import describe
from .options import Options
from .rules import GameResult, judge, legal_moves, is_in_check

APP_TAG = "飞鲨象棋"
SAVE_VERSION = 2


@dataclass
class GameNode:
    move: Move | None
    parent: "GameNode | None"
    children: list = field(default_factory=list)
    board: Board = None
    notation: str = ""
    side: int = -1                 # 本节点的走子方；root 为 -1
    captured: Piece | None = None
    was_dark: bool = False
    ply: int = 0
    halfmove_clock: int = 0
    fullmove: int = 1
    comment: str = ""

    @property
    def side_to_move(self) -> Side:
        return Side(self.side).opponent if self.side >= 0 else Side.RED


class Game:
    def __init__(self, options: Options | None = None, mode=Mode.XIANGQI):
        self.options = options or Options()
        self.mode = Mode(mode)
        self.clocks = {int(Side.RED): 0.0, int(Side.BLACK): 0.0}
        self._manual_result: GameResult | None = None
        self._manual_node: GameNode | None = None
        self.result: GameResult | None = None
        self.reset(self.mode)

    # ---------- 初始化 ----------
    def reset(self, mode=None, seed=None, fen: str | None = None,
              board: Board | None = None):
        if mode is not None:
            self.mode = Mode(mode)
        if board is not None:
            root_board = board.clone()
            root_board.options = self.options
            self.root_side = getattr(root_board, "_fen_side", Side.RED)
        elif fen:
            root_board = Board(self.mode, self.options, fen=fen)
            self.root_side = root_board._fen_side
        else:
            use_seed = seed if seed not in (None, "") else (self.options.seed or None)
            root_board = Board(self.mode, self.options, seed=use_seed)
            self.root_side = Side.RED
        self.mode = root_board.mode
        self.root = GameNode(
            None, None, board=root_board,
            halfmove_clock=getattr(root_board, "_fen_halfmove", 0),
            fullmove=getattr(root_board, "_fen_fullmove", 1))
        self.current = self.root
        self.clocks = {int(Side.RED): 0.0, int(Side.BLACK): 0.0}
        self._manual_result = None
        self._manual_node = None
        self.result = None
        self._positions = {}
        self._recount_positions()
        self._update_result()

    # ---------- 派生属性 ----------
    @property
    def board(self) -> Board:
        return self.current.board

    @property
    def side_to_move(self) -> Side:
        if self.current is self.root:
            return Side(self.root_side)
        return Side(self.current.side).opponent

    @property
    def ply(self) -> int:
        return self.current.ply

    @property
    def fullmove(self) -> int:
        return self.current.fullmove

    @property
    def in_check(self) -> bool:
        return is_in_check(self.board, self.side_to_move, self.options)

    @property
    def is_latest(self) -> bool:
        return not self.current.children

    def legal_moves(self, from_pos=None):
        return legal_moves(self.board, self.side_to_move, self.options, from_pos)

    def targets_from(self, pos):
        return [m.dst for m in self.legal_moves(from_pos=pos)]

    def is_legal(self, move: Move) -> bool:
        return move in self.legal_moves()

    # ---------- 走子 ----------
    def play(self, move: Move) -> bool:
        if self.result is not None or not self.is_legal(move):
            return False
        # 已有相同变例则直接复用
        for child in self.current.children:
            if child.move == move:
                self.current = child
                self._after_navigate()
                return True

        before = self.current.board
        nb = before.clone()
        res = nb.apply(move)
        side = self.side_to_move
        node = GameNode(
            move=move, parent=self.current, board=nb,
            notation=describe(before, move,
                              self.options.piece_style == "traditional"),
            side=int(side), captured=res.captured, was_dark=res.was_dark,
            ply=self.current.ply + 1,
            halfmove_clock=0 if (res.captured or res.was_dark)
            else self.current.halfmove_clock + 1,
            fullmove=self.current.fullmove + (1 if side == Side.BLACK else 0),
        )
        self.current.children.append(node)
        self.current = node
        key = nb.hash_key(self.side_to_move)
        self._positions[key] = self._positions.get(key, 0) + 1
        self._repetition = self._positions[key]
        self._update_result()
        return True

    # ---------- 导航 / 分支 ----------
    def path_to(self, node: GameNode):
        result = []
        n = node
        while n is not None:
            result.append(n)
            n = n.parent
        return list(reversed(result))

    def display_line(self):
        """棋谱显示用：从开局到当前节点，再沿主变延伸到末。"""
        line = self.path_to(self.current)
        node = self.current
        while node.children:
            node = node.children[0]
            line.append(node)
        return line

    def main_line(self):
        line = [self.root]
        node = self.root
        while node.children:
            node = node.children[0]
            line.append(node)
        return line

    def goto(self, node: GameNode):
        if node is not None:
            self.current = node
            self._after_navigate()

    def goto_index(self, index: int):
        line = self.display_line()
        if 0 <= index < len(line):
            self.goto(line[index])

    def undo(self):
        if self.current.parent is not None:
            self.goto(self.current.parent)

    def redo(self):
        if self.current.children:
            self.goto(self.current.children[0])

    def variations(self):
        return self.current.children

    def switch_variation(self, delta: int = 1):
        parent = self.current.parent
        if parent is None or len(parent.children) < 2:
            return
        idx = parent.children.index(self.current)
        self.goto(parent.children[(idx + delta) % len(parent.children)])

    def delete_branch(self):
        parent = self.current.parent
        if parent is None:
            return
        parent.children.remove(self.current)
        self.goto(parent)

    def set_comment(self, text: str):
        self.current.comment = text

    def _after_navigate(self):
        self._recount_positions()
        self._update_result()

    def _recount_positions(self):
        self._positions = {self.root.board.hash_key(Side(self.root_side)): 1}
        for node in self.path_to(self.current)[1:]:
            key = node.board.hash_key(node.side_to_move)
            self._positions[key] = self._positions.get(key, 0) + 1
        self._repetition = self._positions.get(
            self.board.hash_key(self.side_to_move), 1)

    def _update_result(self):
        if self._manual_result is not None and self._manual_node is self.current:
            self.result = self._manual_result
        else:
            self.result = judge(
                self.board, self.side_to_move, self.options,
                self.current.halfmove_clock, self.current.fullmove,
                getattr(self, "_repetition", 1))

    # ---------- 人工终局 / 计时 ----------
    def _finish(self, result: GameResult):
        self._manual_result = result
        self._manual_node = self.current
        self.result = result

    def resign(self, side):
        self._finish(GameResult(True, Side(side).opponent,
                               f"{Side(side).label}认输", "resign"))

    def agree_draw(self):
        self._finish(GameResult(True, None, "双方同意和棋", "agreement"))

    def timeout(self, side):
        self._finish(GameResult(True, Side(side).opponent,
                               f"{Side(side).label}超时", "timeout"))

    def tick(self, dt: float):
        if self.result is not None or not self.options.use_clock:
            return
        side = self.side_to_move
        self.clocks[int(side)] += dt
        if self.clocks[int(side)] >= self.options.minutes_per_side * 60:
            self.timeout(side)

    def clock_texts(self):
        from .constants import format_clock
        return (format_clock(self.clocks[int(Side.RED)]),
                format_clock(self.clocks[int(Side.BLACK)]))

    # ---------- 局面编辑 ----------
    def replace_board(self, board: Board):
        self.reset(mode=board.mode, board=board)

    # ---------- 存读档 ----------
    def _serialize_node(self, node: GameNode) -> dict:
        return {
            "move": node.move.to_list() if node.move else None,
            "notation": node.notation,
            "comment": node.comment,
            "children": [self._serialize_node(ch) for ch in node.children],
        }

    def to_dict(self) -> dict:
        path = self.path_to(self.current)
        indices = []
        for i in range(1, len(path)):
            parent = path[i - 1]
            indices.append(parent.children.index(path[i]))
        return {
            "app": APP_TAG, "version": SAVE_VERSION,
            "mode": int(self.mode),
            "options": self.options.to_dict(),
            "root_board": self.root.board.to_dict(),
            "root_side": int(self.root_side),
            "tree": self._serialize_node(self.root),
            "current_path": indices,
            "clocks": {str(k): v for k, v in self.clocks.items()},
            "manual_result": (self._manual_result.to_dict()
                              if self._manual_result else None),
        }

    @classmethod
    def from_dict(cls, data: dict, options: Options | None = None) -> "Game":
        if data.get("app") != APP_TAG:
            raise ValueError("不是飞鲨象棋对局文件")
        opts = options or Options.from_dict(data.get("options"))
        g = cls.__new__(cls)
        g.options = opts
        g.mode = Mode(int(data.get("mode", 0)))
        g.root_side = Side(int(data.get("root_side", 0)))
        g.root = GameNode(None, None,
                          board=Board.from_dict(data["root_board"], opts))
        g.clocks = {int(k): float(v)
                    for k, v in data.get("clocks", {}).items()}
        g.clocks.setdefault(int(Side.RED), 0.0)
        g.clocks.setdefault(int(Side.BLACK), 0.0)
        g._manual_result = GameResult.from_dict(data.get("manual_result"))
        g._manual_node = None
        g.result = None

        def build(parent: GameNode, d: dict):
            node = parent
            if d.get("move"):
                mv = Move.from_list(d["move"])
                nb = parent.board.clone()
                res = nb.apply(mv)
                side = (Side(g.root_side) if parent is g.root
                        else Side(parent.side).opponent)
                node = GameNode(
                    move=mv, parent=parent, board=nb,
                    notation=d.get("notation", ""),
                    side=int(side), captured=res.captured,
                    was_dark=res.was_dark,
                    ply=parent.ply + 1,
                    halfmove_clock=0 if (res.captured or res.was_dark)
                    else parent.halfmove_clock + 1,
                    fullmove=parent.fullmove + (1 if side == Side.BLACK else 0),
                    comment=d.get("comment", ""),
                )
                parent.children.append(node)
            for cd in d.get("children", []):
                build(node, cd)

        build(g.root, data.get("tree", {}))
        node = g.root
        for idx in data.get("current_path", []):
            if 0 <= idx < len(node.children):
                node = node.children[idx]
            else:
                break
        g.current = node
        if g._manual_result is not None:
            g._manual_node = node
        g._recount_positions()
        g._update_result()
        return g
