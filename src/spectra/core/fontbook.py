"""訳文の描画に使うフォント一式。

- 和文: 内蔵の Droid Sans Fallback（追加ファイル不要）または指定フォントファイル
- 欧文: 和文の書風に合わせて Times 系 / Helvetica 系（PDF 標準フォント、サブセット埋め込み）
- 数式の斜体: Times Italic

外部フォントファイルは、MuPDF がサブセット化できない形式（CFF の OTF など）があるため、
fontTools で使用文字だけのサブセットを作ってから埋め込む。
"""

from __future__ import annotations

import io
import logging
from enum import Enum
from functools import lru_cache
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)

_FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
BUNDLED_REGULAR = _FONTS_DIR / "NotoSansJP-Regular.otf"
BUNDLED_BOLD = _FONTS_DIR / "NotoSansJP-Bold.otf"

# 未使用でも含めておく文字（記号の代替などで後から使われうる）
_BASE_CHARS = "".join(chr(c) for c in range(0x20, 0x7F)) + "、。，．・：；？！「」『』（）［］〈〉《》—…‥ー"


class FontRole(str, Enum):
    JP = "jp"
    JP_BOLD = "jp_bold"
    LATIN = "latin"
    LATIN_BOLD = "latin_bold"
    LATIN_ITALIC = "latin_italic"
    MATH_ITALIC = "math_italic"
    MATH = "math"


def _load_file_font(path: str | None) -> pymupdf.Font | None:
    if not path:
        return None
    try:
        if Path(path).is_file():
            font = pymupdf.Font(fontfile=path)
            if font.has_glyph(ord("あ")):
                return font
            log.warning("font has no Japanese glyphs, ignored: %s", path)
    except Exception:
        log.warning("failed to load font, ignored: %s", path, exc_info=True)
    return None


def subset_font_file(path: str, text: str) -> bytes | None:
    """text に含まれる文字だけのサブセットフォントを作る。失敗したら None。

    CFF アウトラインの OpenType は、サブセット後に MuPDF が描画できないため
    TrueType アウトラインへ変換する（サブセット後なので高速）。
    """
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont

        font = TTFont(path, lazy=False, fontNumber=0)
        if "CFF2" in font or "fvar" in font:
            return None  # 可変フォントは非対応（そのまま埋め込む）
        opts = subset.Options()
        opts.name_IDs = ["*"]
        opts.notdef_outline = True
        opts.layout_features = ["*"]
        opts.hinting = False
        subsetter = subset.Subsetter(opts)
        subsetter.populate(text=text + _BASE_CHARS)
        subsetter.subset(font)
        if "CFF " in font:
            _cff_to_glyf(font)
        buf = io.BytesIO()
        font.save(buf)
        return buf.getvalue()
    except Exception:
        log.warning("font subsetting failed: %s", path, exc_info=True)
        return None


def _cff_to_glyf(font) -> None:
    """CFF（3 次ベジェ）のアウトラインを TrueType（2 次ベジェ）に変換する。"""
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import newTable

    order = font.getGlyphOrder()
    glyph_set = font.getGlyphSet()
    glyf = newTable("glyf")
    glyf.glyphOrder = order
    glyf.glyphs = {}
    for name in order:
        pen = TTGlyphPen(glyph_set)
        glyph_set[name].draw(Cu2QuPen(pen, max_err=1.0, reverse_direction=True))
        glyf.glyphs[name] = pen.glyph()
    font["loca"] = newTable("loca")
    font["glyf"] = glyf
    for tag in ("CFF ", "VORG"):
        if tag in font:
            del font[tag]
    maxp = font["maxp"]
    maxp.tableVersion = 0x00010000
    for attr in (
        "maxTwilightPoints", "maxStorage", "maxFunctionDefs", "maxInstructionDefs",
        "maxStackElements", "maxSizeOfInstructions", "maxComponentElements",
    ):
        setattr(maxp, attr, 0)
    maxp.maxZones = 1
    font["head"].glyphDataFormat = 0
    post = font["post"]
    post.formatType = 2.0
    post.extraNames = []
    post.mapping = {}
    post.glyphOrder = order
    font.sfntVersion = "\x00\x01\x00\x00"


class FontBook:
    def __init__(self, jp_font_path: str | None = None, serif: bool = False, jp_bold_path: str | None = None):
        self.jp_font_path = jp_font_path
        self.jp_bold_path = jp_bold_path
        file_font = _load_file_font(jp_font_path)
        self._external = file_font is not None
        if file_font is None and jp_font_path is None and BUNDLED_REGULAR.is_file():
            # 指定が無ければ同梱の Noto Sans JP（日本語の字形）を使う
            jp_font_path = str(BUNDLED_REGULAR)
            jp_bold_path = jp_bold_path or (str(BUNDLED_BOLD) if BUNDLED_BOLD.is_file() else None)
            self.jp_font_path, self.jp_bold_path = jp_font_path, jp_bold_path
            file_font = _load_file_font(jp_font_path)
            self._external = file_font is not None
        # 最後の予備：MuPDF 内蔵の Droid Sans Fallback（漢字は中国語の字形になる）
        self.jp = file_font or pymupdf.Font("cjk")
        self.jp_bold = _load_file_font(jp_bold_path) if self._external else None
        self.serif = serif
        latin, bold, italic = ("tiro", "tibo", "tiit") if serif else ("helv", "hebo", "heit")
        self.fonts: dict[FontRole, pymupdf.Font] = {
            FontRole.JP: self.jp,
            FontRole.JP_BOLD: self.jp_bold or self.jp,
            FontRole.LATIN: pymupdf.Font(latin),
            FontRole.LATIN_BOLD: pymupdf.Font(bold),
            FontRole.LATIN_ITALIC: pymupdf.Font(italic),
            # 数式は論文の慣習に合わせ、和文の書風によらず Times 系
            FontRole.MATH_ITALIC: pymupdf.Font("tiit"),
            FontRole.MATH: pymupdf.Font("tiro"),
        }
        self._unit_width = lru_cache(maxsize=65536)(self._unit_width_uncached)

    @property
    def has_real_bold(self) -> bool:
        return self.jp_bold is not None

    def jp_role(self, bold: bool) -> FontRole:
        return FontRole.JP_BOLD if bold and self.has_real_bold else FontRole.JP

    def prepare(self, text: str) -> None:
        """外部フォントを text の文字だけのサブセットに差し替える（字幅は変わらない）。"""
        if not self._external:
            return
        for role, path in ((FontRole.JP, self.jp_font_path), (FontRole.JP_BOLD, self.jp_bold_path)):
            if role == FontRole.JP_BOLD and not self.has_real_bold:
                continue
            data = subset_font_file(path, text) if path else None
            if data:
                font = pymupdf.Font(fontbuffer=data)
                self.fonts[role] = font
                if role == FontRole.JP:
                    self.jp = font
                    if not self.has_real_bold:
                        self.fonts[FontRole.JP_BOLD] = font
                else:
                    self.jp_bold = font

    def font(self, role: FontRole) -> pymupdf.Font:
        return self.fonts[role]

    def resolve(self, ch: str, role: FontRole) -> FontRole:
        """その文字を描けるフォントを選ぶ（無ければ和文フォント → 欧文標準の順に代替）。"""
        if self.fonts[role].has_glyph(ord(ch)):
            return role
        if role not in (FontRole.JP, FontRole.JP_BOLD) and self.jp.has_glyph(ord(ch)):
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
