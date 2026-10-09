"""HIG を参考にした共通部品：スイッチ・セグメントコントロール・グループ化フォーム・バナー。"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import icons, theme


def label(text: str = "", role: str | None = None, tone: str | None = None, wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if role:
        lb.setProperty("role", role)
    if tone:
        lb.setProperty("tone", tone)
    lb.setWordWrap(wrap)
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    return lb


def set_prop(widget: QWidget, name: str, value) -> None:
    """動的プロパティを変更し、スタイルシートを再適用する。"""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def button(text: str, primary: bool = False, large: bool = False, destructive: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        b.setProperty("primary", True)
        b.setDefault(True)
    if large:
        b.setProperty("large", True)
    if destructive:
        b.setProperty("destructive", True)
    return b


def link_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("link", True)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFlat(True)
    return b


def tool_button(icon_name: str, tooltip: str, checkable: bool = False) -> QToolButton:
    b = QToolButton()
    b.setProperty("iconName", icon_name)
    b.setToolTip(tooltip)
    b.setAccessibleName(tooltip)
    b.setCheckable(checkable)
    b.setIconSize(QSize(18, 18))
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    refresh_icon(b)
    return b


def refresh_icon(b: QAbstractButton, color: str | None = None) -> None:
    name = b.property("iconName")
    if name:
        t = theme.current()
        b.setIcon(icons.icon(name, color or t.label, 18, t.tertiary))


class Switch(QAbstractButton):
    """macOS 風のトグルスイッチ。スペースキーでも切り替えられる。"""

    def __init__(self, checked: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pos = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:
        return QSize(40, 24)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _get_knob(self) -> float:
        return self._pos

    def _set_knob(self, v: float) -> None:
        self._pos = v
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def paintEvent(self, _event) -> None:
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(2, 3, 36, 20)
        off = QColor(t.control_pressed if not t.dark else "#5A5A5E")
        on = QColor(t.accent)
        mix = QColor(
            int(off.red() + (on.red() - off.red()) * self._pos),
            int(off.green() + (on.green() - off.green()) * self._pos),
            int(off.blue() + (on.blue() - off.blue()) * self._pos),
        )
        if not self.isEnabled():
            mix.setAlphaF(0.45)
        if self.hasFocus():
            p.setPen(QPen(QColor(t.focus), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track.adjusted(-2, -2, 2, 2), 12, 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mix)
        p.drawRoundedRect(track, 10, 10)
        x = track.left() + 2 + (track.width() - 20) * self._pos
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawEllipse(QPointF(x + 8, track.top() + 10.6), 8, 8)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QPointF(x + 8, track.top() + 10), 8, 8)
        p.end()

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)


class SegmentedControl(QFrame):
    """単一選択のセグメントコントロール。"""

    changed = Signal(str)

    def __init__(self, items: Sequence[tuple[str, str]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Segmented")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for key, text in items:
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            b.setProperty("key", key)
            self._group.addButton(b)
            lay.addWidget(b)
            self._buttons[key] = b
        self._group.buttonToggled.connect(self._on_toggled)

    def _on_toggled(self, b: QAbstractButton, checked: bool) -> None:
        if checked:
            self.changed.emit(b.property("key"))

    def set_current(self, key: str) -> None:
        if key in self._buttons:
            self._buttons[key].setChecked(True)

    def current(self) -> str:
        b = self._group.checkedButton()
        return b.property("key") if b else ""

    def set_enabled(self, key: str, enabled: bool) -> None:
        self._buttons[key].setEnabled(enabled)


class FormGroup(QWidget):
    """見出し付きのグループ化フォーム（macOS のシステム設定風）。行の間に区切り線が入る。"""

    def __init__(self, title: str = "", footer: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        if title:
            outer.addWidget(label(title, "section"))
        self.card = QFrame()
        self.card.setProperty("card", True)
        self._rows = QVBoxLayout(self.card)
        self._rows.setContentsMargins(12, 4, 12, 4)
        self._rows.setSpacing(0)
        outer.addWidget(self.card)
        self.footer = label(footer, "caption", wrap=True)
        self.footer.setContentsMargins(4, 0, 4, 0)
        self.footer.setVisible(bool(footer))
        outer.addWidget(self.footer)

    def _separator(self) -> None:
        if self._rows.count():
            line = QFrame()
            line.setProperty("rowSeparator", True)
            self._rows.addWidget(line)

    def add_row(self, title: str, control: QWidget | None = None, subtitle: str = "") -> QWidget:
        self._separator()
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(1)
        t = label(title)
        text.addWidget(t)
        if subtitle:
            text.addWidget(label(subtitle, "caption", wrap=True))
        lay.addLayout(text, 1)
        if control is not None:
            lay.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
            if not control.accessibleName():
                control.setAccessibleName(title)
        self._rows.addWidget(row)
        return row

    def add_widget(self, widget: QWidget) -> None:
        self._separator()
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.addWidget(widget)
        self._rows.addWidget(wrap)

    def set_footer(self, text: str, tone: str | None = None) -> None:
        self.footer.setText(text)
        set_prop(self.footer, "tone", tone)
        self.footer.setVisible(bool(text))


class Banner(QFrame):
    """ウィンドウ上部に出す控えめな通知。"""

    action = Signal()
    closed = Signal()

    _ICONS = {"info": "info", "success": "check", "warning": "warning", "danger": "error"}

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("banner", "info")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 8, 10)
        lay.setSpacing(10)
        self._icon = QLabel()
        self._icon.setFixedSize(20, 20)
        lay.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self._title = label("", "headline", wrap=True)
        self._body = label("", "secondary", wrap=True)
        texts.addWidget(self._title)
        texts.addWidget(self._body)
        lay.addLayout(texts, 1)
        self._action = button("")
        self._action.clicked.connect(self.action)
        lay.addWidget(self._action, 0, Qt.AlignmentFlag.AlignVCenter)
        self._close = tool_button("close", "閉じる")
        self._close.clicked.connect(self._on_close)
        lay.addWidget(self._close, 0, Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.hide()

    def show_message(self, kind: str, title: str, body: str = "", action: str = "") -> None:
        t = theme.current()
        color = {"info": t.accent, "success": t.success, "warning": t.warning, "danger": t.danger}[kind]
        self._icon.setPixmap(icons.pixmap(self._ICONS[kind], color, 20))
        self._title.setText(title)
        self._body.setText(body)
        self._body.setVisible(bool(body))
        self._action.setText(action)
        self._action.setVisible(bool(action))
        set_prop(self, "banner", "info" if kind in ("info", "success") else kind)
        self.show()

    def _on_close(self) -> None:
        self.hide()
        self.closed.emit()


def hline() -> QFrame:
    line = QFrame()
    line.setProperty("rowSeparator", True)
    return line
