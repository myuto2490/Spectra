"""PDF のプレビュー。表示中のページだけを描画し、高解像度ディスプレイにも対応する。"""

from __future__ import annotations

from collections import OrderedDict

import pymupdf
from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

PAGE_GAP = 16
MARGIN = 24
MIN_ZOOM, MAX_ZOOM = 0.25, 4.0
_CACHE_LIMIT = 24


class _PageWidget(QWidget):
    def __init__(self, view: PdfView, index: int):
        super().__init__()
        self._view = view
        self.index = index
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(f"{index + 1} ページ")

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(3, 2, -3, -5)
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
        p.end()


class PdfView(QScrollArea):
    """縦に並べたページのスクロール表示。"""

    zoomChanged = Signal(float)
    pageChanged = Signal(int, int)  # (現在のページ 0 始まり, 総ページ数)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Canvas")
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self._doc: pymupdf.Document | None = None
        self._zoom = 1.0
        self._fit_width = True
        self._pages: list[_PageWidget] = []
        self._cache: OrderedDict[tuple[int, float], QPixmap] = OrderedDict()
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self._layout.setSpacing(PAGE_GAP)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.setWidget(self._container)
        self.verticalScrollBar().valueChanged.connect(self._emit_page)
        self._refit = QTimer(self, singleShot=True, interval=60, timeout=self._apply_fit)

    # -- 文書 ------------------------------------------------------------

    def set_document(self, doc: pymupdf.Document | None) -> None:
        self._doc = doc
        self._cache.clear()
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
                self._pages.append(w)
        if self._fit_width:
            self._apply_fit()
        else:
            self._relayout()
        self._emit_page()

    def document(self) -> pymupdf.Document | None:
        return self._doc

    # -- ズーム ----------------------------------------------------------

    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float, fit_width: bool = False) -> None:
        zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        self._fit_width = fit_width
        # 幅に合わせている間は横スクロールバーを出さない
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff if fit_width else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        if abs(zoom - self._zoom) < 1e-3 and self._pages and self._pages[0].width() > 0:
            return
        ratio = self._scroll_ratio()
        self._zoom = zoom
        self._cache.clear()
        self._relayout()
        self.set_scroll_ratio(ratio)
        self.zoomChanged.emit(zoom)

    def fit_width(self) -> None:
        self._fit_width = True
        self._apply_fit()

    def _apply_fit(self) -> None:
        if not self._doc or not self._fit_width or self._doc.page_count == 0:
            return
        width = max(p.rect.width for p in self._doc)
        bar = self.verticalScrollBar().sizeHint().width()
        avail = self.width() - MARGIN * 2 - 8 - bar
        if avail > 50:
            self.set_zoom(avail / width, fit_width=True)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit_width:
            self._refit.start()

    def _relayout(self) -> None:
        if not self._doc:
            self._container.resize(self.viewport().size())
            return
        for w in self._pages:
            r = self._doc[w.index].rect
            w.setFixedSize(QSize(int(r.width * self._zoom) + 6, int(r.height * self._zoom) + 7))
        self._layout.activate()
        self._container.resize(self._layout.sizeHint())

    # -- スクロール同期 ------------------------------------------------------

    def _scroll_ratio(self) -> float:
        bar = self.verticalScrollBar()
        return bar.value() / bar.maximum() if bar.maximum() else 0.0

    def set_scroll_ratio(self, ratio: float) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(int(round(ratio * bar.maximum())))

    def scroll_to_page(self, index: int) -> None:
        if 0 <= index < len(self._pages):
            self.verticalScrollBar().setValue(self._pages[index].y() - MARGIN)

    def current_page(self) -> int:
        y = self.verticalScrollBar().value() + self.viewport().height() // 3
        for w in self._pages:
            if w.y() + w.height() >= y:
                return w.index
        return max(0, len(self._pages) - 1)

    def _emit_page(self) -> None:
        self.pageChanged.emit(self.current_page(), len(self._pages))

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
            scale = self._zoom * dpr
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


class PreviewPair(QWidget):
    """原文と訳文を左右に並べ、スクロールとズームを同期する。"""

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
        for a, b in ((self.original, self.translated), (self.translated, self.original)):
            a.verticalScrollBar().valueChanged.connect(lambda _v, a=a, b=b: self._sync_scroll(a, b))
            a.zoomChanged.connect(lambda z, b=b: self._sync_zoom(b, z))
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
        QTimer.singleShot(0, active.fit_width)
        other = self.translated if active is self.original else self.original
        QTimer.singleShot(0, lambda: other.isVisible() and other.fit_width())

    def active_view(self) -> PdfView:
        return self.original if self._left.isVisible() else self.translated

    def set_zoom(self, zoom: float) -> None:
        self.active_view().set_zoom(zoom)

    def fit_width(self) -> None:
        for v in (self.original, self.translated):
            if v.isVisible():
                v.fit_width()


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
