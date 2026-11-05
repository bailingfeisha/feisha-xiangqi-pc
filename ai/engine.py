# -*- coding: utf-8 -*-
"""引擎接口：加载 UCI / UCCI 象棋引擎、局面分析、人机对战。

已实现：
  * 引擎进程管理、UCI / UCCI 握手、引擎参数设置；
  * 局面分析（bestmove / 评分 / 深度 / 节点数 / 主变 PV），结果异步回传界面；
  * 内置 AB JChess 1 RB 揭棋引擎；自动发现本机皮卡鱼 / 皮卡揭等引擎；
  * NNUE（EvalFile）加载状态采集与同目录自动定位；
  * AutoPlayController：真正的"人机对战"（可选执红 / 执黑、自动落子）。

AB-JChess 揭棋 FEN（由 core.board.Board.to_fen 生成）：
  xxxxkxxxx/9/1x5x1/.../XXXXKXXXX w R2A2C2P5N2B2r2a2c2p5n2b2 0 1
  * X/x = 红/黑 暗子；R N B A K C P = 车 马 象 士 将帅 炮 卒（大写红、小写黑）
  * 第 3 段是"暗子身份池"，'-' 表示无暗子（普通象棋局面）
  * UCI 坐标 a..i + rank 0..9，rank 0 = 红方底线（内部 row 9）

NNUE / EvalFile 规则（实测 Pikafish 2026-01-31 与 AB JChess 1 RB）：
  * 两个引擎都有 `option name EvalFile type string default <相对文件名>`
    （皮卡鱼 pikafish.nnue / AB-JChess abjchess-20261030.nnue）。
  * 相对文件名按**进程工作目录 cwd** 解析，不是按 exe 所在目录硬绑定；
    本模块 start() 把 cwd 设为 exe 目录，因此等价于"exe 与 nnue 放同一目录"。
  * 若分离，必须在 `uciok` 之后、`isready` 之前发送
    `setoption name EvalFile value <路径>`；含中文的绝对路径在 Pikafish 下会
    加载失败（引擎内部窄字符 fopen），用相对文件名最稳妥。
  * 网络文件必须与引擎版本匹配，否则引擎报错并终止。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal, QTimer

from core.constants import Mode, Side
from core.notation import uci_to_move

# ---------------------------------------------------------------------------
# 路径解析：单 exe（PyInstaller --onefile）下 __file__ 指向临时解压目录
# _MEIxxxxx，那里没有引擎文件，必须改用 sys.executable 所在目录。
# 引擎不打包进 exe，作为外部数据放在 exe 同级的 engines/ 目录。
# ---------------------------------------------------------------------------
BUNDLED_REL = ("engines", "AB-JChess_1_RB_bmi2", "AB-JChess-1-RB-bmi2.exe")
BUNDLED_EXE_NAME = "ab-jchess-1-rb-bmi2.exe"

# 揭棋引擎识别关键词（支持暗子身份池的 5 段 FEN）
JIEQI_KEYS = ("jchess", "jieqi", "jie qi", "揭棋", "暗棋", "jie chess")


def app_dir() -> str:
    """程序所在目录：打包后 = exe 所在目录；源码运行 = 项目根目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def engine_root() -> str:
    """外部引擎目录（exe 同级 engines/）；引擎不嵌入 exe。"""
    return os.path.join(app_dir(), "engines")


def self_exe() -> str:
    """打包后程序自身路径，避免自动扫描时把本 GUI 当成引擎加载。"""
    if getattr(sys, "frozen", False):
        return os.path.normcase(os.path.abspath(sys.executable))
    return ""


def bundled_engine_path() -> str | None:
    """查找随程序附带（但不嵌入 exe）的 AB JChess 揭棋引擎。

    优先级：exe 同级 engines/ 标准路径 → engines/ 下递归同名 exe
            → _MEIPASS（若 spec 里加过 datas）→ 源码项目目录。
    """
    bases = [app_dir()]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.append(meipass)
    seen: set[str] = set()
    for base in bases:
        key = os.path.normcase(os.path.abspath(base))
        if key in seen:
            continue
        seen.add(key)
        direct = os.path.join(base, *BUNDLED_REL)
        if os.path.isfile(direct):
            return os.path.abspath(direct)
        root = os.path.join(base, "engines")
        if not os.path.isdir(root):
            continue
        for cur, _dirs, files in os.walk(root):
            for f in files:
                if f.lower() == BUNDLED_EXE_NAME:
                    return os.path.abspath(os.path.join(cur, f))
    return None


def is_jieqi_name(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in JIEQI_KEYS)


def discover_engines(limit: int = 8) -> list[str]:
    """扫描本机常见目录里的 Windows 象棋引擎 exe（首次启动自动登记）。

    单 exe 部署时优先扫描 exe 同级 engines/ 与 exe 所在目录；
    引擎文件本身不打包，可随 exe 一起拷贝，也可留在 Desktop\\引擎 等原位置。
    """
    home = os.path.expanduser("~")
    roots = [
        engine_root(),                              # exe 同级外部引擎目录
        app_dir(),                                  # 直接放在 exe 旁边
        os.path.join(home, "Desktop", "引擎"),
        os.path.join(home, "Desktop", "engines"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "Downloads"),
    ]
    hints = ("pikafish", "jieqi", "jchess", "eleeye", "cyclone", "象棋", "tchess")
    skip_dir = ("android", "linux", "macos", "arm64", "armv8",
                "__pycache__", "node_modules", ".git")
    # 排除安装器与 GUI 前端（中文名 "飞鲨象棋PC版.exe" 含 "象棋" 关键字）
    skip_file = ("setup", "installer", "unins", "update",
                 "飞鲨", "feisha", "chessgui")
    me = self_exe()
    found: list[str] = []
    seen_roots: set[str] = set()
    for root_dir in roots:
        if not os.path.isdir(root_dir):
            continue
        key = os.path.normcase(os.path.abspath(root_dir))
        if key in seen_roots:
            continue
        seen_roots.add(key)
        for cur, dirs, files in os.walk(root_dir):
            rel = os.path.relpath(cur, root_dir)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            low = cur.lower()
            if depth > 2 or any(s in low for s in skip_dir):
                dirs[:] = []
                continue
            for f in files:
                if not f.lower().endswith(".exe"):
                    continue
                lf = f.lower()
                if not any(h in lf for h in hints):
                    continue
                if any(s in lf for s in skip_file):
                    continue
                full = os.path.abspath(os.path.join(cur, f))
                if me and os.path.normcase(full) == me:
                    continue
                # 没有同目录 NNUE 的引擎加载必然失败，直接跳过
                try:
                    has_net = any(x.lower().endswith(".nnue")
                                  for x in os.listdir(os.path.dirname(full)))
                except OSError:
                    has_net = False
                if not has_net:
                    continue
                found.append(full)
                if len(found) >= limit:
                    return sorted(set(found))
    return sorted(set(found))


@dataclass
class EngineInfo:
    path: str = ""
    name: str = ""
    author: str = ""
    protocol: str = "uci"
    options: dict = field(default_factory=dict)
    nnue_info: str = ""          # NNUE / EvalFile 加载情况
    jieqi_capable: bool = False  # 是否支持揭棋暗子局面

    @property
    def display(self) -> str:
        tag = " · 揭棋" if self.jieqi_capable else ""
        return f"{self.name or os.path.basename(self.path)} [{self.protocol.upper()}{tag}]"


@dataclass
class Analysis:
    bestmove: str = ""
    ponder: str = ""
    score_cp: int | None = None
    mate: int | None = None
    depth: int = 0
    nodes: int = 0
    pv: str = ""
    lines: list = field(default_factory=list)
    fen: str = ""                # 本次分析所用 FEN，供调用方校验结果是否过期
    request_id: int = 0          # 请求编号，防止旧 bestmove 串到新局面
    multipv_moves: list = field(default_factory=list)   # 多主变候选着法（变招）
    _mpv: dict = field(default_factory=dict, repr=False)

    @property
    def score_text(self) -> str:
        if self.mate is not None:
            return f"杀棋 {self.mate} 步"
        if self.score_cp is None:
            return "--"
        return f"{self.score_cp / 100:+.2f} 分"

    @property
    def summary(self) -> str:
        head = f"深度 {self.depth} · {self.score_text}"
        if self.nodes:
            head += f" · {self.nodes:,} 节点"
        return head


class UciEngine(QObject):
    """单个引擎进程；stdout 由后台线程持续读取，不会阻塞界面。"""

    engine_ready = Signal(object)      # EngineInfo
    analysis_ready = Signal(object)    # Analysis
    line_received = Signal(str)        # 引擎原始输出
    nnue_status = Signal(str)          # NNUE / EvalFile 状态
    failed = Signal(str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path
        self.info = EngineInfo(path=path, name=os.path.basename(path))
        self.info.jieqi_capable = is_jieqi_name(self.info.name)
        self.proc: subprocess.Popen | None = None
        self._send_lock = threading.Lock()
        self._bestmove_event = threading.Event()
        self._cur = Analysis()
        self._busy = False
        self._closing = False
        self._req_id = 0
        self._reqs: deque[Analysis] = deque()   # 已发出但尚未收到 bestmove 的请求
        self._req_lock = threading.Lock()
        self.ready = False           # uciok 后才允许发送 position / go

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def supports_jieqi(self) -> bool:
        return is_jieqi_name(self.info.name) or is_jieqi_name(
            os.path.basename(self.path))

    # ---------- 进程 ----------
    def start(self):
        if self.running:
            return
        if not os.path.isfile(self.path):
            raise FileNotFoundError(f"引擎文件不存在：{self.path}")
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        # cwd = exe 目录，NNUE 的默认相对文件名才能被找到
        self.proc = subprocess.Popen(
            [self.path], cwd=os.path.dirname(self.path) or None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1, **kw)
        self._closing = False
        self.ready = False
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._send("uci")

    def _send(self, text: str):
        if not self.proc or self.proc.poll() is not None:
            return
        with self._send_lock:
            try:
                self.proc.stdin.write(text + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def _read_loop(self):
        try:
            for line in iter(self.proc.stdout.readline, ""):
                line = line.rstrip()
                if line:
                    self.line_received.emit(line)
                    self._handle(line)
        except (OSError, ValueError):
            pass
        finally:
            if not self._closing:
                self.failed.emit("引擎进程已退出")

    def _handle(self, line: str):
        low = line.lower()
        if low.startswith("id name "):
            self.info.name = line[8:].strip()
            self.info.jieqi_capable = self.supports_jieqi
        elif low.startswith("id author "):
            self.info.author = line[10:].strip()
        elif low.startswith("option name "):
            name, default = self._parse_option(line[12:])
            self.info.options[name] = default
        elif low in ("uciok", "ucciok"):
            self.info.protocol = "ucci" if low == "ucciok" else "uci"
            self.info.jieqi_capable = self.supports_jieqi
            self.ready = True
            self._prepare_eval_file()
            self.engine_ready.emit(self.info)
        elif "nnue evaluation using" in low:
            self.info.nnue_info = line.strip()
            self.nnue_status.emit(self.info.nnue_info)
        elif ("evalfile" in low or "network file" in low) and \
                ("error" in low or "failed" in low):
            self.info.nnue_info = line.strip()
            self.nnue_status.emit(self.info.nnue_info)
        elif low.startswith("info ") and " score " in low:
            self._parse_info(line)
        elif low.startswith("bestmove "):
            self._parse_bestmove(line)

    @staticmethod
    def _parse_option(rest: str):
        """解析 `option name X type T default V min.. max.. var..`。"""
        rest = rest.strip()
        m = re.search(r"\s+type\s+", rest, re.I)
        if m:
            name, tail = rest[:m.start()].strip(), rest[m.end():].strip()
        else:
            name, tail = rest, ""
        default = ""
        d = re.search(r"\s+default\s+", tail, re.I)
        if d:
            after = tail[d.end():].strip()
            nxt = re.search(r"\s+(?:min|max|var)\s", after, re.I)
            default = (after[:nxt.start()] if nxt else after).strip()
        return name, default

    def _prepare_eval_file(self):
        """uciok 之后、isready 之前，确保 EvalFile 指向同目录可用的 NNUE。"""
        cwd = os.path.dirname(self.path) or os.getcwd()
        default = ""
        for key, val in self.info.options.items():
            if key.lower() == "evalfile":
                default = (val or "").strip()
                break
        if not default or default == "<empty>":
            self._send("isready")
            return
        target = default if os.path.isabs(default) else os.path.join(cwd, default)
        if os.path.isfile(target):
            self.info.nnue_info = f"EvalFile：{default}（引擎同目录）"
        else:
            try:
                cands = sorted(f for f in os.listdir(cwd)
                               if f.lower().endswith(".nnue"))
            except OSError:
                cands = []
            if cands:
                # 只用相对文件名：Pikafish 对含中文的绝对路径会加载失败
                self._send(f"setoption name EvalFile value {cands[0]}")
                self.info.nnue_info = (
                    f"EvalFile：{cands[0]}（同目录自动定位；"
                    f"默认名 {default} 缺失）")
            else:
                self.info.nnue_info = (
                    f"警告：同目录未找到 NNUE（期望 {default}），引擎可能无法搜索")
        self.nnue_status.emit(self.info.nnue_info)
        self._send("isready")

    def _parse_info(self, line: str):
        tokens = line.split()
        a = self._cur
        try:
            if "depth" in tokens:
                a.depth = int(tokens[tokens.index("depth") + 1])
            if "nodes" in tokens:
                a.nodes = int(tokens[tokens.index("nodes") + 1])
            if "score" in tokens:
                i = tokens.index("score")
                kind, val = tokens[i + 1], int(tokens[i + 2])
                if kind == "cp":
                    a.score_cp, a.mate = val, None
                elif kind == "mate":
                    a.mate, a.score_cp = val, None
            if "pv" in tokens:
                i = tokens.index("pv")
                a.pv = " ".join(tokens[i + 1:])
                # MultiPV 第 n 条主变的首着 → 变招候选
                if "multipv" in tokens:
                    n = int(tokens[tokens.index("multipv") + 1])
                    if i + 1 < len(tokens):
                        a._mpv[n] = tokens[i + 1]
                elif not a._mpv and i + 1 < len(tokens):
                    a._mpv[1] = tokens[i + 1]
        except (ValueError, IndexError):
            pass

    def _parse_bestmove(self, line: str):
        tokens = line.split()
        # 严格按 FIFO 把 bestmove 绑定到它自己的请求：分析超时后迟到的
        # bestmove 不会污染下一局面的结果，调用方还能用 a.fen 二次校验。
        with self._req_lock:
            a = self._reqs.popleft() if self._reqs else self._cur
        a.bestmove = tokens[1] if len(tokens) > 1 else ""
        if "ponder" in tokens:
            a.ponder = tokens[tokens.index("ponder") + 1]
        a.lines = a.lines[-19:] + [a.summary + "  " + a.pv]
        a.multipv_moves = [m for _n, m in sorted(a._mpv.items())]
        self._bestmove_event.set()
        self._busy = False
        self.analysis_ready.emit(a)

    # ---------- 控制 ----------
    def set_option(self, name: str, value):
        self._send(f"setoption name {name} value {value}")

    def stop(self):
        self._send("stop")

    def quit(self):
        self._closing = True
        self.ready = False
        with self._req_lock:
            self._reqs.clear()
        self._send("quit")
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except OSError:
            pass
        self.proc = None

    # ---------- 分析 ----------
    def _normalize_fen(self, fen: str) -> str:
        """AB-JChess / PikaJieQi 用 5 段 FEN（第 3 段是暗子池）；
        标准 UCI 引擎（皮卡鱼普通版）用 6 段（castling + en passant）。

        判断依据改为"第 3 段是否含暗子池"，而不是引擎名：
          * 池 != '-'      -> 揭棋局面，必须原样 5 段，绝不补段；
          * 池 == '-'      -> 普通象棋，揭棋引擎保持 5 段，标准引擎补成 6 段。
        """
        parts = fen.split()
        if len(parts) != 5:
            return fen
        if parts[2] != "-":
            return fen
        if self.supports_jieqi:
            return fen
        return f"{parts[0]} {parts[1]} - - {parts[3]} {parts[4]}"

    def analyse(self, fen: str, moves=None, depth=12, movetime=1000,
                use_movetime=True, hash_mb=64, threads=1,
                chase_rule="skyrule", multipv: int = 1):
        """请求引擎分析当前局面，不自动落子。结果通过 analysis_ready 回传。"""
        if not self.running:
            self.failed.emit("引擎未加载")
            return
        if self._busy:
            self.failed.emit("引擎正在分析中")
            return
        parts = fen.split()
        if len(parts) == 5 and parts[2] != "-" and not self.supports_jieqi:
            self.failed.emit("当前引擎不支持揭棋暗子局面，请加载揭棋引擎"
                             "（AB JChess / PikaJieQi）")
            return
        self._busy = True
        self._req_id += 1
        req = Analysis(fen=fen, request_id=self._req_id)
        with self._req_lock:
            self._reqs.append(req)
        self._cur = req
        self._bestmove_event.clear()
        self.set_option("Hash", hash_mb)
        self.set_option("Threads", threads)
        mpv = max(1, int(multipv))
        if mpv != getattr(self, "_mpv_value", 1):
            self.set_option("MultiPV", mpv)
            self._mpv_value = mpv
        if "ChaseRule" in self.info.options:
            self.set_option("ChaseRule", chase_rule)
        pos = f"position fen {self._normalize_fen(fen)}"
        if moves:
            pos += " moves " + " ".join(moves)
        self._send(pos)
        self._send(f"go movetime {int(movetime)}" if use_movetime
                   else f"go depth {int(depth)}")
        timeout = (movetime / 1000 + 15) if use_movetime else 90
        threading.Thread(target=self._wait_result, args=(timeout,),
                         daemon=True).start()

    def _wait_result(self, timeout: float):
        if not self._bestmove_event.wait(timeout):
            self._busy = False
            # 引擎异常：清空未完成请求，避免迟到的 bestmove 错配到新局面
            with self._req_lock:
                self._reqs.clear()
            self._send("stop")
            self.failed.emit("引擎分析超时（未返回 bestmove）")


class EngineManager(QObject):
    """多引擎列表管理（鲨鱼 / 鹏飞式"引擎管理"），路径由 QSettings 持久化。"""

    changed = Signal(object)

    def __init__(self, options, parent=None):
        super().__init__(parent)
        self.options = options
        self.engines: list[EngineInfo] = []
        self.active: UciEngine | None = None
        # QSettings 可能残留已移动/删除的旧路径（单 exe 换目录时常见），先过滤
        paths = [p for p in (options.engine_paths or []) if os.path.isfile(p)]
        # 随程序附带的 AB JChess 揭棋引擎自动登记为默认引擎
        bundled = bundled_engine_path()
        if bundled and os.path.normcase(os.path.abspath(bundled)) not in \
                [os.path.normcase(os.path.abspath(p)) for p in paths]:
            paths.insert(0, bundled)
        # 首次运行自动发现本机引擎（皮卡鱼 / 皮卡揭等）
        known = {os.path.normcase(os.path.abspath(p)) for p in paths}
        for path in discover_engines():
            if os.path.normcase(path) not in known:
                paths.append(path)
                known.add(os.path.normcase(path))
        for path in paths:
            info = EngineInfo(path=path)
            info.jieqi_capable = is_jieqi_name(os.path.basename(path))
            self.engines.append(info)
        self._persist()

    def add(self, path: str) -> EngineInfo:
        path = os.path.abspath(path)
        for e in self.engines:
            if os.path.normcase(e.path) == os.path.normcase(path):
                return e
        info = EngineInfo(path=path, name=os.path.basename(path))
        info.jieqi_capable = is_jieqi_name(path)
        self.engines.append(info)
        self._persist()
        self.changed.emit(self.engines)
        return info

    def scan(self) -> int:
        """手动扫描本机引擎，返回新增数量。"""
        known = {os.path.normcase(os.path.abspath(e.path))
                 for e in self.engines}
        n = 0
        for path in discover_engines(limit=24):
            if os.path.normcase(path) not in known:
                self.add(path)
                known.add(os.path.normcase(path))
                n += 1
        return n

    def remove(self, index: int):
        if 0 <= index < len(self.engines):
            if self.active and self.active.path == self.engines[index].path:
                self.unload()
            self.engines.pop(index)
            self._persist()
            self.changed.emit(self.engines)

    def load(self, index: int) -> UciEngine:
        self.unload()
        eng = UciEngine(self.engines[index].path)
        eng.start()
        self.active = eng
        return eng

    def unload(self):
        if self.active:
            self.active.quit()
            self.active = None

    def _persist(self):
        self.options.engine_paths = [e.path for e in self.engines]


class AutoPlayController(QObject):
    """人机对战控制器：引擎按所选执子方自动落子。

    界面只需：
      configure(game, options, side) -> set_engine(eng) -> start()/stop()
      connect move_ready 到 game.play + refresh
      局面变化（用户走子 / 新局 / 悔棋）后调用 on_position_changed()
    """

    AVAILABLE = True
    move_ready = Signal(object)   # Move
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled = False
        self.game = None
        self.options = None
        self.engine: UciEngine | None = None
        self.side = Side.BLACK
        self._waiting = False
        self._connected: UciEngine | None = None
        self._generation = 0

    def configure(self, game, options, side=Side.BLACK):
        self.game = game
        self.options = options
        self.side = Side(side)

    def set_engine(self, engine):
        if self._connected is not None:
            for sig, slot in (("analysis_ready", self._on_analysis),
                              ("failed", self._on_failed)):
                try:
                    getattr(self._connected, sig).disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.engine = engine
        self._connected = engine
        if engine is not None:
            engine.analysis_ready.connect(self._on_analysis)
            engine.failed.connect(self._on_failed)

    def start(self, side=None):
        if side is not None:
            self.side = Side(side)
        if self.game is None or self.engine is None:
            self.status.emit("请先加载引擎")
            return
        self.enabled = True
        self._waiting = False
        self._generation += 1
        self.status.emit(
            f"人机对战开始：你执 {self.side.opponent.label}，"
            f"引擎执 {self.side.label}")
        self.maybe_move()

    def stop(self):
        self.enabled = False
        self._waiting = False
        self._generation += 1
        if self.engine is not None and self.engine.running:
            self.engine.stop()
        self.status.emit("人机对战已停止")

    def on_position_changed(self):
        self.maybe_move()

    def maybe_move(self):
        if not self.enabled or self.game is None:
            return
        if self._waiting:
            return
        if self.game.result is not None:
            self.enabled = False
            self.status.emit("对局结束，人机对战停止")
            return
        if self.game.side_to_move != self.side:
            return
        if self.engine is None or not self.engine.running:
            self.status.emit("引擎未加载，无法自动走子")
            return
        if self.game.mode == Mode.JIEQI and not self.engine.supports_jieqi:
            self.status.emit("当前引擎不支持揭棋，请加载 AB JChess / PikaJieQi")
            return
        if getattr(self.engine, "_busy", False):
            QTimer.singleShot(200, self.maybe_move)
            return
        fen = self.game.board.to_fen(
            self.game.side_to_move, self.game.current.halfmove_clock,
            self.game.fullmove)
        self._waiting = True
        self._generation += 1
        self.status.emit(f"引擎（{self.side.label}）思考中…")
        self.engine.analyse(
            fen,
            depth=self.options.engine_depth,
            movetime=self.options.engine_movetime,
            use_movetime=self.options.engine_use_movetime,
            hash_mb=self.options.engine_hash,
            threads=self.options.engine_threads,
        )

    def _on_analysis(self, analysis):
        if not self.enabled or not self._waiting:
            return
        self._waiting = False
        if self.game is None or self.game.side_to_move != self.side:
            return
        mv = (uci_to_move(analysis.bestmove,
                          self.options.engine_rank_flip)
              if analysis.bestmove else None)
        if mv is None or not self.game.is_legal(mv):
            self.status.emit(
                f"引擎着法 {analysis.bestmove} 与当前局面不符，已忽略")
            return
        self.status.emit(f"引擎落子 {analysis.bestmove}")
        self.move_ready.emit(mv)

    def _on_failed(self, message):
        if self._waiting:
            self._waiting = False
            self.status.emit("引擎：" + message)
