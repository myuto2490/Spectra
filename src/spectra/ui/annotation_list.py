"""右のパネルの「注釈」タブ：論文の注釈の一覧。色とコメントの有無で絞り込み、クリックでその場所へ移る。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .annotation_popover import (
    ANNOTATION_COLORS,
    COLOR_LABELS,
    KIND_LABELS,
    ColorSwatch,
    annotation_rgb,
)
from .widgets import label

EMPTY_GUIDE = (
    "この論文にはまだ注釈がありません。\n"
    "原文の表示で文字を選ぶと、マーカー・下線・コメントを付けられます。"
    "付箋は、マーカーの道具の ▾ から選んでページをクリックします。"
)


def sort_key(a) -> tuple:
    """ページ順 → 上から → 左から。"""
    quads = getattr(a, "quads", ()) or ((0.0, 0.0, 0.0, 0.0),)
    first = min(quads, key=lambda q: (q[1], q[0]))
    return (a.page, first[1], first[0], a.created_at)


class _Dot(QAbstractButton):
    """行の左の色の印（クリックは行に任せる）。"""

    def __init__(self, color: str, kind: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.color, self.kind = color, kind
        self.setFixedSize(14, 14)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(annotation_rgb(self.color))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        if self.kind == "underline":
            p.drawRoundedRect(1, 10, 12, 3, 1.5, 1.5)
        elif self.kind == "note":
            p.drawRoundedRect(1, 1, 12, 12, 2.5, 2.5)
        else:
            p.drawRoundedRect(1, 2, 12, 10, 2.5, 2.5)


class AnnotationRow(QFrame):
    """一覧の 1 行：色の印・ページ・引用（2 行まで）・コメント（3 行まで）。クリック・Enter で移動する。"""

    clicked = Signal(str, int)  # 注釈の id・ページ（0 始まり）

    def __init__(self, annotation, parent: QWidget | None = None):
        super().__init__(parent)
        self.annotation = annotation
        self.setProperty("card", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        kind = KIND_LABELS.get(annotation.kind, annotation.kind)
        color = COLOR_LABELS.get(annotation.color, annotation.color)
        self.setAccessibleName(f"{annotation.page + 1} ページの{kind}（{color}）：{annotation.quote or annotation.comment}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(_Dot(annotation.color, annotation.kind))
        self.page = label(f"{annotation.page + 1} ページ", "caption")
        head.addWidget(self.page)
        self.kind = label(kind, "caption")
        head.addWidget(self.kind)
        head.addStretch(1)
        lay.addLayout(head)
        self.quote = self._text(annotation.quote, None, 2)
        lay.addWidget(self.quote)
        self.quote.setVisible(bool(annotation.quote))
        self.comment = self._text(annotation.comment, "secondary", 3)
        lay.addWidget(self.comment)
        self.comment.setVisible(bool(annotation.comment))
        if getattr(annotation, "comment_merged", False):
            lay.addWidget(label("ほかのパソコンでの編集と連結しました", "caption", tone="warning", wrap=True))

    @staticmethod
    def _text(text: str, role: str | None, lines: int):
        lb = label(text, role, wrap=True)
        lb.setTextFormat(Qt.TextFormat.PlainText)  # 論文の本文やコメントを、HTML として解釈しない
        lb.setMaximumHeight(lb.fontMetrics().lineSpacing() * lines + 2)
        lb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        return lb

    def set_selected(self, on: bool) -> None:
        self.setProperty("selected", on)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802 - Qt の名前
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.annotation.id, self.annotation.page)
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.annotation.id, self.annotation.page)
        else:
            super().keyPressEvent(e)

    def paintEvent(self, e) -> None:  # noqa: N802
        super().paintEvent(e)
        if self.property("selected"):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QColor(theme.current().accent))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), theme.RADIUS_CARD, theme.RADIUS_CARD)


class AnnotationList(QWidget):
    """注釈の一覧と絞り込み。データは ``set_annotations`` で渡す（Library から読むのは呼び出し側）。"""

    annotationActivated = Signal(str, int)  # 注釈の id・ページ（0 始まり）。読書画面ではその場所へ移る

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Inspector")
        self._all: list = []
        self._colors: set[str] = set()
        self._only_comments = False
        self._selected: str | None = None
        self.rows: list[AnnotationRow] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.filters = QWidget()
        fl = QVBoxLayout(self.filters)
        fl.setContentsMargins(18, 12, 18, 8)
        fl.setSpacing(8)
        colors = QHBoxLayout()
        colors.setSpacing(4)
        self.swatches: dict[str, ColorSwatch] = {}
        for c in ANNOTATION_COLORS:
            sw = ColorSwatch(c)
            sw.setToolTip(f"{COLOR_LABELS.get(c, c)}だけを表示")
            sw.toggled.connect(lambda on, c=c: self._toggle_color(c, on))
            self.swatches[c] = sw
            colors.addWidget(sw)
        colors.addStretch(1)
        fl.addLayout(colors)
        row2 = QHBoxLayout()
        self.only_comments = QPushButton("コメントのあるものだけ")
        self.only_comments.setCheckable(True)
        self.only_comments.setCursor(Qt.CursorShape.PointingHandCursor)
        self.only_comments.toggled.connect(self._toggle_comments)
        row2.addWidget(self.only_comments)
        row2.addStretch(1)
        self.count = label("", "caption")
        row2.addWidget(self.count)
        fl.addLayout(row2)
        outer.addWidget(self.filters)

        self.area = QScrollArea()
        self.area.setObjectName("Inspector")
        self.area.setWidgetResizable(True)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("Inspector")
        self._lay = QVBoxLayout(body)
        self._lay.setContentsMargins(18, 4, 18, 18)
        self._lay.setSpacing(8)
        self.empty = label(EMPTY_GUIDE, "secondary", wrap=True)
        self._lay.addWidget(self.empty)
        self._lay.addStretch(1)
        self.area.setWidget(body)
        outer.addWidget(self.area, 1)
        self._rebuild()

    # -- データ ------------------------------------------------------------

    def set_annotations(self, annotations) -> None:
        self._all = sorted(annotations, key=sort_key)
        self._rebuild()

    def annotations(self) -> list:
        """絞り込む前のすべて（ページ順）。"""
        return list(self._all)

    def visible_annotations(self) -> list:
        return [r.annotation for r in self.rows]

    def set_selected(self, annotation_id: str | None) -> None:
        self._selected = annotation_id
        for r in self.rows:
            r.set_selected(r.annotation.id == annotation_id)

    # -- 絞り込み ------------------------------------------------------------

    def _toggle_color(self, color: str, on: bool) -> None:
        (self._colors.add if on else self._colors.discard)(color)
        self._rebuild()

    def _toggle_comments(self, on: bool) -> None:
        self._only_comments = on
        self._rebuild()

    def _matches(self, a) -> bool:
        if self._colors and a.color not in self._colors:
            return False
        return not (self._only_comments and not a.comment)

    def _rebuild(self) -> None:
        for r in self.rows:
            self._lay.removeWidget(r)
            r.deleteLater()
        self.rows = []
        shown = [a for a in self._all if self._matches(a)]
        for i, a in enumerate(shown):
            r = AnnotationRow(a)
            r.clicked.connect(self._on_clicked)
            r.set_selected(a.id == self._selected)
            self._lay.insertWidget(i, r)
            self.rows.append(r)
        total = len(self._all)
        filtering = bool(self._colors) or self._only_comments
        self.filters.setVisible(total > 0)
        self.empty.setVisible(total == 0 or not shown)
        self.empty.setText(EMPTY_GUIDE if total == 0 else "条件に合う注釈はありません。")
        self.count.setText(f"{len(shown)} 件（全 {total} 件）" if filtering else f"{total} 件")

    def _on_clicked(self, annotation_id: str, page: int) -> None:
        self.set_selected(annotation_id)
        self.annotationActivated.emit(annotation_id, page)
