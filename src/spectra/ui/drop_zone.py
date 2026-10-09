"""PDF を開く前の「空の状態」。中央のドロップ領域から PDF を選ぶ。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import icons, theme
from .widgets import button, label, link_button


class DropCard(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.active = False
        self.setFixedSize(540, 320)

    def paintEvent(self, _event) -> None:
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        fill = QColor(t.accent if self.active else t.surface)
        fill.setAlpha(28 if self.active else 255)
        p.setBrush(fill)
        pen = QPen(QColor(t.accent if self.active else t.border), 1.5, Qt.PenStyle.DashLine)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        p.drawRoundedRect(r, theme.RADIUS_DROP, theme.RADIUS_DROP)
        p.end()


class DropZone(QWidget):
    openRequested = Signal()
    settingsRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        self.card = DropCard()
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(32, 36, 32, 28)
        lay.setSpacing(10)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.icon)
        lay.addSpacing(4)
        title = label("論文の PDF をドロップ", "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)
        body = label("図表・数式・レイアウトはそのままに、本文だけを日本語に翻訳します。", "secondary", wrap=True)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(body)
        lay.addSpacing(10)
        row = QHBoxLayout()
        row.addStretch(1)
        self.open_button = button("PDF を選択…", primary=True, large=True)
        self.open_button.clicked.connect(self.openRequested)
        row.addWidget(self.open_button)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)

        h = QHBoxLayout()
        h.addStretch(1)
        h.addWidget(self.card)
        h.addStretch(1)
        outer.addLayout(h)

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
        outer.addSpacing(16)
        outer.addLayout(notice)
        outer.addStretch(1)
        shortcut = label("⌘O で開く" if theme.IS_MAC else "Ctrl+O で開く", "caption")
        shortcut.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(shortcut)
        outer.addSpacing(16)
        self.refresh_theme()

    def refresh_theme(self) -> None:
        t = theme.current()
        self.icon.setPixmap(icons.pixmap("drop", t.accent if self.card.active else t.secondary, 44))
        self.card.update()

    def set_drag_active(self, active: bool) -> None:
        self.card.active = active
        self.refresh_theme()

    def set_engine_notice(self, text: str, warning: bool) -> None:
        t = theme.current()
        self.notice.setText(text)
        self.notice_icon.setPixmap(icons.pixmap("warning" if warning else "check", t.warning if warning else t.success, 16))
        self.notice_link.setVisible(warning)
