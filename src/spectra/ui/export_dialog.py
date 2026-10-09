"""PDF の書き出し：形式をカードで選んでから保存先を決める。

翻訳結果から作り直すだけなので、形式を変えて何度でも書き出せる（再翻訳は不要）。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from ..core.options import OutputMode
from . import theme
from .widgets import button, label

FORMATS = [
    (OutputMode.TRANSLATED, "翻訳のみ", "元と同じページ構成の日本語版", "_ja"),
    (OutputMode.SIDE_BY_SIDE, "左右に対訳", "原文と訳文を 1 枚に並べる", "_ja_side"),
    (OutputMode.INTERLEAVE, "ページごとに対訳", "原文と訳文のページを交互に", "_ja_alt"),
]


def suffix_for(mode: OutputMode) -> str:
    return next(s for m, _t, _d, s in FORMATS if m == mode)


def output_pages(mode: OutputMode, pages: int) -> int:
    return pages * 2 if mode == OutputMode.INTERLEAVE else pages


class FormatCard(QAbstractButton):
    """形式を表す図と名前の付いた選択カード。"""

    def __init__(self, mode: OutputMode, title: str, desc: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.mode = mode
        self.title = title
        self.desc = desc
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName(f"{title}：{desc}")
        self.setFixedSize(QSize(196, 206))
        self._hover = False

    def enterEvent(self, e) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, e) -> None:
        self._hover = False
        self.update()

    def paintEvent(self, _event) -> None:
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.surface))
        p.drawRoundedRect(r, 14, 14)
        if self.isChecked():
            grad = QLinearGradient(r.topLeft(), r.bottomRight())
            grad.setColorAt(0, QColor(theme.BRAND_START))
            grad.setColorAt(1, QColor(theme.BRAND_END))
            p.setPen(QPen(grad, 2.5))
        else:
            p.setPen(QPen(QColor(t.border if self._hover else t.separator), 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 14, 14)

        # 図：ページの並び
        art = QRectF(r.left() + 16, r.top() + 18, r.width() - 32, 104)
        bg = QColor(t.fill)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(art, 10, 10)
        self._draw_pages(p, art, t)

        p.setPen(QColor(t.label))
        f = self.font()
        f.setPixelSize(theme.TYPE["headline"] + 1)
        f.setWeight(f.Weight.DemiBold)
        p.setFont(f)
        p.drawText(QRectF(r.left() + 16, art.bottom() + 14, r.width() - 32, 22), Qt.AlignmentFlag.AlignLeft, self.title)
        f.setPixelSize(theme.TYPE["caption"])
        f.setWeight(f.Weight.Normal)
        p.setFont(f)
        p.setPen(QColor(t.secondary))
        p.drawText(
            QRectF(r.left() + 16, art.bottom() + 38, r.width() - 32, 40),
            Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
            self.desc,
        )
        if self.hasFocus():
            p.setPen(QPen(QColor(t.focus), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 15, 15)
        p.end()

    def _draw_pages(self, p: QPainter, art: QRectF, t) -> None:
        en = QColor(t.tertiary)
        ja = QColor(theme.BRAND_END)

        def page(x: float, y: float, w: float, h: float, color: QColor) -> None:
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, y, w, h), 3, 3)
            p.fillPath(path, QColor("#FFFFFF"))
            p.setPen(QPen(QColor(0, 0, 0, 30), 0.8))
            p.drawPath(path)
            pen = QPen(color, 2.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            for i in range(5):
                ly = y + 10 + i * (h - 18) / 4.5
                p.drawLine(int(x + 6), int(ly), int(x + w - (6 if i % 3 else 12)), int(ly))

        cy = art.center().y()
        h = 78
        w = 56
        if self.mode == OutputMode.TRANSLATED:
            page(art.center().x() - w / 2, cy - h / 2, w, h, ja)
        elif self.mode == OutputMode.SIDE_BY_SIDE:
            page(art.center().x() - w - 1, cy - h / 2, w, h, en)
            page(art.center().x() + 1, cy - h / 2, w, h, ja)
        else:
            page(art.center().x() - w - 4, cy - h / 2 - 6, w - 8, h - 8, en)
            page(art.center().x() - 10, cy - h / 2 + 6, w - 8, h - 8, ja)
            p.setPen(QColor(t.secondary))
            f = self.font()
            f.setPixelSize(11)
            p.setFont(f)
            p.drawText(QRectF(art.right() - 40, art.bottom() - 22, 32, 16), Qt.AlignmentFlag.AlignRight, "×2")


class ExportDialog(QDialog):
    exportRequested = Signal(object)  # OutputMode

    def __init__(self, current: OutputMode, pages: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("PDF を書き出す")
        self.pages = pages
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 26, 28, 22)
        lay.setSpacing(6)
        lay.addWidget(label("PDF を書き出す", "title"))
        lay.addWidget(label("形式を選んでください。翻訳し直さずに、何度でも別の形式で書き出せます。", "secondary"))
        lay.addSpacing(16)
        row = QHBoxLayout()
        row.setSpacing(14)
        self.group = QButtonGroup(self)
        self.cards: dict[OutputMode, FormatCard] = {}
        for mode, title, desc, _s in FORMATS:
            card = FormatCard(mode, title, desc)
            self.group.addButton(card)
            row.addWidget(card)
            self.cards[mode] = card
            card.toggled.connect(lambda _on: self._refresh())
            card.clicked.connect(self._refresh)
        lay.addLayout(row)
        lay.addSpacing(12)
        self.summary = label("", "caption")
        lay.addWidget(self.summary)
        lay.addSpacing(10)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = button("キャンセル")
        cancel.clicked.connect(self.reject)
        self.ok = button("書き出す…", primary=True)
        self.ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.ok)
        lay.addLayout(buttons)
        self.cards.get(current, self.cards[OutputMode.TRANSLATED]).setChecked(True)
        self.ok.setFocus()
        self._refresh()

    def selected(self) -> OutputMode:
        b = self.group.checkedButton()
        return b.mode if isinstance(b, FormatCard) else OutputMode.TRANSLATED

    def _refresh(self) -> None:
        mode = self.selected()
        n = output_pages(mode, self.pages)
        extra = "（横長のページ）" if mode == OutputMode.SIDE_BY_SIDE else ""
        self.summary.setText(f"出力：{n} ページ{extra}")
        for c in self.cards.values():
            c.update()
