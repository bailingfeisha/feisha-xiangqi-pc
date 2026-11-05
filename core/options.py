# -*- coding: utf-8 -*-
"""全局可配置项（QSettings 持久化 + 规则引擎共享）。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Options:
    # ---- 界面 ----
    theme: str = "wood"                 # wood / dark / bamboo
    piece_style: str = "simplified"     # simplified / traditional
    show_coords: bool = True
    show_hints: bool = True
    show_last_move: bool = True
    show_captured: bool = True
    sound: bool = True
    board_zoom: float = 1.0             # 0.6 ~ 1.4

    # ---- 计时 ----
    use_clock: bool = False
    minutes_per_side: int = 10

    # ---- 规则 ----
    jieqi_advisor_cross: bool = True    # 揭棋明子：士可出九宫、过河
    jieqi_elephant_cross: bool = True   # 揭棋明子：象可过河
    dark_can_be_captured: bool = True   # 暗子是否允许被吃
    no_capture_draw: int = 120          # 无吃子半回合数和棋，0 = 关闭
    max_moves: int = 200                # 最大回合数判和，0 = 关闭
    repetition_draw: bool = True        # 三次重复局面判和
    seed: str = ""                      # 揭棋洗牌种子，空 = 随机

    # ---- 引擎（UCI / UCCI）----
    engine_paths: list = field(default_factory=list)
    engine_depth: int = 12
    engine_movetime: int = 1000
    engine_hash: int = 64
    engine_threads: int = 1
    engine_use_movetime: bool = True
    engine_rank_flip: bool = True       # UCI rank 0 = 红方底线（Pikafish 约定）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Options":
        obj = cls()
        for key, value in (data or {}).items():
            if hasattr(obj, key):
                cur = getattr(obj, key)
                try:
                    if isinstance(cur, bool):
                        value = bool(value)
                    elif isinstance(cur, int):
                        value = int(value)
                    elif isinstance(cur, float):
                        value = float(value)
                    setattr(obj, key, value)
                except (TypeError, ValueError):
                    pass
        return obj
