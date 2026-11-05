# -*- coding: utf-8 -*-
"""引擎联赛窗口：多引擎循环赛、包干用时、实时棋盘观战、积分榜。

与「人机自动下棋 / 连线自动走子」占位不同，联赛是引擎对引擎的赛事模块，
自动推进对局但不代表玩家在第三方平台下棋。
"""
from __future__ import annotations

import json
import os
import random
import time

from PySide6.QtCore import Qt, QTimer, QObject, Signal
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QPushButton, QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QTableWidget,
    QTableWidgetItem, QFileDialog, QMessageBox, QSizePolicy, QHeaderView,
)

from ai.engine import UciEngine, bundled_engine_path
from ai.league import League, LeaguePlayer
from core.board import Board
from core.constants import Mode, Side, format_clock
from core.game import Game
from core.notation import uci_to_move
from core.options import Options
from core.rules import GameResult
from ui.board_widget import BoardWidget


class LeagueController(QObject):
    """驱动联赛：创建引擎进程 → 逐步分析 → 落子 → 判定 → 下一局。"""

    log = Signal(str)
    status = Signal(str)
    position = Signal(object)
    standings_changed = Signal()
    league_done = Signal()

    def __init__(self, league: League, options: Options, parent=None):
        super().__init__(parent)
        self.league = league
        self.options = options
        self.rank_flip = options.engine_rank_flip
        self.game: Game | None = None
        self.match = None
        self.engines: dict[int, UciEngine] = {}
        self.running = False
        self.paused = False
        self.step_once_flag = False
        self._expect_engine = None
        self._think_start = 0.0
        self.pack_left = {int(Side.RED): league.pack_seconds,
                          int(Side.BLACK): league.pack_seconds}

    # ---------- 生命周期 ----------
    def start(self):
        self.running = True
        self.paused = False
        self.step_once_flag = False
        self._begin_match()

    def pause(self):
        self.paused = True
        self.status.emit("已暂停（当前思考中的一步会落子后停下）")

    def resume(self):
        self.paused = False
        if self.game is None or self.game.result is not None:
            self._begin_match()
        else:
            self._request_move()

    def step_once(self):
        self.paused = False
        self.step_once_flag = True
        if self.game is None or self.game.result is not None:
            self._begin_match()
        else:
            self._request_move()

    def stop(self):
        self.running = False
        self.paused = True
        self._release_engines()
        self.status.emit("联赛已终止")

    # ---------- 对局 ----------
    def _begin_match(self):
        if not self.running or self.paused:
            return
        m = self.league.next_match()
        if m is None:
            self.running = False
            self.status.emit("联赛全部结束")
            self.log.emit("=" * 50 + "\n联赛结束，最终积分：\n"
                          + self.league.summary())
            self.league_done.emit()
            self.standings_changed.emit()
            return
        self.match = m
        self._release_engines()
        opts = Options()
        if m.fen:
            board = Board(m.mode, opts, fen=m.fen)
            self.game = Game(opts, m.mode)
            self.game.replace_board(board)
        else:
            self.game = Game(opts, m.mode)
            self.game.reset(m.mode, seed=(m.seed or None))
        for side, player in ((Side.RED, m.red), (Side.BLACK, m.black)):
            eng = UciEngine(player.path)
            eng.start()
            eng.analysis_ready.connect(
                lambda a, e=eng: self._on_analysis(a, e))
            eng.failed.connect(
                lambda msg, e=eng: self._on_engine_failed(msg, e))
            self.engines[int(side)] = eng
        self.pack_left = {int(Side.RED): self.league.pack_seconds,
                          int(Side.BLACK): self.league.pack_seconds}
        self.status.emit(f"第 {m.round_no} 轮 · {m.label}")
        self.log.emit(f"▶ 第 {m.round_no} 轮开始：{m.label}")
        self.position.emit(self.game)
        self._request_move()

    def _request_move(self):
        if not self.running or self.paused or self.game is None:
            return
        if self.game.result is not None:
            self._finish(self.game.result)
            return
        side = self.game.side_to_move
        eng = self.engines.get(int(side))
        if eng is None or not eng.running:
            self.status.emit(f"{side.label} 引擎未就绪")
            return
        self._expect_engine = eng
        fen = self.game.board.to_fen(
            side, self.game.current.halfmove_clock, self.game.fullmove)
        self._think_start = time.monotonic()
        eng.analyse(fen, movetime=self.league.movetime,
                    use_movetime=True, hash_mb=self.league.hash_mb,
                    threads=self.league.threads,
                    chase_rule=self.league.chase_rule)
        self.status.emit(
            f"{side.label} 思考中… 包干剩余 {self._fmt(side)}")

    def _on_analysis(self, analysis, eng):
        if not self.running or eng is not self._expect_engine:
            return
        self._expect_engine = None
        if self.game is None:
            return
        side = self.game.side_to_move
        elapsed = time.monotonic() - self._think_start
        if self.league.pack_seconds > 0:
            self.pack_left[int(side)] -= elapsed
            if self.pack_left[int(side)] <= 0:
                self.game.timeout(side)
                self.log.emit(f"{side.label} 包干用时耗尽，判负")
                self._finish(self.game.result)
                return
        move = uci_to_move(analysis.bestmove, self.rank_flip)
        if move is None or not self.game.is_legal(move):
            legal = self.game.legal_moves()
            if not legal:
                self._finish(self.game.result)
                return
            move = random.choice(legal)
            self.log.emit(f"⚠ {side.short} 引擎着法 {analysis.bestmove} "
                          "不适用当前局面，改走随机合法着法")
        self.game.play(move)
        self.match.moves.append(move)
        self.log.emit(
            f"{self.game.ply:3d}. {side.short}  {self.game.current.notation}"
            f"   [{analysis.score_text} / d{analysis.depth}]")
        self.position.emit(self.game)
        if self.game.result is not None:
            self._finish(self.game.result)
            return
        if self.paused or self.step_once_flag:
            self.paused = True
            self.step_once_flag = False
            self.status.emit("单步完成，已暂停")
            return
        QTimer.singleShot(60, self._request_move)

    def _on_engine_failed(self, message, eng):
        self.log.emit(f"⚠ 引擎异常：{message}")
        if self.running and eng is self._expect_engine and self.game:
            side = self.game.side_to_move
            self.game._finish(GameResult(
                True, side.opponent, "引擎异常退出", "engine_error"))
            self._finish(self.game.result)

    def _finish(self, result: GameResult):
        if self.match is None or self.match.result is not None:
            return
        self.league.apply_result(self.match, result)
        self.log.emit(f"■ 本局结果：{result.text}")
        self.standings_changed.emit()
        self.position.emit(self.game)
        self._release_engines()
        if not self.running:
            return
        if self.paused:
            self.status.emit("已暂停 · 点「继续」开始下一局")
            return
        QTimer.singleShot(500, self._begin_match)

    def _release_engines(self):
        for eng in self.engines.values():
            try:
                eng.quit()
            except Exception:
                pass
        self.engines.clear()
        self._expect_engine = None

    def _fmt(self, side) -> str:
        if self.league.pack_seconds <= 0:
            return "不限"
        return format_clock(self.pack_left[int(side)])


class LeagueWindow(QMainWindow):
    def __init__(self, options: Options, engine_paths=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("引擎联赛 · 飞鲨象棋（普通象棋 / 揭棋）")
        self.resize(1200, 820)
        self.options = options
        self.controller: LeagueController | None = None
        self.league: League | None = None
        self._build_ui(engine_paths or [])

    def _build_ui(self, engine_paths):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        cfg = QGroupBox("联赛设置（多引擎循环赛 · 包干用时）")
        grid = QGridLayout(cfg)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems([f"{Mode.XIANGQI.label}联赛",
                                f"{Mode.JIEQI.label}联赛"])
        self.spn_rounds = QSpinBox(); self.spn_rounds.setRange(1, 20)
        self.spn_rounds.setValue(2)
        self.spn_movetime = QSpinBox(); self.spn_movetime.setRange(50, 30000)
        self.spn_movetime.setSingleStep(100)
        self.spn_movetime.setValue(300)
        self.spn_pack = QDoubleSpinBox(); self.spn_pack.setRange(0, 120)
        self.spn_pack.setDecimals(1); self.spn_pack.setValue(5.0)
        self.spn_win = QDoubleSpinBox(); self.spn_win.setValue(3.0)
        self.spn_draw = QDoubleSpinBox(); self.spn_draw.setValue(1.0)
        self.spn_loss = QDoubleSpinBox(); self.spn_loss.setValue(0.0)
        self.edt_fen = QLineEdit()
        self.edt_fen.setPlaceholderText("留空 = 标准开局；可粘贴 AB-JChess FEN")
        self.edt_seed = QLineEdit()
        self.edt_seed.setPlaceholderText("揭棋洗牌种子，留空随机")
        grid.addWidget(QLabel("联赛模式"), 0, 0)
        grid.addWidget(self.cmb_mode, 0, 1)
        grid.addWidget(QLabel("循环轮数"), 0, 2)
        grid.addWidget(self.spn_rounds, 0, 3)
        grid.addWidget(QLabel("每步时限 (ms)"), 0, 4)
        grid.addWidget(self.spn_movetime, 0, 5)
        grid.addWidget(QLabel("包干用时 (分/方，0=不限)"), 1, 0)
        grid.addWidget(self.spn_pack, 1, 1)
        grid.addWidget(QLabel("胜/和/负 积分"), 1, 2)
        scores = QWidget(); hs = QHBoxLayout(scores)
        hs.setContentsMargins(0, 0, 0, 0)
        for sp in (self.spn_win, self.spn_draw, self.spn_loss):
            sp.setRange(0, 10); sp.setDecimals(1)
            hs.addWidget(sp)
        grid.addWidget(scores, 1, 3, 1, 3)
        grid.addWidget(QLabel("开局 FEN"), 2, 0)
        grid.addWidget(self.edt_fen, 2, 1, 1, 3)
        grid.addWidget(QLabel("揭棋种子"), 2, 4)
        grid.addWidget(self.edt_seed, 2, 5)
        grid.addWidget(QLabel("参赛引擎（可添加多个 UCI 引擎）"), 3, 0)
        self.lst_engines = QListWidget()
        self.lst_engines.setMaximumHeight(76)
        grid.addWidget(self.lst_engines, 3, 1, 1, 4)
        btn_add = QPushButton("添加引擎…")
        btn_del = QPushButton("移除")
        btn_add.clicked.connect(self._add_engine)
        btn_del.clicked.connect(self._del_engine)
        grid.addWidget(btn_add, 3, 5)
        grid.addWidget(btn_del, 4, 5)
        hint = QLabel("提示：揭棋联赛请用 AB JChess 揭棋引擎；普通象棋联赛可加载 "
                      "皮卡鱼等 UCI 引擎。只有 1 个引擎时会自动复制一份自战测试。")
        hint.setWordWrap(True)
        grid.addWidget(hint, 4, 0, 1, 5)
        root.addWidget(cfg)

        ctrl = QHBoxLayout()
        self.btn_start = QPushButton("开始联赛")
        self.btn_pause = QPushButton("暂停")
        self.btn_step = QPushButton("单步推进")
        self.btn_stop = QPushButton("终止")
        self.btn_save = QPushButton("保存结果…")
        for b in (self.btn_start, self.btn_pause, self.btn_step,
                  self.btn_stop, self.btn_save):
            ctrl.addWidget(b)
        self.btn_pause.setEnabled(False)
        self.btn_step.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_save.setEnabled(False)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_pause.clicked.connect(self._on_pause)
        self.btn_step.clicked.connect(self._on_step)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_save.clicked.connect(self._on_save)
        root.addLayout(ctrl)

        self.lbl_status = QLabel("尚未开始")
        self.lbl_status.setStyleSheet("font-weight:bold;")
        root.addWidget(self.lbl_status)

        mid = QHBoxLayout()
        left = QVBoxLayout()
        self.board_widget = BoardWidget()
        self.board_widget.interactive = False
        self.board_widget.apply_display(self.options)
        self.board_widget.setSizePolicy(QSizePolicy.Expanding,
                                        QSizePolicy.Expanding)
        left.addWidget(self.board_widget, 3)
        self.lbl_current = QLabel("当前对局：—")
        left.addWidget(self.lbl_current)
        self.lst_moves = QListWidget()
        self.lst_moves.setMaximumHeight(120)
        left.addWidget(self.lst_moves, 1)
        mid.addLayout(left, 2)

        right = QVBoxLayout()
        right.addWidget(QLabel("赛程 / 对阵"))
        self.lst_schedule = QListWidget()
        self.lst_schedule.setMinimumHeight(140)
        right.addWidget(self.lst_schedule, 1)
        right.addWidget(QLabel("积分榜"))
        self.tbl_standings = QTableWidget(0, 6)
        self.tbl_standings.setHorizontalHeaderLabels(
            ["排名", "引擎", "积分", "胜", "和", "负"])
        self.tbl_standings.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)
        self.tbl_standings.setMinimumHeight(140)
        right.addWidget(self.tbl_standings, 1)
        right.addWidget(QLabel("联赛日志"))
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        right.addWidget(self.txt_log, 2)
        mid.addLayout(right, 2)
        root.addLayout(mid, 1)

        seen = set()
        for path in engine_paths[:6]:
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen:
                self._append_engine(path)
                seen.add(key)
        bundle = bundled_engine_path()
        if bundle and self.lst_engines.count() < 2:
            # 补足两个席位，开箱即可跑自战联赛
            while self.lst_engines.count() < 2:
                self._append_engine(bundle, f" ({self.lst_engines.count() + 1})")

    # ---------- 参赛引擎 ----------
    def _append_engine(self, path, suffix=""):
        name = os.path.basename(path) + suffix
        item = QListWidgetItem(f"{name}  —  {path}")
        item.setData(Qt.UserRole, (path, name))
        self.lst_engines.addItem(item)

    def _add_engine(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "添加联赛引擎", "", "可执行文件 (*.exe);;所有文件 (*)")
        if path:
            self._append_engine(path)

    def _del_engine(self):
        row = self.lst_engines.currentRow()
        if row >= 0:
            self.lst_engines.takeItem(row)

    def _players(self):
        result = []
        for i in range(self.lst_engines.count()):
            path, name = self.lst_engines.item(i).data(Qt.UserRole)
            result.append(LeaguePlayer(path, name))
        if len(result) == 1:
            p = result[0]
            result.append(LeaguePlayer(p.path, p.name + " (2)", "自战测试"))
        return result

    # ---------- 控制 ----------
    def _on_start(self):
        try:
            league = League(
                mode=Mode(self.cmb_mode.currentIndex()),
                players=self._players(),
                rounds=self.spn_rounds.value(),
                fen=self.edt_fen.text().strip(),
                seed=self.edt_seed.text().strip(),
                movetime=self.spn_movetime.value(),
                pack_seconds=self.spn_pack.value() * 60.0,
                score_win=self.spn_win.value(),
                score_draw=self.spn_draw.value(),
                score_loss=self.spn_loss.value(),
                hash_mb=self.options.engine_hash,
                threads=self.options.engine_threads,
                chase_rule="skyrule",
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法开始联赛", str(exc))
            return
        if self.controller:
            self.controller.stop()
        self.league = league
        self.txt_log.clear()
        self.controller = LeagueController(league, self.options, self)
        self.controller.log.connect(self._on_log)
        self.controller.status.connect(self.lbl_status.setText)
        self.controller.position.connect(self._on_position)
        self.controller.standings_changed.connect(self._refresh_tables)
        self.controller.league_done.connect(self._on_done)
        self._refresh_tables()
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_step.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.btn_save.setEnabled(True)
        self._on_log(league.summary().split("\n")[0])
        self.controller.start()

    def _on_pause(self):
        if not self.controller:
            return
        if self.controller.paused:
            self.controller.resume()
            self.btn_pause.setText("暂停")
        else:
            self.controller.pause()
            self.btn_pause.setText("继续")

    def _on_step(self):
        if self.controller:
            self.controller.step_once()
            self.btn_pause.setText("继续")

    def _on_stop(self):
        if self.controller:
            self.controller.stop()
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_step.setEnabled(False)
        self.btn_stop.setEnabled(False)

    def _on_done(self):
        QMessageBox.information(self, "联赛结束",
                                "全部对局结束，可在积分榜查看排名并保存结果。")
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_step.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_pause.setText("暂停")

    def _on_save(self):
        if not self.league:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存联赛结果", "联赛结果.json", "JSON (*.json);;文本报告 (*.txt)")
        if not path:
            return
        try:
            if path.lower().endswith(".txt"):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.league.summary())
            else:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.league.to_dict(), f,
                              ensure_ascii=False, indent=1)
            self._on_log("结果已保存：" + path)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    # ---------- 刷新 ----------
    def _on_log(self, text):
        self.txt_log.appendPlainText(text)

    def _on_position(self, game: Game):
        self.board_widget.set_board(game.board)
        self.board_widget.set_last_move(game.current.move)
        self.board_widget.set_check_pos(
            game.board.general_pos(game.side_to_move) if game.in_check else None)
        m = self.controller.match if self.controller else None
        head = m.label if m else "—"
        self.lbl_current.setText(
            f"{head} · 轮到 {game.side_to_move.label} · 第 {game.ply} 手"
            + (f" · {game.result.text}" if game.result else ""))
        self.lst_moves.clear()
        for i, node in enumerate(game.display_line()):
            self.lst_moves.addItem(
                "开局" if i == 0 else
                f"{i:3d}. {Side(node.side).short}  {node.notation}")
        self.lst_moves.scrollToBottom()

    def _refresh_tables(self):
        if not self.league:
            return
        self.lst_schedule.clear()
        for i, m in enumerate(self.league.schedule, 1):
            mark = "▶" if m is self.league.current_match() else "  "
            self.lst_schedule.addItem(
                f"{mark}{i:2d}. [第{m.round_no}轮] {m.label} — {m.status}")
        standings = self.league.standings()
        self.tbl_standings.setRowCount(len(standings))
        for r, p in enumerate(standings):
            values = [str(r + 1), p.name, f"{p.score:.1f}",
                      str(p.wins), str(p.draws), str(p.losses)]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setTextAlignment(Qt.AlignCenter)
                self.tbl_standings.setItem(r, c, item)

    def closeEvent(self, event):
        if self.controller:
            self.controller.stop()
        super().closeEvent(event)
