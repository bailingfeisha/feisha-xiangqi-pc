# -*- coding: utf-8 -*-
"""连续识别控制器：截图 worker 线程 + 主线程自动下棋状态机。

识别（截图 / ONNX 推理）放在 QThread，界面不卡；状态机留在主线程，
通过 analyse_request 信号请求引擎分析（引擎由视觉窗口选择/持有）。

三条硬性约束（针对「抢走 / 按上个局面出招 / 揭棋不走棋」）：

1. 自动判色：从将帅位置推断目标窗口朝向（我方在下方），并在观察期内
   用「检测到对手走了一步」校正执子，无需手工选红黑。
2. 轮次门控：我方点击后先确认落子生效（可观测签名 == 预期局面），
   再等待一次「对手走子」的变化才允许下一步；开局执黑必须等对手先走。
   绝不在上一局面未变化、或动画中间态时抢着落子。
3. 局面新鲜度：每次分析带 generation；分析期间局面一旦变化立即标记过期，
   回调时只用「当前最新稳定局面」校验着法，签名不符就丢弃结果，
   绝不拿几秒前旧局面算出来的 bestmove 去点击。

v1.2 修复（模拟器 / 连续不符 / 中局启动卡死）：
  * 中途开启自动下棋时用「静态局面轮次提示」判轮次，不再无条件等对手；
  * 着法不符时输出完整诊断、自动纠正 rank 方向 / 执子判反，并且**不锁死**；
  * 点击未生效按链自动降级：后台 Post → Send → ADB → 真实鼠标；
  * 新增「立即出招 / 变招 / 重新判定轮次」外部接口。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QThread, Signal, Slot, QTimer, Qt, QMetaObject

from core.constants import Mode, Side
from core.notation import uci_to_move, move_to_uci
from core.options import Options
from core.rules import legal_moves
from vision.capture import capture
from vision.clicker import (ScreenMapper, play_move, request_abort,
                            clear_abort, aborted)
from vision.model import YoloDetector
from vision.reconstruct import (fit_grid, reconstruct, detect_view,
                                observable_signature, infer_actor,
                                is_initial_layout, position_turn_hint,
                                rotate_move)

PHASE_OBSERVE = "observing"     # 自动判色 / 等待开局
PHASE_READY = "ready"           # 轮到我，等待稳定后请求分析
PHASE_THINK = "thinking"        # 引擎分析中
PHASE_WAIT = "waiting"          # 我方已落子，等待生效 + 对手走子

# key -> (play_move 的 method, message_mode, 界面说明)
CLICK_MODES = {
    "message_post": ("message", "post", "后台消息 PostMessage"),
    "message_send": ("message", "send", "后台消息 SendMessage"),
    "adb": ("adb", "post", "ADB 触摸（模拟器）"),
    "mouse": ("mouse", "post", "真实鼠标点击"),
}


@dataclass
class VisionConfig:
    hwnd: int = 0
    title: str = ""
    mode: Mode = Mode.XIANGQI
    flip: bool = False
    channel_order: str = "RGB"
    conf_thr: float = 0.35
    iou_thr: float = 0.45
    interval_ms: int = 450
    prefer_printwindow: bool = True
    side_mode: str = "auto"        # auto / red / black
    my_side: Side = Side.RED       # 手动模式的执子；auto 模式下为判定结果
    auto_flip: bool = True         # 自动判色时同步推断视角翻转
    autoplay: bool = False
    wait_for_opponent_first: bool = False
    assume_my_turn_on_start: bool = True   # 启动/重开局面稳定时默认轮到我
    wait_timeout_sec: float = 8.0          # 明确轮到对手时，久等无变化的自动放行（0=一直等）
    observe_ms: int = 2500         # 自动判色观察期
    click_confirm_ms: int = 2500   # 点击后等待落子生效的超时
    stable_frames: int = 3
    cooldown_ms: int = 600
    max_moves: int = 120
    click_method: str = "message"   # message(后台) / mouse / adb
    message_mode: str = "post"      # post / send
    click_mode: str = "auto"        # auto / message_post / message_send / adb / mouse
    is_emulator: bool = False
    adb_ready: bool = False
    adb_pid: int = 0
    resolve_child: bool = False     # 解析接收消息的子窗口
    offset_x: int = 0               # 点击坐标微调（物理像素）
    offset_y: int = 0
    verify_title: bool = False      # 点击前严格校验窗口标题
    drag: bool = False
    click_gap_ms: int = 130
    model_path: str = ""


class CaptureWorker(QObject):
    """在 QThread 内定时截图 + 推理。"""

    frame = Signal(object, object, object, object)   # rgb, dets, grid, rect
    failed = Signal(str)

    def __init__(self, cfg: VisionConfig):
        super().__init__()
        self.cfg = cfg
        self.detector: YoloDetector | None = None
        self._timer: QTimer | None = None
        self._busy = False
        self._last_error = ""
        self._empty_frames = 0

    @Slot()
    def init(self):
        try:
            self.detector = YoloDetector(
                path=self.cfg.model_path or None,
                conf_thr=self.cfg.conf_thr,
                iou_thr=self.cfg.iou_thr,
                channel_order=self.cfg.channel_order)
        except Exception as exc:
            self.failed.emit(f"识别模型加载失败：{exc}")
            return
        self._timer = QTimer(self)
        self._timer.setInterval(max(120, int(self.cfg.interval_ms)))
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()

    def _warn(self, text: str):
        if text != self._last_error:
            self._last_error = text
            self.failed.emit(text)

    @Slot()
    def _tick(self):
        if self._busy or aborted() or self.detector is None:
            return
        self._busy = True
        try:
            img, info = capture(self.cfg.hwnd, self.cfg.prefer_printwindow)
            if img is None:
                self._empty_frames += 1
                if self._empty_frames >= 3:
                    self._warn("目标窗口截图失败（已关闭 / 最小化？）")
                return
            self._empty_frames = 0
            self._last_error = ""
            dets = self.detector.detect(img)
            grid = fit_grid(dets)
            self.frame.emit(img, dets, grid, info.rect if info else None)
        except Exception as exc:
            self._warn(f"识别异常：{exc}")
        finally:
            self._busy = False

    @Slot()
    def stop(self):
        if self._timer is not None:
            self._timer.stop()


class VisionController(QObject):
    frame_ready = Signal(object, object, object)      # rgb, dets, grid
    position_ready = Signal(object)                   # Reconstruction
    analyse_request = Signal(str, object, int)        # fen, rec, generation
    engine_stop_request = Signal()                    # 局面变化，停止旧分析
    side_resolved = Signal(object, str)               # Side, 依据说明
    turn_state = Signal(str)                          # 轮次/状态文本
    suggestion = Signal(object)                       # Move
    status = Signal(str)
    log = Signal(str)
    click_method_changed = Signal(str, str)           # 当前落子 key, 说明
    diagnostics = Signal(str)                         # 着法不符诊断文本

    def __init__(self, cfg: VisionConfig, options: Options | None = None,
                 parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.options = options or Options()
        self.adb_bridge = None                        # 由界面注入
        self._thread: QThread | None = None
        self._worker: CaptureWorker | None = None
        self.phase = PHASE_OBSERVE
        self.autoplay = bool(cfg.autoplay)
        self._last_sig = ""
        self._stable = 0
        self._last_rec = None
        self._prev_rec = None          # 上一帧局面，用于推断谁走了
        self._rect = None
        self._grid = None
        self._moves = 0
        self._cooldown_until = 0.0
        self._retry_after = 0.0
        self._illegal_streak = 0

        # 自动判色 / 轮次
        self._my_side: Side | None = None
        self._view_votes: deque = deque(maxlen=7)
        self._observe_deadline = 0.0
        self._turn_ready = False
        self._saw_opponent_move = False
        self._mismatch_sig = ""
        self._wait_change_time = 0.0
        self._wait_hint_logged = False
        self._change_from = None        # 最近一次局面变化「之前」的稳定 board
        self._last_heartbeat = 0.0
        self._last_no_board = 0.0
        self._last_wait_log = 0.0
        self._wait_since = 0.0

        # 分析新鲜度
        self._gen = 0
        self._pending_gen = 0
        self._pending_sig = ""
        self._pending_fen = ""
        self._stale = False

        # 着法坐标解析（可按引擎返回自动纠正，不改全局设置）
        self._rank_flip = bool(self.options.engine_rank_flip)

        # 落子方式链
        self._click_chain: list[str] = []
        self._click_index = 0

        # 落子后确认
        self._pre_click_sig = ""
        self._expected_sig = ""
        self._expected_board = None
        self._wait_sig = ""
        self._click_time = 0.0
        self._wait_start = 0.0
        self._click_retries = 0
        self._applied_logged = False

    # ---------- 生命周期 ----------
    def start(self):
        if self._thread is not None:
            return
        clear_abort()
        self._thread = QThread(self)
        self._worker = CaptureWorker(self.cfg)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.init)
        self._worker.frame.connect(self._on_frame, Qt.QueuedConnection)
        self._worker.failed.connect(self.status)
        self._thread.start()
        self.status.emit("连续识别已启动")
        if self.autoplay:
            self._begin_autoplay()

    def stop(self):
        request_abort()
        if self._worker is not None and self._thread is not None:
            try:
                QMetaObject.invokeMethod(self._worker, "stop",
                                         Qt.BlockingQueuedConnection)
            except Exception:
                pass
            self._thread.quit()
            self._thread.wait(3000)
        self._thread = None
        self._worker = None
        self.autoplay = False
        self.phase = PHASE_OBSERVE
        self.status.emit("连续识别已停止")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    @property
    def my_side(self) -> Side | None:
        return self._my_side

    def update_config(self, **kwargs):
        side_changed = False
        for k, v in kwargs.items():
            if not hasattr(self.cfg, k):
                continue
            if k in ("side_mode", "my_side") and getattr(self.cfg, k) != v:
                side_changed = True
            setattr(self.cfg, k, v)
        if self._worker is not None and self._worker.detector is not None:
            d = self._worker.detector
            d.conf_thr = self.cfg.conf_thr
            d.iou_thr = self.cfg.iou_thr
            d.channel_order = self.cfg.channel_order
        if self._worker is not None and self._worker._timer is not None:
            self._worker._timer.setInterval(max(120, self.cfg.interval_ms))
        if side_changed and self.autoplay:
            self._begin_autoplay()

    # ---------- 落子方式链 ----------
    def _setup_click_chain(self):
        mode = self.cfg.click_mode
        if mode in CLICK_MODES and mode != "auto":
            # 手动选 ADB 也保留真实鼠标兜底：坐标没调好时不至于彻底不动
            self._click_chain = (["adb", "mouse"] if mode == "adb"
                                 else [mode])
        elif self.cfg.is_emulator:
            # 模拟器主窗口基本不处理 WM_LBUTTON*，别在后台消息上白等
            self._click_chain = (["adb", "mouse"] if self.cfg.adb_ready
                                 else ["mouse"])
            if self.cfg.adb_ready:
                self.log.emit("检测到模拟器：优先 ADB 触摸，失败自动降级真实鼠标")
            else:
                self.log.emit("检测到模拟器：后台消息通常无效，直接用真实鼠标"
                              "（可点「ADB 探测」改用不占鼠标的触摸）")
        else:
            self._click_chain = ["message_post", "message_send", "mouse"]
        self._click_index = 0
        self._apply_click_mode()

    def _apply_click_mode(self):
        if not self._click_chain:
            self._setup_click_chain()
            return
        self._click_index = min(self._click_index, len(self._click_chain) - 1)
        key = self._click_chain[self._click_index]
        method, mode, label = CLICK_MODES.get(key, CLICK_MODES["message_post"])
        self.cfg.click_method, self.cfg.message_mode = method, mode
        self.click_method_changed.emit(key, label)

    def _advance_click(self) -> bool:
        """点击未生效 → 换下一种落子方式；返回是否还有得换。"""
        if self._click_index + 1 >= len(self._click_chain):
            return False
        self._click_index += 1
        key = self._click_chain[self._click_index]
        _m, _mm, label = CLICK_MODES[key]
        self._apply_click_mode()
        self.log.emit(f"点击未生效，自动切换落子方式 → {label}")
        return True

    # ---------- 自动下棋 ----------
    def _begin_autoplay(self):
        self._moves = 0
        self._stable = 0
        self._last_sig = ""
        self._last_rec = None
        self._prev_rec = None
        self._cooldown_until = 0.0
        self._retry_after = 0.0
        self._illegal_streak = 0
        self._pending_gen = 0
        self._pending_sig = ""
        self._pending_fen = ""
        self._stale = False
        self._click_retries = 0
        self._applied_logged = False
        self._my_side = None
        self._view_votes.clear()
        self._turn_ready = False
        self._saw_opponent_move = False
        self._mismatch_sig = ""
        self._wait_change_time = 0.0
        self._wait_hint_logged = False
        self._change_from = None
        self._last_heartbeat = 0.0
        self._last_no_board = 0.0
        self._last_wait_log = 0.0
        self._wait_since = 0.0
        self._rank_flip = bool(self.options.engine_rank_flip)
        self._setup_click_chain()

        if self.cfg.side_mode in ("red", "black"):
            self._my_side = Side.RED if self.cfg.side_mode == "red" else Side.BLACK
            # 不再「执黑就无脑等对手」：短观察后按局面轮次决定
            self._observe_deadline = time.time() + min(
                0.8, self.cfg.observe_ms / 1000.0)
            self.side_resolved.emit(self._my_side, "手动指定")
            self.turn_state.emit(f"执{self._my_side.label} · 正在判定当前轮次…")
        else:
            self._observe_deadline = time.time() + self.cfg.observe_ms / 1000.0
            self.turn_state.emit("自动判色中：观察目标窗口朝向 / 当前轮次…")
        self.phase = PHASE_OBSERVE
        key = (self._click_chain[0] if self._click_chain else "message_post")
        how = CLICK_MODES.get(key, CLICK_MODES["message_post"])[2]
        self.status.emit(f"自动下棋已开启（{how}，仅对选定窗口）")
        self.log.emit(f"自动下棋启动：棋种 {self.cfg.mode.label}，落子 {how}")

    def set_autoplay(self, enabled: bool):
        self.autoplay = bool(enabled)
        if enabled:
            clear_abort()
            self._begin_autoplay()
        else:
            request_abort()
            self.engine_stop_request.emit()
            self.phase = PHASE_OBSERVE
            self._stale = False
            self._pending_gen = 0
            self.status.emit("自动下棋已停止（识别同步继续）")
            self.log.emit(f"本次共自动落子 {self._moves} 步")
            self.turn_state.emit("自动下棋已停止")

    # ---------- 自动判色 / 轮次 ----------
    def _grant_turn(self, reason: str):
        self._turn_ready = True
        self.phase = PHASE_READY
        self._retry_after = 0.0
        self._mismatch_sig = ""
        self._wait_since = 0.0
        side = self._my_side.label if self._my_side is not None else "我方"
        self.turn_state.emit(f"执{side} · 轮到我 · {reason}")
        self.log.emit(f"轮次判定：轮到我（{reason}）")

    def _finish_observe(self, rec):
        """观察期结束：判定「现在轮到谁」。

        关键：静态局面**明确**显示轮到对手时（执黑完整开局、红先手，
        或揭棋翻子数为偶数），必须继续等，不能被「启动时默认轮到我」
        覆盖，否则会在对手还没走时抢先落子、随后卡在等待里。
        只有静态信息**无法判定**（中局）时才用默认策略。
        """
        if self._my_side is None or rec is None:
            return
        now = time.time()
        if not self._wait_since:
            self._wait_since = now
        if self.cfg.wait_for_opponent_first and not self._saw_opponent_move:
            if now - self._last_wait_log >= 3.0:
                self._last_wait_log = now
                self.turn_state.emit("已勾选「等对手先走」· 一直等待对手落子"
                                     "（可点「立即出招」跳过）")
            self._observe_deadline = now + 30.0
            return
        turn, why = position_turn_hint(rec.board, self.cfg.mode)
        if turn is None and is_initial_layout(rec.board) and \
                self._my_side == Side.BLACK:
            # 普通象棋没有「翻子数」线索，用完整开局布局兜底
            turn, why = self._my_side.opponent, "开局完整布局，红方先手"
        if turn == self._my_side:
            self._grant_turn(why or "局面判定轮到我")
        elif turn == self._my_side.opponent:
            # 明确轮到对手：等到「检测到对手走子变化」或超时自动放行
            limit = self.cfg.wait_timeout_sec
            left = max(0.0, limit - (now - self._wait_since)) if limit > 0 else 0
            if now - self._last_wait_log >= 3.0:
                self._last_wait_log = now
                tail = (f" · {left:.0f} 秒无变化将自动出招" if limit > 0
                        else " · 一直等待（超时设为 0）")
                self.turn_state.emit(
                    f"执{self._my_side.label} · 轮到对手（{why}）· 等对手落子"
                    + tail + " · 也可点「立即出招」")
                self.log.emit(f"轮次判定：轮到对手（{why}），等待其走子")
            self._observe_deadline = now + 1.0
        elif self.cfg.assume_my_turn_on_start:
            self._grant_turn(
                "局面无法静态判定轮次，启动后稳定 → 按当前轮到我处理")
        else:
            self.turn_state.emit("局面无法判定轮次，等待对手变化…")
            self._observe_deadline = time.time() + 2.0

    def _resolve_side(self, rec, prev_rec, view):
        """开局观察：视角推断 + 对手走子校正 + 静态轮次判定。

        致命细节：一步走子只发生在「某一帧」，下一帧 prev_rec 就已经是
        新局面了。用相邻两帧算 infer_actor 的话，变化帧 _stable=1 会被
        防抖过滤掉，之后每帧 actor=None → 执黑开局永远等不到对手走子。
        因此用「变化前的稳定局面」(_change_from) 持续比对当前局面。
        """
        actor = (infer_actor(self._change_from, rec.board)
                 if self._change_from is not None else None)
        if self._my_side is not None:
            now = time.time()
            limit = self.cfg.wait_timeout_sec
            # 久等无变化：别让用户卡在「等对手」里，自动按轮到我放行
            if not self._turn_ready and not self._saw_opponent_move and \
                    not self.cfg.wait_for_opponent_first and \
                    self._wait_since and limit > 0 and \
                    now - self._wait_since >= limit:
                self._grant_turn(
                    f"等待对手 {limit:.0f} 秒无变化 → 按当前轮到我出招"
                    "（把「久等自动出招」设为 0 可一直等）")
                return
            if not self._turn_ready and self._stable >= 2 and \
                    self._change_from is not None:
                if actor is not None and actor != self._my_side:
                    self._saw_opponent_move = True
                elif actor is None and self._stable >= 3:
                    # 揭棋翻子 / 动画 / 识别抖动会让差异格数 >4，
                    # 但局面已稳定：执黑开局时只可能是对手走了一步
                    self._saw_opponent_move = True
                    self.log.emit("对手走子：变化来源不明确，按局面已稳定放行")
            if not self._turn_ready:
                if self._saw_opponent_move:
                    self._grant_turn("已检测到对手走子")
                elif actor == self._my_side:
                    # 我方刚走（用户手动走了一步），应继续等对手应手
                    self._observe_deadline = time.time() + 1.0
                elif time.time() >= self._observe_deadline:
                    self._finish_observe(rec)
            return

        flip, view_side, reason = view or (False, Side.RED, "默认红方在下")

        # 1) 观察期内若出现一步走子，「走子方 = 对手」优先级最高
        if actor is not None:
            self._commit_side(actor.opponent,
                              f"检测到{actor.label}方先走（对手）",
                              rec=rec, saw_opponent=True)
            return

        # 2) 视角投票高度一致 / 观察期结束 → 用朝向判定
        votes = list(self._view_votes)
        agree = max(sum(1 for _, s in votes if s == Side.RED),
                    sum(1 for _, s in votes if s == Side.BLACK))
        if len(votes) >= 3 and agree >= len(votes) - 1:
            side = (Side.RED if sum(1 for _, s in votes if s == Side.RED) >=
                    sum(1 for _, s in votes if s == Side.BLACK) else Side.BLACK)
            self._commit_side(side, reason, rec=rec)
        elif time.time() >= self._observe_deadline:
            self._commit_side(view_side, reason + "（观察超时）", rec=rec)

    def _commit_side(self, side: Side, reason: str, rec=None,
                     saw_opponent: bool = False):
        self._my_side = Side(side)
        self.cfg.my_side = self._my_side
        self._saw_opponent_move = saw_opponent
        # 视角与执子必须自洽：红在下 → 执红不翻转；黑在下 → 执黑翻转。
        if self.cfg.side_mode == "auto" and self.cfg.auto_flip:
            expected = (self._my_side == Side.BLACK)
            if self.cfg.flip != expected:
                self.cfg.flip = expected
                self.log.emit(f"视角自动校正：flip={expected}"
                              "（与执子判定保持一致）")
        self.side_resolved.emit(self._my_side, reason)
        self.log.emit(f"自动判色：执{self._my_side.label}（{reason}）")
        self._turn_ready = False
        self.phase = PHASE_OBSERVE
        if rec is not None:
            self._finish_observe(rec)      # 立即按局面判定轮次，不再干等
        else:
            self._observe_deadline = time.time() + 0.8

    # ---------- 帧处理 ----------
    @Slot(object, object, object, object)
    def _on_frame(self, img, dets, grid, rect):
        if rect:
            self._rect = rect
        if grid is not None:
            self._grid = grid
        self.frame_ready.emit(img, dets, grid)
        now = time.time()
        if self.autoplay and now - self._last_heartbeat >= 3.0:
            self._last_heartbeat = now
            self._emit_heartbeat(now)

        # 自动模式下先推断视角（投票去抖），保证本帧 reconstruct 与点击
        # 坐标用同一个 flip；flip 抖动会让局面签名每帧都变。
        view = None
        if self.autoplay and self.cfg.side_mode == "auto" \
                and self._my_side is None and grid is not None:
            view = detect_view(dets, grid)
            self._view_votes.append((view[0], view[1]))
            if self.cfg.auto_flip:
                votes = list(self._view_votes)
                maj_flip = sum(1 for f, _ in votes if f) * 2 >= len(votes)
                self.cfg.flip = maj_flip if len(votes) >= 2 else view[0]

        # 注意 Side.RED == 0，不能用 `a or b` 取默认值
        side = self._my_side if self._my_side is not None else self.cfg.my_side
        rec = reconstruct(dets, mode=self.cfg.mode, options=self.options,
                          flip=self.cfg.flip, side_to_move=side)
        if rec is None:
            if now - self._last_no_board >= 3.0:
                self._last_no_board = now
                self.status.emit(
                    "未识别到主棋盘：可降低置信度或切换通道顺序（连续 3 秒）")
            return

        prev_rec, self._prev_rec = self._prev_rec, rec
        self._last_rec = rec
        self.position_ready.emit(rec)

        if rec.signature == self._last_sig:
            self._stable += 1
        else:
            # 记住「变化之前」的稳定局面，后续每一帧都能据此判断是谁走的
            self._change_from = prev_rec.board if prev_rec is not None else None
            self._last_sig = rec.signature
            self._stable = 1
            self.log.emit(f"局面变化：{rec.n_pieces} 子 / {rec.n_dark} 暗子")
            if self._mismatch_sig and rec.signature != self._mismatch_sig:
                self._illegal_streak = 0
                self._mismatch_sig = ""
            if self.phase == PHASE_THINK and not self._stale:
                self._stale = True
                self.engine_stop_request.emit()
                self.log.emit("分析期间局面已变化 → 标记本次分析过期")

        if not self.autoplay or time.time() < self._cooldown_until:
            return

        # ---- 观察 / 判色 / 等对手先走 ----
        if self.phase == PHASE_OBSERVE:
            if self._my_side is None or not self._turn_ready:
                self._resolve_side(rec, prev_rec, view)
            return

        # ---- 我方已落子，等待生效 + 对手走子（轮次门控）----
        if self.phase == PHASE_WAIT:
            self._handle_wait(rec)
            return

        if self.phase == PHASE_THINK:
            return

        # ---- 轮到我：稳定后才请求分析 ----
        if not self._turn_ready or time.time() < self._retry_after:
            return
        if self._stable < self.cfg.stable_frames:
            return
        if self._moves >= self.cfg.max_moves:
            self.set_autoplay(False)
            self.log.emit("达到最大步数上限，自动下棋停止")
            return
        self._request_analysis(rec)

    def _handle_wait(self, rec):
        if self._stable < self.cfg.stable_frames:
            return
        sig = rec.signature
        now = time.time()

        # a) 点击完全没生效 → 换落子方式并限时重试，避免无声卡死
        if sig == self._pre_click_sig:
            if now - self._click_time > self.cfg.click_confirm_ms / 1000.0:
                self._click_retries += 1
                if self._click_retries > 3:
                    self.set_autoplay(False)
                    self.log.emit("多次点击未生效，已停止自动下棋"
                                  "（检查落子方式 / 坐标微调 / ADB 设置）")
                    self.turn_state.emit("点击未生效，自动下棋已停止")
                    return
                if self.cfg.click_method == "adb" and \
                        self.adb_bridge is not None:
                    self.log.emit(
                        "ADB 点击未生效诊断：实际触摸 " +
                        (self.adb_bridge.describe_last_tap() or "(无)") +
                        " · " + self.adb_bridge.status_text() +
                        " · 检查 X/Y 微调或改真实鼠标")
                self._advance_click()
                self.phase = PHASE_READY
                # 关键：落子时把 _turn_ready 关了，重试必须还回来，
                # 否则会永久卡在「phase=ready 轮到我=False」再也不出招。
                self._turn_ready = True
                self._retry_after = now + 0.3
                self.log.emit(f"点击未生效，重试（第 {self._click_retries} 次）"
                              f" · 当前落子 {self.cfg.click_method}")
            return

        # b) 我方落子已生效 → 继续等对手，绝不抢走
        if sig == self._expected_sig:
            if not self._applied_logged:
                self._applied_logged = True
                self.log.emit("我方落子已确认生效，等待对手走子")
                self.turn_state.emit("等待对手走子（不抢走）")
            if not self._wait_hint_logged and self._wait_start and \
                    now - self._wait_start > 18.0:
                self._wait_hint_logged = True
                self.log.emit("已等待对手较久；若确实轮到我，可点「立即出招」")
            return

        # c) 出现新变化：确认是对手走的，才把回合交还给我方
        if not self._wait_change_time:
            self._wait_change_time = now
        actor = infer_actor(self._expected_board, rec.board)
        if actor == self._my_side:
            return                      # 我方落子动画中间态，继续等
        if actor is None and (now - self._wait_change_time) < 1.5:
            return                      # 变化来源不明（识别抖动），再观察
        self._turn_ready = True
        self.phase = PHASE_READY
        self._click_retries = 0
        self._applied_logged = False
        self._wait_change_time = 0.0
        self._wait_hint_logged = False
        self._click_index = 0          # 有效走子后回到首选落子方式
        self._apply_click_mode()
        why = (f"检测到{actor.label}方（对手）走子" if actor is not None
               else "局面已变化且稳定，推断对手已应手")
        self.log.emit(why + "，轮到我方")
        self.turn_state.emit(
            f"执{self._my_side.label} · 轮到我")

    def _emit_heartbeat(self, now: float):
        """每 3 秒打一条内部状态，日志里能直接看出「为什么不下」。"""
        side = (self._my_side if self._my_side is not None
                else self.cfg.my_side)
        self.log.emit(
            f"[状态] phase={self.phase} 轮到我={self._turn_ready} "
            f"稳帧={self._stable}/{self.cfg.stable_frames} "
            f"执子={side.label} flip={self.cfg.flip} 已走={self._moves} "
            f"冷却={max(0.0, self._cooldown_until - now):.1f}s "
            f"重试={max(0.0, self._retry_after - now):.1f}s "
            f"分析号={self._pending_gen} 过期={self._stale} "
            f"落子={self.cfg.click_method}/{self.cfg.message_mode} "
            f"有变化源={self._change_from is not None} "
            f"签名={self._last_sig[:6] or '-'}")

    def request_valid(self, gen: int) -> bool:
        """供界面在延迟重试前确认这次请求还没被作废。"""
        return (self.autoplay and self.phase == PHASE_THINK
                and gen == self._pending_gen)

    def _request_analysis(self, rec):
        self._gen += 1
        self._pending_gen = self._gen
        self._pending_sig = rec.signature
        self._pending_fen = rec.fen
        self._stale = False
        self.phase = PHASE_THINK
        self.log.emit(f"第 {self._moves + 1} 步：请求引擎分析（{rec.fen.split()[1]} 走）")
        self.analyse_request.emit(rec.fen, rec, self._gen)

    # ---------- 引擎结果 ----------
    def on_analysis(self, analysis, gen: int | None = None):
        """引擎分析完成回调；只接受「最新稳定局面」对应的那一次请求。"""
        if self.phase != PHASE_THINK:
            return
        if gen is not None and gen != self._pending_gen:
            return
        self.phase = PHASE_READY
        if analysis is None:
            self._retry_after = time.time() + 0.8
            self.log.emit("引擎未返回结果，稍后重试（不使用旧着法）")
            return

        if self._stale or self._last_rec is None or \
                self._last_rec.signature != self._pending_sig or \
                self._last_rec.fen != self._pending_fen:
            self._stale = False
            self._retry_after = time.time() + 0.2
            self.log.emit("局面/FEN 已变化，丢弃过期引擎结果（不按上个局面出招）")
            self.turn_state.emit("局面变化，重新分析最新局面")
            return

        rec = self._last_rec
        best = getattr(analysis, "bestmove", "") or ""
        mv = uci_to_move(best, self._rank_flip) if best else None
        if mv is None:
            self._retry_after = time.time() + 0.8
            self.log.emit("引擎未返回可用着法")
            return
        side = (self._my_side if self._my_side is not None
                else self.cfg.my_side)
        try:
            legal = legal_moves(rec.board, side, self.options)
        except Exception as exc:
            self._retry_after = time.time() + 0.8
            self.log.emit(f"局面合法性校验失败：{exc}")
            return
        if mv not in legal:
            self._handle_mismatch(best, mv, rec, legal, side, analysis)
            return
        self._illegal_streak = 0
        self._mismatch_sig = ""
        self._execute_move(mv, rec, best, analysis)

    def _handle_mismatch(self, best, mv, rec, legal, side, analysis):
        """着法与局面不符：先诊断 + 自动纠正，绝不永久锁死。"""
        now = time.time()
        alt = uci_to_move(best, not self._rank_flip)
        rot = rotate_move(mv)
        try:
            opp_legal = legal_moves(rec.board, side.opponent, self.options)
        except Exception:
            opp_legal = []

        if alt is not None and alt in legal:
            self._rank_flip = not self._rank_flip
            self.log.emit("自动纠正：引擎着法 rank 方向与当前设置相反，"
                          "已切换坐标解析并继续（不改全局设置）")
            self._illegal_streak = 0
            self._execute_move(alt, rec, best, analysis)
            return

        if mv in opp_legal:
            new_side = side.opponent
            self._my_side = new_side
            self.cfg.my_side = new_side
            self.side_resolved.emit(new_side, "着法校验：执子判反，已自动纠正")
            self.log.emit(f"自动纠正：引擎返回的是对手着法 → 我方改执"
                          f"{new_side.label}，重新分析当前局面（不落子）")
            self._pending_gen = 0
            self._retry_after = now + 0.3
            self._illegal_streak = 0
            self._turn_ready = True
            self.phase = PHASE_READY
            return

        # 真不符：输出可操作的诊断，并软恢复（等下一次局面变化自动重试）
        self._illegal_streak += 1
        self._mismatch_sig = rec.signature
        self._retry_after = now + 1.5
        self._turn_ready = True
        self.phase = PHASE_READY
        diag = (
            f"【着法不符诊断 #{self._illegal_streak}】引擎 {best} → {mv.src}→{mv.dst}\n"
            f"  执子={side.label} · 视角flip={self.cfg.flip} · 棋种={self.cfg.mode.label}\n"
            f"  我方合法着法 {len(legal)} 个 · 对手合法着法 {len(opp_legal)} 个\n"
            f"  旋转180°后{'合法（多为视角/执子判反，可勾『视角翻转』或重新判定轮次）' if rot in legal else '仍不合法'}\n"
            f"  FEN {rec.fen}\n"
            "  建议：核对「棋种 / 分析引擎」是否匹配、执子与视角、"
            "以及识别预览里棋子是否被认错；已自动用最新局面重新分析，不会锁死。")
        self.diagnostics.emit(diag)
        self.log.emit(diag.replace("\n", " | "))
        if self._illegal_streak >= 3:
            self.turn_state.emit("着法连续不符：已自动重新同步，详见诊断面板")
            self._illegal_streak = 0
            self.log.emit("提示：也可点「重新判定轮次」或「立即出招」手动接管")
        else:
            self.turn_state.emit("着法不符，正在用最新局面重新分析")

    def _execute_move(self, mv, rec, best: str, analysis=None):
        """真正落子：换算坐标 → 点击 → 进入 WAIT 等确认。"""
        if self._grid is None or self._rect is None:
            self.log.emit("尚未取得棋盘网格，无法换算点击坐标")
            return False
        if self._last_rec.signature != rec.signature:
            self.log.emit("落子前局面又变了，本次放弃（等待下一稳定局面）")
            return False

        mapper = ScreenMapper(self._rect, self._grid, self.cfg.flip)
        ok = play_move(self.cfg.hwnd, mv, mapper,
                       method=self.cfg.click_method, drag=self.cfg.drag,
                       click_gap_ms=self.cfg.click_gap_ms,
                       expected_title=self.cfg.title,
                       message_mode=self.cfg.message_mode,
                       resolve_child=self.cfg.resolve_child,
                       offset_x=self.cfg.offset_x, offset_y=self.cfg.offset_y,
                       verify_title=self.cfg.verify_title,
                       adb_bridge=self.adb_bridge, adb_pid=self.cfg.adb_pid)
        if not ok:
            self._retry_after = time.time() + 0.5
            if self.adb_bridge is not None and \
                    self.cfg.click_method == "adb":
                self.log.emit("ADB 点击失败：" + (self.adb_bridge.last_error or "未知"))
                self._advance_click()
            else:
                self.log.emit("点击未执行（窗口失效、坐标越界或已中止）")
            return False

        try:
            board_after = rec.board.clone()
            board_after.apply(mv)
            self._expected_sig = observable_signature(board_after)
            self._expected_board = board_after
        except Exception:
            self._expected_sig = ""
            self._expected_board = None
        self._pre_click_sig = rec.signature
        self._click_time = time.time()
        self._wait_start = self._click_time
        self._click_retries = 0
        self._applied_logged = False
        self._wait_change_time = 0.0
        self._wait_hint_logged = False
        self._moves += 1
        self._wait_sig = rec.signature
        self.phase = PHASE_WAIT
        self._turn_ready = False
        self._cooldown_until = time.time() + self.cfg.cooldown_ms / 1000.0
        self.suggestion.emit(mv)
        self.turn_state.emit("我方已落子 · 等待生效与对手应手")
        self.log.emit(f"已落子 {best} · "
                      f"{getattr(analysis, 'summary', '') if analysis else '手动变招'}")
        if self.cfg.click_method == "adb" and self.adb_bridge is not None:
            self.log.emit("ADB 触摸坐标：" +
                          (self.adb_bridge.describe_last_tap() or "(无)") +
                          f" · {self.adb_bridge.status_text()}")
        return True

    # ---------- 外部手动控制 ----------
    def force_now(self):
        """「立即出招」：跳过轮次/稳帧等待，用当前稳定局面马上分析落子。"""
        if not self.autoplay or self._last_rec is None:
            self.log.emit("请先开启自动下棋并等待至少一帧识别")
            return
        self._stale = False
        self._turn_ready = True
        self._retry_after = 0.0
        self._illegal_streak = 0
        self._mismatch_sig = ""
        self._pending_gen = 0
        self.log.emit("手动「立即出招」：以当前识别局面为准重新分析")
        self._request_analysis(self._last_rec)

    def play_now(self, mv) -> bool:
        """「变招」：按用户在候选列表里选的着法立即落子。"""
        rec = self._last_rec
        if rec is None or not self.autoplay:
            self.log.emit("请先开启自动下棋并等待至少一帧识别")
            return False
        side = (self._my_side if self._my_side is not None
                else self.cfg.my_side)
        try:
            legal = legal_moves(rec.board, side, self.options)
        except Exception as exc:
            self.log.emit(f"变招校验失败：{exc}")
            return False
        if mv not in legal:
            self.log.emit(f"变招 {move_to_uci(mv, self._rank_flip)} "
                          "在当前局面不合法，已忽略")
            return False
        self._stale = False
        self._pending_gen = 0
        return self._execute_move(mv, rec, move_to_uci(mv, self._rank_flip))

    def resync(self):
        """「重新判定轮次」：保留执子，重新观察并推断当前该谁走。"""
        if not self.autoplay:
            self.log.emit("请先开启自动下棋")
            return
        self._pending_gen = 0
        self._stale = False
        self._turn_ready = False
        self._retry_after = 0.0
        self._illegal_streak = 0
        self._mismatch_sig = ""
        self._saw_opponent_move = False
        self._observe_deadline = 0.0       # 用户主动重判：下一帧就给结论
        self.phase = PHASE_OBSERVE
        self.log.emit("已重新判定轮次（保留执子，按当前局面重新推断）")
