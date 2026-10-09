"""アプリアイコン（SVG）から各 OS 用のアイコンファイルを生成する。

使い方: python packaging/make_icons.py  → packaging/build/ に icon.png / icon.ico / icon.icns
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QByteArray, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "src" / "spectra" / "assets" / "icon.svg"
OUT = Path(__file__).resolve().parent / "build"


def render(size: int) -> Image.Image:
    renderer = QSvgRenderer(QByteArray(SVG.read_bytes()))
    img = QImage(size, size, QImage.Format.Format_RGBA8888)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    ptr = img.constBits()
    return Image.frombuffer("RGBA", (size, size), bytes(ptr), "raw", "RGBA", 0, 1)


def main() -> int:
    _app = QGuiApplication(sys.argv)
    OUT.mkdir(parents=True, exist_ok=True)
    big = render(1024)
    big.save(OUT / "icon.png")
    big.save(OUT / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    big.save(OUT / "icon.icns")
    print(f"icons written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
