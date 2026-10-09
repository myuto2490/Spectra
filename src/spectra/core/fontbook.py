"""訳文の描画に使うフォント一式。

- 和文: 内蔵の Droid Sans Fallback（追加ファイル不要）または指定フォントファイル
- 欧文: 和文の書風に合わせて Times 系 / Helvetica 系（PDF 標準フォント、サブセット埋め込み）
- 数式の斜体: Times Italic
"""

from __future__ import annotations

import logging
from enum import Enum
from functools import lru_cache
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)


class FontRole(str, Enum):
    JP = "jp"
    LATIN = "latin"
    LATIN_BOLD = "latin_bold"
    LATIN_ITALIC = "latin_italic"
    MATH_ITALIC = "math_italic"
    MATH = "math"


class FontBook:
    def __init__(self, jp_font_path: str | None = None, serif: bool = False):
        self.jp_font_path = jp_font_path
        self.jp = self._load_jp(jp_font_path)
        self.serif = serif
        latin, bold, italic = ("tiro", "tibo", "tiit") if serif else ("helv", "hebo", "heit")
        self.fonts: dict[FontRole, pymupdf.Font] = {
            FontRole.JP: self.jp,
            FontRole.LATIN: pymupdf.Font(latin),
            FontRole.LATIN_BOLD: pymupdf.Font(bold),
            FontRole.LATIN_ITALIC: pymupdf.Font(italic),
            # 数式は論文の慣習に合わせ、和文の書風によらず Times 系
            FontRole.MATH_ITALIC: pymupdf.Font("tiit"),
            FontRole.MATH: pymupdf.Font("tiro"),
        }
        self._unit_width = lru_cache(maxsize=65536)(self._unit_width_uncached)

    @staticmethod
    def _load_jp(path: str | None) -> pymupdf.Font:
        if path:
            try:
                if Path(path).is_file():
                    font = pymupdf.Font(fontfile=path)
                    if font.has_glyph(ord("あ")):
                        return font
                    log.warning("font has no Japanese glyphs, falling back: %s", path)
            except Exception:
                log.warning("failed to load font, falling back: %s", path, exc_info=True)
        return pymupdf.Font("cjk")

    def font(self, role: FontRole) -> pymupdf.Font:
        return self.fonts[role]

    def resolve(self, ch: str, role: FontRole) -> FontRole:
        """その文字を描けるフォントを選ぶ（無ければ和文フォント → 欧文標準の順に代替）。"""
        if self.fonts[role].has_glyph(ord(ch)):
            return role
        if role != FontRole.JP and self.jp.has_glyph(ord(ch)):
            return FontRole.JP
        if self.fonts[FontRole.MATH].has_glyph(ord(ch)):
            return FontRole.MATH
        return role

    def split_by_coverage(self, text: str, role: FontRole) -> list[tuple[str, FontRole]]:
        """グリフの有無でテキストを分割する。"""
        parts: list[tuple[str, FontRole]] = []
        for ch in text:
            r = self.resolve(ch, role)
            if parts and parts[-1][1] == r:
                parts[-1] = (parts[-1][0] + ch, r)
            else:
                parts.append((ch, r))
        return parts

    def width(self, text: str, role: FontRole, size: float) -> float:
        return self._unit_width(text, role) * size

    def _unit_width_uncached(self, text: str, role: FontRole) -> float:
        return self.fonts[role].text_length(text, fontsize=1.0)
