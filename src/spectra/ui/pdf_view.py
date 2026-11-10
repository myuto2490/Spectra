"""PDF のプレビュー。表示中のページだけを描画し、高解像度ディスプレイにも対応する。

ページ移動・ズーム・検索・文字の選択・リンク・ページの一覧と目次を持つ（画面への組み込みは main_window.py）。
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

import pymupdf
from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QGuiApplication,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

PAGE_GAP = 16
MARGIN = 24
MIN_ZOOM, MAX_ZOOM = 0.25, 4.0
ZOOM_PRESETS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)
_CACHE_LIMIT = 24
# 1 ページの描画のピクセル数の上限。極端に大きなページ（ポスターや、細工された PDF）を高い倍率で描いて
# メモリを使い切らないよう、これを超えるときは粗く描いて拡大する（A4 を 400%・Retina で描いても約 3,200 万）
MAX_PAGE_PIXELS = 64_000_000


def capped_scale(width: float, height: float, scale: float, limit: int = MAX_PAGE_PIXELS) -> float:
    """幅 width・高さ height（pt）のページを scale 倍で描くときの倍率。ピクセル数が limit を超えるなら、収まる倍率に下げる。"""
    pixels = max(width, 1.0) * max(height, 1.0) * scale * scale
    if not math.isfinite(pixels) or pixels <= limit:
        return scale
    return scale * math.sqrt(limit / pixels)
MAX_HITS = 2000  # 検索の一致の上限（巨大な文書で重くならないように）
_DRAG_SLOP = 4  # これ以下の動きはクリックとみなす（リンクを開く）
_HIT = QColor(255, 214, 10, 110)
_HIT_CURRENT = QColor(255, 149, 0, 150)
_SELECTION = QColor(10, 132, 255, 90)

# 注釈の色（ページは白地なので、テーマに関わらず同じ色で描く）
ANNOTATION_RGB = {
    "yellow": (255, 214, 10),
    "green": (52, 199, 89),
    "blue": (10, 132, 255),
    "pink": (255, 105, 180),
    "purple": (175, 82, 222),
}
ANNOTATION_NAMES = {"yellow": "黄", "green": "緑", "blue": "青", "pink": "桃", "purple": "紫"}
_NOTE_SIZE = 16  # 付箋の大きさ（画面の px）
_BAND_WIDTH = 5  # 訳文の表示で、左の余白に描く帯の太さ（画面の px）


def annotation_color(name: str, alpha: int = 255) -> QColor:
    r, g, b = ANNOTATION_RGB.get(name, ANNOTATION_RGB["yellow"])
    return QColor(r, g, b, alpha)


@dataclass(frozen=True)
class Link:
    """ページの中のリンク。page_rect はページの座標（pt）。"""

    page_rect: pymupdf.Rect
    uri: str = ""  # 外へのリンク
    target_page: int = -1  # 文書の中へのリンク（0 始まり）
    target_y: float = 0.0  # 移動先のページの中の縦の位置（pt）


class _PageWidget(QWidget):
    def __init__(self, view: PdfView, index: int):
        super().__init__()
        self._view = view
        self.index = index
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(f"{index + 1} ページ")
        self.setMouseTracking(True)

    def _page_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(3, 2, -3, -5)

    def to_page(self, pos: QPointF) -> pymupdf.Point:
        r, z = self._page_rect(), self._view.zoom()
        return pymupdf.Point((pos.x() - r.x()) / z, (pos.y() - r.y()) / z)

    def to_widget(self, rect: pymupdf.Rect) -> QRectF:
        r, z = self._page_rect(), self._view.zoom()
        return QRectF(r.x() + rect.x0 * z, r.y() + rect.y0 * z, rect.width * z, rect.height * z)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._page_rect()
        # 柔らかい影
        for i, alpha in enumerate((18, 12, 6)):
            path = QPainterPath()
            path.addRoundedRect(r.adjusted(-i, -i + 2, i, i + 3), 3 + i, 3 + i)
            p.fillPath(path, QColor(0, 0, 0, alpha))
        pm = self._view.page_pixmap(self.index)
        if pm is None:
            p.fillRect(r, QColor("#FFFFFF"))
        else:
            p.drawPixmap(r.toRect(), pm)
        for rect, current in self._view.hits_on(self.index):
            p.fillRect(self.to_widget(rect), _HIT_CURRENT if current else _HIT)
        for rect in self._view.selection_on(self.index):
            p.fillRect(self.to_widget(rect), _SELECTION)
        self._paint_annotations(p)
        p.end()

    # -- 注釈 ------------------------------------------------------------

    def annotation_rects(self, a) -> list[QRectF]:
        """注釈 a を、このページの部品の座標で見たときの矩形（描画とクリックの判定に使う）。"""
        mode = self._view.annotation_mode()
        r, z = self._page_rect(), self._view.zoom()
        if mode == "bands":  # 訳文の表示: 左の余白に、同じ高さの細い帯
            x0, y0, _x1, y1 = a.rect
            if a.kind == "note":
                return [QRectF(r.x() + 2, r.y() + y0 * z, 11, 11)]
            return [QRectF(r.x() + 3, r.y() + y0 * z, _BAND_WIDTH, max((y1 - y0) * z, 8.0))]
        if a.kind == "note":
            x0, y0 = a.quads[0][0], a.quads[0][1]
            return [QRectF(r.x() + x0 * z, r.y() + y0 * z, _NOTE_SIZE, _NOTE_SIZE)]
        rects = [QRectF(r.x() + q[0] * z, r.y() + q[1] * z, (q[2] - q[0]) * z, (q[3] - q[1]) * z) for q in a.quads]
        if a.has_comment and rects:  # コメントのある印は、右端に小さな吹き出しの目印
            last = rects[-1]
            rects.append(QRectF(last.right() + 3, last.top(), 12, 12))
        return rects

    def annotation_at(self, pos: QPointF):
        """pos（この部品の座標）にある注釈（重なっていれば後で付けたもの）。なければ None。"""
        for a in reversed(self._view.annotations_on(self.index)):
            if any(rc.adjusted(-1, -1, 1, 1).contains(pos) for rc in self.annotation_rects(a)):
                return a
        return None

    def _paint_annotations(self, p: QPainter) -> None:
        view = self._view
        mode = view.annotation_mode()
        for a in view.annotations_on(self.index):
            rects = self.annotation_rects(a)
            if not rects:
                continue
            if mode == "bands":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(annotation_color(a.color, 230))
                p.drawRoundedRect(rects[0], 2, 2)
            elif a.kind == "note":
                _paint_sticky(p, rects[0], a.color)
            else:
                body = rects[:-1] if a.has_comment else rects
                for rc in body:
                    if a.kind == "highlight":
                        p.fillRect(rc, annotation_color(a.color, 110))
                    else:  # underline
                        p.setPen(QPen(annotation_color(a.color, 255), 2))
                        p.drawLine(QPointF(rc.left(), rc.bottom() - 1), QPointF(rc.right(), rc.bottom() - 1))
                if a.has_comment:
                    _paint_comment_mark(p, rects[-1], a.color)
            if a.id in (view.flashing_id(), view.selected_annotation()):
                union = rects[0]
                for rc in rects[1:]:
                    union = union.united(rc)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(10, 132, 255), 2))
                p.drawRoundedRect(union.adjusted(-2, -2, 2, 2), 3, 3)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._view.setFocus()
            self._view.press(self.index, self.to_page(event.position()), event.position())
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pt = self.to_page(event.position())
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._view.drag(self.index, pt, event.position())
        else:
            a = self.annotation_at(event.position())
            if a is not None:
                from .widgets import plain_tooltip

                self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                self.setToolTip(plain_tooltip(_annotation_summary(a)))
            else:
                self.setCursor(self._view.cursor_at(self.index, pt))
                self.setToolTip("")

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            a = self.annotation_at(event.position())
            self._view.release(
                self.index,
                self.to_page(event.position()),
                event.position(),
                event.globalPosition().toPoint(),
                a.id if a is not None else "",
            )
        else:
            super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        self._view.show_context_menu(self.index, self.to_page(QPointF(event.pos())), event.globalPos())
        event.accept()


def _annotation_summary(a) -> str:
    """印に重ねたときのツールチップ: 引用（80 文字まで）とコメント。"""
    quote = " ".join(a.quote.split())
    parts = []
    if quote:
        parts.append("「" + (quote if len(quote) <= 80 else quote[:79] + "…") + "」")
    if a.comment.strip():
        parts.append(a.comment.strip())
    return "\n".join(parts) or "注釈"


def _paint_sticky(p: QPainter, rc: QRectF, color: str) -> None:
    p.setPen(QPen(QColor(0, 0, 0, 90), 1))
    p.setBrush(annotation_color(color, 235))
    p.drawRoundedRect(rc, 3, 3)
    p.setPen(QPen(QColor(0, 0, 0, 120), 1.2))
    y = rc.top() + rc.height() * 0.35
    p.drawLine(QPointF(rc.left() + 3, y), QPointF(rc.right() - 3, y))
    p.drawLine(QPointF(rc.left() + 3, y + 4), QPointF(rc.right() - 6, y + 4))


def _paint_comment_mark(p: QPainter, rc: QRectF, color: str) -> None:
    p.setPen(QPen(QColor(0, 0, 0, 90), 1))
    p.setBrush(annotation_color(color, 235))
    p.drawRoundedRect(rc, 4, 4)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0, 150))
    for i in range(3):
        p.drawEllipse(QPointF(rc.left() + 3.2 + i * 2.8, rc.center().y()), 0.9, 0.9)


class SelectionBar(QFrame):
    """文字を選ぶと、選んだ範囲の上に出す小さな帯。原文では色・下線・コメント・コピー、訳文では原文で付ける案内。"""

    colorChosen = Signal(str)
    underlineChosen = Signal()
    commentChosen = Signal()
    copyChosen = Signal()
    originalChosen = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        from .widgets import button, label, tool_button

        self.setObjectName("SelectionBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(8, 5, 8, 5)
        self._lay.setSpacing(6)
        self._annotate: list[QWidget] = []
        self.color_buttons: dict[str, QPushButton] = {}
        for name in ANNOTATION_RGB:
            b = QPushButton()
            b.setFixedSize(18, 18)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            r, g, bl = ANNOTATION_RGB[name]
            b.setStyleSheet(
                f"QPushButton {{ background: rgb({r},{g},{bl}); border: 1px solid rgba(0,0,0,70); border-radius: 9px;"
                " min-width: 0; min-height: 0; padding: 0; }"
            )
            b.setToolTip(f"{ANNOTATION_NAMES[name]}のマーカーを付ける")
            b.setAccessibleName(b.toolTip())
            b.clicked.connect(lambda _c=False, n=name: self.colorChosen.emit(n))
            self._lay.addWidget(b)
            self.color_buttons[name] = b
            self._annotate.append(b)
        self.underline_btn = tool_button("underline", "下線を付ける")
        self.underline_btn.clicked.connect(self.underlineChosen)
        self.comment_btn = tool_button("comment", "マーカーを付けてコメントを書く")
        self.comment_btn.clicked.connect(self.commentChosen)
        self.copy_btn = tool_button("copy", "コピー")
        self.copy_btn.clicked.connect(self.copyChosen)
        for b in (self.underline_btn, self.comment_btn, self.copy_btn):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._lay.addWidget(b)
            self._annotate.append(b)
        self.hint = label("注釈は原文に付けます", "caption")
        self.original_btn = button("原文で付ける")
        self.original_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.original_btn.clicked.connect(self.originalChosen)
        self._lay.addWidget(self.hint)
        self._lay.addWidget(self.original_btn)
        self.set_mode("annotate")
        self.hide()

    def set_mode(self, mode: str) -> None:
        """annotate（原文: 付ける）/ original（訳文: 原文で付けるよう案内）。"""
        for w in self._annotate:
            w.setVisible(mode == "annotate")
        self.hint.setVisible(mode == "original")
        self.original_btn.setVisible(mode == "original")
        self.adjustSize()


class PdfView(QScrollArea):
    """縦に並べたページのスクロール表示。"""

    zoomChanged = Signal(float)
    pageChanged = Signal(int, int)  # (現在のページ 0 始まり, 総ページ数)
    searchChanged = Signal(int, int)  # (今の一致 1 始まり（なければ 0）, 一致の数)
    linkRequested = Signal(str)  # 外へのリンク（開く前に確認する）
    selectionChanged = Signal(bool)  # 選んだ文字があるか
    findRequested = Signal(str)  # 右クリックの「PDF 内を検索」（選んでいる文字。なければ空）
    annotationRequested = Signal(str, str, bool)  # (種類 highlight/underline, 色, コメントを続けて書くか)。選んでいる文字に付ける
    annotationClicked = Signal(str, QPoint)  # (注釈の id, 画面の位置)
    annotationDeleteRequested = Signal(str)  # 選んでいる印で Delete が押された
    notePlaced = Signal(int, float, float)  # 付箋の道具でページの位置をクリックした (ページ, x, y（pt）)
    originalRequested = Signal(int)  # 訳文で選んだ文字に付けたいとき、原文を表示してほしい（ページ）
    toolCancelled = Signal()  # Esc で、マーカーの道具を止めた
    colorPicked = Signal(str)  # 選んだ帯で色を押した（以後の既定の色）

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Canvas")
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._doc: pymupdf.Document | None = None
        self._zoom = 1.0
        self._fit = "width"  # "width" / "page" / ""（倍率を直接指定）
        self._pages: list[_PageWidget] = []
        self._cache: OrderedDict[tuple[int, float], QPixmap] = OrderedDict()
        self._words: dict[int, list[tuple]] = {}
        self._links: dict[int, list[Link]] = {}
        self._hits: list[tuple[int, pymupdf.Rect]] = []
        self._hit_index = -1
        self._query = ""
        self._context_providers: list[Callable[[int, pymupdf.Point], list[QAction | None]]] = []
        self._annotations: dict[int, list] = {}  # ページ → 注釈（原文の座標。重なりは後のものが上）
        self._ann_mode = ""  # "marks"（原文: 文字に印）/ "bands"（訳文: 左の余白に帯）/ ""
        self._ann_by_id: dict[str, object] = {}
        self._flash_id = ""
        self._selected_ann = ""
        self._flash_timer = QTimer(self, singleShot=True, interval=1600, timeout=self._end_flash)
        self._tool = ""  # マーカーの道具: "" / highlight / underline / note
        self._color = "yellow"
        self._pending_pos: tuple[int, float] | None = None  # 表示の大きさが決まったら戻す位置（ページ, 0〜1）
        self._anchor: tuple[int, int] | None = None  # 選択の起点（ページ, 単語の番号）
        self._cursor_word: tuple[int, int] | None = None
        self._press_pos: QPointF | None = None
        self._press_page = -1
        self._press_point: pymupdf.Point | None = None
        self._dragged = False
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self._layout.setSpacing(PAGE_GAP)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.setWidget(self._container)
        self.verticalScrollBar().valueChanged.connect(self._emit_page)
        self.verticalScrollBar().sliderMoved.connect(lambda _v: setattr(self, "_pending_pos", None))
        self._refit = QTimer(self, singleShot=True, interval=60, timeout=self._apply_fit)
        self.bar = SelectionBar(self.viewport())
        self.bar.colorChosen.connect(self._on_bar_color)
        self.bar.underlineChosen.connect(lambda: self._annotate_selection("underline", False))
        self.bar.commentChosen.connect(lambda: self._annotate_selection("highlight", True))
        self.bar.copyChosen.connect(self._on_bar_copy)
        self.bar.originalChosen.connect(self._on_bar_original)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self._place_bar())

    # -- 文書 ------------------------------------------------------------

    def set_document(self, doc: pymupdf.Document | None) -> None:
        self._doc = doc
        self._cache.clear()
        self._words.clear()
        self._links.clear()
        self._reset_search()
        self._clear_selection()
        # 古いページはその場でレイアウトから外す（deleteLater だけだと、
        # 削除されるまでの間に計算した高さに前の文書のページが含まれてしまう）
        for w in self._pages:
            self._layout.removeWidget(w)
            w.setParent(None)
            w.deleteLater()
        self._pages = []
        if doc is not None:
            for i in range(doc.page_count):
                w = _PageWidget(self, i)
                self._layout.addWidget(w, 0, Qt.AlignmentFlag.AlignHCenter)
                w.show()  # 隠れたままだとレイアウトの大きさに数えられず、最初の配置が崩れる
                self._pages.append(w)
        if self._fit:
            self._apply_fit()
        else:
            self._relayout()
        self._emit_page()

    def document(self) -> pymupdf.Document | None:
        return self._doc

    def page_count(self) -> int:
        return len(self._pages)

    # -- ズーム ----------------------------------------------------------

    def zoom(self) -> float:
        return self._zoom

    def fit_mode(self) -> str:
        """"width"（幅に合わせる）/ "page"（ページ全体）/ ""（倍率を直接指定）。"""
        return self._fit

    def set_zoom(self, zoom: float, fit_width: bool = False, *, anchor: QPointF | None = None) -> None:
        """anchor（表示領域の中の位置）を渡すと、その位置にあるものを動かさずに拡大・縮小する。"""
        self._set_zoom(zoom, "width" if fit_width else "", anchor)

    def _set_zoom(self, zoom: float, fit: str, anchor: QPointF | None = None) -> None:
        zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        self._fit = fit
        # 幅かページに合わせている間は横スクロールバーを出さない
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff if fit else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        if abs(zoom - self._zoom) < 1e-3 and self._pages and self._pages[0].width() > 0:
            return
        old = self._zoom
        ratio = self._scroll_ratio()
        before = None
        if anchor is not None:
            before = (
                (self.horizontalScrollBar().value() + anchor.x()) / old,
                (self.verticalScrollBar().value() + anchor.y()) / old,
            )
        self._zoom = zoom
        self._cache.clear()
        self._relayout()
        if anchor is not None and before is not None:
            self.horizontalScrollBar().setValue(int(before[0] * zoom - anchor.x()))
            self.verticalScrollBar().setValue(int(before[1] * zoom - anchor.y()))
        else:
            self.set_scroll_ratio(ratio)
        self.zoomChanged.emit(zoom)

    def fit_width(self) -> None:
        self._fit = "width"
        self._apply_fit()

    def fit_page(self) -> None:
        self._fit = "page"
        self._apply_fit()

    def _apply_fit(self) -> None:
        if not self._doc or not self._fit or self._doc.page_count == 0:
            return
        width = max(p.rect.width for p in self._doc)
        bar = self.verticalScrollBar().sizeHint().width()
        avail = self.width() - MARGIN * 2 - 8 - bar
        if avail <= 50:
            return
        zoom = avail / width
        if self._fit == "page":
            index = self.current_page()
            height = self._doc[index].rect.height
            zoom = min(zoom, (self.viewport().height() - MARGIN * 2 - 8) / height)
        self._set_zoom(zoom, self._fit)
        if self._pending_pos is not None:
            self._apply_pending_pos()
            self._pending_pos = None
        elif self._fit == "page":
            self.scroll_to_page(self.current_page())

    def refit(self) -> None:
        """幅やページに合わせている間だけ、今の表示の大きさに合わせ直す（倍率を指定しているときは触らない）。"""
        if self._fit:
            self._apply_fit()

    def _settle_pending_pos(self) -> None:
        self._apply_pending_pos()
        self._pending_pos = None

    def _apply_pending_pos(self) -> None:
        if self._pending_pos is None or not self._pages:
            return
        index, ratio = self._pending_pos
        index = min(max(index, 0), len(self._pages) - 1)
        w = self._pages[index]
        self.verticalScrollBar().setValue(int(w.y() - MARGIN + min(max(ratio, 0.0), 1.0) * w.height()))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit:
            self._refit.start()
        elif self._pending_pos is not None and self.isVisible():
            QTimer.singleShot(0, self._settle_pending_pos)

    def wheelEvent(self, event) -> None:
        # ⌘（Windows・Linux では Ctrl）+ スクロールで、ポインタの位置を中心に拡大・縮小する
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier and self._doc is not None:
            delta = event.angleDelta().y()
            if delta:
                at = self.viewport().mapFromGlobal(event.globalPosition().toPoint()).toPointF()
                self.set_zoom(self._zoom * 1.0015**delta, anchor=at)
            event.accept()
            return
        super().wheelEvent(event)

    def viewportEvent(self, event) -> bool:
        # トラックパッドのピンチ（macOS）
        if event.type() == QEvent.Type.NativeGesture and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
            self.set_zoom(self._zoom * (1 + event.value()), anchor=event.position())
            return True
        return super().viewportEvent(event)

    def _relayout(self) -> None:
        if not self._doc:
            self._container.resize(self.viewport().size())
            return
        for w in self._pages:
            r = self._doc[w.index].rect
            w.setFixedSize(QSize(int(r.width * self._zoom) + 6, int(r.height * self._zoom) + 7))
        self._layout.activate()
        self._container.resize(self._layout.sizeHint())

    # -- ページの移動 ----------------------------------------------------------

    def _scroll_ratio(self) -> float:
        bar = self.verticalScrollBar()
        return bar.value() / bar.maximum() if bar.maximum() else 0.0

    def set_scroll_ratio(self, ratio: float) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(int(round(ratio * bar.maximum())))

    def scroll_to_page(self, index: int) -> None:
        if 0 <= index < len(self._pages):
            self.verticalScrollBar().setValue(self._pages[index].y() - MARGIN)

    def go_to_page(self, index: int) -> int:
        """index（0 始まり）に移動し、実際に移動したページを返す（範囲の外は端に寄せる）。"""
        if not self._pages:
            return 0
        index = min(max(index, 0), len(self._pages) - 1)
        self.scroll_to_page(index)
        return index

    def next_page(self) -> None:
        self.go_to_page(self.current_page() + 1)

    def previous_page(self) -> None:
        # ページの途中にいるときは、まずそのページの先頭へ戻る
        index = self.current_page()
        top = self._pages[index].y() - MARGIN if self._pages else 0
        self.go_to_page(index if self.verticalScrollBar().value() > top + 8 else index - 1)

    def current_page(self) -> int:
        y = self.verticalScrollBar().value() + self.viewport().height() // 3
        for w in self._pages:
            if w.y() + w.height() >= y:
                return w.index
        return max(0, len(self._pages) - 1)

    def _emit_page(self) -> None:
        self.pageChanged.emit(self.current_page(), len(self._pages))

    def scroll_to_rect(self, index: int, rect: pymupdf.Rect) -> None:
        """ページの中の rect（pt）が表示領域の中ほどに来るようにスクロールする。"""
        if not 0 <= index < len(self._pages):
            return
        w = self._pages[index]
        target = w.y() + w.to_widget(rect).center().y()
        self.verticalScrollBar().setValue(int(target - self.viewport().height() / 2))
        left = w.x() + w.to_widget(rect).center().x()
        bar = self.horizontalScrollBar()
        if bar.maximum() and not bar.value() <= left <= bar.value() + self.viewport().width():
            bar.setValue(int(left - self.viewport().width() / 2))

    # -- 見ていた場所の記憶 ---------------------------------------------------------

    def view_state(self) -> dict:
        """倍率と位置（論文ごとに覚えて、次に開いたときに戻す）。settings.reading_state と同じ形。

        fit は "none"（倍率を直接指定）/ "width" / "page"、ratio は今のページの中でどこまで読んだか（0〜1）。
        """
        index = self.current_page()
        ratio = 0.0
        if self._pages:
            w = self._pages[index]
            ratio = (self.verticalScrollBar().value() + MARGIN - w.y()) / max(w.height(), 1)
        return {
            "zoom": round(self._zoom, 4),
            "fit": self._fit or "none",
            "page": index,
            "ratio": round(min(max(ratio, 0.0), 1.0), 4),
        }

    def restore_view_state(self, state: dict | None) -> None:
        if not isinstance(state, dict) or not self._pages:
            return
        try:
            fit = state.get("fit", "none")
            fit = fit if fit in ("width", "page") else ""
            zoom = float(state.get("zoom", 1.0))
            index = int(state.get("page", 0))
            ratio = float(state.get("ratio", 0.0))
        except (TypeError, ValueError):
            return
        self._pending_pos = (index, ratio)
        if fit:
            # 表示の大きさが決まってから（_apply_fit）、倍率を合わせて位置を戻す
            self._fit = fit
            self._refit.start()
        else:
            self._set_zoom(zoom, "")
            self._apply_pending_pos()
            if self.isVisible():  # まだ表示されていなければ、表示の大きさが決まったとき（resizeEvent）にもう一度戻す
                self._pending_pos = None

    # -- 描画 ------------------------------------------------------------

    def page_pixmap(self, index: int) -> QPixmap | None:
        if self._doc is None:
            return None
        dpr = self.devicePixelRatioF()
        key = (index, round(self._zoom * dpr, 4))
        pm = self._cache.get(key)
        if pm is not None:
            self._cache.move_to_end(key)
            return pm
        try:
            page = self._doc[index]
            scale = capped_scale(page.rect.width, page.rect.height, self._zoom * dpr)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=True)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        except Exception:
            return None
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        self._cache[key] = pm
        while len(self._cache) > _CACHE_LIMIT:
            self._cache.popitem(last=False)
        return pm

    # -- 検索 ------------------------------------------------------------

    def search(self, text: str) -> int:
        """text を全ページから探して一致を色で示し、最初の一致（今の位置より後）へ移る。一致の数を返す。"""
        text = text.strip()
        self._reset_search()
        self._query = text
        if text and self._doc is not None:
            start = self.current_page()
            for index in range(self._doc.page_count):
                try:
                    rects = self._doc[index].search_for(text)
                except Exception:
                    rects = []
                for rect in rects:
                    self._hits.append((index, rect))
                if len(self._hits) >= MAX_HITS:
                    self._hits = self._hits[:MAX_HITS]
                    break
            if self._hits:
                first = next((i for i, (page, _r) in enumerate(self._hits) if page >= start), 0)
                self._hit_index = first
                self._reveal_hit()
        self._update_pages_for_search()
        self._emit_search()
        return len(self._hits)

    def find_next(self) -> None:
        self._step_hit(1)

    def find_previous(self) -> None:
        self._step_hit(-1)

    def clear_search(self) -> None:
        self._reset_search()
        self._update_pages_for_search()
        self._emit_search()

    def search_text(self) -> str:
        return self._query

    def hit_count(self) -> int:
        return len(self._hits)

    def hit_index(self) -> int:
        """今の一致（0 始まり。一致がなければ -1）。"""
        return self._hit_index

    def hits_on(self, index: int) -> list[tuple[pymupdf.Rect, bool]]:
        return [(r, i == self._hit_index) for i, (page, r) in enumerate(self._hits) if page == index]

    def _reset_search(self) -> None:
        self._hits = []
        self._hit_index = -1
        self._query = ""

    def _step_hit(self, step: int) -> None:
        if not self._hits:
            return
        self._hit_index = (self._hit_index + step) % len(self._hits)
        self._reveal_hit()
        self._update_pages_for_search()
        self._emit_search()

    def _reveal_hit(self) -> None:
        page, rect = self._hits[self._hit_index]
        self.scroll_to_rect(page, rect)

    def _update_pages_for_search(self) -> None:
        for w in self._pages:
            w.update()

    def _emit_search(self) -> None:
        self.searchChanged.emit(self._hit_index + 1 if self._hits else 0, len(self._hits))

    # -- 文字の選択とコピー -------------------------------------------------------

    def _page_words(self, index: int) -> list[tuple]:
        """(x0, y0, x1, y1, 単語, ブロック, 行, 番号) の並び（読む順）。"""
        words = self._words.get(index)
        if words is None:
            try:
                words = self._doc[index].get_text("words", sort=False) if self._doc is not None else []
            except Exception:
                words = []
            self._words[index] = words
        return words

    def _word_at(self, index: int, pt: pymupdf.Point, tolerance: float = 0.0) -> int:
        """pt にある単語の番号（なければ、tolerance（pt）以内でいちばん近い単語。それもなければ -1）。"""
        best, best_d = -1, None
        for i, w in enumerate(self._page_words(index)):
            dx = max(w[0] - pt.x, 0, pt.x - w[2])
            dy = max(w[1] - pt.y, 0, pt.y - w[3])
            d = (dx * dx + dy * dy) ** 0.5
            if d <= tolerance and (best_d is None or d < best_d):
                best, best_d = i, d
        return best

    def _page_links(self, index: int) -> list[Link]:
        links = self._links.get(index)
        if links is None:
            links = []
            try:
                raw = self._doc[index].get_links() if self._doc is not None else []
            except Exception:
                raw = []
            for item in raw:
                kind = item.get("kind")
                rect = pymupdf.Rect(item.get("from"))
                if kind == pymupdf.LINK_URI and item.get("uri"):
                    links.append(Link(rect, uri=str(item["uri"])))
                elif kind == pymupdf.LINK_GOTO and item.get("page", -1) >= 0:
                    to = item.get("to")
                    links.append(Link(rect, target_page=int(item["page"]), target_y=float(to.y) if to else 0.0))
            self._links[index] = links
        return links

    def link_at(self, index: int, pt: pymupdf.Point) -> Link | None:
        return next((link for link in self._page_links(index) if link.page_rect.contains(pt)), None)

    def cursor_at(self, index: int, pt: pymupdf.Point) -> QCursor:
        if self.link_at(index, pt) is not None:
            return QCursor(Qt.CursorShape.PointingHandCursor)
        if self._word_at(index, pt, 2.0) >= 0:
            return QCursor(Qt.CursorShape.IBeamCursor)
        return QCursor(Qt.CursorShape.ArrowCursor)

    def press(self, index: int, pt: pymupdf.Point, pos: QPointF) -> None:
        self._press_page, self._press_point, self._press_pos, self._dragged = index, pt, pos, False
        had = self.has_selection()
        self._clear_selection()
        self.bar.hide()
        word = self._word_at(index, pt, 6.0)
        self._anchor = (index, word) if word >= 0 else None
        self._cursor_word = self._anchor
        if had:
            self.selectionChanged.emit(False)

    def drag(self, index: int, pt: pymupdf.Point, pos: QPointF) -> None:
        if self._press_pos is None:
            return
        if not self._dragged and (pos - self._press_pos).manhattanLength() <= _DRAG_SLOP and index == self._press_page:
            return
        self._dragged = True
        if self._anchor is None:
            return
        word = self._word_at(index, pt, 1e9)  # 単語の上でなくても、いちばん近い単語まで選ぶ
        if word >= 0:
            self._cursor_word = (index, word)
            for w in self._pages:
                w.update()
            self.selectionChanged.emit(self.has_selection())

    def release(
        self, index: int, pt: pymupdf.Point, pos: QPointF, global_pos: QPoint | None = None, annotation_id: str = ""
    ) -> None:
        moved = self._dragged
        self._press_pos = None
        if moved:
            if self.has_selection():
                if self._tool in ("highlight", "underline") and self._ann_mode == "marks":
                    self._annotate_selection(self._tool, False)  # マーカーの道具: 選んだらすぐ付ける
                else:
                    self._place_bar()
            return
        if annotation_id:
            self._selected_ann = annotation_id
            self._update_pages()
            self.annotationClicked.emit(annotation_id, global_pos or QPoint())
            return
        if self._selected_ann:
            self._selected_ann = ""
            self._update_pages()
        if self._tool == "note" and self._ann_mode == "marks":
            self.notePlaced.emit(index, float(pt.x), float(pt.y))
            return
        link = self.link_at(index, pt)
        if link is None:
            return
        if link.uri:
            self.linkRequested.emit(link.uri)
        elif 0 <= link.target_page < len(self._pages):
            if link.target_y > 0:  # 図・参考文献など、ページの途中への移動
                top = self._pages[link.target_page].y() + link.target_y * self._zoom - MARGIN
                self.verticalScrollBar().setValue(int(top))
            else:
                self.go_to_page(link.target_page)

    def _selection_span(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if self._anchor is None or self._cursor_word is None:
            return None
        a, b = sorted((self._anchor, self._cursor_word))
        return a, b

    def has_selection(self) -> bool:
        return self._dragged and self._selection_span() is not None

    def _clear_selection(self) -> None:
        self._anchor = self._cursor_word = None
        self._dragged = False
        self.bar.hide()
        for w in self._pages:
            w.update()

    def clear_selection(self) -> None:
        had = self.has_selection()
        self._clear_selection()
        if had:
            self.selectionChanged.emit(False)

    def _selected_words(self, index: int) -> list[tuple]:
        span = self._selection_span()
        if span is None or not self.has_selection():
            return []
        (p0, w0), (p1, w1) = span
        if not p0 <= index <= p1:
            return []
        words = self._page_words(index)
        lo = w0 if index == p0 else 0
        hi = w1 if index == p1 else len(words) - 1
        return words[lo : hi + 1]

    def selection_on(self, index: int) -> list[pymupdf.Rect]:
        """選んだ文字の範囲（ページの座標）。同じ行の単語はひと続きにする。"""
        rects: list[pymupdf.Rect] = []
        last_line = None
        for w in self._selected_words(index):
            line = (w[5], w[6])
            r = pymupdf.Rect(w[:4])
            if rects and line == last_line:
                rects[-1] |= r
            else:
                rects.append(r)
            last_line = line
        return rects

    def selected_text(self) -> str:
        span = self._selection_span()
        if span is None or not self.has_selection():
            return ""
        out: list[str] = []
        for index in range(span[0][0], span[1][0] + 1):
            last_line = None
            line_words: list[str] = []
            for w in self._selected_words(index):
                line = (w[5], w[6])
                if last_line is not None and line != last_line:
                    out.append(" ".join(line_words))
                    line_words = []
                line_words.append(w[4])
                last_line = line
            if line_words:
                out.append(" ".join(line_words))
        return "\n".join(out)

    def copy_selection(self) -> bool:
        text = self.selected_text()
        if text:
            QGuiApplication.clipboard().setText(text)
        return bool(text)

    def select_all(self) -> None:
        if not self._pages:
            return
        last = len(self._pages) - 1
        self._anchor = (0, 0)
        self._cursor_word = (last, max(len(self._page_words(last)) - 1, 0))
        self._dragged = True
        for w in self._pages:
            w.update()
        self.selectionChanged.emit(self.has_selection())

    # -- 注釈（マーカー・下線・コメント・付箋） ----------------------------------------------

    def set_annotation_mode(self, mode: str) -> None:
        """"marks": 原文。文字そのものに印を描く / "bands": 訳文。左の余白に同じ高さの帯を描く / "": 描かない。"""
        self._ann_mode = mode if mode in ("marks", "bands") else ""
        self.bar.set_mode("annotate" if self._ann_mode == "marks" else "original")
        self._update_pages()

    def annotation_mode(self) -> str:
        return self._ann_mode

    def set_annotations(self, annotations: list) -> None:
        """この論文の注釈（library.Annotation のリスト）を表示する。"""
        self._annotations = {}
        for a in annotations:
            self._annotations.setdefault(a.page, []).append(a)
        self._ann_by_id = {a.id: a for a in annotations}
        if self._selected_ann and self._selected_ann not in self._ann_by_id:
            self._selected_ann = ""
        self._update_pages()

    def annotations_on(self, index: int) -> list:
        return self._annotations.get(index, []) if self._ann_mode else []

    def annotation_by_id(self, annotation_id: str):
        return self._ann_by_id.get(annotation_id)

    def selected_annotation(self) -> str:
        return self._selected_ann

    def select_annotation(self, annotation_id: str) -> None:
        self._selected_ann = annotation_id if annotation_id in self._ann_by_id else ""
        self._update_pages()

    def flashing_id(self) -> str:
        return self._flash_id

    def flash_annotation(self, annotation_id: str) -> None:
        """印を、少しの間だけ枠で強調する。"""
        if annotation_id in self._ann_by_id:
            self._flash_id = annotation_id
            self._flash_timer.start()
            self._update_pages()

    def _end_flash(self) -> None:
        self._flash_id = ""
        self._update_pages()

    def go_to_annotation(self, annotation_id: str) -> bool:
        """その注釈の場所へ移って、印を強調する。"""
        a = self._ann_by_id.get(annotation_id)
        if a is None or not 0 <= a.page < len(self._pages):
            return False
        self.scroll_to_rect(a.page, pymupdf.Rect(a.rect))
        self.flash_annotation(annotation_id)
        return True

    def _update_pages(self) -> None:
        for w in self._pages:
            w.update()

    def annotation_at_point(self, index: int, pt: pymupdf.Point):
        """ページの座標 pt（原文）にある注釈（右クリックのメニュー用）。なければ None。"""
        if self._ann_mode != "marks" or not 0 <= index < len(self._pages):
            return None
        w = self._pages[index]
        pos = w.to_widget(pymupdf.Rect(pt.x, pt.y, pt.x, pt.y)).topLeft()
        return w.annotation_at(pos)

    def global_pos_of(self, annotation) -> QPoint:
        """注釈の印の下あたりの、画面の位置（吹き出しを出す場所）。"""
        if not 0 <= annotation.page < len(self._pages):
            return QCursor.pos()
        w = self._pages[annotation.page]
        rects = w.annotation_rects(annotation)
        anchor = rects[-1].bottomLeft() if rects else QPointF(0, 0)
        return w.mapToGlobal(anchor.toPoint() + QPoint(0, 6))

    # マーカーの道具（押している間は、文字を選ぶとすぐ付ける / 付箋はクリックした位置に置く）

    def set_tool(self, tool: str) -> None:
        self._tool = tool if tool in ("highlight", "underline", "note") else ""

    def tool(self) -> str:
        return self._tool

    def set_current_color(self, color: str) -> None:
        if color in ANNOTATION_RGB:
            self._color = color

    def current_color(self) -> str:
        return self._color

    def selection_parts(self) -> list[tuple[int, list[tuple[float, float, float, float]], str]]:
        """選んでいる文字を、ページごとに (ページ, 行ごとの矩形（pt）, 選んだ文字) にする（注釈は 1 ページに 1 件ずつ付ける）。"""
        span = self.selection_span()
        if span is None or not self.has_selection():
            return []
        parts = []
        for index in range(span[0][0], span[1][0] + 1):
            words = self._selected_words(index)
            if not words:
                continue
            quads = [tuple(float(v) for v in r) for r in self.selection_on(index)]
            parts.append((index, quads, self._text_of(words)))
        return parts

    def selection_span(self):
        return self._selection_span()

    @staticmethod
    def _text_of(words: list[tuple]) -> str:
        out, last_line = [], None
        for w in words:
            line = (w[5], w[6])
            out.append(("\n" if last_line is not None and line != last_line else " " if out else "") + w[4])
            last_line = line
        return "".join(out)

    def _on_bar_color(self, color: str) -> None:
        self._color = color
        self.colorPicked.emit(color)
        self._annotate_selection("highlight", False)

    def _annotate_selection(self, kind: str, with_comment: bool) -> None:
        if not self.has_selection():
            return
        self.annotationRequested.emit(kind, self._color, with_comment)
        self.clear_selection()

    def _on_bar_copy(self) -> None:
        self.copy_selection()
        self.clear_selection()

    def _on_bar_original(self) -> None:
        parts = self.selection_parts()
        page = parts[0][0] if parts else self.current_page()
        self.clear_selection()
        self.originalRequested.emit(page)

    def _place_bar(self) -> None:
        """選んだ範囲の上（上に余裕がなければ下）に、小さな帯を出す。"""
        if not self.has_selection() or not self._ann_mode:
            self.bar.hide()
            return
        span = self._selection_span()
        first_page, last_page = span[0][0], span[1][0]
        rects = self.selection_on(first_page)
        w = self._pages[first_page]
        top_left = w.mapTo(self.viewport(), w.to_widget(rects[0]).topLeft().toPoint()) if rects else QPoint(8, 8)
        self.bar.adjustSize()
        x = max(4, min(top_left.x(), self.viewport().width() - self.bar.width() - 4))
        y = top_left.y() - self.bar.height() - 6
        if y < 4:  # 上に余裕がなければ、選んだ範囲の下に
            last_rects = self.selection_on(last_page)
            lw = self._pages[last_page]
            if last_rects:
                bottom = lw.mapTo(self.viewport(), lw.to_widget(last_rects[-1]).bottomLeft().toPoint())
                y = bottom.y() + 6
            y = max(4, min(y, self.viewport().height() - self.bar.height() - 4))
        self.bar.move(x, y)
        self.bar.show()
        self.bar.raise_()

    # -- 右クリックのメニュー ------------------------------------------------------------

    def add_context_provider(self, provider: Callable[[int, pymupdf.Point], list[QAction | None]]) -> None:
        """右クリックのメニューに項目を足す関数を登録する。

        provider(ページ, ページの中の位置（pt）) は QAction のリストを返す（None は区切り線）。標準の項目の後ろに、
        登録した順に並ぶ（マーカーやコメントなど、位置に応じた項目を後から足すための入口）。
        """
        self._context_providers.append(provider)

    def context_actions(self, index: int, pt: pymupdf.Point) -> list[QAction | None]:
        """右クリックのメニューの項目（None は区切り線）。標準の項目＋登録された項目。"""
        copy = QAction("コピー", self)
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.setEnabled(self.has_selection())
        copy.triggered.connect(lambda: self.copy_selection())
        select_all = QAction("すべてを選択", self)
        select_all.setShortcut(QKeySequence.StandardKey.SelectAll)
        select_all.setEnabled(self._doc is not None and bool(self._pages))
        select_all.triggered.connect(lambda: self.select_all())
        find = QAction("PDF 内を検索…", self)
        find.setShortcut(QKeySequence.StandardKey.Find)
        find.triggered.connect(lambda: self.findRequested.emit(self._selected_line()))
        actions: list[QAction | None] = [copy, select_all, None, find]
        for provider in self._context_providers:
            extra = provider(index, pt)
            if extra:
                actions.append(None)
                actions.extend(extra)
        return actions

    def build_context_menu(self, index: int, pt: pymupdf.Point) -> QMenu:
        menu = QMenu(self)
        for a in self.context_actions(index, pt):
            if a is None:
                menu.addSeparator()
            else:
                a.setParent(menu)
                menu.addAction(a)
        return menu

    def show_context_menu(self, index: int, pt: pymupdf.Point, global_pos) -> None:
        self.setFocus()
        self.build_context_menu(index, pt).exec(global_pos)

    def _selected_line(self) -> str:
        """選んでいる文字を、検索欄に入れる 1 行にする（長すぎるものは入れない）。"""
        text = " ".join(self.selected_text().split())
        return text if 0 < len(text) <= 100 else ""

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy) and self.copy_selection():
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.SelectAll) and self._doc is not None:
            self.select_all()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            if self._tool:
                self.toolCancelled.emit()
                event.accept()
                return
            if self.has_selection() or self._selected_ann:
                self._selected_ann = ""
                self.clear_selection()
                self._update_pages()
                event.accept()
                return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected_ann:
            self.annotationDeleteRequested.emit(self._selected_ann)
            event.accept()
            return
        super().keyPressEvent(event)

    # -- 目次（しおり） -----------------------------------------------------------

    def outline(self) -> list[tuple[int, str, int]]:
        """PDF のしおり: (階層 1 から, 題, ページ 0 始まり) の並び。なければ空。"""
        if self._doc is None:
            return []
        try:
            toc = self._doc.get_toc(simple=True)
        except Exception:
            return []
        return [(int(lvl), str(title), max(int(page) - 1, 0)) for lvl, title, page in toc if int(page) >= 1]


class PreviewPair(QWidget):
    """原文と訳文を左右に並べ、スクロールとズームを同期する。"""

    searchChanged = Signal(int, int)
    linkRequested = Signal(str)
    findRequested = Signal(str)  # 右クリックの「PDF 内を検索」
    annotationRequested = Signal(str, str, bool)  # 選んでいる文字に付ける (種類, 色, コメントを続けて書くか)
    annotationClicked = Signal(str, QPoint)
    annotationDeleteRequested = Signal(str)
    notePlaced = Signal(int, float, float)
    originalRequested = Signal(int)  # 訳文で選んだ文字に付けたい → 原文を表示してほしい（ページ）
    toolCancelled = Signal()
    colorPicked = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.original = PdfView()
        self.translated = PdfView()
        self.original.setAccessibleName("原文のプレビュー")
        self.translated.setAccessibleName("訳文のプレビュー")
        self._left = _pane("原文", self.original)
        self._right = _pane("翻訳", self.translated)
        lay.addWidget(self._left, 1)
        lay.addWidget(self._right, 1)
        self._syncing = False
        self._query = ""
        self.original.set_annotation_mode("marks")  # 原文: 文字に印を描き、付ける
        self.translated.set_annotation_mode("bands")  # 訳文: 左の余白に帯（付けるのは原文で）
        for v in (self.original, self.translated):
            v.annotationClicked.connect(self.annotationClicked)
            v.annotationDeleteRequested.connect(self.annotationDeleteRequested)
            v.originalRequested.connect(self.originalRequested)
        self.original.annotationRequested.connect(self.annotationRequested)
        self.original.notePlaced.connect(self.notePlaced)
        self.original.toolCancelled.connect(self.toolCancelled)
        self.original.colorPicked.connect(self.colorPicked)
        for a, b in ((self.original, self.translated), (self.translated, self.original)):
            a.verticalScrollBar().valueChanged.connect(lambda _v, a=a, b=b: self._sync_scroll(a, b))
            a.zoomChanged.connect(lambda z, b=b: self._sync_zoom(b, z))
            a.searchChanged.connect(lambda cur, total, a=a: self._on_search(a, cur, total))
            a.linkRequested.connect(self.linkRequested)
            a.findRequested.connect(self.findRequested)
        self.set_mode("original")

    def _sync_scroll(self, src: PdfView, dst: PdfView) -> None:
        if self._syncing or not dst.isVisible() or dst.document() is None:
            return
        self._syncing = True
        dst.set_scroll_ratio(src._scroll_ratio())
        self._syncing = False

    def _sync_zoom(self, dst: PdfView, zoom: float) -> None:
        if self._syncing or dst.document() is None:
            return
        self._syncing = True
        dst.set_zoom(zoom)
        self._syncing = False

    def set_mode(self, mode: str) -> None:
        """original / both / translated"""
        self._left.setVisible(mode in ("original", "both"))
        self._right.setVisible(mode in ("translated", "both"))
        active = self.active_view()
        QTimer.singleShot(0, active.refit)  # 幅やページに合わせているときだけ（倍率を指定していれば、そのまま）
        other = self.translated if active is self.original else self.original
        QTimer.singleShot(0, lambda: other.isVisible() and other.refit())
        if self._query:  # 表示を切り替えたら、今見えている側で探し直す
            self.search(self._query)

    def active_view(self) -> PdfView:
        """検索やページ移動の対象: 文字を選ぶなどで触れた側（見えているほう）。なければ左（原文）。"""
        for v in (self.translated, self.original):
            if v.isVisible() and (v.hasFocus() or v.viewport().hasFocus()):
                return v
        return self.original if self._left.isVisible() else self.translated

    def set_zoom(self, zoom: float) -> None:
        self.active_view().set_zoom(zoom)

    def fit_width(self) -> None:
        for v in (self.original, self.translated):
            if v.isVisible():
                v.fit_width()

    def fit_page(self) -> None:
        for v in (self.original, self.translated):
            if v.isVisible():
                v.fit_page()

    def go_to_page(self, index: int) -> int:
        return self.active_view().go_to_page(index)

    def next_page(self) -> None:
        self.active_view().next_page()

    def previous_page(self) -> None:
        self.active_view().previous_page()

    # -- 検索（見えているほうだけ） ---------------------------------------------------

    def search(self, text: str) -> int:
        self._query = text.strip()
        target = self.active_view()
        for v in (self.original, self.translated):
            if v is not target:
                v.clear_search()
        return target.search(text)

    def find_next(self) -> None:
        self.active_view().find_next()

    def find_previous(self) -> None:
        self.active_view().find_previous()

    def clear_search(self) -> None:
        self._query = ""
        for v in (self.original, self.translated):
            v.clear_search()

    def _on_search(self, view: PdfView, current: int, total: int) -> None:
        if view is self.active_view():
            self.searchChanged.emit(current, total)

    # -- 注釈（原文に付け、訳文には余白の帯で示す） ---------------------------------------------

    def set_annotations(self, annotations: list) -> None:
        for v in (self.original, self.translated):
            v.set_annotations(annotations)

    def set_tool(self, tool: str) -> None:
        self.original.set_tool(tool)

    def set_current_color(self, color: str) -> None:
        self.original.set_current_color(color)

    def go_to_annotation(self, annotation_id: str) -> bool:
        """その注釈へ移って強調する（訳文だけを表示しているときは、訳文の帯の場所へ）。"""
        view = self.translated if not self._left.isVisible() else self.original
        ok = view.go_to_annotation(annotation_id)
        other = self.translated if view is self.original else self.original
        if ok and other.isVisible():
            other.flash_annotation(annotation_id)
        return ok

    def add_context_provider(self, provider) -> None:
        """原文・訳文の両方の右クリックのメニューに、項目を足す関数を登録する（PdfView.add_context_provider）。"""
        for v in (self.original, self.translated):
            v.add_context_provider(provider)

    def copy_selection(self) -> bool:
        return any(v.isVisible() and v.copy_selection() for v in (self.active_view(), self.original, self.translated))


class ThumbnailList(QListWidget):
    """ページの一覧（サムネイル）。クリックでそのページへ移る。表示されたものだけ描画する。"""

    pageActivated = Signal(int)
    THUMB_WIDTH = 120

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Thumbnails")
        self.setViewMode(QListWidget.ViewMode.ListMode)
        self.setIconSize(QSize(self.THUMB_WIDTH, int(self.THUMB_WIDTH * 1.42)))
        self.setSpacing(6)
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAccessibleName("ページの一覧")
        self._doc: pymupdf.Document | None = None
        self._loader = QTimer(self, singleShot=True, interval=30, timeout=self._load_visible)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self._loader.start())
        self.itemClicked.connect(lambda it: self.pageActivated.emit(self.row(it)))

    def set_document(self, doc: pymupdf.Document | None) -> None:
        self._doc = doc
        self.clear()
        if doc is not None:
            for i in range(doc.page_count):
                it = QListWidgetItem(str(i + 1))
                it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
                self.addItem(it)
        self._loader.start()

    def set_current_page(self, index: int) -> None:
        if 0 <= index < self.count() and self.currentRow() != index:
            self.blockSignals(True)
            self.setCurrentRow(index)
            self.blockSignals(False)
            self.scrollToItem(self.item(index))

    def _load_visible(self) -> None:
        if self._doc is None or self.count() == 0:
            return
        view = self.viewport().rect()
        for i in range(self.count()):
            it = self.item(i)
            if not self.visualItemRect(it).intersects(view) or not it.icon().isNull():
                continue
            try:
                page = self._doc[i]
                scale = self.THUMB_WIDTH / max(page.rect.width, 1)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
            except Exception:
                continue
            it.setIcon(QPixmap.fromImage(img))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._loader.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._loader.start()


class OutlineTree(QTreeWidget):
    """PDF のしおり（目次）。クリックでそのページへ移る。"""

    pageActivated = Signal(int)
    _PAGE_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Outline")
        self.setHeaderHidden(True)
        self.setAccessibleName("目次")
        self.itemClicked.connect(lambda it, _c: self.pageActivated.emit(int(it.data(0, self._PAGE_ROLE))))

    def set_outline(self, entries: list[tuple[int, str, int]]) -> None:
        self.clear()
        stack: list[tuple[int, QTreeWidgetItem]] = []
        for level, title, page in entries:
            item = QTreeWidgetItem([title])
            item.setData(0, self._PAGE_ROLE, page)
            item.setToolTip(0, f"{page + 1} ページ")
            while stack and stack[-1][0] >= level:
                stack.pop()
            if stack:
                stack[-1][1].addChild(item)
            else:
                self.addTopLevelItem(item)
            stack.append((level, item))
        self.expandToDepth(0)


class NavPane(QWidget):
    """左に開閉するページの一覧と目次。目次がない PDF では一覧だけを出す。"""

    pageActivated = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        from .widgets import SegmentedControl

        self.setObjectName("NavPane")
        self.setMinimumWidth(150)
        self.setMaximumWidth(220)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.tabs = SegmentedControl([("pages", "ページ"), ("outline", "目次")])
        self.tabs.setAccessibleName("ページの一覧と目次")
        header = QWidget()
        header.setObjectName("PaneHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(8, 6, 8, 6)
        hl.addWidget(self.tabs)
        self._header = header
        lay.addWidget(header)
        self.thumbnails = ThumbnailList()
        self.outline_tree = OutlineTree()
        self._stack = QStackedWidget()
        self._stack.addWidget(self.thumbnails)
        self._stack.addWidget(self.outline_tree)
        lay.addWidget(self._stack, 1)
        self.thumbnails.pageActivated.connect(self.pageActivated)
        self.outline_tree.pageActivated.connect(self.pageActivated)
        self.tabs.changed.connect(lambda key: self.show_outline() if key == "outline" else self.show_thumbnails())
        self.tabs.set_current("pages")
        self._header.hide()

    def set_document(self, doc: pymupdf.Document | None, outline: list[tuple[int, str, int]]) -> None:
        self.thumbnails.set_document(doc)
        self.outline_tree.set_outline(outline)
        self._header.setVisible(bool(outline))  # しおりがない PDF では切り替えを出さない
        self.show_thumbnails()

    def has_outline(self) -> bool:
        return self.outline_tree.topLevelItemCount() > 0

    def show_thumbnails(self) -> None:
        self._stack.setCurrentWidget(self.thumbnails)
        self.tabs.set_current("pages")

    def show_outline(self) -> None:
        if self.has_outline():
            self._stack.setCurrentWidget(self.outline_tree)
            self.tabs.set_current("outline")

    def showing_outline(self) -> bool:
        return self._stack.currentWidget() is self.outline_tree

    def set_current_page(self, index: int) -> None:
        self.thumbnails.set_current_page(index)


class FindBar(QWidget):
    """読書画面の「ページ内を検索」の帯。Enter で次、Shift+Enter で前、Esc で閉じる。"""

    queryChanged = Signal(str)
    nextRequested = Signal()
    previousRequested = Signal()
    closed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        from .widgets import label, tool_button

        self.setObjectName("PaneHeader")
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 6, 12, 6)
        h.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setObjectName("Search")
        self.edit.setPlaceholderText("ページ内を検索")
        self.edit.setAccessibleName("PDF 内を検索")
        self.edit.setClearButtonEnabled(True)
        self.edit.setMaximumWidth(360)
        h.addWidget(self.edit, 1)
        self.count = label("", "caption")
        self.count.setMinimumWidth(72)
        self.count.setAccessibleName("検索の一致の数")
        h.addWidget(self.count)
        self.prev_btn = tool_button("chevron_up", "前の一致（Shift+Enter）")
        self.next_btn = tool_button("chevron", "次の一致（Enter）")
        self.close_btn = tool_button("close", "検索を閉じる（Esc）")
        for b in (self.prev_btn, self.next_btn, self.close_btn):
            h.addWidget(b)
        h.addStretch(1)
        self._sent = ""  # 最後に探した語
        self._timer = QTimer(self, singleShot=True, interval=200, timeout=self._emit_query)
        self.edit.textChanged.connect(lambda _t: self._timer.start())
        self.edit.returnPressed.connect(self._on_return)
        self.prev_btn.clicked.connect(self.previousRequested)
        self.next_btn.clicked.connect(self.nextRequested)
        self.close_btn.clicked.connect(self.close_bar)
        self.hide()

    def open(self) -> None:
        self.show()
        self.edit.setFocus()
        self.edit.selectAll()

    def set_query(self, text: str) -> None:
        """検索欄に text を入れて、すぐ探す。"""
        self.edit.setText(text)
        self.edit.selectAll()
        self._emit_query()

    def close_bar(self) -> None:
        self._timer.stop()
        self._sent = ""
        self.hide()
        self.closed.emit()

    def query(self) -> str:
        return self.edit.text().strip()

    def set_count(self, current: int, total: int) -> None:
        if not self.query():
            self.count.setText("")
        elif total == 0:
            self.count.setText("見つかりません")
        else:
            self.count.setText(f"{current} / {total} 件" + ("以上" if total >= MAX_HITS else ""))

    def _emit_query(self) -> None:
        self._timer.stop()
        self._sent = self.edit.text()
        self.queryChanged.emit(self._sent)

    def _on_return(self) -> None:
        if self.edit.text() != self._sent:  # 打った直後に Enter を押したときは、まず探す
            self._emit_query()
            return
        if QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.previousRequested.emit()
        else:
            self.nextRequested.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_bar()
            event.accept()
            return
        super().keyPressEvent(event)


def _pane(title: str, view: PdfView) -> QWidget:
    from .widgets import label

    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    header = QWidget()
    header.setObjectName("PaneHeader")
    hl = QHBoxLayout(header)
    hl.setContentsMargins(16, 6, 16, 6)
    hl.addWidget(label(title, "caption"))
    hl.addStretch(1)
    lay.addWidget(header)
    lay.addWidget(view, 1)
    return w
