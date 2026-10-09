"""アプリ内アイコン（オリジナルの SVG）。

線の太さと角の丸めをそろえた、24×24 の線画アイコン。色はテーマに合わせて描画時に指定する。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_STROKE = 'fill="none" stroke="{c}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"'

_PATHS: dict[str, str] = {
    "doc": '<path d="M7 3h7l5 5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M14 3v5h5"/>'
    '<path d="M9 13h6M9 16.5h6"/>',
    "folder": '<path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    "gear": '<path d="M12.00 5.00 L13.79 2.98 L15.52 3.50 L15.89 6.18 L16.95 7.05 L19.65 6.89 L20.50 8.48 L18.87 10.63 L19.00 12.00 L21.02 13.79 L20.50 15.52 L17.82 15.89 L16.95 16.95 L17.11 19.65 L15.52 20.50 L13.37 18.87 L12.00 19.00 L10.21 21.02 L8.48 20.50 L8.11 17.82 L7.05 16.95 L4.35 17.11 L3.50 15.52 L5.13 13.37 L5.00 12.00 L2.98 10.21 L3.50 8.48 L6.18 8.11 L7.05 7.05 L6.89 4.35 L8.48 3.50 L10.63 5.13Z"/><circle cx="12" cy="12" r="3"/>',
    "sidebar": '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M15 4.5v15"/><path d="M17.3 8.5h1.4M17.3 11h1.4"/>',
    "translate": '<path d="M4 5h8M8 3.5V5M10.5 5c-.6 3.6-3 6.6-6.5 8.3M6.3 8.2c1 2 2.6 3.6 4.7 4.8"/>'
    '<path d="M12.5 20.5l4-10 4 10M14 17h5"/>',
    "save": '<path d="M12 3.5v11M7.5 10.5l4.5 4.5 4.5-4.5"/><path d="M4.5 15.5v3a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-3"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="M8 12.3l2.7 2.7L16.2 9.5"/>',
    "warning": '<path d="M12 4L2.8 19.5h18.4z"/><path d="M12 10v4.2M12 17.2v.1"/>',
    "error": '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.8v.1"/>',
    "close": '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye_off": '<path d="M2.5 12S6 5.5 12 5.5c2 0 3.7.7 5.1 1.7M21.5 12s-3.5 6.5-9.5 6.5c-2 0-3.7-.7-5.1-1.7"/>'
    '<path d="M4 20L20 4"/>',
    "zoom_in": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L20.5 20.5M8 10.5h5M10.5 8v5"/>',
    "zoom_out": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L20.5 20.5M8 10.5h5"/>',
    "fit": '<path d="M4 9V5.5A1.5 1.5 0 0 1 5.5 4H9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15'
    'M9 20H5.5A1.5 1.5 0 0 1 4 18.5V15"/>',
    "external": '<path d="M13.5 4.5h6v6M19.5 4.5l-8 8"/><path d="M17 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 4 18.5v-10'
    'A1.5 1.5 0 0 1 5.5 7H10"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l8.5-8.5M16.5 6.5l2.5 2.5M14 9l2 2"/>',
    "trash": '<path d="M4.5 6.5h15M9.5 6.5V4.5h5v2M6.5 6.5l1 13h9l1-13M10 10v6.5M14 10v6.5"/>',
    "refresh": '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
    "chevron": '<path d="M6 9l6 6 6-6"/>',
    "drop": '<path d="M12 3.5v10.5M7.5 9.5L12 14l4.5-4.5"/><path d="M4 14.5v3.5a2.5 2.5 0 0 0 2.5 2.5h11a2.5 2.5 0 0 0 2.5-2.5v-3.5"/>',
    "font": '<path d="M4 19.5L9.5 5h1l5.5 14.5M6 14.5h8"/><path d="M17.5 12.5c.6-.6 1.4-1 2.3-1 1.3 0 2.2.9 2.2 2.2V19.5'
    'M22 16c-.6-.4-1.4-.6-2.2-.6-1.4 0-2.3.8-2.3 2.1s.9 2.1 2.1 2.1c1.1 0 1.9-.6 2.4-1.4"/>',
    "sliders": '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
    "github": '<path d="M9 19c-4 1.3-4-2-5.5-2.5M14.5 21v-3.2a2.8 2.8 0 0 0-.8-2.2c2.6-.3 5.3-1.3 5.3-5.8a4.5 4.5 0 0 0-1.2-3.1'
    ' 4.2 4.2 0 0 0-.1-3.1s-1-.3-3.2 1.2a11 11 0 0 0-5.8 0C6.5 3.3 5.5 3.6 5.5 3.6a4.2 4.2 0 0 0-.1 3.1 4.5 4.5 0 0 0-1.2 3.1'
    'c0 4.5 2.7 5.5 5.3 5.8a2.8 2.8 0 0 0-.8 2.2V21"/>',
}


def svg(name: str, color: str) -> str:
    body = _PATHS[name]
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><g {_STROKE.format(c=color)}>{body}</g></svg>'


@lru_cache(maxsize=512)
def pixmap(name: str, color: str, size: int = 18, dpr: float = 2.0) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg(name, color).encode()))
    pm = QPixmap(QSize(int(size * dpr), int(size * dpr)))
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size * dpr, size * dpr))
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def icon(name: str, color: str, size: int = 18, disabled_color: str | None = None) -> QIcon:
    ic = QIcon()
    ic.addPixmap(pixmap(name, color, size), QIcon.Mode.Normal)
    if disabled_color:
        ic.addPixmap(pixmap(name, disabled_color, size), QIcon.Mode.Disabled)
    return ic


def write_stylesheet_assets(directory: Path, color: str) -> dict[str, str]:
    """スタイルシートから参照するアイコンをファイルとして書き出し、パスを返す。"""
    directory.mkdir(parents=True, exist_ok=True)
    out = {}
    for name in ("chevron",):
        path = directory / f"{name}-{color.lstrip('#')}.svg"
        if not path.exists():
            path.write_text(svg(name, color), encoding="utf-8")
        out[name] = path.as_posix()
    return out
