# -*- coding: utf-8 -*-
"""目标窗口 / 进程枚举与客户区截图。

截图优先级：
  1. PrintWindow(PW_RENDERFULLCONTENT=2)：可抓被遮挡窗口，不抢前台；
  2. 屏幕区域抓取（PIL ImageGrab）：PrintWindow 黑屏时回退；
返回统一 RGB uint8 numpy 数组，坐标即窗口左上角为原点的像素坐标。
"""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

import numpy as np

try:
    # DPI 感知：保证 GetWindowRect 与 ImageGrab 使用同一物理像素坐标系
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import win32gui
import win32process
import win32ui
import psutil
from PIL import ImageGrab

# 这些类名是桌面 / 系统壳窗口，不作为目标
SKIP_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow", "DV2ControlHost", "Button",
    "ComboLBox", "SysShadow", "Tooltip", "#32768",
}


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    proc_name: str
    cls_name: str
    rect: tuple            # (left, top, right, bottom) 屏幕物理像素

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    @property
    def is_self(self) -> bool:
        return self.pid == os.getpid()

    @property
    def display(self) -> str:
        tag = "〈本程序〉" if self.is_self else ""
        t = self.title if len(self.title) <= 42 else self.title[:41] + "…"
        return f"{self.proc_name} [{self.pid}] {tag} · {t}"

    def refresh_rect(self) -> "WindowInfo":
        try:
            self.rect = win32gui.GetWindowRect(self.hwnd)
        except Exception:
            pass
        return self


def list_windows(min_size: int = 160) -> list[WindowInfo]:
    """枚举可见、有标题、足够大的顶层窗口（含进程名 / PID）。"""
    found: list[WindowInfo] = []

    def cb(hwnd, _unused):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.IsIconic(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return True
            cls_name = win32gui.GetClassName(hwnd)
            if cls_name in SKIP_CLASSES:
                return True
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] < min_size or rect[3] - rect[1] < min_size:
                return True
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                proc_name = psutil.Process(pid).name()
            except Exception:
                proc_name = "?"
            found.append(WindowInfo(int(hwnd), title, int(pid),
                                    proc_name, cls_name, tuple(rect)))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    found.sort(key=lambda w: (w.proc_name.lower(), w.title.lower()))
    return found


def window_info(hwnd: int) -> WindowInfo | None:
    for w in list_windows(min_size=1):
        if w.hwnd == int(hwnd):
            return w
    return None


def _grab_screen(rect) -> np.ndarray:
    img = ImageGrab.grab(bbox=rect, all_screens=True)
    return np.asarray(img.convert("RGB"), dtype=np.uint8)


def _grab_printwindow(hwnd: int, w: int, h: int) -> np.ndarray | None:
    """PrintWindow 抓整窗（含标题栏），返回 RGB；失败 / 全黑返回 None。"""
    hwnd_dc = mfc_dc = save_dc = None
    bmp = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        bits = bmp.GetBitmapBits(True)
        arr = np.frombuffer(bits, dtype=np.uint8).reshape(h, w, 4)
        rgb = np.ascontiguousarray(arr[:, :, :3][:, :, ::-1])  # BGRA -> RGB
        if not ok or float(rgb.mean()) < 3.0 or float(rgb.std()) < 2.0:
            return None
        return rgb
    except Exception:
        return None
    finally:
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
            if save_dc is not None:
                save_dc.DeleteDC()
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
            if hwnd_dc is not None:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def capture(hwnd: int, prefer_printwindow: bool = True) -> tuple:
    """抓取指定窗口，返回 (RGB ndarray, WindowInfo)；失败返回 (None, info)。"""
    info = None
    try:
        rect = win32gui.GetWindowRect(hwnd)
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            pname = psutil.Process(pid).name()
        except Exception:
            pname = "?"
        info = WindowInfo(int(hwnd), win32gui.GetWindowText(hwnd), int(pid),
                          pname, win32gui.GetClassName(hwnd), tuple(rect))
    except Exception:
        return None, None

    w, h = info.width, info.height
    if w < 40 or h < 40:
        return None, info

    img = _grab_printwindow(hwnd, w, h) if prefer_printwindow else None
    if img is None:
        try:
            img = _grab_screen(info.rect)
        except Exception:
            return None, info
    return img, info
