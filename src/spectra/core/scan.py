"""スキャンした PDF（ページ全体の画像の上に、透明な OCR テキストを重ねたもの）の扱い。

スキャン PDF で目に見えている文字は画像の一部で、文字として抽出できるのは画像の上に重ねた
透明なテキスト（OCR の結果）だけである。透明なテキストを消しても原文は見えたまま残るため、
訳文を書く前に原文の行を紙の色で塗りつぶす。
"""

from __future__ import annotations

import logging
from collections import Counter

import pymupdf

from .models import Rect

log = logging.getLogger(__name__)

INVISIBLE = 3  # テキストの描画モード 3（塗りも線も描かない）
MIN_IMAGE_COVER = 0.5  # ページに対する画像の面積の割合
PAPER_DPI = 24


def is_scanned(page: pymupdf.Page) -> bool:
    """ページの文字の大半が透明で、その下にページの大部分を覆う画像があるか。"""
    try:
        traces = page.get_texttrace()
        images = page.get_image_info()
    except Exception:
        log.debug("page %d: could not inspect text", page.number + 1, exc_info=True)
        return False
    visible = invisible = 0
    for t in traces:
        n = len(t.get("chars", ()))
        if t.get("type") == INVISIBLE or t.get("opacity", 1) == 0:
            invisible += n
        else:
            visible += n
    if invisible <= visible:
        return False
    page_area = abs(page.rect)
    covered = (pymupdf.Rect(i["bbox"]) & page.rect for i in images)
    return any(abs(r) >= page_area * MIN_IMAGE_COVER for r in covered)


def image_dpi(page: pymupdf.Page) -> float:
    """ページで最も大きな画像の解像度（dpi）。"""
    best, dpi = 0.0, 150.0
    for i in page.get_image_info():
        r = pymupdf.Rect(i["bbox"])
        if abs(r) > best and r.width > 0:
            best, dpi = abs(r), i["width"] / r.width * 72
    return dpi


def paper_color(page: pymupdf.Page) -> tuple[float, float, float]:
    """紙の色（ページで最も多い明るい色）。見つからなければ白。"""
    pix = page.get_pixmap(dpi=PAPER_DPI, colorspace=pymupdf.csRGB, alpha=False)
    s = pix.samples
    counts: Counter = Counter()
    sums: dict[tuple[int, int, int], list[int]] = {}
    for r, g, b in zip(s[0::3], s[1::3], s[2::3]):
        if r + g + b < 3 * 160:
            continue  # 文字や図の色は除く
        key = (r >> 3, g >> 3, b >> 3)
        counts[key] += 1
        acc = sums.setdefault(key, [0, 0, 0])
        acc[0] += r
        acc[1] += g
        acc[2] += b
    if not counts:
        return (1.0, 1.0, 1.0)
    key, n = counts.most_common(1)[0]
    return tuple(v / n / 255 for v in sums[key])  # type: ignore[return-value]


def cover_rect(line: Rect) -> Rect:
    """画像の中の原文の行を塗りつぶす範囲。OCR の範囲は字形とずれるので、少し広げる。"""
    pad = max(0.8, (line[3] - line[1]) * 0.15)
    return (line[0] - 1.0, line[1] - pad, line[2] + 1.0, line[3] + pad)


def cover(page: pymupdf.Page, rects: list[Rect], color: tuple[float, float, float]) -> None:
    shape = page.new_shape()
    for r in rects:
        rect = pymupdf.Rect(r) & page.rect
        if not rect.is_empty:
            shape.draw_rect(rect)
    shape.finish(color=None, fill=color, width=0)
    shape.commit(overlay=True)
