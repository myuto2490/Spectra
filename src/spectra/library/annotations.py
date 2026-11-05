"""注釈の値の検証。手元の編集と、ほかの端末から来た値（同期）の両方で使う。"""

from __future__ import annotations

import math

from .models import ANNOTATION_COLORS, ANNOTATION_KINDS, MAX_COMMENT, MAX_QUOTE

MAX_QUADS = 2_000  # 1 件の注釈の行の数
MAX_COORD = 100_000.0  # ページの座標の絶対値の上限（pt）
MAX_PAGE = 100_000


def clean_quads(raw) -> tuple[tuple[float, float, float, float], ...]:
    """行ごとの矩形を検証し、(x0, y0, x1, y1) の左上・右下の順にそろえる。合わなければ ValueError。"""
    if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= MAX_QUADS:
        raise ValueError("quads must be a non-empty list of rectangles")
    out = []
    for q in raw:
        values = tuple(q) if isinstance(q, (list, tuple)) else ()
        if len(values) != 4 or not all(_coord(v) for v in values):
            raise ValueError(f"invalid rectangle: {q!r}")
        x0, y0, x1, y1 = (float(v) for v in values)
        out.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
    return tuple(out)


def check(kind, page, color, quote, comment) -> None:
    if kind not in ANNOTATION_KINDS:
        raise ValueError(f"unknown annotation kind: {kind!r}")
    if color not in ANNOTATION_COLORS:
        raise ValueError(f"unknown annotation color: {color!r}")
    if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page < MAX_PAGE:
        raise ValueError(f"invalid page: {page!r}")
    if not isinstance(quote, str) or len(quote) > MAX_QUOTE:
        raise ValueError(f"quote must be a string of at most {MAX_QUOTE} characters")
    if not isinstance(comment, str) or len(comment) > MAX_COMMENT:
        raise ValueError(f"comment must be a string of at most {MAX_COMMENT} characters")


def _coord(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and abs(value) <= MAX_COORD
