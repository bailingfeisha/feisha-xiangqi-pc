# -*- coding: utf-8 -*-
"""视觉识别窗口：选目标窗口 → 识别主棋盘 → 同步局面 / 连续自动下棋。

用途：自有或本地单机棋盘窗口的识别与自动化测试。
默认只开启「识别同步」，自动落子需手动开启；程序不做注入、内存读取、
协议抓包或任何反检测。用于在线对弈平台会违反平台规则，请勿这样使用。
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QPushButton, QComboBox, QCheckBox, QSpinBox, QDoubleSpinBox,
    QPlainTextEdit, QLineEdit, QTableWidget, QTableWidgetItem, QTabWidget,
    QMessageBox, QApplication, QSplitter, QHeaderView, QFileDialog,
)

from ai.engine import UciEngine, is_jieqi_name
from core.board import Board
from core.constants import Mode, Side
from core.game import Game
from core.notation import uci_to_move, move_to_uci
from core.rules import legal_moves
from vision.adb_clicker import AdbBridge, AdbTapConfig, detect_emulator
from vision.capture import list_windows, capture, WindowInfo
from vision.controller import VisionConfig, VisionController, PHASE_WAIT
from vision.model import (YoloDetector, CLASS_COLORS, default_model_path,
                          model_source, IGNORED_CLASSES)
from ui.dialogs import QQ_GROUP
from vision.reconstruct import fit_grid, reconstruct

CLICK_CHOICES = ("auto", "message_post", "message_send", "adb", "mouse")


class VisionWindow(QMainWindow):
    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main = main_window
        self.setWindowTitle("视觉识别 / 连续自动下棋 · 飞鲨象棋")
        self.resize(1200, 840)
        self.setMinimumSize(1040, 720)

        self._windows: list[WindowInfo] = []
        self._once_detector: YoloDetector | None = None
        self._last_dets = []
        self._last_rec = None
        self._suggestion = None
        self._bound_engine: UciEngine | None = None
        self._own_engine: UciEngine | None = None   # 视觉窗口独立加载的引擎
        self._engine: UciEngine | None = None        # 当前实际使用的引擎
        self._auto_gen: int | None = None            # 当前自动分析的 generation
        self._auto_fen = ""
        self._analysing = False
        self._emulator = ""
        self._candidates: list = []                  # [(uci, Move)]
        self._last_analysis = None
        self._analyse_start = 0.0
        self._budget_spent = 0.0                     # 已用思考时间（ms）
        self._moves_done = 0
        self._pondering = False
        self._ponder_fen = ""

        self.adb = AdbBridge(AdbTapConfig())
        self.cfg = VisionConfig(model_path=default_model_path())
        self.controller = VisionController(self.cfg, self.main.options, self)
        self.controller.adb_bridge = self.adb
        self.controller.frame_ready.connect(self._on_frame)
        self.controller.position_ready.connect(self._on_position)
        self.controller.analyse_request.connect(self._on_analyse_request)
        self.controller.engine_stop_request.connect(self._on_engine_stop_request)
        self.controller.side_resolved.connect(self._on_side_resolved)
        self.controller.turn_state.connect(self._on_turn_state)
        self.controller.suggestion.connect(self._on_suggestion)
        self.controller.status.connect(self._on_status)
        self.controller.log.connect(self._on_log)
        self.controller.click_method_changed.connect(self._on_click_method_changed)
        self.controller.diagnostics.connect(self._on_diagnostics)

        self._ponder_timer = QTimer(self)
        self._ponder_timer.setInterval(2600)
        self._ponder_timer.timeout.connect(self._on_ponder_tick)

        # 构建控件时 valueChanged / stateChanged 会立即触发参数槽，先屏蔽
        self._building = True
        self._build_ui()
        self._building = False
        self._refresh_windows()
        self._refresh_engine_list()
        self.statusBar().showMessage(
            "选择目标窗口 → 单次识别确认网格 → 选好引擎 → 再开启连续同步 / 自动下棋")
        self._write_log_file(
            "", "=" * 16 +
            f" 连线会话 {time.strftime('%Y-%m-%d %H:%M:%S')} " + "=" * 16)

    # ---------- 界面 ----------
    def _build_ui(self):
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._left_panel())
        splitter.addWidget(self._right_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)
        qq = QPushButton(f"  官方交流群 {QQ_GROUP}  ")
        qq.setToolTip("点击复制群号")
        qq.setFlat(True)
        qq.clicked.connect(self._on_copy_qq)
        self.statusBar().addPermanentWidget(qq)

    def _on_copy_qq(self):
        QApplication.clipboard().setText(QQ_GROUP)
        self.statusBar().showMessage(
            f"官方交流群 {QQ_GROUP} 已复制到剪贴板", 4000)

    def _left_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(8)
        v.setContentsMargins(8, 8, 8, 8)

        g = QGroupBox("① 目标窗口 / 进程")
        gl = QVBoxLayout(g)
        row = QHBoxLayout()
        self.cmb_windows = QComboBox()
        self.cmb_windows.setMinimumWidth(360)
        self.cmb_windows.currentIndexChanged.connect(self._on_window_changed)
        btn_refresh = QPushButton("刷新列表")
        btn_refresh.clicked.connect(self._refresh_windows)
        row.addWidget(self.cmb_windows, 1)
        row.addWidget(btn_refresh)
        gl.addLayout(row)
        self.lbl_win_info = QLabel("尚未选择窗口")
        self.lbl_win_info.setWordWrap(True)
        gl.addWidget(self.lbl_win_info)
        v.addWidget(g)

        self.lbl_preview = QLabel("预览：选择窗口后点「单次识别」")
        self.lbl_preview.setAlignment(Qt.AlignCenter)
        self.lbl_preview.setMinimumSize(560, 400)
        self.lbl_preview.setStyleSheet(
            "background:#20242b;color:#aeb8c8;border:1px solid #3a4250;")
        v.addWidget(self.lbl_preview, 1)

        p = QGroupBox("② 识别参数（只识别主棋盘，忽略托盘计数器）")
        grid = QGridLayout(p)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems([Mode.XIANGQI.label, Mode.JIEQI.label])
        self.cmb_mode.currentIndexChanged.connect(self._on_params_changed)
        grid.addWidget(QLabel("棋种"), 0, 0)
        grid.addWidget(self.cmb_mode, 0, 1)

        self.spin_conf = QDoubleSpinBox()
        self.spin_conf.setRange(0.05, 0.95)
        self.spin_conf.setSingleStep(0.05)
        self.spin_conf.setValue(0.35)
        self.spin_conf.valueChanged.connect(self._on_params_changed)
        grid.addWidget(QLabel("置信度"), 0, 2)
        grid.addWidget(self.spin_conf, 0, 3)

        self.spin_iou = QDoubleSpinBox()
        self.spin_iou.setRange(0.1, 0.9)
        self.spin_iou.setSingleStep(0.05)
        self.spin_iou.setValue(0.45)
        self.spin_iou.valueChanged.connect(self._on_params_changed)
        grid.addWidget(QLabel("NMS IoU"), 0, 4)
        grid.addWidget(self.spin_iou, 0, 5)

        self.cmb_channel = QComboBox()
        self.cmb_channel.addItems(["RGB", "BGR"])
        self.cmb_channel.currentIndexChanged.connect(self._on_params_changed)
        grid.addWidget(QLabel("通道"), 1, 0)
        grid.addWidget(self.cmb_channel, 1, 1)

        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(150, 3000)
        self.spin_interval.setSingleStep(50)
        self.spin_interval.setValue(450)
        self.spin_interval.setSuffix(" ms")
        self.spin_interval.valueChanged.connect(self._on_params_changed)
        grid.addWidget(QLabel("采样间隔"), 1, 2)
        grid.addWidget(self.spin_interval, 1, 3)

        self.chk_flip = QCheckBox("视角翻转（红方在上）")
        self.chk_flip.stateChanged.connect(self._on_params_changed)
        self.chk_pw = QCheckBox("优先 PrintWindow")
        self.chk_pw.setChecked(True)
        self.chk_pw.stateChanged.connect(self._on_params_changed)
        grid.addWidget(self.chk_flip, 1, 4, 1, 2)
        grid.addWidget(self.chk_pw, 2, 4, 1, 2)
        self.lbl_model = QLabel("")
        self.lbl_model.setWordWrap(True)
        grid.addWidget(self.lbl_model, 2, 0, 1, 4)
        v.addWidget(p)

        row2 = QHBoxLayout()
        self.btn_once = QPushButton("单次识别")
        self.btn_once.clicked.connect(self._on_once)
        self.btn_sync = QPushButton("开始连续同步")
        self.btn_sync.setCheckable(True)
        self.btn_sync.clicked.connect(self._on_sync_toggle)
        btn_stop = QPushButton("全部停止")
        btn_stop.clicked.connect(self._on_stop_all)
        for b in (self.btn_once, self.btn_sync, btn_stop):
            row2.addWidget(b)
        v.addLayout(row2)
        return w

    def _right_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 8, 8, 8)
        tabs = QTabWidget()

        # --- 识别结果 ---
        t1 = QWidget()
        v1 = QVBoxLayout(t1)
        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["类别", "置信度", "格子", "中心"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        v1.addWidget(self.tbl, 1)
        self.lbl_stat = QLabel("尚未识别")
        v1.addWidget(self.lbl_stat)
        tabs.addTab(t1, "识别结果")

        # --- 局面 / FEN ---
        t2 = QWidget()
        v2 = QVBoxLayout(t2)
        v2.addWidget(QLabel("还原局面（9 列 × 10 行）："))
        self.txt_board = QPlainTextEdit()
        self.txt_board.setReadOnly(True)
        self.txt_board.setFont(self._mono_font())
        self.txt_board.setMaximumBlockCount(14)
        v2.addWidget(self.txt_board, 2)
        v2.addWidget(QLabel("FEN（AB-JChess 5 段，可直接给引擎）："))
        self.txt_fen = QPlainTextEdit()
        self.txt_fen.setMaximumHeight(74)
        self.txt_fen.setFont(self._mono_font())
        v2.addWidget(self.txt_fen)
        r = QHBoxLayout()
        b_copy = QPushButton("复制 FEN")
        b_copy.clicked.connect(self._on_copy_fen)
        b_import = QPushButton("导入到主棋盘")
        b_import.clicked.connect(self._on_import_board)
        b_analyse = QPushButton("用引擎分析")
        b_analyse.clicked.connect(self._on_manual_analyse)
        for b in (b_copy, b_import, b_analyse):
            r.addWidget(b)
        v2.addLayout(r)
        self.lbl_analyse = QLabel("引擎建议：--")
        self.lbl_analyse.setWordWrap(True)
        v2.addWidget(self.lbl_analyse)
        tabs.addTab(t2, "局面 / FEN")

        # --- 自动下棋 ---
        t3 = QWidget()
        v3 = QVBoxLayout(t3)
        warn = QLabel(
            "用途声明：仅用于你自己开发 / 本地单机的棋盘窗口做识别与自动化测试。"
            "落子方式默认「自动」：普通窗口用后台窗口消息（不移动鼠标、"
            "不抢焦点）；检测到模拟器自动改用 ADB 触摸 / 真实鼠标，"
            "点击未生效会自动降级；不含注入、内存读取或反检测。"
            "请勿用于在线对弈平台（会违反平台规则）。")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "background:#fff3cd;color:#7a4b00;border:1px solid #e6c37a;"
            "border-radius:4px;padding:6px;")
        v3.addWidget(warn)

        ge = QGroupBox("分析引擎（可在这里单独选，不影响主窗口）")
        ve = QVBoxLayout(ge)
        er = QHBoxLayout()
        self.cmb_engine = QComboBox()
        self.cmb_engine.setMinimumWidth(300)
        self.cmb_engine.currentIndexChanged.connect(self._on_engine_selected)
        btn_eng_reload = QPushButton("重新加载")
        btn_eng_reload.clicked.connect(lambda: self._activate_engine(force=True))
        btn_eng_add = QPushButton("添加引擎…")
        btn_eng_add.clicked.connect(self._on_add_engine)
        er.addWidget(self.cmb_engine, 1)
        er.addWidget(btn_eng_reload)
        er.addWidget(btn_eng_add)
        ve.addLayout(er)
        self.lbl_engine_state = QLabel("尚未加载引擎")
        self.lbl_engine_state.setWordWrap(True)
        ve.addWidget(self.lbl_engine_state)
        self.lbl_turn_state = QLabel("轮次：未开始")
        self.lbl_turn_state.setWordWrap(True)
        self.lbl_turn_state.setStyleSheet("color:#1f6f3d;font-weight:bold;")
        ve.addWidget(self.lbl_turn_state)
        v3.addWidget(ge)

        g = QGroupBox("自动下棋参数")
        gl = QGridLayout(g)
        self.cmb_my_side = QComboBox()
        self.cmb_my_side.addItems([
            "自动判断（推荐）", "我方执红（先手）", "我方执黑（后手）"])
        gl.addWidget(QLabel("执子"), 0, 0)
        gl.addWidget(self.cmb_my_side, 0, 1)
        self.chk_wait_first = QCheckBox("等对手先走")
        gl.addWidget(self.chk_wait_first, 0, 2, 1, 2)

        self.spin_stable = QSpinBox()
        self.spin_stable.setRange(1, 10)
        self.spin_stable.setValue(3)
        gl.addWidget(QLabel("稳帧数"), 1, 0)
        gl.addWidget(self.spin_stable, 1, 1)
        self.spin_movetime = QSpinBox()
        self.spin_movetime.setRange(100, 10000)
        self.spin_movetime.setSingleStep(100)
        self.spin_movetime.setValue(600)
        self.spin_movetime.setSuffix(" ms")
        gl.addWidget(QLabel("思考时间"), 1, 2)
        gl.addWidget(self.spin_movetime, 1, 3)

        self.cmb_click = QComboBox()
        self.cmb_click.addItems([
            "自动（推荐：按窗口类型选，失败自动降级）",
            "后台消息 PostMessage（不占鼠标）",
            "后台消息 SendMessage（不占鼠标）",
            "ADB 触摸（模拟器专用，不占鼠标）",
            "真实鼠标点击（会占用鼠标）",
        ])
        gl.addWidget(QLabel("落子方式"), 2, 0)
        gl.addWidget(self.cmb_click, 2, 1, 1, 3)
        self.chk_drag = QCheckBox("拖拽落子")
        gl.addWidget(self.chk_drag, 2, 4)
        self.chk_child = QCheckBox("解析子窗口")
        gl.addWidget(self.chk_child, 3, 4)

        self.spin_gap = QSpinBox()
        self.spin_gap.setRange(40, 800)
        self.spin_gap.setValue(130)
        self.spin_gap.setSuffix(" ms")
        gl.addWidget(QLabel("两次点击间隔"), 3, 0)
        gl.addWidget(self.spin_gap, 3, 1)
        self.spin_cooldown = QSpinBox()
        self.spin_cooldown.setRange(200, 5000)
        self.spin_cooldown.setSingleStep(100)
        self.spin_cooldown.setValue(900)
        self.spin_cooldown.setSuffix(" ms")
        gl.addWidget(QLabel("落子后冷却"), 3, 2)
        gl.addWidget(self.spin_cooldown, 3, 3)

        self.spin_max_moves = QSpinBox()
        self.spin_max_moves.setRange(1, 500)
        self.spin_max_moves.setValue(120)
        gl.addWidget(QLabel("最大自动步数"), 4, 0)
        gl.addWidget(self.spin_max_moves, 4, 1)
        self.chk_auto_flip = QCheckBox("自动校正视角")
        self.chk_auto_flip.setChecked(True)
        self.chk_auto_flip.stateChanged.connect(self._on_params_changed)
        gl.addWidget(self.chk_auto_flip, 4, 2, 1, 2)
        self.chk_assume_turn = QCheckBox(
            "启动 / 重开时按当前局面判定「轮到我」（不再干等对手）")
        self.chk_assume_turn.setChecked(True)
        self.chk_assume_turn.setToolTip(
            "揭棋开局对手已走第一步、或中局轮到我时开启自动下棋，"
            "会直接判定轮次并出招，无需自己先手动走一步。")
        gl.addWidget(self.chk_assume_turn, 5, 0, 1, 4)
        self.spin_wait_timeout = QSpinBox()
        self.spin_wait_timeout.setRange(0, 120)
        self.spin_wait_timeout.setValue(8)
        self.spin_wait_timeout.setSuffix(" 秒")
        self.spin_wait_timeout.setToolTip(
            "明确轮到对手时，多久没检测到对手走子就自动按「轮到我」出招；"
            "0 = 一直等对手（勾了「等对手先走」时始终一直等）")
        gl.addWidget(QLabel("久等自动出招"), 6, 0)
        gl.addWidget(self.spin_wait_timeout, 6, 1)
        tip_timeout = QLabel("0 = 一直等；等对手先走勾上时不受影响")
        tip_timeout.setWordWrap(True)
        gl.addWidget(tip_timeout, 6, 2, 1, 2)
        self.spin_wait_timeout.valueChanged.connect(self._on_params_changed)
        self.cmb_my_side.currentIndexChanged.connect(self._on_params_changed)
        v3.addWidget(g)

        r2 = QHBoxLayout()
        self.btn_auto = QPushButton("开始连续自动下棋")
        self.btn_auto.setCheckable(True)
        self.btn_auto.clicked.connect(self._on_auto_toggle)
        r2.addWidget(self.btn_auto)
        btn_test = QPushButton("测试后台点击（鼠标不动）")
        btn_test.clicked.connect(self._on_test_click)
        r2.addWidget(btn_test)
        v3.addLayout(r2)

        r3 = QHBoxLayout()
        btn_force = QPushButton("立即出招（跳过轮次等待）")
        btn_force.clicked.connect(self._on_force_now)
        btn_resync = QPushButton("重新判定轮次")
        btn_resync.clicked.connect(self._on_resync)
        r3.addWidget(btn_force)
        r3.addWidget(btn_resync)
        self.lbl_click_now = QLabel("落子方式：未启动")
        self.lbl_click_now.setWordWrap(True)
        r3.addWidget(self.lbl_click_now, 1)
        v3.addLayout(r3)

        v3.addWidget(QLabel("运行日志："))
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFont(self._mono_font())
        self.txt_log.setMaximumBlockCount(2000)
        v3.addWidget(self.txt_log, 1)
        tabs.addTab(t3, "自动下棋")
        tabs.addTab(self._control_tab(), "对局控制 / 诊断")
        v.addWidget(tabs)
        return w

    def _control_tab(self) -> QWidget:
        """对局控制：包干用时 / 思考细节 / 后台思考 / 变招 / 模拟器 ADB。"""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(8)
        v.setContentsMargins(6, 8, 6, 8)

        # --- 时间分配 ---
        gt = QGroupBox("时间分配（包干 / 每步）")
        q = QGridLayout(gt)
        self.cmb_time_mode = QComboBox()
        self.cmb_time_mode.addItems([
            "固定每步思考时间", "包干制（总时长按剩余步数分配）",
            "每步递增（越到后面想越久）"])
        q.addWidget(QLabel("策略"), 0, 0)
        q.addWidget(self.cmb_time_mode, 0, 1, 1, 3)
        self.spin_total_sec = QSpinBox()
        self.spin_total_sec.setRange(10, 7200)
        self.spin_total_sec.setValue(300)
        self.spin_total_sec.setSuffix(" 秒")
        self.spin_budget_moves = QSpinBox()
        self.spin_budget_moves.setRange(1, 300)
        self.spin_budget_moves.setValue(40)
        q.addWidget(QLabel("包干总时长"), 1, 0)
        q.addWidget(self.spin_total_sec, 1, 1)
        q.addWidget(QLabel("预计步数"), 1, 2)
        q.addWidget(self.spin_budget_moves, 1, 3)
        self.lbl_time_left = QLabel("剩余 05:00 · 已用 0.0s · 本步 600ms")
        q.addWidget(self.lbl_time_left, 2, 0, 1, 4)
        self.cmb_time_mode.currentIndexChanged.connect(self._update_time_label)
        self.spin_total_sec.valueChanged.connect(self._update_time_label)
        self.spin_budget_moves.valueChanged.connect(self._update_time_label)
        self.spin_movetime.valueChanged.connect(self._update_time_label)
        v.addWidget(gt)

        # --- 思考细节 / 变招 ---
        ga = QGroupBox("思考细节 / 变招")
        va = QVBoxLayout(ga)
        row = QHBoxLayout()
        self.chk_ponder = QCheckBox("后台思考（等对手时预分析，仅展示不落子）")
        self.chk_multipv = QCheckBox("多候选着法 MultiPV 3")
        self.chk_multipv.setChecked(True)
        row.addWidget(self.chk_ponder)
        row.addWidget(self.chk_multipv)
        va.addLayout(row)
        self.txt_thinking = QPlainTextEdit()
        self.txt_thinking.setReadOnly(True)
        self.txt_thinking.setFont(self._mono_font())
        self.txt_thinking.setMaximumBlockCount(80)
        self.txt_thinking.setPlaceholderText("引擎深度 / 评分 / 节点 / 主变 / 候选着法")
        va.addWidget(self.txt_thinking, 1)
        vr = QHBoxLayout()
        self.cmb_variants = QComboBox()
        self.cmb_variants.setMinimumWidth(160)
        btn_play_variant = QPushButton("按选中变招落子")
        btn_play_variant.clicked.connect(self._on_play_variant)
        vr.addWidget(QLabel("候选："))
        vr.addWidget(self.cmb_variants, 1)
        vr.addWidget(btn_play_variant)
        va.addLayout(vr)
        v.addWidget(ga, 1)

        # --- 模拟器 / ADB ---
        ge = QGroupBox("模拟器落子（ADB 触摸，不占鼠标）")
        ve = QGridLayout(ge)
        self.lbl_adb = QLabel("未探测 · 选到模拟器窗口后可点「ADB 探测」")
        self.lbl_adb.setWordWrap(True)
        ve.addWidget(self.lbl_adb, 0, 0, 1, 4)
        btn_probe = QPushButton("ADB 探测 / 连接")
        btn_probe.clicked.connect(self._on_probe_adb)
        btn_adb_path = QPushButton("手动指定 adb.exe")
        btn_adb_path.clicked.connect(self._on_pick_adb)
        ve.addWidget(btn_probe, 1, 0)
        ve.addWidget(btn_adb_path, 1, 1)
        self.chk_adb_swap = QCheckBox("额外交换宽高（自动识别失效时勾）")
        ve.addWidget(self.chk_adb_swap, 1, 2)
        self.spin_adb_dx = QSpinBox()
        self.spin_adb_dx.setRange(-500, 500)
        self.spin_adb_dx.setPrefix("X微调 ")
        self.spin_adb_dy = QSpinBox()
        self.spin_adb_dy.setRange(-500, 500)
        self.spin_adb_dy.setPrefix("Y微调 ")
        self.spin_adb_dx.valueChanged.connect(self._on_adb_tune)
        self.spin_adb_dy.valueChanged.connect(self._on_adb_tune)
        self.chk_adb_swap.stateChanged.connect(self._on_adb_tune)
        ve.addWidget(self.spin_adb_dx, 2, 0)
        ve.addWidget(self.spin_adb_dy, 2, 1)
        ve.addWidget(QLabel("画面有黑边/工具栏时微调"), 2, 2, 1, 2)
        v.addWidget(ge)

        # --- 诊断 ---
        v.addWidget(QLabel("着法 / 轮次诊断："))
        self.txt_diag = QPlainTextEdit()
        self.txt_diag.setReadOnly(True)
        self.txt_diag.setFont(self._mono_font())
        self.txt_diag.setMaximumBlockCount(60)
        self.txt_diag.setMaximumHeight(110)
        v.addWidget(self.txt_diag)
        lr = QHBoxLayout()
        btn_copy_log = QPushButton("复制全部日志")
        btn_copy_log.clicked.connect(self._on_copy_log)
        btn_open_log = QPushButton("打开日志文件")
        btn_open_log.clicked.connect(self._on_open_log)
        lr.addWidget(btn_copy_log)
        lr.addWidget(btn_open_log)
        tip = QLabel("日志自动保存到 exe 同级：飞鲨象棋_连线日志.txt")
        tip.setWordWrap(True)
        lr.addWidget(tip, 1)
        v.addLayout(lr)
        return w

    @staticmethod
    def _mono_font():
        from PySide6.QtGui import QFont
        f = QFont("Consolas", 9)
        f.setStyleHint(QFont.Monospace)
        return f

    # ---------- 窗口列表 ----------
    def _refresh_windows(self):
        self._windows = list_windows()
        self.cmb_windows.blockSignals(True)
        self.cmb_windows.clear()
        for w in self._windows:
            self.cmb_windows.addItem(w.display, w.hwnd)
        self.cmb_windows.blockSignals(False)
        p = default_model_path()
        self.lbl_model.setText(
            f"模型：{p or '未找到 detector.onnx'}"
            + (f"　〔{model_source(p)}〕" if p else ""))
        self._on_window_changed()

    def _current_window(self) -> WindowInfo | None:
        hwnd = self.cmb_windows.currentData()
        if hwnd is None:
            return None
        for w in self._windows:
            if w.hwnd == int(hwnd):
                return w.refresh_rect()
        return None

    def _on_window_changed(self):
        w = self._current_window()
        if w is None:
            self.lbl_win_info.setText("尚未选择窗口")
            return
        self.cfg.hwnd = w.hwnd
        self.cfg.title = w.title
        self._emulator = detect_emulator(w.proc_name, w.title, w.cls_name)
        self.cfg.is_emulator = bool(self._emulator)
        self.cfg.adb_pid = w.pid
        self.adb.pid = w.pid
        self.controller.update_config(
            hwnd=w.hwnd, title=w.title,
            is_emulator=bool(self._emulator), adb_pid=w.pid)
        tag = ("　〈本程序窗口，注意递归识别〉" if w.is_self else
               (f"　〈模拟器 {self._emulator}：后台消息通常无效，"
                "建议用 ADB 触摸 / 真实鼠标〉" if self._emulator else ""))
        self.lbl_win_info.setText(
            f"PID {w.pid} · {w.proc_name} · 类名 {w.cls_name} · "
            f"位置 ({w.rect[0]}, {w.rect[1]}) · 尺寸 {w.width}×{w.height}"
            + tag)

    def _on_params_changed(self, *_):
        if getattr(self, "_building", False):
            return
        old_mode = self.cfg.mode
        idx = self.cmb_my_side.currentIndex()
        self.cfg.mode = Mode(self.cmb_mode.currentIndex())
        self.cfg.side_mode = ("auto", "red", "black")[max(0, min(2, idx))]
        self.cfg.auto_flip = self.chk_auto_flip.isChecked()
        if idx == 1:
            self.cfg.my_side = Side.RED
        elif idx == 2:
            self.cfg.my_side = Side.BLACK
        # 自动模式下 flip 由控制器投票判定，别被复选框覆盖回去
        if not (self.cfg.side_mode == "auto" and self.cfg.auto_flip):
            self.cfg.flip = self.chk_flip.isChecked()
        self.cfg.channel_order = self.cmb_channel.currentText()
        self.cfg.conf_thr = self.spin_conf.value()
        self.cfg.iou_thr = self.spin_iou.value()
        self.cfg.interval_ms = self.spin_interval.value()
        self.cfg.prefer_printwindow = self.chk_pw.isChecked()
        self.cfg.click_mode = CLICK_CHOICES[
            max(0, min(len(CLICK_CHOICES) - 1, self.cmb_click.currentIndex()))]
        kw = dict(
            mode=self.cfg.mode, channel_order=self.cfg.channel_order,
            conf_thr=self.cfg.conf_thr, iou_thr=self.cfg.iou_thr,
            interval_ms=self.cfg.interval_ms,
            prefer_printwindow=self.cfg.prefer_printwindow,
            hwnd=self.cfg.hwnd, title=self.cfg.title,
            side_mode=self.cfg.side_mode, auto_flip=self.cfg.auto_flip,
            wait_timeout_sec=float(self.spin_wait_timeout.value()),
            click_mode=self.cfg.click_mode,
            is_emulator=self.cfg.is_emulator, adb_pid=self.cfg.adb_pid,
            adb_ready=self.cfg.adb_ready)
        if self.cfg.side_mode != "auto":
            kw.update(flip=self.cfg.flip, my_side=self.cfg.my_side)
        self.controller.update_config(**kw)
        # 棋种变了且已加载过引擎：自动匹配模式换成适配该棋种的引擎
        if old_mode != self.cfg.mode and self._engine_choice() == "auto" \
                and (self._engine is not None or self.controller.autoplay):
            self._activate_engine(force=True)

    # ---------- 识别 ----------
    def _get_once_detector(self) -> YoloDetector:
        if self._once_detector is None:
            self._once_detector = YoloDetector(
                path=self.cfg.model_path or None,
                conf_thr=self.cfg.conf_thr, iou_thr=self.cfg.iou_thr,
                channel_order=self.cfg.channel_order)
        self._once_detector.conf_thr = self.cfg.conf_thr
        self._once_detector.iou_thr = self.cfg.iou_thr
        self._once_detector.channel_order = self.cfg.channel_order
        return self._once_detector

    def _on_once(self):
        w = self._current_window()
        if w is None:
            QMessageBox.information(self, "提示", "请先选择一个目标窗口")
            return
        self._on_params_changed()
        try:
            det = self._get_once_detector()
            img, info = capture(w.hwnd, self.cfg.prefer_printwindow)
            if img is None:
                QMessageBox.warning(self, "截图失败",
                                    "无法抓取该窗口，试试取消勾选「优先 PrintWindow」")
                return
            dets = det.detect(img)
            grid = fit_grid(dets)
            self._on_frame(img, dets, grid)
            rec = reconstruct(dets, self.cfg.mode, self.main.options,
                              self.cfg.flip, self.cfg.my_side)
            if rec:
                self._on_position(rec)
            else:
                self._on_status("未识别到主棋盘，可调低置信度 / 切换通道顺序")
        except Exception as exc:
            QMessageBox.warning(self, "识别失败", str(exc))

    def _on_frame(self, rgb, dets, grid):
        self._last_dets = dets
        self._draw_preview(rgb, dets, grid)
        used = [d for d in dets if d.cls not in IGNORED_CLASSES]
        self.tbl.setRowCount(len(used))
        for i, d in enumerate(used):
            cell = ""
            if grid is not None and d.is_piece:
                c, r, dist = grid.nearest(d.cx, d.cy)
                cell = f"({c},{r})" if dist <= 0.58 * min(grid.sx, grid.sy) else "越界"
            vals = [d.name, f"{d.conf:.2f}", cell,
                    f"{d.cx:.0f},{d.cy:.0f}"]
            for j, text in enumerate(vals):
                self.tbl.setItem(i, j, QTableWidgetItem(text))
        ignored = len(dets) - len(used)
        self.lbl_stat.setText(
            f"检测 {len(used)} 个有效目标（棋盘/棋子）"
            f" · 忽略托盘计数器 {ignored} 个"
            + (f" · 网格残差 {grid.residual:.1f}px" if grid else " · 未定位网格"))

    def _draw_preview(self, rgb, dets, grid):
        try:
            import cv2
            import numpy as np
            img = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR)
            if grid is not None:
                x1, y1, x2, y2 = grid.board_box
                cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)),
                              (60, 200, 90), 1)
                for r in range(10):
                    for c in range(9):
                        x, y = grid.point(c, r)
                        cv2.circle(img, (int(x), int(y)), 2, (80, 200, 120), -1)
            for d in dets:
                if d.cls in IGNORED_CLASSES:
                    continue
                color = CLASS_COLORS.get(d.cls, (200, 200, 200))
                cv2.rectangle(img, (int(d.x1), int(d.y1)),
                              (int(d.x2), int(d.y2)), color, 2 if d.is_board else 1)
                cv2.putText(img, f"{d.name} {d.conf:.2f}",
                            (int(d.x1), max(12, int(d.y1) - 3)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.38, color, 1,
                            cv2.LINE_AA)
            if self._suggestion is not None and grid is not None:
                a = grid.point(*self._suggestion.src)
                b = grid.point(*self._suggestion.dst)
                cv2.arrowedLine(img, (int(a[0]), int(a[1])),
                                (int(b[0]), int(b[1])), (60, 60, 240), 3,
                                tipLength=0.2)
            out = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            h, w = out.shape[:2]
            qimg = QImage(out.tobytes(), w, h, 3 * w,
                          QImage.Format_RGB888).copy()
            pix = QPixmap.fromImage(qimg).scaled(
                self.lbl_preview.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation)
            self.lbl_preview.setPixmap(pix)
        except Exception as exc:
            self.lbl_preview.setText(f"预览绘制失败：{exc}")

    def _on_position(self, rec):
        self._last_rec = rec
        self.txt_board.setPlainText(rec.board.ascii())
        self.txt_fen.setPlainText(rec.fen)

    # ---------- 连续同步 / 自动下棋 ----------
    def _on_sync_toggle(self, checked):
        if checked:
            if self._current_window() is None:
                QMessageBox.information(self, "提示", "请先选择目标窗口")
                self.btn_sync.setChecked(False)
                return
            w = self._current_window()
            self.cfg.hwnd, self.cfg.title = w.hwnd, w.title
            self._on_params_changed()
            self.controller.set_autoplay(False)
            self.controller.start()
            self.btn_sync.setText("停止连续同步")
            self._on_log("连续识别已启动（仅同步，不落子）")
        else:
            self._on_stop_all()

    def _on_stop_all(self):
        self._auto_gen = None
        self.controller.stop()
        self.btn_sync.setChecked(False)
        self.btn_sync.setText("开始连续同步")
        self.btn_auto.setChecked(False)
        self.btn_auto.setText("开始连续自动下棋")

    def _on_test_click(self):
        """发一次后台点击：验证消息可达、且真实鼠标未被占用。"""
        w = self._current_window()
        grid = self._last_rec.grid if self._last_rec is not None else None
        if w is None or grid is None:
            QMessageBox.information(
                self, "提示",
                "请先选择目标窗口，并做一次「单次识别」取得棋盘网格")
            return
        choice = self.cfg.click_mode
        if choice == "auto":
            choice = ("adb" if (self.cfg.is_emulator and self.cfg.adb_ready)
                      else "message_post")
        if choice == "mouse":
            QMessageBox.information(
                self, "提示",
                "真实鼠标模式会移动鼠标并激活窗口，不适合做「鼠标不动」测试。\n"
                "可直接开启自动下棋观察落子是否生效。")
            return
        try:
            import win32api
            from vision.clicker import ScreenMapper, message_click
            mapper = ScreenMapper(w.rect, grid, self.cfg.flip)
            x, y = mapper.to_screen(0, 0)      # 棋盘左上角交叉点，无害
            before = win32api.GetCursorPos()
            if choice == "adb":
                ok, msg = self.adb.ensure(quick=True)
                if not ok:
                    QMessageBox.warning(self, "ADB 未就绪", msg)
                    return
                ax, ay, size = self.adb.window_to_android(w.hwnd, x, y)
                ok = self.adb.tap(w.hwnd, x, y)
                how = (f"ADB 触摸 ({x},{y}) → Android ({ax},{ay}) / "
                       f"{size[0]}×{size[1]}" + ("" if ok else " · 失败"))
            else:
                mm = "send" if choice == "message_send" else "post"
                message_click(w.hwnd, x, y, message_mode=mm,
                              resolve_child=self.chk_child.isChecked())
                how = f"后台消息 {mm} ({x},{y})"
            after = win32api.GetCursorPos()
            moved = before != after
            self._on_log(f"测试落子：{how} → hwnd {w.hwnd}；"
                         f"鼠标 {before} → {after}，"
                         + ("未移动 ✔" if not moved else "被移动，请检查设置"))
            self.statusBar().showMessage(
                "已发送点击，鼠标未被占用" if not moved else "鼠标被移动",
                4000)
        except Exception as exc:
            QMessageBox.warning(self, "测试失败", str(exc))

    def _on_auto_toggle(self, checked):
        if not checked:
            self._auto_gen = None
            self.controller.set_autoplay(False)
            self.btn_auto.setText("开始连续自动下棋")
            return
        if self._current_window() is None:
            QMessageBox.information(self, "提示", "请先选择目标窗口")
            self.btn_auto.setChecked(False)
            return
        self._activate_engine()
        if self._engine is None or not self._engine.running:
            QMessageBox.warning(
                self, "需要引擎",
                "没有找到可用引擎。请在上方「分析引擎」下拉里选择，"
                "或点「添加引擎…」。\n\n"
                "普通象棋用皮卡鱼；揭棋用 AB JChess / PikaJieQi。")
            self.btn_auto.setChecked(False)
            return
        if self.cfg.mode == Mode.JIEQI and not self._engine.supports_jieqi:
            QMessageBox.warning(
                self, "引擎与棋种不匹配",
                "揭棋局面（FEN 带暗子身份池）必须用揭棋引擎分析。\n"
                "请在「分析引擎」里切换到 AB JChess / PikaJieQi，"
                "或把棋种改为普通象棋。")
            self.btn_auto.setChecked(False)
            return
        # 用「当前下拉选中项」算文案，别用可能残留上一次的 cfg.click_mode
        ui_choice = CLICK_CHOICES[max(0, min(len(CLICK_CHOICES) - 1,
                                             self.cmb_click.currentIndex()))]
        how = {
            "auto": "自动选择（模拟器优先 ADB / 真实鼠标，失败自动降级）",
            "message_post": "后台消息 PostMessage，不移动鼠标、不抢焦点",
            "message_send": "后台消息 SendMessage，不移动鼠标、不抢焦点",
            "adb": "ADB 触摸（模拟器），不移动鼠标、不抢焦点",
            "mouse": "真实鼠标点击，会占用鼠标并激活窗口",
        }.get(ui_choice, "自动选择")
        ret = QMessageBox.question(
            self, "确认自动下棋",
            f"落子方式：{how}。\n"
            "执子：自动判断（按目标窗口朝向 + 对手首着校正）。\n"
            "轮次：启动时按当前局面判定；落子后确认生效并等对手应手，"
            "绝不按旧局面抢着出招。\n\n"
            "请确认这是你自己开发 / 本地单机的测试窗口，不要用于在线对弈平台。\n"
            "确认开始？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            self.btn_auto.setChecked(False)
            return
        w = self._current_window()
        self.cfg.hwnd, self.cfg.title = w.hwnd, w.title
        self._on_params_changed()
        idx = self.cmb_my_side.currentIndex()
        self.cfg.side_mode = ("auto", "red", "black")[max(0, min(2, idx))]
        if idx == 1:
            self.cfg.my_side = Side.RED
        elif idx == 2:
            self.cfg.my_side = Side.BLACK
        self.cfg.auto_flip = self.chk_auto_flip.isChecked()
        self.cfg.wait_for_opponent_first = self.chk_wait_first.isChecked()
        self.cfg.assume_my_turn_on_start = self.chk_assume_turn.isChecked()
        self.cfg.wait_timeout_sec = float(self.spin_wait_timeout.value())
        self.cfg.stable_frames = self.spin_stable.value()
        idx_click = max(0, min(len(CLICK_CHOICES) - 1,
                               self.cmb_click.currentIndex()))
        self.cfg.click_mode = CLICK_CHOICES[idx_click]
        self.cfg.resolve_child = self.chk_child.isChecked()
        self.cfg.drag = self.chk_drag.isChecked()
        self.cfg.click_gap_ms = self.spin_gap.value()
        self.cfg.cooldown_ms = self.spin_cooldown.value()
        self.cfg.max_moves = self.spin_max_moves.value()
        # 只要检测到模拟器就快速探测 ADB（不扫端口，避免卡界面）：
        # 手动选「ADB 触摸」时同样要拿到设备、分辨率和屏幕旋转信息。
        if self.cfg.is_emulator:
            self._probe_adb(silent=True)
        self.controller.update_config(
            side_mode=self.cfg.side_mode, my_side=self.cfg.my_side,
            auto_flip=self.cfg.auto_flip,
            wait_for_opponent_first=self.cfg.wait_for_opponent_first,
            assume_my_turn_on_start=self.cfg.assume_my_turn_on_start,
            wait_timeout_sec=self.cfg.wait_timeout_sec,
            stable_frames=self.cfg.stable_frames,
            click_mode=self.cfg.click_mode,
            is_emulator=self.cfg.is_emulator, adb_ready=self.cfg.adb_ready,
            adb_pid=self.cfg.adb_pid,
            resolve_child=self.cfg.resolve_child, drag=self.cfg.drag,
            click_gap_ms=self.cfg.click_gap_ms,
            cooldown_ms=self.cfg.cooldown_ms, max_moves=self.cfg.max_moves)
        self._budget_spent = 0.0
        self._moves_done = 0
        self._pondering = False
        self._analyse_start = 0.0
        self._update_time_label()
        self._on_log(f"落子方式：{how}（{self.cfg.click_mode}）")
        self._on_log(
            "启动配置：" + " | ".join((
                f"棋种={self.cfg.mode.label}",
                f"执子={self.cfg.side_mode}({self.cfg.my_side.label})",
                f"等对手先走={self.cfg.wait_for_opponent_first}",
                f"启动判定轮到我={self.cfg.assume_my_turn_on_start}",
                f"久等自动出招={self.cfg.wait_timeout_sec:.0f}s",
                f"视角翻转={self.cfg.flip}",
                f"自动校正视角={self.cfg.auto_flip}",
                f"采样={self.cfg.interval_ms}ms",
                f"稳帧={self.cfg.stable_frames}",
                f"落子冷却={self.cfg.cooldown_ms}ms",
                f"思考={self._current_movetime()}ms",
                f"模拟器={self._emulator or '否'}",
                f"ADB就绪={self.cfg.adb_ready}",
                f"ADB触摸坐标系={self.adb.touch_size}",
                f"窗口={self.cfg.title[:40]}",
            )))
        if self._engine is None or not self._engine.running:
            self._activate_engine(force=True)
        if not self.controller.running:
            self.controller.cfg.autoplay = False
            self.controller.start()
            self.btn_sync.setChecked(True)
            self.btn_sync.setText("停止连续同步")
        self.controller.set_autoplay(True)
        self.btn_auto.setText("停止自动下棋")

    # ---------- 引擎选择（独立于主窗口）----------
    def _engine_choice(self) -> str:
        cmb = getattr(self, "cmb_engine", None)
        if cmb is None or cmb.currentIndex() < 0:
            return "auto"
        return cmb.currentData() or "auto"

    def _refresh_engine_list(self, keep: str | None = None):
        choice = keep or self._engine_choice()
        cmb = self.cmb_engine
        cmb.blockSignals(True)
        cmb.clear()
        cmb.addItem("自动（按棋种匹配）", "auto")
        for e in self.main.engine_manager.engines:
            if not os.path.isfile(e.path):
                continue
            base = os.path.basename(e.path)
            jq = e.jieqi_capable or is_jieqi_name(base)
            name = e.name or base
            cmb.addItem(f"{name} · [{'揭棋' if jq else '普通'}] {base}",
                        "p:" + e.path)
        cmb.addItem("跟随主窗口当前引擎", "main")
        idx = cmb.findData(choice)
        cmb.setCurrentIndex(idx if idx >= 0 else 0)
        cmb.blockSignals(False)
        self._update_engine_label()

    def _pick_engine_path(self, mode) -> str | None:
        """按棋种自动挑引擎：揭棋必须揭棋引擎，普通优先标准 UCI 引擎。"""
        engs = [e for e in self.main.engine_manager.engines
                if os.path.isfile(e.path)]

        def jq(e):
            return e.jieqi_capable or is_jieqi_name(os.path.basename(e.path))

        if mode == Mode.JIEQI:
            c = [e for e in engs if jq(e)]
            c.sort(key=lambda e: (
                0 if "pikajieqi" in os.path.basename(e.path).lower() else 1,
                0 if "jchess" in os.path.basename(e.path).lower() else 1))
            return c[0].path if c else None
        c = [e for e in engs if not jq(e)]
        if not c:                       # 揭棋引擎也能下普通象棋（池为 '-'）
            c = engs
        return c[0].path if c else None

    def _on_engine_selected(self):
        if getattr(self, "_building", False):
            return
        self._activate_engine(force=True)

    def _on_add_engine(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 UCI 引擎", "", "引擎程序 (*.exe);;所有文件 (*)")
        if not path:
            return
        path = os.path.abspath(path)
        self.main.engine_manager.add(path)
        self._refresh_engine_list(keep="p:" + path)
        self._activate_engine(force=True)
        self._on_log(f"已添加引擎：{path}")

    def _disconnect_engine(self):
        eng = self._bound_engine
        if eng is not None:
            for sig, slot in (("analysis_ready", self._on_engine_analysis),
                              ("failed", self._on_engine_failed),
                              ("engine_ready", self._on_engine_ready),
                              ("nnue_status", self._on_nnue_status)):
                try:
                    getattr(eng, sig).disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self._bound_engine = None

    def _activate_engine(self, force: bool = False):
        """按当前下拉选择加载引擎。独立实例，不干扰主窗口人机对战。"""
        choice = self._engine_choice()
        eng = None
        if choice == "main":
            eng = self.main.active_engine
        else:
            path = (self._pick_engine_path(self.cfg.mode)
                    if choice == "auto" else choice[2:])
            if not path:
                self._on_log("没有找到适配当前棋种的引擎，请点「添加引擎…」")
            else:
                own = self._own_engine
                if own is not None and own.path == path and own.running and not force:
                    eng = own
                else:
                    if own is not None:
                        own.quit()
                        self._own_engine = None
                    try:
                        e = UciEngine(path)
                        e.start()
                        self._own_engine = e
                        eng = e
                        self._on_log(f"已加载引擎：{os.path.basename(path)}")
                    except Exception as exc:
                        self._on_log(f"引擎加载失败：{exc}")
        if eng is not self._bound_engine:
            self._disconnect_engine()
            self._bound_engine = eng
            if eng is not None:
                eng.analysis_ready.connect(self._on_engine_analysis)
                eng.failed.connect(self._on_engine_failed)
                eng.engine_ready.connect(self._on_engine_ready)
                eng.nnue_status.connect(self._on_nnue_status)
        self._engine = eng
        self._update_engine_label()

    def _update_engine_label(self):
        eng = self._engine
        if eng is None:
            hint = ("揭棋需要揭棋引擎（AB JChess / PikaJieQi）"
                    if self.cfg.mode == Mode.JIEQI else "未找到可用引擎")
            self.lbl_engine_state.setText(f"未加载引擎 · {hint}，可点「添加引擎…」")
            return
        jq = eng.supports_jieqi
        tag = "揭棋" if jq else "普通"
        src = "独立加载" if eng is self._own_engine else "主窗口引擎"
        net = eng.info.nnue_info or "NNUE 待加载"
        warn = ("　⚠ 不支持揭棋暗子，请换揭棋引擎"
                if self.cfg.mode == Mode.JIEQI and not jq else "")
        self.lbl_engine_state.setText(
            f"{src}：{eng.info.name} [{tag}]{warn} · {net}")

    def _on_engine_ready(self, info):
        self._update_engine_label()
        self._on_log(f"引擎就绪：{info.name} · {info.protocol.upper()}"
                     + (" · 揭棋" if info.jieqi_capable else " · 普通"))

    def _on_nnue_status(self, text):
        self._update_engine_label()
        self._on_log("NNUE：" + text)

    def _on_engine_stop_request(self):
        """分析期间局面变了：本次结果已作废，等它自然返回后被丢弃。

        刻意不发 UCI `stop`：引擎可能不返回 bestmove，会让 _busy 卡住、
        或让迟到的 bestmove 与后续请求错配。movetime 本身很短（几百毫秒），
        由 FEN + generation 双重校验丢弃即可，既安全又不阻塞下一轮分析。
        """
        self._on_log("局面变化：当前分析作废，等待最新稳定局面重新分析")

    # ---------- 分析请求 / 回传 ----------
    def _analyse_fen(self, fen: str, gen: int | None = None, retries: int = 0,
                     ponder: bool = False):
        eng = self._engine
        if eng is None or not eng.running:
            self._activate_engine(force=True)
            eng = self._engine
        if eng is None or not eng.running:
            self._on_log("引擎未加载，无法分析")
            if gen is not None:
                self.controller.on_analysis(None, gen)
            return
        if gen is not None and not self.controller.request_valid(gen):
            return                       # 请求已被作废，别再发
        if not eng.ready or getattr(eng, "_busy", False):
            if retries < 20:
                QTimer.singleShot(
                    150, lambda: self._analyse_fen(fen, gen, retries + 1,
                                                   ponder))
            else:
                self._on_log("引擎就绪超时，本次分析放弃")
                if gen is not None:
                    self.controller.on_analysis(None, gen)
            return
        self._auto_gen = gen
        self._auto_fen = fen
        self._ponder_fen = fen if ponder else ""
        self._analyse_start = time.time()
        budget = self._current_movetime(ponder=ponder)
        mpv = (3 if (getattr(self, "chk_multipv", None) is not None
                     and self.chk_multipv.isChecked() and not ponder) else 1)
        eng.analyse(fen, movetime=budget, use_movetime=True,
                    hash_mb=self.main.options.engine_hash,
                    threads=self.main.options.engine_threads,
                    multipv=mpv)

    def _on_analyse_request(self, fen, _rec, gen):
        self.lbl_analyse.setText(f"分析中：{fen[:46]}…")
        self._analyse_fen(fen, gen)

    def _on_manual_analyse(self):
        fen = self.txt_fen.toPlainText().strip()
        if not fen:
            QMessageBox.information(self, "提示", "请先识别或粘贴一个 FEN")
            return
        self._analyse_fen(fen, None)

    def _show_thinking(self, analysis, elapsed: float, ponder: bool):
        eng = self._engine
        lines = [
            ("【后台预分析 · 不落子】" if ponder else "【本步分析】")
            + f"  {time.strftime('%H:%M:%S')}",
            f"引擎：{eng.info.name if eng else '--'}",
            f"最佳：{getattr(analysis, 'bestmove', '') or '--'}"
            f"　{getattr(analysis, 'summary', '')}",
            f"主变：{getattr(analysis, 'pv', '') or '--'}",
            "候选：" + (", ".join(getattr(analysis, "multipv_moves", []))
                       or "--"),
            f"用时：{elapsed:.0f} ms　·　本步预算 "
            f"{self._current_movetime(ponder=ponder)} ms"
            f"　·　累计 {self._mmss(self._budget_spent)}",
            f"局面：{getattr(analysis, 'fen', '')[:78]}",
            "--- 最近搜索信息 ---",
        ]
        lines += list(getattr(analysis, "lines", []))[-6:]
        self.txt_thinking.setPlainText("\n".join(lines))

    def _fill_variants(self, analysis):
        """把引擎多主变首着 + 若干合法着法填进「变招」下拉。"""
        flip = getattr(self.controller, "_rank_flip",
                       self.main.options.engine_rank_flip)
        ucis = [u for u in (getattr(analysis, "multipv_moves", []) or []) if u]
        if not ucis and analysis.bestmove:
            ucis = [analysis.bestmove]
        cand = []
        for u in ucis[:6]:
            mv = uci_to_move(u, flip)
            if mv is not None:
                cand.append((u, mv))
        rec = getattr(self.controller, "_last_rec", None)
        if rec is not None and len(cand) < 3:
            side = self.controller.my_side or self.cfg.my_side
            try:
                legal = legal_moves(rec.board, side, self.main.options)
            except Exception:
                legal = []
            seen = {u for u, _ in cand}
            for m in legal:
                u = move_to_uci(m, flip)
                if u not in seen:
                    cand.append((u + " ·备选", m))
                    seen.add(u)
                if len(cand) >= 6:
                    break
        self._candidates = cand
        self.cmb_variants.blockSignals(True)
        self.cmb_variants.clear()
        for u, _m in cand:
            self.cmb_variants.addItem(u)
        self.cmb_variants.blockSignals(False)

    def _on_engine_analysis(self, analysis):
        req_fen = getattr(analysis, "fen", "") or ""
        # 迟到的后台预分析结果：只丢弃它，绝不能把正在进行的本步分析作废
        if self._auto_gen is not None and req_fen and \
                req_fen == self._ponder_fen and req_fen != self._auto_fen:
            self._on_log("后台预分析结果（迟到）已忽略，不影响本步分析")
            self._ponder_fen = ""
            return
        best = getattr(analysis, "bestmove", "") or ""
        elapsed = ((time.time() - self._analyse_start) * 1000.0
                   if self._analyse_start else 0.0)
        self._analyse_start = 0.0
        ponder = self._pondering
        self._pondering = False
        self._last_analysis = analysis
        self.lbl_analyse.setText(
            f"引擎建议：{best or '--'} · {getattr(analysis, 'summary', '')}")
        self._show_thinking(analysis, elapsed, ponder)
        if not ponder:
            self._fill_variants(analysis)
        if self._auto_gen is None:
            return                       # 手动 / 后台预分析：不落子
        self._budget_spent += elapsed
        self._update_time_label()
        # 二次校验：结果必须属于本次请求，禁止旧 bestmove 串到新局面
        req_fen = getattr(analysis, "fen", "") or ""
        if req_fen and req_fen != self._auto_fen:
            self._on_log("忽略过期引擎结果（FEN 与当前请求不符）")
            gen, self._auto_gen = self._auto_gen, None
            self.controller.on_analysis(None, gen)   # 让状态机重试，别卡在 THINK
            return
        gen, self._auto_gen = self._auto_gen, None
        self.controller.on_analysis(analysis, gen)

    def _on_engine_failed(self, message):
        self.lbl_analyse.setText("引擎：" + message)
        self._pondering = False
        self._analyse_start = 0.0
        if self._auto_gen is not None:
            gen, self._auto_gen = self._auto_gen, None
            self._on_log("引擎分析失败：" + message)
            self.controller.on_analysis(None, gen)

    # ---------- 对局控制：用时 / 思考 / 变招 / ADB ----------
    @staticmethod
    def _mmss(ms: float) -> str:
        s = max(0, int(ms / 1000))
        return f"{s // 60:02d}:{s % 60:02d}"

    def _current_movetime(self, ponder: bool = False) -> int:
        if ponder:
            return int(max(120, min(400, self.spin_movetime.value() / 3)))
        base = self.spin_movetime.value()
        mode = self.cmb_time_mode.currentIndex()
        if mode == 0:
            return base
        if mode == 2:
            return int(min(10000, base + self._moves_done * 60))
        total = self.spin_total_sec.value() * 1000.0
        left = max(0.0, total - self._budget_spent)
        remain = max(1, self.spin_budget_moves.value() - self._moves_done)
        return int(max(100, min(10000, left / remain * 0.85)))

    def _update_time_label(self, *_):
        if getattr(self, "_building", False):
            return
        total = self.spin_total_sec.value() * 1000.0
        self.lbl_time_left.setText(
            f"剩余 {self._mmss(total - self._budget_spent)}"
            f" · 已用 {self._budget_spent / 1000:.1f}s"
            f" · 本步 {self._current_movetime()}ms"
            f" · 已走 {self._moves_done} 步")

    def _on_force_now(self):
        self.controller.force_now()
        self._on_log("手动：立即出招（以当前识别局面为准）")

    def _on_resync(self):
        self.controller.resync()
        self._on_log("手动：重新判定轮次")

    def _on_play_variant(self):
        i = self.cmb_variants.currentIndex()
        if i < 0 or i >= len(self._candidates):
            QMessageBox.information(self, "提示", "还没有可选变招，先做一次分析")
            return
        uci, mv = self._candidates[i]
        if self.controller.play_now(mv):
            self._on_log(f"手动变招落子：{uci}")
        else:
            self._on_log(f"变招 {uci} 未执行（局面可能已变化）")

    def _probe_adb(self, silent: bool = False) -> bool:
        if getattr(self, "_building", False):
            return False
        self.adb.pid = self.cfg.adb_pid or 0
        self.lbl_adb.setText("正在探测 ADB / 连接模拟器，请稍候…")
        QApplication.processEvents()
        try:
            ok, msg = self.adb.ensure(quick=silent)
        except Exception as exc:            # 任何 adb 异常都不许打断自动下棋
            ok, msg = False, f"ADB 探测异常：{exc}"
        self.cfg.adb_ready = ok
        self.controller.update_config(adb_ready=ok)
        self.lbl_adb.setText(("已连接：" if ok else "未就绪：") + msg)
        self._on_log("ADB：" + msg)
        return ok

    def _on_probe_adb(self):
        self._probe_adb(silent=False)

    def _on_pick_adb(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 adb.exe", "", "adb.exe (adb.exe);;所有文件 (*)")
        if not path:
            return
        self.adb.cfg.adb_path = path
        self.adb.cfg.serial = ""
        self.adb.cfg.size = (0, 0)
        self._on_log(f"已指定 adb：{path}")
        self._probe_adb(silent=False)

    def _on_adb_tune(self, *_):
        if getattr(self, "_building", False):
            return
        c = self.adb.cfg
        c.swap_xy = self.chk_adb_swap.isChecked()
        c.offset_x = self.spin_adb_dx.value()
        c.offset_y = self.spin_adb_dy.value()

    def _on_ponder_tick(self):
        """后台思考：等对手时做一次短分析，只更新思考细节，不落子。"""
        if not self.chk_ponder.isChecked() or self._pondering:
            return
        if self._auto_gen is not None or not self.controller.autoplay:
            return
        if self.controller.phase != PHASE_WAIT:
            return
        eng = self._engine
        if eng is None or not eng.running or not eng.ready or \
                getattr(eng, "_busy", False):
            return
        rec = getattr(self.controller, "_last_rec", None)
        if rec is None:
            return
        self._pondering = True
        # 兜底：分析被放弃 / 引擎无响应时也别把后台思考永久关掉
        QTimer.singleShot(2000,
                          lambda: setattr(self, "_pondering", False))
        self._analyse_fen(rec.fen, None, ponder=True)

    def _on_click_method_changed(self, key: str, label: str):
        self.lbl_click_now.setText("落子方式：" + label)
        self._on_log("落子方式 → " + label)

    def _on_diagnostics(self, text: str):
        self.txt_diag.appendPlainText(text)
        self._write_log_file("[诊断] ", text)

    # ---------- 判色 / 轮次显示 ----------
    def _on_side_resolved(self, side, reason):
        self._on_log(f"执子判定：{side.label}（{reason}）")
        if self.cfg.auto_flip:
            self.chk_flip.blockSignals(True)
            self.chk_flip.setChecked(self.cfg.flip)
            self.chk_flip.blockSignals(False)

    def _on_turn_state(self, text):
        self.lbl_turn_state.setText("轮次：" + text)
        self.statusBar().showMessage(text, 3000)
        self._write_log_file("[轮次] ", text)

    # ---------- 结果动作 ----------
    def _on_suggestion(self, mv):
        self._suggestion = mv
        self._moves_done += 1
        self._update_time_label()
        self._on_log(f"建议/已落子：{mv.src} → {mv.dst}")

    def _on_copy_fen(self):
        text = self.txt_fen.toPlainText().strip()
        if text:
            QApplication.clipboard().setText(text)
            self.statusBar().showMessage("FEN 已复制", 3000)

    def _on_import_board(self):
        rec = self._last_rec
        if rec is None:
            QMessageBox.information(self, "提示", "请先识别一个局面")
            return
        try:
            mode = Mode(self.cmb_mode.currentIndex())
            board = Board(mode, self.main.options, fen=rec.fen)
            self.main.game = Game(self.main.options, mode)
            self.main.game.replace_board(board)
            self.main._clear_selection()
            self.main.cmb_mode.blockSignals(True)
            self.main.cmb_mode.setCurrentIndex(int(mode))
            self.main.cmb_mode.blockSignals(False)
            self.main._refresh()
            self.statusBar().showMessage("识别局面已导入主棋盘（可继续拆棋/分析）",
                                        4000)
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", str(exc))

    # ---------- 运行日志落盘 ----------
    def _log_path(self) -> str:
        """exe 同级（源码运行时项目根），方便直接把文件发给开发者。"""
        from ai.engine import app_dir
        return os.path.join(app_dir(), "飞鲨象棋_连线日志.txt")

    def _write_log_file(self, prefix: str, text: str):
        try:
            with open(self._log_path(), "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {prefix}{text}\n")
        except OSError:
            pass

    def _on_copy_log(self):
        text = self.txt_log.toPlainText()
        QApplication.clipboard().setText(text)
        self._on_status(f"已复制 {len(text.splitlines())} 行日志，可直接粘贴")

    def _on_open_log(self):
        p = self._log_path()
        if os.path.isfile(p):
            try:
                os.startfile(p)
            except OSError:
                self._on_status("日志文件：" + p)
        else:
            self._on_status("日志文件尚未生成")

    def _on_status(self, text):
        self.statusBar().showMessage(text, 4000)
        self._write_log_file("[状态栏] ", text)

    def _on_log(self, text):
        self.txt_log.appendPlainText(text)
        self._write_log_file("", text)

    # ---------- 关闭 ----------
    def closeEvent(self, event):
        try:
            self._ponder_timer.stop()
            self.controller.stop()
            self._disconnect_engine()
            if self._own_engine is not None:
                self._own_engine.quit()
                self._own_engine = None
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)
