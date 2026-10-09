"""アプリアイコン（SVG から各サイズを生成）。"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


@lru_cache(maxsize=1)
def _svg() -> bytes:
    return resources.files("spectra.assets").joinpath("icon.svg").read_bytes()


def app_pixmap(size: int, dpr: float = 2.0) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(_svg()))
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p, QRectF(0, 0, size * dpr, size * dpr))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def app_icon() -> QIcon:
    ic = QIcon()
    for s in (16, 32, 64, 128, 256, 512):
        ic.addPixmap(app_pixmap(s, 1.0))
    return ic
