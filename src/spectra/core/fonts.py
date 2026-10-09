"""PDF 内フォント名の判定（数式・等幅・セリフ）。

フォント名には `ABCDEF+CMMI10` のようなサブセット接頭辞が付くため除去してから判定する。
"""

from __future__ import annotations

import re
from functools import lru_cache

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")

# 数式専用フォント。CMR（Computer Modern Roman）は本文にも使われるため含めない。
_MATH_FONT = re.compile(
    r"""^(
        CMMI | CMBSY | CMSY | CMEX | CMMIB | MSAM | MSBM | EUFM | EUFB | EUSM | EURM | RSFS |
        LMMathItalic | LMMathSymbols | LMMathExtension | LMMath |
        txmi | txsy | txex | pxmi | pxsy | pxex | txsya | pxsya | ntxmi | ntxsy | ntxexx |
        stix.*math | xits.*math | cambria.?math | latinmodern.?math | asana.?math |
        tex.?gyre.*math | fira.?math | garamond.?math | libertinus.?math |
        MTSY | MTMI | MTEX | MT.?Extra | Euclid | esint | stmary | wasy | bbm | dsrom |
        Symbol | SymbolMT | MathematicalPi | Mathematica | MathJax | KaTeX_(?!Main)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

_MONO_FONT = re.compile(
    r"(mono|courier|consol|menlo|inconsolata|source.?code|fira.?code|"
    r"^cmtt|^sftt|^lmmono|^txtt|^pcrr|^nimbusmono|typewriter|^lmtt|code)",
    re.IGNORECASE,
)

_SANS_FONT = re.compile(
    r"(sans|helvet|arial|^cmss|^lmss|^sfss|verdana|tahoma|calibri|segoe|gothic|"
    r"roboto|inter\b|lato|open.?sans|source.?sans|fira.?sans|nimbussan|myriad|frutiger|avenir)",
    re.IGNORECASE,
)

_MATH_CHARS = set(
    "∑∏∫∮√∂∇∞≈≠≡≤≥≪≫∈∉∋⊂⊃⊆⊇∪∩∧∨¬∀∃∅→←↔⇒⇐⇔↦×÷±∓·∘⊗⊕⊙†‡′″ˆ˜⟨⟩‖∥⊤⊥"
    "αβγδεζηθικλμνξοπρστυφχψωϵϑϕϖϱςΓΔΘΛΞΠΣΥΦΨΩ"
)


@lru_cache(maxsize=1024)
def base_name(font: str) -> str:
    return _SUBSET_PREFIX.sub("", font or "")


@lru_cache(maxsize=1024)
def is_math_font(font: str) -> bool:
    return bool(_MATH_FONT.match(base_name(font)))


@lru_cache(maxsize=1024)
def is_mono_font(font: str) -> bool:
    return bool(_MONO_FONT.search(base_name(font)))


@lru_cache(maxsize=1024)
def is_sans_font(font: str) -> bool:
    return bool(_SANS_FONT.search(base_name(font)))


def has_math_chars(text: str) -> bool:
    return any(c in _MATH_CHARS for c in text)


def math_char_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(c in _MATH_CHARS for c in chars) / len(chars)


_CM_TEXT_FONT = re.compile(r"^(CMR|CMBX|CMB\d|CMSL|LMRoman|LMRomanSlant|CMU\s?Serif)", re.IGNORECASE)


def is_math_span(font: str, text: str, body_font: str) -> bool:
    """数式の一部とみなすスパンか。

    本文が Times 等の論文では、数式中の関数名（softmax 等）や数字は CMR で組まれる。
    本文フォント自体が CMR の論文（素の LaTeX）ではこの規則は適用しない。
    """
    if is_math_font(font):
        return True
    t = text.strip()
    if t and len(t) <= 4 and math_char_ratio(t) >= 0.5:
        return True
    name = base_name(font)
    return bool(_CM_TEXT_FONT.match(name)) and not _same_family(name, body_font)


def _same_family(a: str, b: str) -> bool:
    strip = re.compile(r"\d+(\.\d+)?$")
    return strip.sub("", a).lower()[:5] == strip.sub("", base_name(b)).lower()[:5]


# Adobe Symbol エンコーディング（ToUnicode の無い Symbol フォントは英字として取り出される）
_SYMBOL_MAP = str.maketrans(
    "abgdezhqiklmnxoprstufcywjvJVABGDEZHQIKLMNXOPRSTUFCYW",
    "αβγδεζηθικλμνξοπρστυφχψωϕϖϑςΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ",
)


def decode_symbol_font(font: str, text: str) -> str:
    """Symbol フォントのスパンで英字になってしまった文字をギリシャ文字に戻す。"""
    name = base_name(font).lower()
    if not name.startswith("symbol") or not text.isascii():
        return text
    return text.translate(_SYMBOL_MAP)
