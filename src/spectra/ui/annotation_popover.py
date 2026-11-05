"""注釈の吹き出し（色・種類・コメントの編集と削除）と、削除の取り消しの帯。

どちらも ``library`` を渡すと、変更を Library に書き込む（``update_annotation`` / ``delete_annotation`` /
``restore_annotation``）。渡さなければシグナルで知らせるだけ。注釈は duck typing で扱う
（id・kind・color・comment・quote・comment_merged を持つ ``library.models.Annotation``）。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import icons, theme
from .widgets import button, label, plain_tooltip, tool_button

log = logging.getLogger(__name__)

try:  # データ層（別のブランチ）が入る前は、同じ値を仮に使う
    from ..library.models import ANNOTATION_COLORS, MAX_COMMENT
except ImportError:  # pragma: no cover
    ANNOTATION_COLORS = ("yellow", "green", "blue", "pink", "purple")
    MAX_COMMENT = 10_000

COLOR_LABELS = {"yellow": "黄", "green": "緑", "blue": "青", "pink": "桃", "purple": "紫"}
KIND_LABELS = {"highlight": "マーカー", "underline": "下線", "note": "付箋"}
_LIGHT = {"yellow": "#FFE066", "green": "#8FD694", "blue": "#8CC8FF", "pink": "#FFA8C5", "purple": "#C9A8FF"}
_DARK = {"yellow": "#B89B1E", "green": "#3F9B4A", "blue": "#3B84C9", "pink": "#C0587F", "purple": "#8A63C9"}

SAVE_DELAY_MS = 1000
UNDO_MS = 5000


def annotation_rgb(color: str, dark: bool | None = None) -> str:
    """注釈の色の名前を、画面に出す色（#RRGGBB）にする。明るい・暗いテーマで変わる。知らない名前は黄。"""
    table = _DARK if (theme.is_dark() if dark is None else dark) else _LIGHT
    return table.get(color, table["yellow"])


class ColorSwatch(QAbstractButton):
    """色の名前を持つ小さな丸いボタン。"""

    def __init__(self, color: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.color = color
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(22, 22)
        self.setToolTip(COLOR_LABELS.get(color, color))
        self.setAccessibleName(f"{COLOR_LABELS.get(color, color)}色")

    def paintEvent(self, _event) -> None:
        from PySide6.QtGui import QColor, QPainter, QPen

        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(annotation_rgb(self.color)))
        p.drawEllipse(self.rect().adjusted(3, 3, -3, -3))
        if self.isChecked():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(t.accent), 2))
            p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))


class AnnotationPopover(QFrame):
    """印をクリックしたときの小さな吹き出し。色・種類の切り替え、コメントの編集（入力をやめて 1 秒後に保存）、削除。"""

    changed = Signal(object)  # 更新後の注釈（library を渡したときは library が返したもの）
    deleted = Signal(object)  # 削除した注釈（「元に戻す」に使う）

    def __init__(self, annotation, library=None, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("AnnotationPopover")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(300)
        t = theme.current()
        self.setStyleSheet(
            f"#AnnotationPopover {{ background: {t.surface}; border: 1px solid {t.separator}; border-radius: {theme.RADIUS_CARD}px; }}"
        )
        self.library = library
        self.annotation = annotation
        self._dirty = False
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(4)
        self._color_group = QButtonGroup(self)
        self._color_group.setExclusive(True)
        self.swatches: dict[str, ColorSwatch] = {}
        for c in ANNOTATION_COLORS:
            sw = ColorSwatch(c)
            self._color_group.addButton(sw)
            self.swatches[c] = sw
            head.addWidget(sw)
            sw.clicked.connect(lambda _checked=False, c=c: self._set_color(c))
        head.addSpacing(8)
        self._kind_group = QButtonGroup(self)
        self._kind_group.setExclusive(True)
        self.kind_buttons: dict[str, QPushButton] = {}
        for k in ("highlight", "underline"):
            b = QPushButton(KIND_LABELS[k])
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self._kind_group.addButton(b)
            self.kind_buttons[k] = b
            head.addWidget(b)
            b.clicked.connect(lambda _checked=False, k=k: self._set_kind(k))
        head.addStretch(1)
        self.delete_btn = tool_button("trash", "この注釈を削除")
        self.delete_btn.clicked.connect(self.delete)
        head.addWidget(self.delete_btn)
        lay.addLayout(head)

        self.quote = label("", "caption", wrap=True)
        self.quote.setTextFormat(Qt.TextFormat.PlainText)  # 論文の本文を含むので、HTML として解釈しない
        self.quote.setMaximumHeight(self.quote.fontMetrics().lineSpacing() * 3 + 4)
        lay.addWidget(self.quote)

        self.merged = label("ほかのパソコンでの編集と連結しました。内容を確かめて整理してください。", "caption", tone="warning", wrap=True)
        lay.addWidget(self.merged)
        self.comment = QPlainTextEdit()
        self.comment.setPlaceholderText("コメント")
        self.comment.setAccessibleName("コメント")
        self.comment.setFixedHeight(96)
        self.comment.textChanged.connect(self._on_text_changed)
        lay.addWidget(self.comment)
        self.status = label("", "caption", wrap=True)
        lay.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY_MS)
        self._timer.timeout.connect(self.flush)
        self._show_annotation(annotation)

    # -- 表示 ------------------------------------------------------------

    def _show_annotation(self, a) -> None:
        self.annotation = a
        for c, sw in self.swatches.items():
            sw.setChecked(c == a.color)
        is_note = a.kind == "note"
        for k, b in self.kind_buttons.items():
            b.setChecked(k == a.kind)
            b.setVisible(not is_note)  # 付箋は、マーカー・下線とは切り替えない
        self.quote.setText(a.quote)
        self.quote.setVisible(bool(a.quote))
        self.merged.setVisible(bool(getattr(a, "comment_merged", False)))
        self._loading = True
        try:
            self.comment.setPlainText(a.comment)
        finally:
            self._loading = False
        self._dirty = False

    def popup_at(self, global_pos: QPoint) -> None:
        self.adjustSize()
        self.move(global_pos)
        self.show()
        self.comment.setFocus()

    # -- 変更 ------------------------------------------------------------

    def _update(self, **fields) -> None:
        if self.library is not None:
            try:
                updated = self.library.update_annotation(self.annotation.id, **fields)
            except Exception as e:  # noqa: BLE001 - 失敗しても入力は残す
                log.warning("could not update the annotation", exc_info=True)
                self.status.setText(f"保存できませんでした（{e}）")
                return
        else:
            from dataclasses import replace

            try:
                updated = replace(self.annotation, **fields)
            except TypeError:  # dataclass でない注釈（テストの偽物など）
                updated = self.annotation
                for k, v in fields.items():
                    setattr(updated, k, v)
        self.annotation = updated
        self.status.setText("")
        self.changed.emit(updated)

    def _set_color(self, color: str) -> None:
        if color != self.annotation.color:
            self._update(color=color)

    def _set_kind(self, kind: str) -> None:
        if kind != self.annotation.kind:
            self._update(kind=kind)

    def _on_text_changed(self) -> None:
        if self._loading:
            return
        text = self.comment.toPlainText()
        if len(text) > MAX_COMMENT:
            self._loading = True
            try:
                pos = min(self.comment.textCursor().position(), MAX_COMMENT)
                self.comment.setPlainText(text[:MAX_COMMENT])
                c = self.comment.textCursor()
                c.setPosition(pos)
                self.comment.setTextCursor(c)
            finally:
                self._loading = False
            self.status.setText(f"コメントは {MAX_COMMENT:,} 文字までです。超えた分は入りません。")
        else:
            self.status.setText("")
        self._dirty = True
        self._timer.start()

    def flush(self) -> None:
        """入力中のコメントを今すぐ保存する。"""
        self._timer.stop()
        if not self._dirty:
            return
        text = self.comment.toPlainText()
        self._dirty = False
        if text != self.annotation.comment or getattr(self.annotation, "comment_merged", False):
            self._update(comment=text)
            self.merged.setVisible(False)

    def delete(self) -> None:
        """注釈を削除する。削除した注釈を ``deleted`` で通知する（``UndoBar`` で元に戻せる）。"""
        self._timer.stop()
        self._dirty = False
        removed = self.annotation
        if self.library is not None:
            try:
                removed = self.library.delete_annotation(self.annotation.id)
            except Exception as e:  # noqa: BLE001
                log.warning("could not delete the annotation", exc_info=True)
                self.status.setText(f"削除できませんでした（{e}）")
                return
        self.deleted.emit(removed)
        self.close()

    def hideEvent(self, event):  # noqa: N802 - Qt の名前
        self.flush()
        super().hideEvent(event)


class UndoBar(QFrame):
    """「注釈を削除しました  [元に戻す]」の帯。5 秒で消える。"""

    undone = Signal(object)  # 元に戻した注釈
    expired = Signal()

    def __init__(self, library=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("banner", "info")
        self.library = library
        self._annotation = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 8, 8)
        lay.setSpacing(10)
        self._icon = label("")
        self._icon.setFixedSize(20, 20)
        lay.addWidget(self._icon)
        self.message = label("注釈を削除しました", "secondary")
        lay.addWidget(self.message, 1)
        self.undo_btn = button("元に戻す")
        self.undo_btn.clicked.connect(self._undo)
        lay.addWidget(self.undo_btn)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(UNDO_MS)
        self._timer.timeout.connect(self._expire)
        self.hide()

    def show_deleted(self, annotation) -> None:
        self._annotation = annotation
        self._icon.setPixmap(icons.pixmap("info", theme.current().accent, 20))
        self.message.setToolTip(plain_tooltip(annotation.quote[:80]))
        self.show()
        self._timer.start()

    def _undo(self) -> None:
        a, self._annotation = self._annotation, None
        self._timer.stop()
        self.hide()
        if a is None:
            return
        if self.library is not None:
            try:
                a = self.library.restore_annotation(a)
            except Exception:  # noqa: BLE001
                log.warning("could not restore the annotation", exc_info=True)
                return
        self.undone.emit(a)

    def _expire(self) -> None:
        self._annotation = None
        self.hide()
        self.expired.emit()
