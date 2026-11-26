"""PDF 内フォント名の判定（数式・等幅・セリフ）。

フォント名には `ABCDEF+CMMI10` のようなサブセット接頭辞が付くため除去してから判定する。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")

# 数式専用フォント。CMR（Computer Modern Roman）は本文にも使われるため含めない。
# Adv* は Elsevier の古い組版の記号フォント（"=" が "¼"、"−" が "\x03"、根号の上線が "ﬃ" として取り出される）。
# AdvEls-ent* は 1 字ずつの特殊文字のフォントで、"°" が "1"、"±" が "7"、"∼" が "X" のように、論文ごとに別の文字として
# 取り出される。正しい文字に対応づけられないので、数式と同じく元の字形のまま残す。
# 番号だけの名前の AdvP4C4E74 などは、論文によって本文の Times のこともあるので、ここには含めず文書ごとに決める（_ADV_NUMBERED）
_MATH_FONT = re.compile(
    r"""^(
        CMMI | CMBSY | CMSY | CMEX | CMMIB | MSAM | MSBM | EUFM | EUFB | EUSM | EURM | RSFS |
        LMMathItalic | LMMathSymbols | LMMathExtension | LMMath |
        r?txmi | r?txbmi | r?txsy | r?txex | r?pxmi | r?pxbmi | r?pxsy | r?pxex | txsya | pxsya | ntxmi | ntxsy | ntxexx |
        stix.*math | xits.*math | cambria.?math | latinmodern.?math | asana.?math |
        tex.?gyre.*math | fira.?math | garamond.?math | libertinus.?math |
        MTSY | MTMI | MTEX | MT.?Extra | Euclid | esint | stmary | wasy | bbm | dsrom |
        Symbol | SymbolMT | MathematicalPi | Mathematica | MathJax | KaTeX_(?!Main) |
        Adv(PSMP | PSSym | PSMSAM | PSMSBM | MTSY | MTMI | MTEX | Pi\d | MacMthSy\w* | Els-ent\d+)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# 名前が番号だけの Elsevier のフォント。記号フォント（AdvP4C4E74 など）のことも、本文の Times（AdvP4DF60E など）のこともある
_ADV_NUMBERED = re.compile(r"^AdvP[0-9A-F]{6}$")
# 文書の中で文章を組んでいるとわかった、番号だけの名前のフォント（find_text_fonts）。解析の間だけ設定する
_TEXT_FONTS: ContextVar[frozenset[str]] = ContextVar("text_fonts", default=frozenset())

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


def is_math_font(font: str) -> bool:
    name = base_name(font)
    if _ADV_NUMBERED.match(name):
        return name not in _TEXT_FONTS.get()
    return _is_named_math_font(name)


@lru_cache(maxsize=1024)
def _is_named_math_font(name: str) -> bool:
    return bool(_MATH_FONT.match(name))


_WORD = re.compile(r"[A-Za-z]{2,}")
_LIGATURES = re.compile(r"(ff|fi|ffi|fl|ffl)+")  # 根号の上線は "ffiffi…" として取り出される
_MIN_DISTINCT_WORDS = 5


def find_text_fonts(spans: Iterable[tuple[str, str]]) -> frozenset[str]:
    """番号だけの名前の Elsevier のフォントのうち、文章を組んでいるもの（異なる英単語が 5 つ以上）。

    spans は (フォント名, 文字列) の並び。記号フォントの文字は "¼" "\x03" "ﬃ" などで、英単語にならない。
    """
    words: dict[str, set[str]] = defaultdict(set)
    for font, text in spans:
        name = base_name(font)
        if _ADV_NUMBERED.match(name) and len(words[name]) < _MIN_DISTINCT_WORDS:
            words[name].update(w.lower() for w in _WORD.findall(text) if not _LIGATURES.fullmatch(w))
    return frozenset(name for name, ws in words.items() if len(ws) >= _MIN_DISTINCT_WORDS)


def has_numbered_adv_fonts(font_names: Iterable[str]) -> bool:
    return any(_ADV_NUMBERED.match(base_name(f)) for f in font_names)


@contextmanager
def text_fonts(names: frozenset[str]) -> Iterator[None]:
    """この中では、names のフォントを（名前が記号フォントに似ていても）本文のフォントとして扱う。"""
    token = _TEXT_FONTS.set(names)
    try:
        yield
    finally:
        _TEXT_FONTS.reset(token)


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


_GARBLED = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ue000-\uf8ff]")

_CM_TEXT_FONT = re.compile(r"^(CMR|CMBX|CMB\d|CMSL|LMRoman|LMRomanSlant|CMU\s?Serif)", re.IGNORECASE)
# txfonts・pxfonts の数式の立体（"+" "=" "(" や数字）。本文の合字 "ff" "ffi" や "Keck/LWS" の "/" もこのフォントで
# 組まれるので、英字と単独の "/" は数式としない
_TX_MATH_ROMAN = re.compile(r"^r[tp]xr$", re.IGNORECASE)


def is_math_span(font: str, text: str, body_font: str) -> bool:
    """数式の一部とみなすスパンか。

    本文が Times 等の論文では、数式中の関数名（softmax 等）や数字は CMR で組まれる。
    本文フォント自体が CMR の論文（素の LaTeX）ではこの規則は適用しない。
    """
    if is_math_font(font):
        return True
    t = text.strip()
    if t and len(t) <= 4 and _GARBLED.search(t):
        return True  # 制御文字・私用領域の文字は、文字の対応が壊れた記号（訳文に出せないので元の字形のまま残す）
    if t and len(t) <= 4 and math_char_ratio(t) >= 0.5:
        return True
    name = base_name(font)
    if _TX_MATH_ROMAN.match(name):
        return bool(t) and t != "/" and not any(c.isalpha() for c in t) and not _same_family(name, body_font)
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
