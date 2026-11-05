# -*- coding: utf-8 -*-
"""网络连线接口（预留空位，本期不实现）。

鲨鱼象棋 / 鹏飞象棋式「连线」：识别第三方象棋客户端窗口棋盘、把本方着法
自动点击到目标平台、自动续盘等。本期按需求只保留接口骨架，界面按钮禁用。

后续接入点：
  * connect(platform, window_title) -> 建立窗口句柄 / 图像识别会话
  * sync_board(board)               -> 从目标窗口同步局面
  * send_move(move)                 -> 把引擎着法转换成屏幕点击
  * poll()                          -> 读取对方着法
  * disconnect()                    -> 释放会话
"""
from __future__ import annotations


class NetworkClient:
    AVAILABLE = False

    def __init__(self):
        self.connected = False
        self.platform = ""

    def connect(self, platform: str, window_title: str = ""):
        raise NotImplementedError("连线功能为预留空位，尚未接入")

    def sync_board(self, board):
        raise NotImplementedError("连线功能为预留空位，尚未接入")

    def send_move(self, move):
        raise NotImplementedError("连线功能为预留空位，尚未接入")

    def poll(self):
        raise NotImplementedError("连线功能为预留空位，尚未接入")

    def disconnect(self):
        raise NotImplementedError("连线功能为预留空位，尚未接入")


class PlatformProfile:
    """各平台棋子外观 / 棋盘坐标配置（预留，供后续连线方案使用）。"""

    def __init__(self, name: str, board_rect=None, piece_size: int = 0):
        self.name = name
        self.board_rect = board_rect
        self.piece_size = piece_size
