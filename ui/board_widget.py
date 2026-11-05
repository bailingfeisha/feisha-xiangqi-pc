# -*- coding: utf-8 -*-
"""棋盘控件：纯 QPainter 自绘，无外部图片资源。

包含：木纹棋盘 / 楚河汉界 / 九宫 / 炮兵位标记 / 明暗棋子 / 选中高亮 /
可走点 / 上一步 / 将军提示 / 引擎建议箭头 / 翻转视角 / 坐标 / 被吃子展示。
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QPointF, QRectF, QLineF, Signal
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QFontDatabase,
    QRadialGradient, QPainterPath, QPolygonF,
)
from PySide6.QtWidgets import QWidget

from core.constants import Side, CN_DIGITS, piece_name

THEMES = {
    "wood": {
        "bg": "#EFE4CE", "board": "#F7DEB2", "frame": "#5B3B1E",
        "line": "#7C5531", "river": "#6B4A28", "text": "#4B331B",
        "piece_lo": "#FFF7E4", "piece_hi": "#E4C084", "edge": "#A9792F",
        "red": "#B02424", "black": "#1E2228",
        "dark_lo": "#17463B", "dark_hi": "#31705D", "dark_edge": "#D8B45C",
        "hint": "#2E8B57", "capture": "#C63C2F", "select": "#E9A81B",
        "last": "#2F6FC0", "hover": "#F0C86B", "check": "#D32F2F",
        "arrow": "#6A4BC3",
    },
    "dark": {
        "bg": "#22262D", "board": "#333A43", "frame": "#0F1216",
        "line": "#9AA6B4", "river": "#C9D3DF", "text": "#D7DEE7",
        "piece_lo": "#F0E4CB", "piece_hi": "#C7A46C", "edge": "#8A6A38",
        "red": "#E0534B", "black": "#20242A",
        "dark_lo": "#123A33", "dark_hi": "#2B6A58", "dark_edge": "#C9A24E",
        "hint": "#4BC08A", "capture": "#E26053", "select": "#F2C64B",
        "last": "#63A4F0", "hover": "#6B7A8C", "check": "#F06A5F",
        "arrow": "#A78BFA",
    },
    "bamboo": {
        "bg": "#E7EEDC", "board": "#F2F5D9", "frame": "#46583A",
        "line": "#6C8055", "river": "#4C6238", "text": "#3B4A2C",
        "piece_lo": "#FFFBEA", "piece_hi": "#DDD8A8", "edge": "#8A8A45",
        "red": "#A82B22", "black": "#243024",
        "dark_lo": "#1D4637", "dark_hi": "#3A7358", "dark_edge": "#CFB65E",
        "hint": "#3B8F5A", "capture": "#C0442F", "select": "#DDA51C",
        "last": "#3B7BB5", "hover": "#D6DE9A", "check": "#CC3333",
        "arrow": "#6B5BB5",
    },
}

MARK_POS = ([(1, 2), (7, 2), (1, 7), (7, 7)]
            + [(c, 3) for c in (0, 2, 4, 6, 8)]
            + [(c, 6) for c in (0, 2, 4, 6, 8)])


def theme(name: str) -> dict:
    return THEMES.get(name, THEMES["wood"])


def pick_font(candidates, fallback="Microsoft YaHei") -> str:
    try:
        fams = set(QFontDatabase.families())
        for f in candidates:
            if f in fams:
                return f
    except Exception:
        pass
    return fallback


PIECE_FONT = None


def piece_font() -> str:
    global PIECE_FONT
    if PIECE_FONT is None:
        PIECE_FONT = pick_font(["KaiTi", "STKaiti", "Kaiti SC", "SimSun",
                                "Microsoft YaHei"])
    return PIECE_FONT


def draw_piece(p, x, y, radius, piece, th, traditional=False, selected=False):
    """绘制一枚棋子（明子写汉字，暗子画花纹，不显示身份）。"""
    # 阴影
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 55))
    p.drawEllipse(QPointF(x + radius * 0.10, y + radius * 0.14),
                  radius * 0.98, radius * 0.98)
    if piece.dark:
        grad = QRadialGradient(x - radius * 0.35, y - radius * 0.40,
                               radius * 0.12, x, y, radius * 1.30)
        grad.setColorAt(0, QColor(th["dark_hi"]))
        grad.setColorAt(1, QColor(th["dark_lo"]))
        edge = QColor(th["dark_edge"])
    else:
        grad = QRadialGradient(x - radius * 0.35, y - radius * 0.40,
                               radius * 0.12, x, y, radius * 1.30)
        grad.setColorAt(0, QColor(th["piece_lo"]))
        grad.setColorAt(1, QColor(th["piece_hi"]))
        edge = QColor(th["edge"])
    p.setBrush(QBrush(grad))
    p.setPen(QPen(edge, max(1.2, radius * 0.09)))
    p.drawEllipse(QPointF(x, y), radius, radius)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(edge, max(0.8, radius * 0.035)))
    p.drawEllipse(QPointF(x, y), radius * 0.82, radius * 0.82)

    if piece.dark:
        # 暗子花纹：菱形 + 中心点，不渲染任何字
        p.setPen(QPen(QColor(th["dark_edge"]), max(1.0, radius * 0.05)))
        d = radius * 0.42
        path = QPainterPath()
        path.moveTo(x, y - d)
        path.lineTo(x + d, y)
        path.lineTo(x, y + d)
        path.lineTo(x - d, y)
        path.closeSubpath()
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(th["dark_edge"]))
        p.drawEllipse(QPointF(x, y), radius * 0.10, radius * 0.10)
    else:
        font = QFont(piece_font(), max(8, int(radius * 1.02)))
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(th["red"] if piece.side == Side.RED else th["black"]))
        p.drawText(QRectF(x - radius, y - radius, 2 * radius, 2 * radius),
                   Qt.AlignCenter,
                   piece_name(piece.side, piece.ptype, traditional))
    if selected:
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(th["select"]), max(2.0, radius * 0.14)))
        p.drawEllipse(QPointF(x, y), radius * 1.15, radius * 1.15)


class BoardWidget(QWidget):
    square_clicked = Signal(object)     # (col, row)
    cancel_requested = Signal()
    zoom_requested = Signal(int)        # +1 / -1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(380, 430)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.board = None
        self.flipped = False
        self.selected = None
        self.targets = []
        self.last_move = None
        self.check_pos = None
        self.hint_move = None
        self.hover = None
        self.interactive = True
        self.theme_name = "wood"
        self.traditional = False
        self.show_coords = True
        self.show_hints = True
        self.show_last_move = True
        self.zoom = 1.0

    # ---------- 对外接口 ----------
    def set_board(self, board):
        self.board = board
        self.update()

    def set_selection(self, pos, targets):
        self.selected = pos
        self.targets = list(targets)
        self.update()

    def clear_selection(self):
        self.selected = None
        self.targets = []
        self.update()

    def set_last_move(self, move):
        self.last_move = move
        self.update()

    def set_check_pos(self, pos):
        self.check_pos = pos
        self.update()

    def set_hint(self, move):
        self.hint_move = move
        self.update()

    def set_flipped(self, value: bool):
        self.flipped = bool(value)
        self.update()

    def apply_display(self, options):
        self.theme_name = options.theme
        self.traditional = options.piece_style == "traditional"
        self.show_coords = options.show_coords
        self.show_hints = options.show_hints
        self.show_last_move = options.show_last_move
        self.zoom = options.board_zoom
        self.update()

    # ---------- 几何 ----------
    def _geo(self):
        w, h = self.width(), self.height()
        base = min((w - 60) / 8.0, (h - 60) / 9.0)
        cell = max(12.0, base * self.zoom)
        ox = (w - 8 * cell) / 2.0
        oy = (h - 9 * cell) / 2.0
        return ox, oy, cell

    def _to_screen(self, col, row) -> QPointF:
        ox, oy, cell = self._geo()
        if self.flipped:
            return QPointF(ox + (8 - col) * cell, oy + (9 - row) * cell)
        return QPointF(ox + col * cell, oy + row * cell)

    def _from_screen(self, x, y):
        ox, oy, cell = self._geo()
        col = int(round((x - ox) / cell))
        row = int(round((y - oy) / cell))
        if self.flipped:
            col, row = 8 - col, 9 - row
        if not (0 <= col < 9 and 0 <= row < 10):
            return None
        p = self._to_screen(col, row)
        if (x - p.x()) ** 2 + (y - p.y()) ** 2 > (0.48 * cell) ** 2:
            return None
        return col, row

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        th = theme(self.theme_name)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(th["bg"]))
        ox, oy, cell = self._geo()
        pad = cell * 0.55
        radius = cell * 0.42

        # 棋盘面板 + 双线外框
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(th["board"]))
        p.drawRoundedRect(QRectF(ox - pad, oy - pad,
                                 8 * cell + 2 * pad, 9 * cell + 2 * pad), 8, 8)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(th["frame"]), max(1.5, cell * 0.025)))
        p.drawRect(QRectF(ox - pad, oy - pad,
                          8 * cell + 2 * pad, 9 * cell + 2 * pad))
        p.setPen(QPen(QColor(th["frame"]), max(0.8, cell * 0.012)))
        p.drawRect(QRectF(ox - pad * 0.55, oy - pad * 0.55,
                          8 * cell + 1.1 * pad, 9 * cell + 1.1 * pad))

        # 网格
        p.setPen(QPen(QColor(th["line"]), max(1.0, cell * 0.018)))
        for r in range(10):
            p.drawLine(QLineF(self._to_screen(0, r), self._to_screen(8, r)))
        for c in range(9):
            if c in (0, 8):
                p.drawLine(QLineF(self._to_screen(c, 0), self._to_screen(c, 9)))
            else:
                p.drawLine(QLineF(self._to_screen(c, 0), self._to_screen(c, 4)))
                p.drawLine(QLineF(self._to_screen(c, 5), self._to_screen(c, 9)))

        # 九宫斜线
        for c1, r1, c2, r2 in ((3, 0, 5, 2), (5, 0, 3, 2),
                               (3, 7, 5, 9), (5, 7, 3, 9)):
            p.drawLine(QLineF(self._to_screen(c1, r1), self._to_screen(c2, r2)))

        # 炮位 / 兵位角标
        self._draw_position_marks(p, cell, th)

        # 楚河汉界
        p.save()
        if self.flipped:
            p.translate(w, h)
            p.rotate(180)
        f = QFont(piece_font(), max(10, int(cell * 0.40)))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(th["river"]))
        p.drawText(QRectF(ox, oy + 4.02 * cell, 8 * cell, 0.96 * cell),
                   Qt.AlignCenter, "楚 河              汉 界")
        p.restore()

        # 坐标
        if self.show_coords:
            f = QFont(piece_font(), max(8, int(cell * 0.24)))
            p.setFont(f)
            p.setPen(QColor(th["text"]))
            for sc in range(9):
                x = ox + sc * cell
                top = CN_DIGITS[sc] if self.flipped else str(sc + 1)
                bottom = str(9 - sc) if self.flipped else CN_DIGITS[8 - sc]
                p.drawText(QRectF(x - 0.4 * cell, oy - 0.52 * cell,
                                  0.8 * cell, 0.44 * cell),
                           Qt.AlignCenter, top)
                p.drawText(QRectF(x - 0.4 * cell, oy + 9 * cell + 0.08 * cell,
                                  0.8 * cell, 0.44 * cell),
                           Qt.AlignCenter, bottom)

        # 悬停格
        if self.hover and self.interactive:
            hp = self._to_screen(*self.hover)
            col = QColor(th["hover"])
            col.setAlpha(70)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(hp.x() - cell * 0.45, hp.y() - cell * 0.45,
                                     0.9 * cell, 0.9 * cell), 6, 6)

        # 上一步起止标记
        if self.show_last_move and self.last_move:
            col = QColor(th["last"])
            for pos in (self.last_move.src, self.last_move.dst):
                self._corner_marks(p, self._to_screen(*pos), cell * 0.40,
                                   col, max(1.5, cell * 0.035))

        # 将军提示
        if self.check_pos:
            cp = self._to_screen(*self.check_pos)
            cc = QColor(th["check"])
            p.setBrush(QColor(cc.red(), cc.green(), cc.blue(), 45))
            p.setPen(QPen(cc, max(2.0, cell * 0.05)))
            p.drawEllipse(cp, cell * 0.46, cell * 0.46)

        # 可走点提示
        if self.show_hints and self.selected is not None:
            for pos in self.targets:
                tp = self._to_screen(*pos)
                occupant = self.board.get(*pos) if self.board else None
                if occupant is None:
                    hc = QColor(th["hint"])
                    hc.setAlpha(180)
                    p.setPen(Qt.NoPen)
                    p.setBrush(hc)
                    p.drawEllipse(tp, cell * 0.14, cell * 0.14)
                else:
                    p.setBrush(Qt.NoBrush)
                    p.setPen(QPen(QColor(th["capture"]),
                                  max(2.0, cell * 0.055)))
                    p.drawEllipse(tp, cell * 0.47, cell * 0.47)

        # 棋子
        if self.board is not None:
            for c, r, piece in self.board.pieces():
                sp = self._to_screen(c, r)
                draw_piece(p, sp.x(), sp.y(), radius, piece, th,
                           self.traditional,
                           selected=(self.selected == (c, r)))

        # 引擎建议箭头（画在棋子之上，半透明）
        if self.hint_move:
            a = self._to_screen(*self.hint_move.src)
            b = self._to_screen(*self.hint_move.dst)
            ac = QColor(th["arrow"])
            ac.setAlpha(200)
            p.setPen(QPen(ac, max(2.5, cell * 0.075)))
            p.drawLine(QLineF(a, b))
            ang = math.atan2(b.y() - a.y(), b.x() - a.x())
            L = cell * 0.26
            p1 = QPointF(b.x() - L * math.cos(ang - 0.42),
                         b.y() - L * math.sin(ang - 0.42))
            p2 = QPointF(b.x() - L * math.cos(ang + 0.42),
                         b.y() - L * math.sin(ang + 0.42))
            p.setPen(Qt.NoPen)
            p.setBrush(ac)
            p.drawPolygon(QPolygonF([b, p1, p2]))
        p.end()

    def _draw_position_marks(self, p, cell, th):
        p.setPen(QPen(QColor(th["line"]), max(1.0, cell * 0.015)))
        gap, length = cell * 0.09, cell * 0.10
        for c, r in MARK_POS:
            sp = self._to_screen(c, r)
            for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                if (c == 0 and sx < 0) or (c == 8 and sx > 0):
                    continue
                x = sp.x() + sx * gap
                y = sp.y() + sy * gap
                p.drawLine(QLineF(x, y, x + sx * length, y))
                p.drawLine(QLineF(x, y, x, y + sy * length))

    def _corner_marks(self, p, center, size, color, width):
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(color, width))
        x, y, d = center.x(), center.y(), size
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            cx, cy = x + sx * d, y + sy * d
            p.drawLine(QLineF(cx, cy, cx - sx * d * 0.45, cy))
            p.drawLine(QLineF(cx, cy, cx, cy - sy * d * 0.45))

    # ---------- 交互 ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self.cancel_requested.emit()
            return
        if e.button() != Qt.LeftButton or not self.interactive:
            return
        pos = self._from_screen(e.position().x(), e.position().y())
        if pos is not None:
            self.square_clicked.emit(pos)

    def mouseMoveEvent(self, e):
        pos = self._from_screen(e.position().x(), e.position().y())
        if pos != self.hover:
            self.hover = pos
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.zoom_requested.emit(1 if e.angleDelta().y() > 0 else -1)
        else:
            super().wheelEvent(e)


class CapturedPiecesWidget(QWidget):
    """被吃棋子展示区（两行小圆片，暗子带金点标记）。"""

    def __init__(self, side, parent=None):
        super().__init__(parent)
        self.side = Side(side)
        self.pieces = []
        self.theme_name = "wood"
        self.traditional = False
        self.setMinimumHeight(56)
        self.setMaximumHeight(64)

    def set_pieces(self, pieces):
        self.pieces = list(pieces)
        self.update()

    def apply_display(self, options):
        self.theme_name = options.theme
        self.traditional = options.piece_style == "traditional"
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        th = theme(self.theme_name)
        r, gap, row_h = 11.0, 25.0, 26.0
        cols = max(1, (self.width() - 16) // int(gap))
        for i, piece in enumerate(self.pieces):
            x = 14 + (i % cols) * gap
            y = 16 + (i // cols) * row_h
            if y + r > self.height():
                break
            draw_piece(p, x, y, r, piece, th, self.traditional)
            if piece.dark:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(th["dark_edge"]))
                p.drawEllipse(QPointF(x + r * 0.78, y - r * 0.78), 2.6, 2.6)
        p.end()
