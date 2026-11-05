# -*- coding: utf-8 -*-
"""检测框 → 棋盘局面（只识别主棋盘，忽略 tray / tray_badge 计数器）。

流程：
  1. 取 board 类框（没有就用棋子外接框外扩）估计 9x10 交叉点网格；
  2. 用棋子中心做一维最小二乘回归校准 origin / step（容忍皮肤边距差异）；
  3. 每个棋子框吸附到最近交叉点，冲突取置信度最高；
  4. 明子直接映射 core 棋子，back_red/back_black 映射揭棋暗子；
  5. 揭棋暗子身份池 = 初始 15 子 - 已翻开明子，固定种子分配保证 FEN 稳定；
  6. 输出 Board 与 AB-JChess 5 段 FEN，供引擎分析 / 导入主棋盘。
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from core.board import Board, Move
from core.constants import (
    BOARD_COLS, BOARD_ROWS, Side, Mode, PieceType, DARK_POOL,
)
from core.options import Options
from vision.model import Detection

# YOLO 类别 → (方, 棋子类型)
CLASS_TO_PIECE = {
    0: (Side.RED, PieceType.SOLDIER),     # redBing
    1: (Side.RED, PieceType.CANNON),      # redPao
    2: (Side.RED, PieceType.HORSE),       # redMa
    3: (Side.RED, PieceType.CHARIOT),     # redJu
    4: (Side.RED, PieceType.ELEPHANT),    # redXiang
    5: (Side.RED, PieceType.ADVISOR),     # redShi
    6: (Side.RED, PieceType.GENERAL),     # redShuai
    7: (Side.BLACK, PieceType.SOLDIER),   # blackBing
    8: (Side.BLACK, PieceType.CANNON),    # blackPao
    9: (Side.BLACK, PieceType.HORSE),     # blackMa
    10: (Side.BLACK, PieceType.CHARIOT),  # blackJu
    11: (Side.BLACK, PieceType.ELEPHANT), # blackXiang
    12: (Side.BLACK, PieceType.ADVISOR),  # blackShi
    13: (Side.BLACK, PieceType.GENERAL),  # blackShuai
}
DARK_SIDE = {14: Side.RED, 15: Side.BLACK}


@dataclass
class GridFit:
    x0: float                 # 第 0 列交叉点 x
    y0: float                 # 第 0 行交叉点 y
    sx: float                 # 列间距
    sy: float                 # 行间距
    board_box: tuple          # (x1,y1,x2,y2)
    residual: float = 0.0     # 拟合平均残差（像素）
    n_anchors: int = 0

    def point(self, col: int, row: int) -> tuple[float, float]:
        return self.x0 + col * self.sx, self.y0 + row * self.sy

    def nearest(self, x: float, y: float):
        c = int(round((x - self.x0) / max(1e-6, self.sx)))
        r = int(round((y - self.y0) / max(1e-6, self.sy)))
        c = max(0, min(BOARD_COLS - 1, c))
        r = max(0, min(BOARD_ROWS - 1, r))
        px, py = self.point(c, r)
        return c, r, float(np.hypot(x - px, y - py))


@dataclass
class Reconstruction:
    board: Board
    fen: str
    grid: GridFit | None
    assignments: dict = field(default_factory=dict)
    ignored: int = 0
    signature: str = ""

    @property
    def n_pieces(self) -> int:
        return len(self.assignments)

    @property
    def n_dark(self) -> int:
        return sum(1 for d in self.assignments.values() if d.is_dark)


def _fit_axis(values: list[float], n: int, lo: float, hi: float,
              step_candidates: list[float]):
    """一维等距网格拟合：分配 → 最小二乘回归 origin/step，多候选取残差最小。"""
    vals = np.asarray(values, dtype=float)
    best = None
    for step0 in step_candidates:
        if step0 <= 1:
            continue
        origin = (lo + hi) / 2.0 - (n - 1) * step0 / 2.0
        step = step0
        if vals.size >= 3:
            for _ in range(4):
                idx = np.clip(np.rint((vals - origin) / step), 0, n - 1)
                good = np.abs(vals - (origin + idx * step)) < 0.8 * step
                if int(good.sum()) < 3:
                    break
                ii, vv = idx[good], vals[good]
                A = np.vstack([np.ones_like(ii), ii]).T
                try:
                    sol = np.linalg.lstsq(A, vv, rcond=None)[0]
                except np.linalg.LinAlgError:
                    break
                origin = float(sol[0])
                step = float(np.clip(sol[1], step0 * 0.75, step0 * 1.30))
        if vals.size:
            idx = np.clip(np.rint((vals - origin) / step), 0, n - 1)
            errs = np.abs(vals - (origin + idx * step))
            keep = max(1, int(len(errs) * 0.8))
            residual = float(np.mean(np.sort(errs)[:keep]))
        else:
            residual = 999.0
        if best is None or residual < best[0]:
            best = (residual, origin, step)
    return best or (999.0, lo, (hi - lo) / max(1, n - 1))


def fit_grid(detections: list[Detection]) -> GridFit | None:
    boards = [d for d in detections if d.is_board]
    pieces = [d for d in detections if d.is_piece and
              d.cls not in (17, 18)]
    if not pieces:
        return None
    if boards:
        b = max(boards, key=lambda d: d.conf)
        x1, y1, x2, y2 = b.x1, b.y1, b.x2, b.y2
    else:
        x1 = min(d.x1 for d in pieces)
        y1 = min(d.y1 for d in pieces)
        x2 = max(d.x2 for d in pieces)
        y2 = max(d.y2 for d in pieces)
        mx, my = (x2 - x1) * 0.12, (y2 - y1) * 0.12
        x1, y1, x2, y2 = x1 - mx, y1 - my, x2 + mx, y2 + my
    w, h = max(1.0, x2 - x1), max(1.0, y2 - y1)

    xs = [d.cx for d in pieces]
    ys = [d.cy for d in pieces]
    rx = _fit_axis(xs, BOARD_COLS, x1, x2, [w / 9.0, w / 8.0])
    ry = _fit_axis(ys, BOARD_ROWS, y1, y2, [h / 10.0, h / 9.0])
    grid = GridFit(rx[1], ry[1], rx[2], ry[2], (x1, y1, x2, y2),
                   (rx[0] + ry[0]) / 2.0, len(pieces))
    return grid


def assign_pieces(detections: list[Detection], grid: GridFit,
                  tolerance: float = 0.58) -> tuple[dict, int]:
    """把棋子吸附到交叉点；返回 {(c,r): Detection} 与被丢弃数量。"""
    out: dict = {}
    ignored = 0
    limit = tolerance * min(grid.sx, grid.sy)
    for d in detections:
        if not d.is_piece or d.cls in (17, 18):
            continue
        c, r, dist = grid.nearest(d.cx, d.cy)
        if dist > limit:
            ignored += 1
            continue
        key = (c, r)
        prev = out.get(key)
        if prev is None or d.conf > prev.conf:
            out[key] = d
    return out, ignored


def _pool_for_side(mode, assignments) -> dict:
    """揭棋暗子身份池 = 初始 15 子 - 已翻开明子（将帅明放，本来不在池里）。"""
    counts = {Side.RED: Counter(DARK_POOL), Side.BLACK: Counter(DARK_POOL)}
    if mode != Mode.JIEQI:
        return counts
    for d in assignments.values():
        if d.cls in CLASS_TO_PIECE:
            side, ptype = CLASS_TO_PIECE[d.cls]
            if ptype != PieceType.GENERAL and counts[side][ptype] > 0:
                counts[side][ptype] -= 1
    return counts


def reconstruct(detections: list[Detection], mode=Mode.XIANGQI,
                options: Options | None = None, flip: bool = False,
                side_to_move=Side.RED, halfmove: int = 0,
                fullmove: int = 1) -> Reconstruction | None:
    """检测列表 → Board + FEN。flip=True 表示目标窗口红方在上，需要旋转 180°。"""
    grid = fit_grid(detections)
    if grid is None:
        return None
    assignments, ignored = assign_pieces(detections, grid)
    if not assignments:
        return None
    mode = Mode(mode)
    options = options or Options()
    board = Board(mode, options)
    board.squares = [[None] * BOARD_COLS for _ in range(BOARD_ROWS)]
    board.captured = {int(Side.RED): [], int(Side.BLACK): []}
    board._next_uid = 1

    mapped: dict = {}
    for (c, r), d in assignments.items():
        cc, rr = (BOARD_COLS - 1 - c, BOARD_ROWS - 1 - r) if flip else (c, r)
        mapped[(cc, rr)] = d

    visible = [d for d in mapped.values() if d.cls in CLASS_TO_PIECE]
    darks = {Side.RED: [], Side.BLACK: []}
    for key, d in mapped.items():
        if d.cls in CLASS_TO_PIECE:
            side, ptype = CLASS_TO_PIECE[d.cls]
            board.put(key[0], key[1], board._mk(side, ptype))
        elif d.cls in DARK_SIDE:
            darks[DARK_SIDE[d.cls]].append(key)

    counts = _pool_for_side(mode, mapped)
    for side in (Side.RED, Side.BLACK):
        # 必须排序：YOLO 输出顺序逐帧可能变化，否则暗子身份分配会抖动，
        # 导致连续帧签名不一致 → 稳定帧永远凑不满 → 揭棋自动下棋不走棋。
        slots = sorted(darks[side])
        if not slots:
            continue
        pool: list = []
        for ptype, n in counts[side].items():
            pool += [ptype] * max(0, n)
        while len(pool) < len(slots):
            pool += list(DARK_POOL)
        if len(pool) > len(slots):
            # 被吃掉的暗子身份永远不可见，池会多出来；按等距抽样确定性裁减，
            # 既保持身份分布大致均匀，又保证同一局面逐帧 FEN 完全一致。
            step = len(pool) / max(1, len(slots))
            pool = [pool[min(len(pool) - 1, int(i * step))]
                    for i in range(len(slots))]
        # 固定种子：同一局面连续帧必须生成一致的 FEN / 签名
        random.Random(0).shuffle(pool)
        for key, ptype in zip(slots, pool):
            board.put(key[0], key[1], board._mk(side, ptype, dark=True))

    fen = board.to_fen(side_to_move, halfmove, fullmove)
    # 签名只编码「能被看见」的信息：暗子隐藏身份是程序猜的，明子类别也可能
    # 因模型抖动而变，都不能参与稳定性/变化判定。
    return Reconstruction(board, fen, grid, mapped, ignored,
                          observable_signature(board))


def observable_signature(board: Board) -> str:
    """可观测局面签名：90 格，'.' 空 / 'X''x' 暗子 / 'M''m' 明子。

    刻意不含暗子的真实身份（属于隐藏信息，由程序按池随机分配）与明子的
    具体兵种（识别抖动会导致逐帧变化）。这样连续帧签名稳定，且任何一步
    走子（含揭棋翻子、吃子）都会真实反映在签名变化上。
    """
    out = []
    for r in range(BOARD_ROWS):
        for c in range(BOARD_COLS):
            p = board.get(c, r)
            if p is None:
                out.append(".")
            elif p.dark:
                out.append("X" if p.side == Side.RED else "x")
            else:
                out.append("M" if p.side == Side.RED else "m")
    return "".join(out)


def position_signature(board: Board) -> str:
    """兼容旧调用：返回可观测签名。"""
    return observable_signature(board)


def detect_view(detections, grid) -> tuple[bool, Side, str]:
    """从检测框推断目标窗口的显示朝向，进而推断「我方执子」。

    约定：象棋软件一般把「我方」放在屏幕下方。
      * 红帅在下方 / 黑将在上方 → 红在下 → 我方执红，无需翻转；
      * 红帅在上方 / 黑将在下方 → 黑在下 → 我方执黑，需要 flip=True。

    返回 (是否翻转, 我方执子, 依据说明)。将帅优先（始终在九宫，最稳），
    否则退化到双方棋子的平均纵坐标。
    """
    red_gen = [d for d in detections if d.cls == 6]
    black_gen = [d for d in detections if d.cls == 13]
    if red_gen and black_gen:
        ry = max(red_gen, key=lambda d: d.conf).cy
        by = max(black_gen, key=lambda d: d.conf).cy
        if abs(ry - by) > 1.0:
            flip = ry < by
            side = Side.BLACK if flip else Side.RED
            return flip, side, f"将帅位置（红帅 y={ry:.0f} / 黑将 y={by:.0f}）"
    ry_all = [d.cy for d in detections if 0 <= d.cls <= 6]
    by_all = [d.cy for d in detections if 7 <= d.cls <= 13]
    if len(ry_all) >= 2 and len(by_all) >= 2:
        ry = float(np.mean(ry_all))
        by = float(np.mean(by_all))
        margin = 0.2 * (grid.sy if grid is not None else 40.0)
        if abs(ry - by) > margin:
            flip = ry < by
            side = Side.BLACK if flip else Side.RED
            return flip, side, f"双方棋子分布（红 y={ry:.0f} / 黑 y={by:.0f}）"
    return False, Side.RED, "无法判定，按红方在下"


def infer_actor(prev: Board | None, cur: Board | None):
    """两帧差异判断「刚走子的是哪一方」，用于轮次门控与自动判色。

    只比较可观测签名，因此揭棋暗子翻开（隐藏身份不同）不会误判。
    """
    if prev is None or cur is None:
        return None
    a, b = observable_signature(prev), observable_signature(cur)
    changed = [i for i in range(len(a)) if a[i] != b[i]]
    if not 1 <= len(changed) <= 4:
        return None
    # 起点格：之前有子、现在变空（普通走子 / 翻子的源格）
    for i in changed:
        c, r = i % BOARD_COLS, i // BOARD_COLS
        p = prev.get(c, r)
        if p is not None and b[i] == ".":
            return Side(p.side)
    # 退化：取任一「之前有子且发生变化」的格子
    for i in changed:
        c, r = i % BOARD_COLS, i // BOARD_COLS
        p = prev.get(c, r)
        if p is not None and a[i] != b[i]:
            return Side(p.side)
    return None


def is_initial_layout(board: Board) -> bool:
    """是否近似开局布局：用于自动判红（先手）时允许主动走第一步。"""
    n = sum(1 for _ in board.pieces())
    if n < 28:
        return False
    red = board.general_pos(Side.RED)
    black = board.general_pos(Side.BLACK)
    return red is not None and black is not None and red[1] >= 7 and black[1] <= 2


def rotate_move(move: Move) -> Move:
    """把一步棋旋转 180°（视角判反时用于诊断 / 纠正）。"""
    return Move((BOARD_COLS - 1 - move.src[0], BOARD_ROWS - 1 - move.src[1]),
                (BOARD_COLS - 1 - move.dst[0], BOARD_ROWS - 1 - move.dst[1]))


def position_turn_hint(board: Board, mode=Mode.XIANGQI):
    """从静态局面推断「现在该谁走」，返回 (Side|None, 依据文本)。

    用于中途开启自动下棋：不能再无条件等一次「对手走子变化」，否则明明
    轮到我也会一直干等，必须靠用户手动走一步才恢复。

    * 被将军的一方一定轮到他走（最可靠）；
    * 揭棋开局：每个暗子第一步都会翻开成明子，非将帅明子数 = 已走步数，
      红先走 → 偶数轮红、奇数轮黑。开局对手已走第一步（翻 1 子）时，
      执黑的我方就能被正确判定为「轮到我」；
    * 中局明子也参与走子，静态信息不足，返回 None 交给观察期策略。
    """
    try:
        from core.rules import is_in_check
        if is_in_check(board, Side.RED, board.options):
            return Side.RED, "红方正被将军，轮到红方应将"
        if is_in_check(board, Side.BLACK, board.options):
            return Side.BLACK, "黑方正被将军，轮到黑方应将"
    except Exception:
        pass
    if Mode(mode) == Mode.JIEQI:
        flipped = 0
        dark = 0
        for _c, _r, p in board.pieces():
            if p.dark:
                dark += 1
            elif p.ptype != PieceType.GENERAL:
                flipped += 1
        if flipped <= 2 and dark >= 24:
            side = Side.RED if flipped % 2 == 0 else Side.BLACK
            return side, (f"揭棋开局已翻开 {flipped} 个暗子"
                          f"（红先手，现轮{side.label}）")
    return None, ""


def infer_move(prev: Board | None, cur: Board) -> Move | None:
    """两帧棋盘差异推断一步走子（普通/揭棋翻子/吃子都适用）。"""
    if prev is None:
        return None

    def cell(b, c, r):
        p = b.get(c, r)
        if p is None:
            return "."
        if p.dark:
            return "X" if p.side == Side.RED else "x"
        return ("R" if p.side == Side.RED else "B") + str(int(p.ptype))

    changed = [(c, r) for r in range(BOARD_ROWS) for c in range(BOARD_COLS)
               if cell(prev, c, r) != cell(cur, c, r)]
    if not 1 <= len(changed) <= 3:
        return None
    emptied = [k for k in changed
               if cell(prev, *k) != "." and cell(cur, *k) == "."]
    filled = [k for k in changed
              if cell(cur, *k) != "." and cell(prev, *k) != cell(cur, *k)]
    if len(emptied) == 1 and len(filled) == 1:
        return Move(emptied[0], filled[0])
    return None
