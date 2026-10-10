"""コア処理で共有するデータ構造。

座標はすべて PDF のページ座標系（PyMuPDF と同じ、左上原点・単位 pt）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

Rect = tuple[float, float, float, float]
Point = tuple[float, float]


@dataclass(frozen=True)
class Span:
    """同一書式の連続した文字列。"""

    text: str
    font: str
    size: float
    color: int  # 0xRRGGBB
    flags: int
    bbox: Rect
    origin: Point  # ベースライン始点

    @property
    def is_bold(self) -> bool:
        return bool(self.flags & 16) or _name_has(self.font, ("bold", "black", "heavy", "semibold", "medi"))

    @property
    def is_italic(self) -> bool:
        return bool(self.flags & 2) or _name_has(self.font, ("italic", "oblique", "-it", "ital"))


def _name_has(font: str, keys: tuple[str, ...]) -> bool:
    low = font.lower()
    return any(k in low for k in keys)


@dataclass
class Line:
    spans: list[Span]
    bbox: Rect
    direction: Point = (1.0, 0.0)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def baseline(self) -> float:
        main = max(self.spans, key=lambda s: len(s.text.strip()) * s.size)
        return main.origin[1]


@dataclass
class TextBlock:
    page: int
    index: int
    bbox: Rect
    lines: list[Line]

    @property
    def spans(self) -> list[Span]:
        return [s for ln in self.lines for s in ln.spans]

    @property
    def text(self) -> str:
        return " ".join(ln.text for ln in self.lines)

    @property
    def char_count(self) -> int:
        return sum(len(s.text.strip()) for s in self.spans)


class SkipReason(str, Enum):
    ROTATED = "rotated"
    FIGURE = "figure"
    TABLE = "table"
    MATH = "math"
    NO_WORDS = "no_words"
    NUMERIC = "numeric"
    CODE = "code"
    ALREADY_JAPANESE = "already_japanese"
    REFERENCES = "references"
    AUTHORS = "authors"
    RUNNING_HEADER = "running_header"
    OVERLAP = "overlap"  # 訳文に置き換えると、重なっている翻訳対象外の文字まで消えてしまう
    CAPTION_DISABLED = "caption_disabled"
    FOOTNOTE_DISABLED = "footnote_disabled"
    OUT_OF_RANGE = "out_of_range"


class BlockKind(str, Enum):
    BODY = "body"
    HEADING = "heading"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    TITLE = "title"


@dataclass
class BlockDecision:
    block: TextBlock
    kind: BlockKind
    skip: SkipReason | None = None

    @property
    def translate(self) -> bool:
        return self.skip is None


class Align(str, Enum):
    JUSTIFY = "justify"
    LEFT = "left"
    CENTER = "center"


@dataclass(frozen=True)
class TextStyle:
    """段落全体の基本書式。"""

    size: float
    color: int
    bold: bool
    serif: bool
    line_pitch: float  # ベースライン間隔
    first_indent: float  # 1 行目の字下げ量
    align: Align


@dataclass(frozen=True)
class InlinePiece:
    """保護対象（数式・引用番号・上付き文字など）を構成する 1 スパン分の情報。"""

    text: str
    size_ratio: float  # 段落の基本サイズに対する比
    rise_ratio: float  # ベースラインからの持ち上げ量 / 基本サイズ（上付きは正）
    italic: bool
    bold: bool
    color: int
    math: bool
    dx_ratio: float | None = None  # 塊の左端からの横位置 / 基本サイズ（分数など 2 次元配置の再現用）


@dataclass(frozen=True)
class InlineRun:
    """訳文中で 1 つのプレースホルダとして扱う、分割不可の塊。"""

    pieces: tuple[InlinePiece, ...]
    width_ratio: float | None = None  # 原文での塊の幅 / 基本サイズ。None なら順に並べる
    # 原文での位置（根号の上線や分数の横線などのベクター線を一緒に移動するため）
    page: int | None = None
    bbox: Rect | None = None
    origin: Point | None = None  # (左端 x, ベースライン y)
    base_size: float | None = None

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.pieces)


@dataclass
class Region:
    """訳文を流し込む 1 つの矩形領域（段落が段やページを跨ぐ場合は複数）。"""

    page: int
    rect: Rect
    line_rects: list[Rect]
    style: TextStyle
    source_chars: int  # 原文の文字数
    block_index: int
    first_baseline: float | None = None  # 原文 1 行目のベースライン
    # 各行の文字の並び（スパン）の範囲。翻訳対象外の文字と重なる行だけ、行全体ではなくスパン単位で消す
    span_rects: list[list[Rect]] = field(default_factory=list)


@dataclass
class Segment:
    """翻訳の単位。"""

    id: str
    kind: BlockKind
    source: str  # プレースホルダ入りのマークアップ文字列
    runs: dict[int, InlineRun]
    regions: list[Region]
    translated: str | None = None

    @property
    def plain_length(self) -> int:
        return len(self.source)


@dataclass
class PageAnalysis:
    page: int
    width: float
    height: float
    decisions: list[BlockDecision]
    figure_rects: list[Rect] = field(default_factory=list)
    table_rects: list[Rect] = field(default_factory=list)
    drawing_rects: list[Rect] = field(default_factory=list)  # ベクター描画の範囲（領域拡張の障害物の判定に使う）


@dataclass
class DocumentAnalysis:
    pages: list[PageAnalysis]
    segments: list[Segment]
    body_size: float

    @property
    def translatable_chars(self) -> int:
        return sum(len(s.source) for s in self.segments)


# ---- 矩形ユーティリティ -------------------------------------------------


def rect_area(r: Rect) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def rect_intersect(a: Rect, b: Rect) -> Rect:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def rect_union(a: Rect, b: Rect) -> Rect:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def overlap_ratio(inner: Rect, outer: Rect) -> float:
    """inner の面積のうち outer に含まれる割合。"""
    area = rect_area(inner)
    if area <= 0:
        return 0.0
    return rect_area(rect_intersect(inner, outer)) / area


def erase_rect(line: Rect) -> Rect:
    """原文の文字（行・スパン）を消すときの範囲。上下を少し縮め、隣接する行の文字を巻き込まないようにする。"""
    shrink = (line[3] - line[1]) * 0.18
    return (line[0] - 0.2, line[1] + shrink, line[2] + 0.2, line[3] - shrink)


def inflate(r: Rect, dx: float, dy: float | None = None) -> Rect:
    dy = dx if dy is None else dy
    return (r[0] - dx, r[1] - dy, r[2] + dx, r[3] + dy)
