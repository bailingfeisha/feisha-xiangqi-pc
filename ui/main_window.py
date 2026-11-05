# -*- coding: utf-8 -*-
"""主窗口：引擎前端式布局（参考鲨鱼象棋 / 鹏飞象棋 / xiangqi_pyqt）。"""
from __future__ import annotations

import json
import os
import random

from PySide6.QtCore import Qt, QTimer, QSettings, QByteArray
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QFont, QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QPushButton, QComboBox, QListWidget, QListWidgetItem,
    QPlainTextEdit, QScrollArea, QCheckBox, QFileDialog, QMessageBox,
    QApplication, QSizePolicy, QToolBar, QDialog, QTabWidget,
)

from ai.engine import (
    EngineManager, bundled_engine_path, AutoPlayController,
)
from core.board import Board, Move
from core.constants import (
    Side, Mode, PieceType, DARK_POOL, GENERAL_POS, piece_name,
)
from core.game import Game
from core.notation import uci_to_move
from core.options import Options
from ui.board_widget import BoardWidget, CapturedPiecesWidget
from ui.dialogs import (
    SettingsDialog, RulesDialog, AboutDialog, FenDialog, EngineDialog,
    QQ_GROUP,
)
from ui.league_window import LeagueWindow


def _edit_items():
    items = [("擦除（删除该格棋子）", None, None, False)]
    for side in (Side.RED, Side.BLACK):
        for t in PieceType:
            items.append((f"{side.short}·{piece_name(side, t)}",
                          side, t, False))
    items += [("红·暗子（随机身份）", Side.RED, None, True),
              ("黑·暗子（随机身份）", Side.BLACK, None, True)]
    return items


class MainWindow(QMainWindow):
    EDIT_ITEMS = _edit_items()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("飞鲨象棋 PC 版  ·  普通象棋 / 揭棋")
        self.setMinimumSize(1120, 760)
        self.resize(1360, 900)
        self.settings = QSettings("FeishaChess", "FeishaChess")
        try:
            saved = json.loads(self.settings.value("options", "{}") or "{}")
        except (ValueError, TypeError):
            saved = {}
        self.options = Options.from_dict(saved)
        self.game = Game(self.options, Mode.XIANGQI)
        self.engine_manager = EngineManager(self.options, self)
        self.active_engine = None
        self.ai_controller = AutoPlayController(self)
        self.ai_controller.move_ready.connect(self._on_ai_move)
        self.ai_controller.status.connect(self._on_ai_status)
        self.selected = None
        self.flipped = False
        self.edit_mode = False
        self._updating = False
        self.acts = {}
        self.vision_window = None

        self._build_actions()
        self._build_ui()
        self._build_menu()
        self._build_toolbar()
        self._apply_options()
        self._refresh_engine_combo()
        geom = self.settings.value("geometry")
        if isinstance(geom, QByteArray):
            self.restoreGeometry(geom)
        if self.width() < 1200 or self.height() < 800:
            self.resize(1360, 900)
        self._auto_load_engine()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start(1000)
        self._refresh()

    # ---------- 动作 ----------
    def _action(self, key, text, slot, shortcut=None, checkable=False,
                enabled=True):
        a = QAction(text, self)
        a.setEnabled(enabled)
        if checkable:
            a.setCheckable(True)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(lambda *_, f=slot: f())
        self.acts[key] = a
        return a

    def _build_actions(self):
        self._action("new", "新局(&N)", self._on_new_game, "Ctrl+N")
        self._action("open", "打开对局…", self._on_load, "Ctrl+O")
        self._action("save", "保存对局…", self._on_save, "Ctrl+S")
        self._action("fen_out", "复制 / 导出 FEN…", self._on_export_fen)
        self._action("fen_in", "导入 FEN 局面…", self._on_import_fen)
        self._action("quit", "退出", self.close, "Ctrl+Q")
        self._action("undo", "悔棋", self._on_undo, "Ctrl+Z")
        self._action("redo", "重做", self._on_redo, "Ctrl+Y")
        self._action("prev", "上一步", self._on_undo, "Left")
        self._action("next", "下一步", self._on_redo, "Right")
        self._action("resign", "认输…", self._on_resign)
        self._action("draw", "双方和棋", self._on_draw)
        self._action("flip", "翻转棋盘", self._on_flip, "F")
        self._action("variation", "切换变例", self._on_variation, "Ctrl+V")
        self._action("delbranch", "删除当前分支", self._on_delete_branch)
        self._action("edit", "局面编辑 / 摆局", self._on_edit_toggle, "Ctrl+E")
        self._action("settings", "设置…", self._on_settings, "Ctrl+P")
        self._action("rules", "规则说明", self._on_rules, "F1")
        self._action("about", "关于", self._on_about)
        self._action("qq", f"官方交流群 {QQ_GROUP}（点击复制）",
                     self._on_copy_qq)
        self._action("engine", "引擎管理…", self._on_engine_manage)
        self._action("analyse", "引擎分析当前局面", self._on_analyse, "F2")
        self._action("engine_stop", "停止引擎思考", self._on_engine_stop, "F3")
        self._action("league", "引擎联赛…", self._on_league, "Ctrl+L")
        self._action("vision", "视觉识别 / 连续自动下棋…",
                    self._on_vision, "F5")
        self._action("ai", "开始 / 停止人机对战", self._on_ai_toggle, "F4")
        self._action("net", "联机 / 连线（预留）", lambda: None, enabled=False)
        self._action("book", "开局库 OBK（预留）", lambda: None, enabled=False)

        group = QActionGroup(self)
        group.setExclusive(True)
        a1 = self._action("mode_xiangqi", "普通象棋",
                          lambda: self._on_new_game(Mode.XIANGQI),
                          checkable=True)
        a2 = self._action("mode_jieqi", "揭棋",
                          lambda: self._on_new_game(Mode.JIEQI),
                          checkable=True)
        a1.setChecked(True)
        group.addAction(a1)
        group.addAction(a2)

    # ---------- 界面 ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        self.board_widget = BoardWidget()
        self.board_widget.setSizePolicy(QSizePolicy.Expanding,
                                        QSizePolicy.Expanding)
        self.board_widget.setMinimumWidth(560)
        self.board_widget.square_clicked.connect(self._on_square_clicked)
        self.board_widget.cancel_requested.connect(self._clear_selection)
        self.board_widget.zoom_requested.connect(self._on_zoom)
        root.addWidget(self.board_widget, 1)

        tabs = QTabWidget()
        tabs.setFixedWidth(366)
        self.tabs = tabs

        def page(*groups):
            w = QWidget()
            v = QVBoxLayout(w)
            v.setSpacing(8)
            v.setContentsMargins(6, 8, 6, 8)
            for g in groups:
                v.addWidget(g)
            v.addStretch(1)
            return w

        tabs.addTab(page(self._group_game(), self._group_human(),
                         self._group_operations()), "对局")
        tabs.addTab(page(self._group_engine(), self._group_placeholder()),
                    "引擎")
        tabs.addTab(page(self._group_notation(), self._group_captured()),
                    "棋谱")
        tabs.addTab(page(self._group_edit()), "摆局")
        root.addWidget(tabs, 0)

        self.setStatusBar(self.statusBar())
        self.statusBar().showMessage("就绪 · 红方先行")
        qq = QPushButton(f"  官方交流群 {QQ_GROUP}  ")
        qq.setToolTip("点击复制群号")
        qq.setFlat(True)
        qq.setCursor(Qt.PointingHandCursor)
        qq.clicked.connect(self._on_copy_qq)
        self.statusBar().addPermanentWidget(qq)

    def _group_game(self):
        g = QGroupBox("对局")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems([Mode.XIANGQI.label, Mode.JIEQI.label])
        self.cmb_mode.currentIndexChanged.connect(self._on_mode_combo)
        row.addWidget(QLabel("模式"))
        row.addWidget(self.cmb_mode, 1)
        btn_new = QPushButton("新局")
        btn_new.clicked.connect(self._on_new_game)
        row.addWidget(btn_new)
        v.addLayout(row)
        self.lbl_side = QLabel("轮到：红方")
        self.lbl_side.setStyleSheet("font-weight:bold;font-size:14px;")
        self.lbl_status = QLabel("对局进行中")
        self.lbl_round = QLabel("第 1 回合 · 第 0 手")
        v.addWidget(self.lbl_side)
        v.addWidget(self.lbl_status)
        v.addWidget(self.lbl_round)
        clocks = QHBoxLayout()
        self.lbl_clock_red = QLabel("红 00:00")
        self.lbl_clock_black = QLabel("黑 00:00")
        for lbl in (self.lbl_clock_red, self.lbl_clock_black):
            lbl.setFont(QFont("Consolas", 11))
            clocks.addWidget(lbl)
        v.addLayout(clocks)
        return g

    def _group_human(self):
        g = QGroupBox("人机对战（选红 / 选黑）")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        row.addWidget(QLabel("我方执"))
        self.cmb_ai_side = QComboBox()
        self.cmb_ai_side.addItems(["红方（引擎执黑）", "黑方（引擎执红）"])
        row.addWidget(self.cmb_ai_side, 1)
        v.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("思考"))
        self.cmb_ai_time = QComboBox()
        self.cmb_ai_time.addItems(["0.3 秒", "1 秒", "3 秒", "5 秒"])
        self.cmb_ai_time.setCurrentIndex(1)
        row2.addWidget(self.cmb_ai_time, 1)
        v.addLayout(row2)
        row3 = QHBoxLayout()
        self.btn_ai_start = QPushButton("开始对战")
        self.btn_ai_stop = QPushButton("停止")
        self.btn_ai_stop.setEnabled(False)
        self.btn_ai_start.clicked.connect(self._on_ai_start)
        self.btn_ai_stop.clicked.connect(self._on_ai_stop)
        row3.addWidget(self.btn_ai_start)
        row3.addWidget(self.btn_ai_stop)
        v.addLayout(row3)
        self.lbl_ai_status = QLabel("状态：双人对弈（未开始）")
        self.lbl_ai_status.setWordWrap(True)
        v.addWidget(self.lbl_ai_status)
        tip = QLabel("选黑时引擎执红先走第一步；引擎思考期间不能替它走子。"
                     "揭棋请加载 AB JChess / PikaJieQi 揭棋引擎。")
        tip.setWordWrap(True)
        v.addWidget(tip)
        return g

    def _group_engine(self):
        g = QGroupBox("引擎（UCI / UCCI · 普通 / 揭棋）")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.cmb_engine = QComboBox()
        self.cmb_engine.currentIndexChanged.connect(self._on_engine_combo)
        row.addWidget(self.cmb_engine, 1)
        btn_manage = QPushButton("管理")
        btn_manage.clicked.connect(self._on_engine_manage)
        row.addWidget(btn_manage)
        v.addLayout(row)
        self.lbl_engine = QLabel("未加载引擎")
        self.lbl_engine.setWordWrap(True)
        v.addWidget(self.lbl_engine)
        self.lbl_nnue = QLabel("NNUE：—")
        self.lbl_nnue.setWordWrap(True)
        self.lbl_nnue.setStyleSheet("color:#666;")
        v.addWidget(self.lbl_nnue)
        row2 = QHBoxLayout()
        self.btn_analyse = QPushButton("分析当前局面")
        self.btn_analyse.clicked.connect(self._on_analyse)
        btn_league = QPushButton("引擎联赛")
        btn_league.clicked.connect(self._on_league)
        self.btn_engine_stop = QPushButton("停止")
        self.btn_engine_stop.clicked.connect(self._on_engine_stop)
        row2.addWidget(self.btn_analyse)
        row2.addWidget(btn_league)
        row2.addWidget(self.btn_engine_stop)
        v.addLayout(row2)
        self.txt_pv = QPlainTextEdit()
        self.txt_pv.setReadOnly(True)
        self.txt_pv.setMaximumHeight(86)
        self.txt_pv.setPlaceholderText("引擎分析：评分 / 深度 / 主变 PV")
        v.addWidget(self.txt_pv)
        hint = QLabel("分析只在棋盘上用箭头建议着法，不自动落子。"
                     "人机对战在「对局」页开启。NNUE 与引擎 exe 放同一目录"
                     "即可，程序会自动定位 EvalFile。")
        hint.setWordWrap(True)
        v.addWidget(hint)
        return g

    def _group_placeholder(self):
        g = QGroupBox("其他预览功能")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        for text, tip in (
            ("联机 / 连线", "network/client.py :: NetworkClient"),
            ("开局库 OBK", "开局库接口预留"),
        ):
            b = QPushButton(text + "（预留）")
            b.setEnabled(False)
            b.setToolTip(tip)
            row.addWidget(b)
        v.addLayout(row)
        lab = QLabel("人机对战已实现（「对局」页）。连线自动走子、开局库"
                     "仍留接口，后续可接入，无需改动规则内核。")
        lab.setWordWrap(True)
        v.addWidget(lab)
        return g

    def _group_notation(self):
        g = QGroupBox("棋谱（分支棋谱 · 点击跳转）")
        v = QVBoxLayout(g)
        self.lst_moves = QListWidget()
        self.lst_moves.setMinimumHeight(150)
        self.lst_moves.currentRowChanged.connect(self._on_move_row)
        v.addWidget(self.lst_moves)
        row = QHBoxLayout()
        for text, slot in (("上一步", self._on_undo), ("下一步", self._on_redo),
                           ("切换变例", self._on_variation),
                           ("删分支", self._on_delete_branch)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        v.addLayout(row)
        return g

    def _group_operations(self):
        g = QGroupBox("操作")
        grid = QGridLayout(g)
        entries = [
            ("悔棋", self._on_undo), ("重做", self._on_redo),
            ("翻转棋盘", self._on_flip), ("认输", self._on_resign),
            ("和棋", self._on_draw), ("局面编辑", self._on_edit_toggle),
            ("保存对局", self._on_save), ("载入对局", self._on_load),
            ("导出 FEN", self._on_export_fen), ("导入 FEN", self._on_import_fen),
            ("设置", self._on_settings), ("规则", self._on_rules),
        ]
        for i, (text, slot) in enumerate(entries):
            b = QPushButton(text)
            b.clicked.connect(slot)
            grid.addWidget(b, i // 3, i % 3)
        return g

    def _group_edit(self):
        g = QGroupBox("局面编辑 / 摆局")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.cmb_edit_piece = QComboBox()
        for label, *_ in self.EDIT_ITEMS:
            self.cmb_edit_piece.addItem(label)
        row.addWidget(QLabel("落子"))
        row.addWidget(self.cmb_edit_piece, 1)
        v.addLayout(row)
        row2 = QHBoxLayout()
        self.cmb_edit_side = QComboBox()
        self.cmb_edit_side.addItems(["红方先行", "黑方先行"])
        row2.addWidget(self.cmb_edit_side)
        for text, slot in (("清空棋盘", self._on_edit_clear),
                           ("恢复开局", self._on_edit_reset),
                           ("完成编辑", self._on_edit_done)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row2.addWidget(b)
        v.addLayout(row2)
        tip = QLabel("编辑模式下点击棋盘格放置 / 删除棋子；暗子身份随机。")
        tip.setWordWrap(True)
        v.addWidget(tip)
        g.setVisible(False)
        self.grp_edit = g
        return g

    def _group_captured(self):
        g = QGroupBox("被吃棋子")
        v = QVBoxLayout(g)
        self.cap_red = CapturedPiecesWidget(Side.RED)
        self.cap_black = CapturedPiecesWidget(Side.BLACK)
        v.addWidget(QLabel("红方吃子："))
        v.addWidget(self.cap_red)
        v.addWidget(QLabel("黑方吃子："))
        v.addWidget(self.cap_black)
        self.grp_captured = g
        return g

    # ---------- 菜单 / 工具栏 ----------
    def _build_menu(self):
        mb = self.menuBar()
        m = mb.addMenu("文件(&F)")
        for k in ("new", "open", "save", "fen_out", "fen_in"):
            m.addAction(self.acts[k])
        m.addSeparator()
        m.addAction(self.acts["quit"])

        m = mb.addMenu("对局(&G)")
        for k in ("undo", "redo", "prev", "next", "variation", "delbranch"):
            m.addAction(self.acts[k])
        m.addSeparator()
        for k in ("resign", "draw", "flip", "edit"):
            m.addAction(self.acts[k])

        m = mb.addMenu("模式(&M)")
        m.addAction(self.acts["mode_xiangqi"])
        m.addAction(self.acts["mode_jieqi"])

        m = mb.addMenu("引擎(&E)")
        for k in ("engine", "analyse", "engine_stop", "league"):
            m.addAction(self.acts[k])
        m.addSeparator()
        m.addAction(self.acts["vision"])
        m.addAction(self.acts["ai"])
        m.addAction(self.acts["net"])
        m.addAction(self.acts["book"])

        m = mb.addMenu("帮助(&H)")
        m.addAction(self.acts["settings"])
        m.addAction(self.acts["rules"])
        m.addAction(self.acts["qq"])
        m.addSeparator()
        m.addAction(self.acts["about"])

    def _build_toolbar(self):
        tb = QToolBar("主工具栏", self)
        tb.setMovable(False)
        self.addToolBar(tb)
        for k in ("new", "undo", "redo", "flip", "analyse", "engine_stop",
                  "league", "ai", "vision"):
            tb.addAction(self.acts[k])
        tb.addSeparator()
        for k in ("edit", "settings", "rules"):
            tb.addAction(self.acts[k])

    # ---------- 选项 ----------
    def _apply_options(self):
        self.game.options = self.options
        self.engine_manager.options = self.options
        self.board_widget.apply_display(self.options)
        self.cap_red.apply_display(self.options)
        self.cap_black.apply_display(self.options)
        self.grp_captured.setVisible(self.options.show_captured)

    # ---------- 刷新 ----------
    def _refresh(self):
        self._updating = True
        self.board_widget.set_board(self.game.board)
        self.board_widget.set_last_move(self.game.current.move)
        check_pos = None
        if self.game.in_check:
            check_pos = self.game.board.general_pos(self.game.side_to_move)
        self.board_widget.set_check_pos(check_pos)
        ai_turn = (self.ai_controller.enabled
                   and self.game.side_to_move == self.ai_controller.side)
        self.board_widget.interactive = (self.game.result is None
                                         and not self.edit_mode
                                         and not ai_turn)
        self.board_widget.set_flipped(self.flipped)

        self.cmb_mode.setCurrentIndex(int(self.game.mode))
        self.acts["mode_xiangqi"].setChecked(self.game.mode == Mode.XIANGQI)
        self.acts["mode_jieqi"].setChecked(self.game.mode == Mode.JIEQI)
        self.lbl_side.setText(f"轮到：{self.game.side_to_move.label}")
        if self.game.result:
            self.lbl_status.setText(self.game.result.text)
        elif self.game.in_check:
            self.lbl_status.setText("将军！")
        elif self.edit_mode:
            self.lbl_status.setText("局面编辑模式")
        else:
            self.lbl_status.setText("对局进行中")
        self.lbl_round.setText(
            f"第 {self.game.fullmove} 回合 · 第 {self.game.ply} 手"
            + ("" if self.game.is_latest else " · 历史节点（走子将生成分支）"))
        self._update_clocks()
        self._refresh_notation()
        self.cap_red.set_pieces(self.game.board.captured[int(Side.RED)])
        self.cap_black.set_pieces(self.game.board.captured[int(Side.BLACK)])
        self.grp_edit.setVisible(self.edit_mode)
        self._updating = False
        self.ai_controller.configure(self.game, self.options,
                                     self.ai_controller.side)
        self.btn_ai_start.setEnabled(not self.ai_controller.enabled)
        self.btn_ai_stop.setEnabled(self.ai_controller.enabled)
        if not self.edit_mode:
            self.ai_controller.on_position_changed()

    def _update_clocks(self):
        r, b = self.game.clock_texts()
        self.lbl_clock_red.setText(f"红 {r}")
        self.lbl_clock_black.setText(f"黑 {b}")
        self.lbl_clock_red.setVisible(self.options.use_clock)
        self.lbl_clock_black.setVisible(self.options.use_clock)

    def _refresh_notation(self):
        self.lst_moves.clear()
        line = self.game.display_line()
        for i, node in enumerate(line):
            if i == 0:
                text = "开局"
            else:
                text = (f"{i:3d}. {Side(node.side).short}   "
                        f"{node.notation}")
                parent = node.parent
                if parent and len(parent.children) > 1:
                    k = parent.children.index(node) + 1
                    text += f"   [变例 {k}/{len(parent.children)}]"
                if node.comment:
                    text += "  ✎"
            item = QListWidgetItem(text)
            if node is self.game.current:
                item.setBackground(QColor("#F6DFA8"))
                item.setForeground(QColor("#3A2A10"))
            self.lst_moves.addItem(item)
        self.lst_moves.setCurrentRow(self.game.ply)

    # ---------- 棋盘交互 ----------
    def _clear_selection(self):
        self.selected = None
        self.board_widget.clear_selection()

    def _select(self, pos):
        self.selected = pos
        self.board_widget.set_selection(pos, self.game.targets_from(pos))

    def _on_square_clicked(self, pos):
        if self.edit_mode:
            self._apply_edit(pos)
            return
        if self.game.result is not None:
            return
        if self.ai_controller.enabled and \
                self.game.side_to_move == self.ai_controller.side:
            self.statusBar().showMessage("引擎正在思考，请稍候", 2000)
            return
        piece = self.game.board.get(*pos)
        side = int(self.game.side_to_move)
        if self.selected is None:
            if piece and piece.side == side:
                self._select(pos)
            return
        if pos == self.selected:
            self._clear_selection()
            return
        if pos in self.game.targets_from(self.selected):
            if not self.game.is_latest:
                self.statusBar().showMessage("已从历史节点创建新分支", 3500)
            if self.game.play(Move(self.selected, pos)):
                self._after_move()
            return
        if piece and piece.side == side:
            self._select(pos)
        else:
            self._clear_selection()

    def _after_move(self):
        self._clear_selection()
        self.board_widget.set_hint(None)
        if self.options.sound:
            QApplication.beep()
        self._refresh()
        if self.game.result:
            QMessageBox.information(self, "对局结束", self.game.result.text)

    # ---------- 对局操作 ----------
    def _on_new_game(self, mode=None):
        if mode is None:
            mode = self.game.mode
        self.edit_mode = False
        self.game.reset(mode, seed=(self.options.seed or None))
        self._clear_selection()
        self.board_widget.set_hint(None)
        self._refresh()
        extra = f" · 种子 {self.options.seed}" if self.options.seed else ""
        self.statusBar().showMessage(f"新局：{Mode(mode).label}{extra}", 4000)

    def _on_mode_combo(self, index):
        if not self._updating and Mode(index) != self.game.mode:
            self._on_new_game(Mode(index))

    def _on_undo(self):
        self.game.undo()
        self._clear_selection()
        self.board_widget.set_hint(None)
        self._refresh()

    def _on_redo(self):
        self.game.redo()
        self._clear_selection()
        self.board_widget.set_hint(None)
        self._refresh()

    def _on_move_row(self, row):
        if self._updating or row < 0:
            return
        self.game.goto_index(row)
        self._clear_selection()
        self.board_widget.set_hint(None)
        self._refresh()

    def _on_variation(self):
        if len(self.game.variations()) < 2 and \
                (self.game.current.parent is None or
                 len(self.game.current.parent.children) < 2):
            self.statusBar().showMessage("当前没有可切换的变例", 2500)
            return
        self.game.switch_variation(1)
        self._refresh()

    def _on_delete_branch(self):
        if self.game.current is self.game.root:
            return
        self.game.delete_branch()
        self._refresh()

    def _on_flip(self):
        self.flipped = not self.flipped
        self.board_widget.set_flipped(self.flipped)
        self.statusBar().showMessage(
            "视角：" + ("黑方在下" if self.flipped else "红方在下"), 2500)

    def _on_resign(self):
        side = self.game.side_to_move
        if QMessageBox.question(
                self, "认输", f"确认 {side.label} 认输？",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.game.resign(side)
            self._refresh()
            QMessageBox.information(self, "对局结束", self.game.result.text)

    def _on_draw(self):
        self.game.agree_draw()
        self._refresh()
        QMessageBox.information(self, "对局结束", self.game.result.text)

    def _on_tick(self):
        if not self.options.use_clock or self.game.result is not None:
            return
        if self.game.is_latest and not self.edit_mode:
            self.game.tick(1.0)
            if self.game.result:
                self._refresh()
                QMessageBox.information(self, "对局结束", self.game.result.text)
        self._update_clocks()

    # ---------- 存读档 / FEN ----------
    def _on_save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存对局", "", "飞鲨象棋对局 (*.fsq);;JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.game.to_dict(), f, ensure_ascii=False, indent=1)
            self.statusBar().showMessage("对局已保存：" + path, 4000)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def _on_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "载入对局", "", "飞鲨象棋对局 (*.fsq);;JSON (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.game = Game.from_dict(data, self.options)
            self.edit_mode = False
            self._clear_selection()
            self.board_widget.set_hint(None)
            self._apply_options()
            self._refresh()
            self.statusBar().showMessage("对局已载入：" + path, 4000)
        except Exception as exc:
            QMessageBox.warning(self, "载入失败", str(exc))

    def _on_export_fen(self):
        fen = self.game.board.to_fen(
            self.game.side_to_move, self.game.current.halfmove_clock,
            self.game.fullmove)
        FenDialog("导出 / 复制局面 FEN", fen, readonly=True, parent=self).exec()

    def _on_import_fen(self):
        dlg = FenDialog("导入局面 FEN", "", readonly=False, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        text = dlg.text()
        if not text:
            return
        try:
            board = Board(self.game.mode, self.options, fen=text)
            self.game.replace_board(board)
            self.edit_mode = False
            self._clear_selection()
            self._refresh()
            self.statusBar().showMessage("局面已导入（FEN 与 AB-JChess 引擎互通）", 4000)
        except Exception as exc:
            QMessageBox.warning(self, "FEN 无效", str(exc))

    # ---------- 局面编辑 ----------
    def _on_edit_toggle(self):
        self.edit_mode = not self.edit_mode
        if self.edit_mode:
            self.tabs.setCurrentIndex(3)
        self._clear_selection()
        self._refresh()
        self.statusBar().showMessage(
            "局面编辑：点击棋盘落子；再次按 Ctrl+E 或点「完成编辑」退出", 5000)

    def _apply_edit(self, pos):
        board = self.game.board.clone()
        item = self.EDIT_ITEMS[self.cmb_edit_piece.currentIndex()]
        _, side, ptype, dark = item
        if side is None:
            board.put(*pos, None)
        else:
            real = ptype if ptype is not None else random.choice(DARK_POOL)
            board.put(*pos, board._mk(side, real, dark))
        board._fen_side = (Side.RED if self.cmb_edit_side.currentIndex() == 0
                          else Side.BLACK)
        board._fen_halfmove = 0
        board._fen_fullmove = 1
        self.game.replace_board(board)
        self._refresh()

    def _on_edit_clear(self):
        board = Board(self.game.mode, self.options,
                      fen="9/9/9/9/9/9/9/9/9/9 w - 0 1")
        self.game.replace_board(board)
        self._refresh()

    def _on_edit_reset(self):
        board = Board(self.game.mode, self.options,
                      seed=(self.options.seed or None))
        self.game.replace_board(board)
        self._refresh()

    def _on_edit_done(self):
        board = self.game.board.clone()
        board._fen_side = (Side.RED if self.cmb_edit_side.currentIndex() == 0
                          else Side.BLACK)
        board._fen_halfmove = 0
        board._fen_fullmove = 1
        self.game.replace_board(board)
        self.edit_mode = False
        self._refresh()

    # ---------- 引擎 ----------
    def _refresh_engine_combo(self):
        self._updating = True
        self.cmb_engine.clear()
        for info in self.engine_manager.engines:
            self.cmb_engine.addItem(info.display, info.path)
        if self.active_engine:
            idx = self.cmb_engine.findData(self.active_engine.path)
            if idx >= 0:
                self.cmb_engine.setCurrentIndex(idx)
        self._updating = False

    def _on_engine_combo(self, index):
        if self._updating or index < 0:
            return
        self._load_engine(index)

    def _auto_load_engine(self):
        """启动时自动加载引擎。

        优先 exe 同级 engines/ 里的内置 AB JChess；单 exe 单独分发（没有
        engines 目录）时，自动从已发现的本机引擎里挑一个可用的揭棋引擎，
        退而求其次挑任意存在的引擎，保证开箱即用。
        """
        engines = self.engine_manager.engines
        idx = -1
        bundled = bundled_engine_path()
        if bundled:
            key = os.path.normcase(os.path.abspath(bundled))
            for i, e in enumerate(engines):
                if os.path.normcase(os.path.abspath(e.path)) == key:
                    idx = i
                    break
        if idx < 0:
            # 按兼容性排序：已实测能吃揭棋 5 段 FEN 的引擎优先
            def score(e):
                if not os.path.isfile(e.path):
                    return 0
                n = os.path.basename(e.path).lower()
                if "pikajieqi" in n:
                    return 4
                if "ab-jchess-1-rb" in n or "abjchess-1-rb" in n:
                    return 3
                if e.jieqi_capable:
                    return 2
                return 1
            ranked = sorted(((score(e), i) for i, e in enumerate(engines)
                             if score(e) > 0), key=lambda t: (-t[0], t[1]))
            if ranked:
                idx = ranked[0][1]
        if idx < 0:
            self.lbl_engine.setText("未找到可用引擎，请在「引擎」页添加或扫描")
            return
        try:
            self._attach_engine(self.engine_manager.load(idx))
        except Exception as exc:
            self.lbl_engine.setText(f"默认引擎加载失败：{exc}")

    def _attach_engine(self, eng):
        self.active_engine = eng
        self.ai_controller.set_engine(eng)
        eng.engine_ready.connect(self._on_engine_ready)
        eng.analysis_ready.connect(self._on_analysis_ready)
        eng.nnue_status.connect(self._on_nnue_status)
        eng.failed.connect(self._on_engine_failed)
        self.lbl_engine.setText(f"已加载：{eng.info.name or eng.info.display}")
        self.lbl_nnue.setText("NNUE：等待引擎握手…")
        self._refresh_engine_combo()
        if self.ai_controller.enabled:
            self.ai_controller.on_position_changed()

    def _load_engine(self, index):
        try:
            eng = self.engine_manager.load(index)
        except Exception as exc:
            QMessageBox.warning(self, "引擎加载失败", str(exc))
            return
        self._attach_engine(eng)
        self.statusBar().showMessage("引擎已加载：" + eng.path, 4000)

    def _on_engine_ready(self, info):
        tag = "揭棋 / 普通" if info.jieqi_capable else "普通象棋"
        self.lbl_engine.setText(
            f"{info.display} · {info.author or 'UCI 引擎'} · 适用：{tag}")
        self._refresh_engine_combo()

    def _on_nnue_status(self, text):
        self.lbl_nnue.setText("NNUE：" + text)
        if "警告" in text or "failed" in text.lower():
            self.statusBar().showMessage(text, 6000)

    # ---------- 人机对战 ----------
    def _on_ai_start(self):
        eng = self.active_engine
        if eng is None or not eng.running:
            QMessageBox.information(
                self, "人机对战",
                "请先在「引擎」页加载一个 UCI 引擎。\n"
                "程序会自动发现本机皮卡鱼 / 皮卡揭，并内置 AB JChess 揭棋引擎。")
            return
        if self.game.mode == Mode.JIEQI and not eng.supports_jieqi:
            QMessageBox.warning(
                self, "人机对战",
                f"当前引擎「{eng.info.name}」不支持揭棋暗子局面。\n"
                "请在「引擎」页切换为 AB JChess 或 PikaJieQi 揭棋引擎。")
            return
        side = Side.BLACK if self.cmb_ai_side.currentIndex() == 0 else Side.RED
        self.options.engine_movetime = (300, 1000, 3000, 5000)[
            self.cmb_ai_time.currentIndex()]
        self.options.engine_use_movetime = True
        self.ai_controller.configure(self.game, self.options, side)
        self.ai_controller.start()
        self.tabs.setCurrentIndex(0)
        self._refresh()

    def _on_ai_stop(self):
        self.ai_controller.stop()
        self._refresh()

    def _on_ai_toggle(self):
        if self.ai_controller.enabled:
            self._on_ai_stop()
        else:
            self._on_ai_start()

    def _on_ai_move(self, move):
        if self.game.result is None and self.game.play(move):
            self._after_move()

    def _on_ai_status(self, text):
        self.lbl_ai_status.setText("状态：" + text)
        self.statusBar().showMessage("[人机] " + text, 4000)

    def _on_league(self):
        self.league_window = LeagueWindow(
            self.options, self.options.engine_paths, self)
        self.league_window.show()
        self.statusBar().showMessage(
            "引擎联赛：普通象棋 / 揭棋多引擎循环赛，支持包干用时与积分榜", 5000)

    def _on_vision(self):
        from ui.vision_window import VisionWindow
        if self.vision_window is None:
            self.vision_window = VisionWindow(self)
        self.vision_window.show()
        self.vision_window.raise_()
        self.vision_window.activateWindow()
        self.statusBar().showMessage(
            "视觉识别：选窗口 → 识别主棋盘 → 同步局面 / 连续自动下棋", 5000)

    def _on_engine_manage(self):
        dlg = EngineDialog(self.engine_manager, self)
        dlg.engine_loaded.connect(self._attach_engine)
        dlg.exec()
        self._refresh_engine_combo()
        self._save_settings()

    def _on_analyse(self):
        eng = self.active_engine
        if eng is None or not eng.running:
            QMessageBox.information(
                self, "引擎分析",
                "尚未加载引擎。请先在「引擎管理」中加载 UCI 引擎；\n"
                "程序已内置 AB JChess 揭棋引擎，启动时会自动加载。")
            return
        fen = self.game.board.to_fen(
            self.game.side_to_move, self.game.current.halfmove_clock,
            self.game.fullmove)
        self.board_widget.set_hint(None)
        self.lbl_engine.setText("引擎思考中…")
        self.btn_analyse.setEnabled(False)
        eng.analyse(
            fen,
            depth=self.options.engine_depth,
            movetime=self.options.engine_movetime,
            use_movetime=self.options.engine_use_movetime,
            hash_mb=self.options.engine_hash,
            threads=self.options.engine_threads,
        )

    def _on_analysis_ready(self, analysis):
        self.btn_analyse.setEnabled(True)
        self.lbl_engine.setText(
            f"{analysis.summary} · 建议着法 {analysis.bestmove or '--'}")
        self.txt_pv.setPlainText("\n".join(analysis.lines[-8:]))
        move = uci_to_move(analysis.bestmove,
                           self.options.engine_rank_flip)
        if move and self.game.is_legal(move):
            self.board_widget.set_hint(move)
            self.statusBar().showMessage(
                f"引擎建议 {analysis.bestmove}（棋盘箭头标出，不自动落子）", 5000)
        else:
            self.board_widget.set_hint(None)
            self.statusBar().showMessage(
                "引擎建议着法与当前局面不符（揭棋暗子按期望搜索），仅显示文本", 5000)

    def _on_engine_stop(self):
        if self.active_engine:
            self.active_engine.stop()
        self.btn_analyse.setEnabled(True)

    def _on_engine_failed(self, message):
        self.btn_analyse.setEnabled(True)
        self.lbl_engine.setText("引擎：" + message)
        self.statusBar().showMessage("引擎：" + message, 6000)

    # ---------- 杂项 ----------
    def _on_zoom(self, delta):
        self.options.board_zoom = min(
            1.4, max(0.7, self.options.board_zoom + delta * 0.05))
        self.board_widget.zoom = self.options.board_zoom
        self.board_widget.update()

    def _on_settings(self):
        dlg = SettingsDialog(self.options, self)
        if dlg.exec() == QDialog.Accepted:
            self.options = dlg.result_options()
            self._apply_options()
            self._refresh()
            self._save_settings()
            self.statusBar().showMessage("设置已保存（规则选项即时生效）", 3500)

    def _on_rules(self):
        RulesDialog(self).exec()

    def _on_about(self):
        AboutDialog(self).exec()

    def _on_copy_qq(self):
        QApplication.clipboard().setText(QQ_GROUP)
        self.statusBar().showMessage(
            f"官方交流群 {QQ_GROUP} 已复制到剪贴板", 4000)

    def _save_settings(self):
        self.settings.setValue(
            "options", json.dumps(self.options.to_dict(), ensure_ascii=False))

    def closeEvent(self, event):
        if self.vision_window is not None:
            try:
                self.vision_window.controller.stop()
                self.vision_window.close()
            except Exception:
                pass
        self.ai_controller.stop()
        self.engine_manager.unload()
        self._save_settings()
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)
