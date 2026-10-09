"""PDF を開く前の「空の状態」。ブランドの見出しと、中央のドロップ領域から PDF を選ぶ。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import icons, theme
from .app_icon import app_pixmap
from .widgets import add_soft_shadow, button, label, link_button

FEATURES = ("図表をそのまま", "数式を保護", "原文と並べて確認", "訳文を PDF に書き出し")


class DropCard(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.active = False
        self.setFixedSize(560, 250)

    def paintEvent(self, _event) -> None:
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        fill = QColor(t.surface)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(r, theme.RADIUS_DROP, theme.RADIUS_DROP)
        if self.active:
            glow = QColor(theme.BRAND_END)
            glow.setAlpha(26)
            p.setBrush(glow)
            p.drawRoundedRect(r, theme.RADIUS_DROP, theme.RADIUS_DROP)
        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        start, end = QColor(theme.BRAND_START), QColor(theme.BRAND_END)
        if not self.active:
            start.setAlpha(110)
            end.setAlpha(110)
        grad.setColorAt(0, start)
        grad.setColorAt(1, end)
        pen = QPen(grad, 2.0 if self.active else 1.6, Qt.PenStyle.DashLine)
        pen.setDashPattern([6, 5])
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, theme.RADIUS_DROP, theme.RADIUS_DROP)
        p.end()


class DropZone(QWidget):
    openRequested = Signal()
    settingsRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.addStretch(3)

        # ブランド
        logo = QLabel()
        logo.setPixmap(app_pixmap(84))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(logo)
        outer.addSpacing(10)
        wordmark = label("Spectra", "hero")
        wordmark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(wordmark)
        tagline = label("論文を、そのままのレイアウトで日本語に。", "secondary")
        tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(tagline)
        outer.addSpacing(26)

        # ドロップ領域
        self.card = DropCard()
        add_soft_shadow(self.card, blur=40, alpha=26, dy=10)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(32, 30, 32, 28)
        lay.setSpacing(8)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.icon)
        title = label("PDF をここにドロップ", "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)
        hint = label("または", "caption")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch(1)
        self.open_button = button("PDF を選択", primary=True, large=True)
        self.open_button.clicked.connect(self.openRequested)
        row.addWidget(self.open_button)
        row.addStretch(1)
        lay.addLayout(row)
        h = QHBoxLayout()
        h.addStretch(1)
        h.addWidget(self.card)
        h.addStretch(1)
        outer.addLayout(h)
        outer.addSpacing(22)

        # 特長
        chips = QHBoxLayout()
        chips.setSpacing(8)
        chips.addStretch(1)
        for text in FEATURES:
            chips.addWidget(label(text, "chip"))
        chips.addStretch(1)
        outer.addLayout(chips)
        outer.addSpacing(18)

        # API キー未設定時の案内
        notice = QHBoxLayout()
        notice.addStretch(1)
        self.notice_icon = QLabel()
        self.notice = label("", "secondary")
        self.notice_link = link_button("設定を開く")
        self.notice_link.clicked.connect(self.settingsRequested)
        notice.addWidget(self.notice_icon)
        notice.addWidget(self.notice)
        notice.addWidget(self.notice_link)
        notice.addStretch(1)
        outer.addLayout(notice)
        outer.addStretch(4)
        shortcut = label("⌘O で開く" if theme.IS_MAC else "Ctrl+O で開く", "caption")
        shortcut.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(shortcut)
        outer.addSpacing(14)
        self.refresh_theme()

    def refresh_theme(self) -> None:
        t = theme.current()
        self.icon.setPixmap(icons.pixmap("drop", theme.BRAND_END if self.card.active else t.secondary, 34))
        self.card.update()

    def set_drag_active(self, active: bool) -> None:
        self.card.active = active
        self.refresh_theme()

    def set_engine_notice(self, text: str, warning: bool) -> None:
        t = theme.current()
        self.notice.setText(text)
        self.notice_icon.setPixmap(icons.pixmap("warning" if warning else "check", t.warning if warning else t.success, 16))
        self.notice_link.setVisible(warning)
