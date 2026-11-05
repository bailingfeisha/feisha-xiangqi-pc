# -*- coding: utf-8 -*-
"""设置 / 规则说明 / 关于 / FEN / 引擎管理 对话框。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QClipboard
from PySide6.QtWidgets import (
    QDialog, QTabWidget, QWidget, QFormLayout, QVBoxLayout, QHBoxLayout,
    QComboBox, QCheckBox, QSpinBox, QDoubleSpinBox, QLineEdit, QPushButton,
    QDialogButtonBox, QTextBrowser, QLabel, QPlainTextEdit, QListWidget,
    QListWidgetItem, QFileDialog, QMessageBox, QApplication,
)

from core.constants import PieceType, Side, piece_name
from core.options import Options

# 官方交流群（状态栏、关于、帮助菜单、视觉窗口共用）
QQ_GROUP = "1131198508"

RULES_XIANGQI = """
<h3>普通象棋 · 基本规则</h3>
<p>棋盘 9 列 × 10 行，棋子落在交叉点上；红方在下先行，黑方在上。中间为河界，
两侧各有一个 3×3 九宫。</p>
<ul>
<li><b>车</b>：直线任意步，遇子停，可吃挡路的敌方子。</li>
<li><b>马</b>：走「日」字；先走的一格（马腿）有子则不能走（蹩马腿）。</li>
<li><b>炮</b>：不吃子时同车；吃子时必须隔<b>恰好一个</b>棋子（炮架）。</li>
<li><b>象/相</b>：走「田」字，不能过河；田字中心有子则不能走（塞象眼）。</li>
<li><b>士/仕</b>：九宫内斜走一格。</li>
<li><b>将/帅</b>：九宫内直走一格；双方将帅不得在同一列无遮挡照面（飞将）。</li>
<li><b>兵/卒</b>：未过河只能向前一格；过河后可向前或左右一格，永不后退。</li>
</ul>
<p><b>胜负</b>：将死、困毙（无合法着法）、吃掉对方将帅、认输、超时判负。
<b>和棋</b>：双方同意、三次重复局面、连续无吃子回合、超过最大回合数。</p>
<p><b>本程序暂不判定</b>长将 / 长捉等竞赛禁手（可在设置中调整自然限着）。</p>
"""

RULES_JIEQI = """
<h3>揭棋 · 本程序采用的规则</h3>
<p>揭棋（又称冚棋、潮州象棋）使用与中国象棋相同的棋盘与 32 枚棋子。</p>
<ol>
<li><b>摆子</b>：双方把将/帅<b>明放</b>在九宫原点；其余各 15 枚棋子背面朝上，
洗乱后随机摆在各自一边的<b>中国象棋开局原始格位</b>上（这些叫「暗子」）。</li>
<li><b>暗子走第一步</b>：暗子按其<b>所在格位</b>对应的开局棋子行动——
在兵位就当兵（只能向前一步），在车位就当车（直线任意步并可吃子），
炮位当炮（需隔一子吃），马位当马（蹩马腿），象位当象（塞象眼、不过河），
士位当士（九宫内斜走）。<b>走完第一步必须立即翻开成为明子</b>。</li>
<li><b>翻开身份是随机的</b>：每枚暗子在洗牌时已获得一个真实身份，
翻开后才显示；此后按该真实身份走棋（可能与第一步的「位置身份」不同）。</li>
<li><b>暗子可以吃子，也可以被吃</b>；被吃后保持背面朝上移出棋盘，双方都不翻看。</li>
<li><b>明子</b>：与象棋基本相同，唯一区别是揭棋的<b>士可出九宫、象可过河</b>
（可在「设置 → 规则」中关闭）。</li>
<li><b>胜负</b>：先吃掉对方将/帅者胜；将死、困毙、认输、超时判负；
长将长捉、无吃子限着、最大回合数判和。</li>
</ol>
<p>本程序支持指定<b>洗牌种子</b>以复现同一局暗子布局（设置 → 规则）。</p>
"""

RULES_ENGINE = """
<h3>引擎与 FEN（已适配 AB JChess 1 RB）</h3>
<p>程序随附 <b>AB JChess 1 RB</b> 揭棋引擎（engines/AB-JChess_1_RB_bmi2/，
含 NNUE 评估文件），启动时自动登记并加载；也可在「引擎管理」里加载其他
UCI / UCCI 引擎。</p>
<p>引擎界面提供<b>局面分析</b>（最佳着法、评分、深度、节点数、主变 PV、
棋盘箭头提示）；F5 打开视觉识别窗口，可选择目标窗口做连续识别与自动下棋
（含包干用时、思考细节、变招、模拟器 ADB 落子）。</p>
<h4>揭棋 FEN 格式</h4>
<pre>xxxxkxxxx/9/1x5x1/x1x1x1x1x/9/9/X1X1X1X1X/1X5X1/9/XXXXKXXXX w R2A2C2P5N2B2r2a2c2p5n2b2 0 1</pre>
<ul>
<li>棋盘：从上到下 10 行（黑方底线到红方底线），大写红 / 小写黑。</li>
<li><code>R</code>车 <code>N</code>马 <code>B</code>象 <code>A</code>士
<code>K</code>将帅 <code>C</code>炮 <code>P</code>卒兵；<code>X</code>/<code>x</code> 为暗子。</li>
<li>第 3 段是<b>暗子身份池</b>（各身份剩余数量），<code>-</code> 表示无暗子。</li>
<li>第 2、4、5 段：行棋方 w/b、半回合数、回合数。</li>
<li>着法坐标 a..i + rank 0..9，rank 0 = 红方底线。</li>
</ul>
<p>「复制 FEN / 导入 FEN」与本引擎完全互通；普通象棋局面池字段为 <code>-</code>。
揭棋时引擎按暗子身份分布做期望搜索，翻出的具体棋子以界面为准。</p>
<h4>预留空位</h4>
<p><code>network/client.py :: NetworkClient</code>（连线对战）与开局库 OBK
仍未接入，界面对应按钮为禁用占位；本机视觉识别与自动落子已实现。</p>
"""


class SettingsDialog(QDialog):
    def __init__(self, options: Options, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置 · 飞鲨象棋")
        self.resize(500, 430)
        self.opt = Options.from_dict(options.to_dict())
        tabs = QTabWidget()
        tabs.addTab(self._tab_ui(), "界面")
        tabs.addTab(self._tab_rules(), "规则")
        tabs.addTab(self._tab_clock(), "计时")
        tabs.addTab(self._tab_engine(), "引擎")
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel
                               | QDialogButtonBox.RestoreDefaults)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        box.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._restore)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(box)

    # ---------- 控件构造 ----------
    def _tab_ui(self):
        w = QWidget()
        f = QFormLayout(w)
        self.cmb_theme = QComboBox()
        for key, label in (("wood", "经典木纹"), ("dark", "暗色"),
                           ("bamboo", "竹青")):
            self.cmb_theme.addItem(label, key)
        self.cmb_theme.setCurrentIndex(
            max(0, self.cmb_theme.findData(self.opt.theme)))
        self.cmb_style = QComboBox()
        self.cmb_style.addItem("简体（帅仕相马车炮兵）", "simplified")
        self.cmb_style.addItem("繁体（帥仕相傌俥炮兵）", "traditional")
        self.cmb_style.setCurrentIndex(
            max(0, self.cmb_style.findData(self.opt.piece_style)))
        self.chk_coords = QCheckBox(); self.chk_coords.setChecked(self.opt.show_coords)
        self.chk_hints = QCheckBox(); self.chk_hints.setChecked(self.opt.show_hints)
        self.chk_last = QCheckBox(); self.chk_last.setChecked(self.opt.show_last_move)
        self.chk_captured = QCheckBox(); self.chk_captured.setChecked(self.opt.show_captured)
        self.chk_sound = QCheckBox(); self.chk_sound.setChecked(self.opt.sound)
        self.spn_zoom = QDoubleSpinBox()
        self.spn_zoom.setRange(0.7, 1.4)
        self.spn_zoom.setSingleStep(0.05)
        self.spn_zoom.setValue(self.opt.board_zoom)
        f.addRow("棋盘主题", self.cmb_theme)
        f.addRow("棋子文字", self.cmb_style)
        f.addRow("显示坐标", self.chk_coords)
        f.addRow("显示可走点", self.chk_hints)
        f.addRow("标记上一步", self.chk_last)
        f.addRow("显示被吃棋子", self.chk_captured)
        f.addRow("走子提示音", self.chk_sound)
        f.addRow("棋盘缩放（Ctrl+滚轮）", self.spn_zoom)
        return w

    def _tab_rules(self):
        w = QWidget()
        f = QFormLayout(w)
        self.chk_adv = QCheckBox("士可出九宫 / 过河")
        self.chk_adv.setChecked(self.opt.jieqi_advisor_cross)
        self.chk_ele = QCheckBox("象可过河")
        self.chk_ele.setChecked(self.opt.jieqi_elephant_cross)
        self.chk_darkcap = QCheckBox("暗子允许被吃")
        self.chk_darkcap.setChecked(self.opt.dark_can_be_captured)
        self.spn_nocap = QSpinBox(); self.spn_nocap.setRange(0, 400)
        self.spn_nocap.setValue(self.opt.no_capture_draw)
        self.spn_maxmove = QSpinBox(); self.spn_maxmove.setRange(0, 1000)
        self.spn_maxmove.setValue(self.opt.max_moves)
        self.chk_repeat = QCheckBox(); self.chk_repeat.setChecked(self.opt.repetition_draw)
        self.edt_seed = QLineEdit(str(self.opt.seed))
        self.edt_seed.setPlaceholderText("留空 = 每局随机；填写后可复现暗子布局")
        f.addRow("揭棋 · 明子士", self.chk_adv)
        f.addRow("揭棋 · 明子象", self.chk_ele)
        f.addRow("揭棋 · 暗子", self.chk_darkcap)
        f.addRow("无吃子半回合判和（0=关）", self.spn_nocap)
        f.addRow("最大回合数判和（0=关）", self.spn_maxmove)
        f.addRow("三次重复局面判和", self.chk_repeat)
        f.addRow("揭棋洗牌种子", self.edt_seed)
        return w

    def _tab_clock(self):
        w = QWidget()
        f = QFormLayout(w)
        self.chk_clock = QCheckBox(); self.chk_clock.setChecked(self.opt.use_clock)
        self.spn_minutes = QSpinBox(); self.spn_minutes.setRange(1, 180)
        self.spn_minutes.setValue(self.opt.minutes_per_side)
        f.addRow("启用对局计时", self.chk_clock)
        f.addRow("每方时限（分钟）", self.spn_minutes)
        note = QLabel("超时判负；浏览历史棋谱时计时暂停。")
        note.setWordWrap(True)
        f.addRow(note)
        return w

    def _tab_engine(self):
        w = QWidget()
        f = QFormLayout(w)
        self.cmb_gomode = QComboBox()
        self.cmb_gomode.addItem("按思考时间", "movetime")
        self.cmb_gomode.addItem("按搜索深度", "depth")
        self.cmb_gomode.setCurrentIndex(
            0 if self.opt.engine_use_movetime else 1)
        self.spn_movetime = QSpinBox(); self.spn_movetime.setRange(100, 60000)
        self.spn_movetime.setSingleStep(100)
        self.spn_movetime.setValue(self.opt.engine_movetime)
        self.spn_depth = QSpinBox(); self.spn_depth.setRange(1, 60)
        self.spn_depth.setValue(self.opt.engine_depth)
        self.spn_hash = QSpinBox(); self.spn_hash.setRange(1, 4096)
        self.spn_hash.setValue(self.opt.engine_hash)
        self.spn_threads = QSpinBox(); self.spn_threads.setRange(1, 64)
        self.spn_threads.setValue(self.opt.engine_threads)
        self.cmb_chase = QComboBox()
        for k, label in (("skyrule", "天空规则（默认）"),
                         ("default", "标准"), ("none", "不判长捉")):
            self.cmb_chase.addItem(label, k)
        self.cmb_chase.setCurrentIndex(
            max(0, self.cmb_chase.findData("skyrule")))
        self.chk_rankflip = QCheckBox("UCI rank 0 = 红方底线（AB-JChess）")
        self.chk_rankflip.setChecked(self.opt.engine_rank_flip)
        f.addRow("思考方式", self.cmb_gomode)
        f.addRow("思考时间（毫秒）", self.spn_movetime)
        f.addRow("搜索深度", self.spn_depth)
        f.addRow("Hash（MB）", self.spn_hash)
        f.addRow("线程数", self.spn_threads)
        f.addRow("长捉规则 ChaseRule", self.cmb_chase)
        f.addRow(self.chk_rankflip)
        return w

    def _restore(self):
        self.opt = Options()
        self._restored = True
        self.accept()

    def _collect(self):
        o = self.opt
        o.theme = self.cmb_theme.currentData()
        o.piece_style = self.cmb_style.currentData()
        o.show_coords = self.chk_coords.isChecked()
        o.show_hints = self.chk_hints.isChecked()
        o.show_last_move = self.chk_last.isChecked()
        o.show_captured = self.chk_captured.isChecked()
        o.sound = self.chk_sound.isChecked()
        o.board_zoom = self.spn_zoom.value()
        o.jieqi_advisor_cross = self.chk_adv.isChecked()
        o.jieqi_elephant_cross = self.chk_ele.isChecked()
        o.dark_can_be_captured = self.chk_darkcap.isChecked()
        o.no_capture_draw = self.spn_nocap.value()
        o.max_moves = self.spn_maxmove.value()
        o.repetition_draw = self.chk_repeat.isChecked()
        o.seed = self.edt_seed.text().strip()
        o.use_clock = self.chk_clock.isChecked()
        o.minutes_per_side = self.spn_minutes.value()
        o.engine_use_movetime = self.cmb_gomode.currentData() == "movetime"
        o.engine_movetime = self.spn_movetime.value()
        o.engine_depth = self.spn_depth.value()
        o.engine_hash = self.spn_hash.value()
        o.engine_threads = self.spn_threads.value()
        o.engine_rank_flip = self.chk_rankflip.isChecked()

    def accept(self):
        if not getattr(self, "_restored", False):
            self._collect()
        super().accept()

    def result_options(self) -> Options:
        return self.opt


class RulesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("规则说明 · 飞鲨象棋")
        self.resize(620, 560)
        tabs = QTabWidget()
        for title, html in (("普通象棋", RULES_XIANGQI),
                            ("揭棋", RULES_JIEQI),
                            ("引擎 / FEN", RULES_ENGINE)):
            view = QTextBrowser()
            view.setHtml(html)
            view.setOpenExternalLinks(True)
            tabs.addTab(view, title)
        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(box)


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关于 · 飞鲨象棋")
        self.resize(450, 320)
        label = QLabel(
            "<h2>飞鲨象棋 PC 版</h2>"
            "<p>版本 1.2 发布版 · 基于 PySide6</p>"
            "<p>支持 <b>普通象棋</b> 与 <b>揭棋</b>，双模式完整规则、"
            "分支棋谱、悔棋拆棋、局面编辑、FEN 导入导出、对局计时、"
            "中文记谱、棋盘翻转、人机对战、引擎联赛。</p>"
            "<p>内置 <b>YOLO 识别模型</b> 与窗口识别 / 连续自动下棋；"
            "可加载 <b>AB JChess 1 RB</b> 及其他 UCI/UCCI 引擎。</p>"
            f"<p>官方交流群：<b style='font-size:15px'>{QQ_GROUP}</b></p>"
            "<p><i>连线 / 开局库：预留空位。</i></p>")
        label.setWordWrap(True)
        label.setTextFormat(Qt.RichText)
        btn_copy = QPushButton("复制群号")
        btn_copy.clicked.connect(lambda: self._copy(btn_copy))
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn_copy)
        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(label)
        lay.addLayout(row)
        lay.addWidget(box)

    def _copy(self, btn):
        QApplication.clipboard().setText(QQ_GROUP)
        btn.setText("已复制：" + QQ_GROUP)


class FenDialog(QDialog):
    def __init__(self, title: str, text: str = "", readonly: bool = False,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 220)
        self.edit = QPlainTextEdit()
        self.edit.setPlainText(text)
        self.edit.setReadOnly(readonly)
        lay = QVBoxLayout(self)
        tip = QLabel("AB JChess / UCI 揭棋 FEN：明子用 R N B A K C P，"
                     "暗子 X/x，第 3 段为暗子身份池（无暗子填 -）。")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        lay.addWidget(self.edit)
        if readonly:
            btn_copy = QPushButton("复制到剪贴板")
            btn_copy.clicked.connect(self._copy)
            box = QDialogButtonBox(QDialogButtonBox.Close)
            box.rejected.connect(self.reject)
            box.accepted.connect(self.accept)
            box.addButton(btn_copy, QDialogButtonBox.ActionRole)
            lay.addWidget(box)
        else:
            box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            box.accepted.connect(self.accept)
            box.rejected.connect(self.reject)
            lay.addWidget(box)

    def _copy(self):
        QApplication.clipboard().setText(self.text())

    def text(self) -> str:
        return self.edit.toPlainText().strip()


class EngineDialog(QDialog):
    """引擎管理：加载 / 移除 UCI 引擎（鲨鱼、鹏飞式引擎列表）。"""

    engine_loaded = Signal(object)

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("引擎管理 · 飞鲨象棋")
        self.resize(700, 410)
        self.manager = manager
        self.list = QListWidget()
        self.status = QLabel("")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "已登记引擎（自动发现本机皮卡鱼 / 皮卡揭，内置 AB JChess 揭棋引擎）："))
        lay.addWidget(self.list)
        row = QHBoxLayout()
        btn_add = QPushButton("添加引擎…")
        btn_scan = QPushButton("扫描本机引擎")
        btn_remove = QPushButton("移除")
        btn_load = QPushButton("加载 / 重新加载")
        btn_close = QPushButton("关闭")
        for b in (btn_add, btn_scan, btn_remove, btn_load, btn_close):
            row.addWidget(b)
        lay.addLayout(row)
        lay.addWidget(self.status)
        note = QLabel(
            "NNUE：请把 .nnue 与引擎 exe 放同一目录（EvalFile 默认是相对文件名，"
            "按进程工作目录解析）；程序启动引擎时会自动定位，并在引擎页显示加载结果。")
        note.setWordWrap(True)
        lay.addWidget(note)
        btn_add.clicked.connect(self._add)
        btn_scan.clicked.connect(self._scan)
        btn_remove.clicked.connect(self._remove)
        btn_load.clicked.connect(self._load)
        btn_close.clicked.connect(self.reject)
        self._reload()

    def _reload(self):
        self.list.clear()
        for i, info in enumerate(self.manager.engines):
            tag = "揭棋" if info.jieqi_capable else "普通"
            item = QListWidgetItem(
                f"{i + 1}. [{tag}] {info.display}  —  {info.path}")
            self.list.addItem(item)
        if self.manager.engines:
            self.list.setCurrentRow(0)

    def _scan(self):
        n = self.manager.scan()
        self._reload()
        self.status.setText(f"扫描完成，新增 {n} 个引擎")

    def _add(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择引擎可执行文件", "",
            "可执行文件 (*.exe);;所有文件 (*)")
        if path:
            self.manager.add(path)
            self._reload()
            self.status.setText("已添加：" + os.path.basename(path))

    def _remove(self):
        row = self.list.currentRow()
        if row >= 0:
            self.manager.remove(row)
            self._reload()

    def _load(self):
        row = self.list.currentRow()
        if row < 0:
            QMessageBox.information(self, "引擎管理", "请先选择一个引擎")
            return
        try:
            eng = self.manager.load(row)
            self.engine_loaded.emit(eng)
            self.status.setText("已加载：" + self.manager.engines[row].display)
        except Exception as exc:
            QMessageBox.warning(self, "引擎加载失败", str(exc))
