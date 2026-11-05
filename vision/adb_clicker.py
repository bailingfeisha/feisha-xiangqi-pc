# -*- coding: utf-8 -*-
"""Android 模拟器触摸通道（ADB input tap）。

模拟器（MuMu / 雷电 / 夜神 / 逍遥 / BlueStacks / MEmu 等）用 OpenGL /
DirectX 自绘画面，主窗口通常**不处理** WM_LBUTTONDOWN，所以后台窗口消息
会「发出去了但没走棋」。真实鼠标可用，但会占用鼠标。

ADB 的 `input tap` 直接把触摸事件送进 Android 输入系统：
  * 不移动 Windows 鼠标、不抢焦点、不注入目标进程；
  * 目标窗口被遮挡时同样有效；
  * 需要本机可执行的 adb.exe 与一个已连接的模拟器设备。

坐标换算（v1.2.2 重点修复）：
  * `wm size` 返回**物理分辨率**（如 1920x1080），但模拟器可能处于
    旋转 90/270 的竖屏显示，此时 `input tap` 用的是**当前方向**的坐标系
    （如 1080x1920）。直接按 wm size 映射会让 X 被拉伸、Y 被压缩，
    点击全部落偏 —— 雷电 9 默认就是这种情况。
  * 因此用 `dumpsys window` 的 `DisplayFrames w=.. h=.. r=..` 或
    `mCurrentRotation=ROTATION_x` 自动推断触摸坐标系；用户还能用
    `swap_xy` 做一次额外手动交换。
  * 截图 / 网格坐标是「整窗物理像素」，先减去窗口内客户区原点，
    再按客户区尺寸线性映射到触摸坐标系。黑边/侧边栏用 pad/offset 微调。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass

import psutil
import win32gui

EMU_HINTS = {
    "MuMu": ("mumu", "nemu", "nemup", "mumuplayer", "mumu player"),
    "雷电": ("ldplayer", "dnplayer", "leidian", "ld9", "dnplayer2"),
    "夜神": ("nox", "noxplayer"),
    "逍遥": ("memu", "microvirt", "xyaz"),
    "BlueStacks": ("bluestacks", "bstk", "hd-player"),
    "腾讯手游助手": ("tencent", "androws", "aow_exe", "gameassist"),
}
COMMON_PORTS = (7555, 16384, 62001, 21503, 5555, 5556, 5557,
                5558, 5559, 16416, 16448, 62025, 5565, 7556)
# 常见模拟器安装目录（不依赖目标进程也能找到自带 adb）
COMMON_ROOTS = (
    r"D:\leidian\LDPlayer9", r"C:\leidian\LDPlayer9",
    r"D:\leidian\LDPlayer5", r"C:\leidian\LDPlayer5",
    r"C:\Program Files\leidian\LDPlayer9",
    r"C:\Program Files (x86)\leidian\LDPlayer9",
    r"C:\LDPlayer\LDPlayer9",
)
CREATE_NO_WINDOW = 0x08000000


def detect_emulator(proc_name: str = "", title: str = "",
                    cls_name: str = "") -> str:
    """根据进程名 / 标题 / 类名识别模拟器，返回品牌名，空串表示不是。"""
    text = f"{proc_name} {title} {cls_name}".lower().replace(" ", "")
    for brand, keys in EMU_HINTS.items():
        if any(k.replace(" ", "") in text for k in keys):
            return brand
    return ""


@dataclass
class AdbTapConfig:
    adb_path: str = ""
    serial: str = ""
    size: tuple = (0, 0)             # 物理分辨率（wm size）
    swap_xy: bool = False            # 在自动旋转检测之后再额外交换一次
    auto_rotation: bool = True       # 自动按当前屏幕方向推断触摸坐标系
    pad_l: int = 0
    pad_t: int = 0
    pad_r: int = 0
    pad_b: int = 0
    offset_x: int = 0                # 最终像素微调
    offset_y: int = 0


class AdbBridge:
    """轻量 adb 封装：找 adb、连设备、推断屏幕方向、发送 tap。"""

    def __init__(self, cfg: AdbTapConfig | None = None, pid: int = 0):
        self.cfg = cfg or AdbTapConfig()
        self.pid = pid
        self.last_error = ""
        self.touch_size: tuple = (0, 0)      # 当前方向下的触摸坐标系尺寸
        self.rotation_info = ""
        self.tap_history: deque = deque(maxlen=4)

    # ---------- 探测 ----------
    def find_adb(self) -> str:
        if self.cfg.adb_path and os.path.isfile(self.cfg.adb_path):
            return self.cfg.adb_path
        roots: list[str] = []
        # 1) 优先模拟器进程同目录（雷电自带 adb 与它的 server 版本最匹配）
        try:
            exe = psutil.Process(self.pid).exe() if self.pid else ""
        except Exception:
            exe = ""
        if exe:
            d = os.path.dirname(exe)
            roots += [d, os.path.dirname(d), os.path.dirname(os.path.dirname(d))]
        # 2) 常见安装位置（含 D 盘，不依赖进程 pid）
        roots += [r for r in COMMON_ROOTS if os.path.isdir(r)]
        for env in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env, "")
            if base:
                roots += [os.path.join(base, "Netease", "MuMu Player 12"),
                          os.path.join(base, "Netease", "MuMu"),
                          os.path.join(base, "leidian", "LDPlayer9"),
                          os.path.join(base, "BlueStacks")]
        seen: set[str] = set()
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            key = os.path.normcase(root)
            if key in seen:
                continue
            seen.add(key)
            direct = os.path.join(root, "adb.exe")
            if os.path.isfile(direct):
                self.cfg.adb_path = direct
                return direct
            for cur, _dirs, files in os.walk(root):
                for f in files:
                    if f.lower() == "adb.exe":
                        p = os.path.join(cur, f)
                        self.cfg.adb_path = p
                        return p
                if cur.count(os.sep) - root.count(os.sep) >= 2:
                    _dirs[:] = []
        # 3) PATH / Android SDK
        p = shutil.which("adb") or ""
        if not p:
            for base in (os.environ.get("LOCALAPPDATA", ""),
                         os.environ.get("USERPROFILE", ""), "C:\\"):
                cand = os.path.join(base, "AndroidSDK", "platform-tools",
                                    "adb.exe")
                if os.path.isfile(cand):
                    p = cand
                    break
        if p:
            self.cfg.adb_path = p
        return p

    def _run(self, args: list[str], timeout: float = 6.0) -> str:
        """执行 adb；超时 / 失败一律返回空串，绝不让异常冒到界面线程。"""
        try:
            out = subprocess.run(args, capture_output=True, text=True,
                                 timeout=timeout, encoding="utf-8",
                                 errors="replace",
                                 creationflags=CREATE_NO_WINDOW)
            return (out.stdout or "") + (out.stderr or "")
        except subprocess.TimeoutExpired:
            self.last_error = (
                f"adb 命令超时：{os.path.basename(str(args[0]))} "
                f"{(args[1] if len(args) > 1 else '')}")
            return ""
        except Exception as exc:
            self.last_error = f"adb 命令失败：{exc}"
            return ""

    def _shell(self, command: str, timeout: float = 8.0) -> str:
        return self._run([self.cfg.adb_path, "-s", self.cfg.serial,
                          "shell", command], timeout=timeout)

    # ---------- 方向 / 分辨率 ----------
    def _detect_touch_size(self) -> tuple[tuple, str]:
        """推断 `input tap` 实际使用的坐标系尺寸。

        优先 `DisplayFrames w=.. h=.. r=..`；回退用 mCurrentRotation +
        wm size 交换；都拿不到就用物理尺寸。
        """
        w, h = self.cfg.size
        if not w or not h:
            return (0, 0), ""
        if not self.cfg.auto_rotation:
            return (w, h), "自动旋转检测已关闭"
        out = self._shell(
            "dumpsys window | grep -E 'DisplayFrames|mCurrentRotation'",
            timeout=10)
        m = re.search(r"DisplayFrames\s+w=(\d+)\s+h=(\d+)\s+r=(\d+)", out)
        if m:
            return ((int(m.group(1)), int(m.group(2))),
                    f"DisplayFrames 触摸坐标系 {m.group(1)}×{m.group(2)}"
                    f"（旋转 r={m.group(3)}）")
        m = re.search(r"mCurrentRotation=ROTATION_(\d+)", out)
        if m:
            rot = int(m.group(1))
            if rot in (90, 270):
                return (h, w), f"ROTATION_{rot} 自动交换为 {h}×{w}"
            return (w, h), f"ROTATION_{rot} 无需交换 {w}×{h}"
        return (w, h), "未读到旋转信息，按物理尺寸"

    def ensure(self, quick: bool = False) -> tuple[bool, str]:
        """确认 adb + 设备 + 分辨率/方向可用。返回 (ok, 状态说明)。

        quick=True 只查已连接设备，不逐个试端口，避免自动开启时卡界面。
        """
        self.last_error = ""
        adb = self.find_adb()
        if not adb:
            self.last_error = "未找到 adb.exe（点「ADB 探测」可手动指定路径）"
            return False, self.last_error
        if not self.cfg.serial:
            serial = ""
            out = self._run([adb, "devices"],
                            timeout=(3.0 if quick else 10.0))
            for line in out.splitlines():
                if "\tdevice" in line:
                    serial = line.split("\t")[0].strip()
                    break
            if not serial and not quick:
                self._run([adb, "start-server"], timeout=10)
                for port in COMMON_PORTS[:6]:
                    self._run([adb, "connect", f"127.0.0.1:{port}"],
                              timeout=2.5)
                    out = self._run([adb, "devices"], timeout=4)
                    serial = next(
                        (ln.split("\t")[0].strip() for ln in out.splitlines()
                         if "\tdevice" in ln), "")
                    if serial:
                        break
            if not serial:
                self.last_error = ("没有已连接的 Android 设备"
                                   "（点「ADB 探测 / 连接」自动连端口）"
                                   if quick else
                                   "没有已连接的 Android 设备（可手动指定 adb.exe）")
                return False, self.last_error
            self.cfg.serial = serial
        if not self.cfg.size or 0 in self.cfg.size:
            out = self._shell("wm size")
            m = re.search(r"(\d+)\s*x\s*(\d+)", out)
            if m:
                self.cfg.size = (int(m.group(1)), int(m.group(2)))
                self.touch_size = (0, 0)      # 分辨率变了，重新判方向
            else:
                self.last_error = "读取 Android 分辨率失败（wm size 无返回）"
                return False, self.last_error
        if not self.touch_size or 0 in self.touch_size:
            self.touch_size, self.rotation_info = self._detect_touch_size()
        self.last_error = ""
        return True, self.status_text()

    def status_text(self) -> str:
        w, h = self.touch_size or self.cfg.size
        return (f"{self.cfg.serial} · 物理 {self.cfg.size[0]}×{self.cfg.size[1]}"
                f" · 触摸 {w}×{h} · {self.rotation_info or '方向未知'}"
                f" · {os.path.basename(self.cfg.adb_path)}")

    # ---------- 坐标 / 点击 ----------
    def window_to_android(self, hwnd: int, x: int, y: int):
        """屏幕物理坐标 → Android 触摸坐标。"""
        rect = win32gui.GetWindowRect(hwnd)
        try:
            cl, ct = win32gui.ClientToScreen(hwnd, (0, 0))
            _l, _t, cr, cb = win32gui.GetClientRect(hwnd)
        except Exception:
            cl, ct = rect[0], rect[1]
            cr, cb = rect[2] - rect[0], rect[3] - rect[1]
        ox, oy = cl - rect[0], ct - rect[1]
        wx, wy = x - rect[0] - ox, y - rect[1] - oy
        c = self.cfg
        aw, ah = self.touch_size or c.size
        if c.swap_xy:                 # 用户手动额外交换
            aw, ah = ah, aw
        area_w = max(1.0, cr - c.pad_l - c.pad_r)
        area_h = max(1.0, cb - c.pad_t - c.pad_b)
        ax = (wx - c.pad_l) / area_w * aw + c.offset_x
        ay = (wy - c.pad_t) / area_h * ah + c.offset_y
        return int(round(ax)), int(round(ay)), (aw, ah)

    def describe_last_tap(self) -> str:
        return (" / ".join(f"({x},{y})" for x, y in self.tap_history)
                if self.tap_history else "")

    def tap(self, hwnd: int, x: int, y: int) -> bool:
        """把一次 Windows 屏幕坐标点击转成 Android 触摸。"""
        ok, _msg = self.ensure(quick=True)
        if not ok:
            return False
        ax, ay, (aw, ah) = self.window_to_android(hwnd, x, y)
        if not (0 <= ax < aw and 0 <= ay < ah):
            self.last_error = f"触摸坐标越界：({ax},{ay}) / {aw}×{ah}"
            return False
        self.last_error = ""
        self._run([self.cfg.adb_path, "-s", self.cfg.serial,
                   "shell", "input", "tap", str(ax), str(ay)], timeout=5)
        if self.last_error:
            return False
        self.tap_history.append((ax, ay))
        return True

    def swipe(self, hwnd: int, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 260) -> bool:
        ok, _msg = self.ensure(quick=True)
        if not ok:
            return False
        ax1, ay1, _ = self.window_to_android(hwnd, x1, y1)
        ax2, ay2, _ = self.window_to_android(hwnd, x2, y2)
        self.last_error = ""
        self._run([self.cfg.adb_path, "-s", self.cfg.serial,
                   "shell", "input", "swipe",
                   str(ax1), str(ay1), str(ax2), str(ay2),
                   str(int(duration_ms))], timeout=5)
        if self.last_error:
            return False
        self.tap_history.append((ax2, ay2))
        return True
