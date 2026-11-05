# -*- coding: utf-8 -*-
"""棋盘格 → 屏幕坐标 → 后台窗口消息落子（不占用鼠标）。

仅用于自有 / 本地单机棋盘窗口的识别与自动化测试。默认方式是
PostMessage / SendMessage 投递 WM_LBUTTONDOWN/UP 到目标窗口客户区：
  * 不移动真实鼠标、不抢前台焦点、不改窗口 Z 序，用户可照常用鼠标做别的事；
  * 目标窗口被遮挡（但仍可见、未最小化）时依然能收到点击；
  * 不含注入、内存读取、协议抓包或任何反检测。

实测（150% DPI 的 PySide6 窗口）：后台消息能稳定产生
mousePressEvent / mouseReleaseEvent，GetCursorPos 前后完全一致；
而 SetCursorPos + mouse_event 在非前台窗口上反而会点空并抢走鼠标。

安全约束：
  * 每次点击前重新读取目标窗口矩形，窗口关闭 / 最小化即中止；
  * 全局 abort 标志供「停止」立即打断；
  * 落子坐标必须在当前窗口矩形内，越界直接拒绝。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import win32api
import win32con
import win32gui

from core.board import Move
from vision.reconstruct import GridFit

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
CWP_SKIPINVISIBLE = 0x0001

_abort = threading.Event()


def request_abort():
    _abort.set()


def clear_abort():
    _abort.clear()


def aborted() -> bool:
    return _abort.is_set()


@dataclass
class ScreenMapper:
    rect: tuple                 # 目标窗口屏幕矩形 (l,t,r,b) 物理像素
    grid: GridFit               # 截图坐标系里的棋盘网格
    flip: bool = False

    def to_window(self, col: int, row: int) -> tuple[float, float]:
        if self.flip:
            col, row = 8 - col, 9 - row
        return self.grid.point(col, row)

    def to_screen(self, col: int, row: int) -> tuple[int, int]:
        x, y = self.to_window(col, row)
        return int(round(self.rect[0] + x)), int(round(self.rect[1] + y))

    def contains(self, x: int, y: int, margin: int = 4) -> bool:
        l, t, r, b = self.rect
        return (l - margin <= x <= r + margin and
                t - margin <= y <= b + margin)


def window_rect_valid(hwnd: int, expected_title: str = "",
                      verify_title: bool = False) -> tuple | None:
    """返回当前窗口矩形；窗口关闭 / 最小化时返回 None。

    默认不校验标题（很多棋盘程序会把轮次写进标题，容易误判）。
    """
    try:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return None
        if win32gui.IsIconic(hwnd):
            return None
        if verify_title and expected_title and \
                win32gui.GetWindowText(hwnd) != expected_title:
            return None
        rect = tuple(win32gui.GetWindowRect(hwnd))
        if rect[2] - rect[0] < 100 or rect[3] - rect[1] < 100:
            return None
        return rect
    except Exception:
        return None


def _focus(hwnd: int):
    """仅真实鼠标模式使用；后台消息模式不会调用。"""
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _mouse_click(x: int, y: int):
    win32api.SetCursorPos((x, y))
    time.sleep(0.025)
    win32api.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.025)
    win32api.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _client_lparam(hwnd: int, x: int, y: int,
                   resolve_child: bool = False) -> tuple[int, int]:
    """屏幕物理坐标 → 目标 HWND 的客户区 lParam。

    Windows 消息坐标是物理像素；Qt / WPF 等会自行换算成自己的逻辑坐标。
    """
    target = hwnd
    cx, cy = win32gui.ScreenToClient(hwnd, (x, y))
    if resolve_child:
        try:
            child = win32gui.ChildWindowFromPointEx(hwnd, (cx, cy),
                                                    CWP_SKIPINVISIBLE)
            if child and child != hwnd and win32gui.IsWindow(child):
                target = child
                cx, cy = win32gui.ScreenToClient(child, (x, y))
        except Exception:
            pass
    return target, ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)


def _post_mouse(hwnd: int, x: int, y: int, msg: int, wparam: int,
                message_mode: str, resolve_child: bool):
    target, lp = _client_lparam(hwnd, x, y, resolve_child)
    send = win32gui.SendMessage if message_mode == "send" else win32gui.PostMessage
    try:
        send(target, WM_MOUSEMOVE, wparam, lp)   # 部分程序依赖 hover / hit-test
    except Exception:
        pass
    try:
        send(target, msg, wparam, lp)
    except Exception:
        pass
    return target


def message_click(hwnd: int, x: int, y: int,
                  message_mode: str = "post",
                  resolve_child: bool = False) -> int:
    """后台点击一次（按下 + 抬起），不移动真实鼠标。返回接收 HWND。"""
    _post_mouse(hwnd, x, y, WM_LBUTTONDOWN, MK_LBUTTON,
                message_mode, resolve_child)
    time.sleep(0.02)
    return _post_mouse(hwnd, x, y, WM_LBUTTONUP, 0,
                       message_mode, resolve_child)


def message_drag(hwnd: int, x1: int, y1: int, x2: int, y2: int,
                 message_mode: str = "post",
                 resolve_child: bool = False) -> int:
    """后台拖拽：MOVE → DOWN → 分段 MOVE → UP。"""
    _post_mouse(hwnd, x1, y1, WM_LBUTTONDOWN, MK_LBUTTON,
                message_mode, resolve_child)
    send = win32gui.SendMessage if message_mode == "send" else win32gui.PostMessage
    for i in range(1, 5):
        mx = int(x1 + (x2 - x1) * i / 4)
        my = int(y1 + (y2 - y1) * i / 4)
        target, lp = _client_lparam(hwnd, mx, my, resolve_child)
        try:
            send(target, WM_MOUSEMOVE, MK_LBUTTON, lp)
        except Exception:
            pass
        time.sleep(0.015)
    target, lp = _client_lparam(hwnd, x2, y2, resolve_child)
    try:
        send(target, WM_LBUTTONUP, 0, lp)
    except Exception:
        pass
    return target


def play_move(hwnd: int, move: Move, mapper: ScreenMapper,
              method: str = "message", drag: bool = False,
              click_gap_ms: int = 130, expected_title: str = "",
              message_mode: str = "post", resolve_child: bool = False,
              offset_x: int = 0, offset_y: int = 0,
              verify_title: bool = False,
              adb_bridge=None, adb_pid: int = 0) -> bool:
    """在目标窗口执行一步棋；成功返回 True，被中止 / 失效返回 False。

    method:
      message —— 后台窗口消息（默认，不占鼠标）
      mouse   —— 真实鼠标点击（会移动鼠标并激活窗口，兼容备选）
      adb     —— 模拟器 ADB 触摸（不占鼠标，需 adb_bridge）
    """
    if aborted():
        return False
    rect = window_rect_valid(hwnd, expected_title, verify_title)
    if rect is None:
        return False
    mapper.rect = rect
    x1, y1 = mapper.to_screen(*move.src)
    x2, y2 = mapper.to_screen(*move.dst)
    x1, x2 = x1 + int(offset_x), x2 + int(offset_x)
    y1, y2 = y1 + int(offset_y), y2 + int(offset_y)
    if not (mapper.contains(x1, y1) and mapper.contains(x2, y2)):
        return False

    if method == "message":
        if drag:
            message_drag(hwnd, x1, y1, x2, y2, message_mode, resolve_child)
            return True
        message_click(hwnd, x1, y1, message_mode, resolve_child)
        time.sleep(click_gap_ms / 1000.0)
        if aborted():
            return False
        message_click(hwnd, x2, y2, message_mode, resolve_child)
        return True

    if method == "adb":
        # 模拟器主窗口通常不处理 WM_LBUTTON*；用 Android 输入系统触摸，
        # 同样不移动 Windows 鼠标、不抢焦点。
        if adb_bridge is None:
            return False
        adb_bridge.pid = adb_pid or adb_bridge.pid
        if drag:
            return adb_bridge.swipe(hwnd, x1, y1, x2, y2)
        if not adb_bridge.tap(hwnd, x1, y1):
            return False
        time.sleep(click_gap_ms / 1000.0)
        if aborted():
            return False
        return adb_bridge.tap(hwnd, x2, y2)

    # ---- 真实鼠标（兼容备选）----
    _focus(hwnd)
    if aborted() or window_rect_valid(hwnd, expected_title, verify_title) is None:
        return False
    if drag:
        win32api.SetCursorPos((x1, y1))
        time.sleep(0.03)
        win32api.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.06)
        win32api.SetCursorPos((x2, y2))
        time.sleep(0.06)
        win32api.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    else:
        _mouse_click(x1, y1)
        time.sleep(click_gap_ms / 1000.0)
        if aborted():
            return False
        _mouse_click(x2, y2)
    return True
