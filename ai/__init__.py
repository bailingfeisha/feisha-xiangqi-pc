# -*- coding: utf-8 -*-
"""引擎接口：UCI/UCCI 引擎加载、局面分析、引擎联赛（预留自动下棋）。"""
from .engine import (
    EngineInfo, Analysis, UciEngine, EngineManager, AutoPlayController,
    bundled_engine_path,
)

__all__ = ["EngineInfo", "Analysis", "UciEngine", "EngineManager",
           "AutoPlayController", "bundled_engine_path"]
