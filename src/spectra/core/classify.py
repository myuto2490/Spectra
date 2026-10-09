"""テキストブロックごとに「翻訳するか」「どの種類か」を判定する。

基本方針は「迷ったら訳さない」。翻訳しないブロックは PDF 上で一切変更されないため、
誤って訳さないことによる損失は、誤って図や数式を壊すことよりずっと小さい。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

import pymupdf

from .extract import SECTION_NAME, extract_blocks, find_figure_rects, find_table_rects
from .fonts import base_name, is_math_font, is_math_span, is_mono_font, math_char_ratio
from .models import (
    BlockDecision,
    BlockKind,
    PageAnalysis,
    SkipReason,
    TextBlock,
    inflate,
    overlap_ratio,
)
from .options import AnalysisOptions

_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*[A-Za-z]")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")
_URL_OR_MAIL = re.compile(r"(https?://\S+|www\.\S+|\S+@\S+\.\w+)")
# 番号の後に区切り記号が続くものだけをキャプションとみなす（"Figure 1 shows ..." は本文）
_CAPTION = re.compile(
    r"^\s*(figure|fig\.|table|tab\.|algorithm|listing|scheme|chart|plate|exhibit)\s*"
    r"(s\d+|[a-z]?\d+|[ivxlc]+)[a-z]?\s*[:.|\-–—]",
    re.IGNORECASE,
)
# 擬似コード（アルゴリズムの枠）の行： "1:" "12:" や "Input:" "Require:" で始まる
_PSEUDOCODE = re.compile(r"^\s*(\d{1,2}:|(input|output|require|ensure|return)\s*:)", re.IGNORECASE)
_EQUATION_SIGN = re.compile(r"[=≈≤≥<>∝≡]")
_LONG_WORD = re.compile(r"[A-Za-z]{4,}")
_ABSTRACT = re.compile(r"^\s*(abstract|summary)\b", re.IGNORECASE)
_ABSTRACT_HEADING = re.compile(r"^\s*(abstract|summary)\s*[.:—-]?\s*$", re.IGNORECASE)
_SENTENCE = re.compile(r"[a-z]{2,}[,;]?\s+[a-z]{2,}\s+[a-z]{2,}.*[.]\s*$")
_REFERENCES = re.compile(
    r"^\s*(\d+\.?|[ivx]+\.)?\s*(references|bibliography|literature\s+cited|works\s+cited|参考文献)\s*$",
    re.IGNORECASE,
)


class BlockStats:
    """判定に使うブロックの統計量。"""

    __slots__ = (
        "chars", "math_chars", "mono_chars", "letters", "digits", "cjk",
        "words", "size", "all_bold", "rotated", "text",
    )

    def __init__(self, block: TextBlock, body_font: str = ""):
        self.chars = self.math_chars = self.mono_chars = 0
        self.letters = self.digits = self.cjk = 0
        size_weight: dict[float, int] = defaultdict(int)
        plain_parts: list[str] = []
        bold_chars = 0
        has_math = any(is_math_font(s.font) for s in block.spans)
        for span in block.spans:
            t = span.text.strip()
            n = len(t)
            if not n:
                continue
            self.chars += n
            if is_math_font(span.font) or (has_math and is_math_span(span.font, span.text, body_font)):
                self.math_chars += n
                continue
            if is_mono_font(span.font):
                self.mono_chars += n
            if span.is_bold:
                bold_chars += n
            size_weight[round(span.size, 1)] += n
            plain_parts.append(span.text)
        plain = " ".join(plain_parts)
        self.math_chars += int(math_char_ratio(plain) * len(plain.replace(" ", "")))
        self.text = block.text.strip()
        no_links = _URL_OR_MAIL.sub(" ", plain)
        self.words = len(_WORD.findall(no_links))
        self.letters = sum(c.isascii() and c.isalpha() for c in no_links)
        self.digits = sum(c.isdigit() for c in no_links)
        self.cjk = len(_CJK.findall(plain))
        self.size = max(size_weight, key=size_weight.get) if size_weight else block.spans[0].size
        non_math = self.chars - self.math_chars
        self.all_bold = non_math > 0 and bold_chars >= non_math * 0.9
        self.rotated = any(abs(ln.direction[1]) > 1e-3 or ln.direction[0] < 0 for ln in block.lines)


def estimate_body_size(blocks: Iterable[TextBlock]) -> float:
    """文字数で重み付けした最頻の文字サイズ＝本文サイズ。"""
    weight: dict[float, int] = defaultdict(int)
    for b in blocks:
        for s in b.spans:
            if not is_math_font(s.font):
                weight[round(s.size * 2) / 2] += len(s.text.strip())
    return max(weight, key=weight.get) if weight else 10.0


def estimate_body_font(blocks: Iterable[TextBlock]) -> str:
    weight: dict[str, int] = defaultdict(int)
    for b in blocks:
        for s in b.spans:
            if not is_math_font(s.font):
                weight[base_name(s.font)] += len(s.text.strip())
    return max(weight, key=weight.get) if weight else ""


def _tabular_blocks(blocks: list[TextBlock], stats: dict[int, BlockStats]) -> set[int]:
    """同じベースラインに 3 つ以上の短い 1 行ブロックが並んでいる＝表の行や図のラベル。"""
    rows: dict[int, list[TextBlock]] = defaultdict(list)
    for b in blocks:
        if len(b.lines) == 1 and stats[b.index].words <= 6:
            rows[round(b.lines[0].baseline / 1.5)].append(b)
    result: set[int] = set()
    for members in rows.values():
        if len(members) >= 3:
            result.update(b.index for b in members)
    return result


def has_column_gaps(block: TextBlock, size: float) -> bool:
    """行の大半に大きな列間隔がある＝罫線の無い表。"""
    if len(block.lines) < 2:
        return False
    gapped = 0
    for ln in block.lines:
        spans = [s for s in ln.spans if s.text.strip()]
        gaps = sum(1 for a, b in zip(spans, spans[1:]) if b.bbox[0] - a.bbox[2] > size * 1.5)
        if gaps >= 1:
            gapped += 1
    return gapped >= len(block.lines) * 0.6


def _looks_like_equation(block: TextBlock, st: BlockStats) -> bool:
    """数式フォントを使わずに組まれた独立数式（Word の論文に多い）。

    1 行で、等号などを含み、長い英単語がほとんど無いもの。例: "s = min(1, w / w0)   (1)"
    """
    if len(block.lines) != 1 or not _EQUATION_SIGN.search(st.text):
        return False
    return len(_LONG_WORD.findall(st.text)) <= 1 and st.words <= 4


class DocumentClassifier:
    """ページを順に解析し、参考文献セクションなど文書全体の状態を引き継ぐ。"""

    def __init__(self, options: AnalysisOptions, body_size: float, body_font: str = ""):
        self.options = options
        self.body_size = body_size
        self.body_font = body_font
        self.in_references = False
        # 1 ページ目のタイトル〜概要の間（著者・所属）
        self.seen_title = False
        self.seen_abstract = False
        self._page_height = 842.0

    def analyze_page(self, page: pymupdf.Page, blocks: list[TextBlock] | None = None) -> PageAnalysis:
        blocks = extract_blocks(page) if blocks is None else blocks
        figures = find_figure_rects(page)
        tables = find_table_rects(page)
        stats = {b.index: BlockStats(b, self.body_font) for b in blocks}
        tabular = _tabular_blocks(blocks, stats)
        height = float(page.rect.height)
        self._page_height = height
        front = self._front_matter(page.number, blocks, stats)
        decisions = []
        for b in blocks:
            st = stats[b.index]
            kind = self._kind(b, st, height)
            if b.index in front:
                kind, skip = front[b.index]
            else:
                skip = self._skip(b, st, kind, figures, tables, b.index in tabular)
            decisions.append(BlockDecision(block=b, kind=kind, skip=skip))
        return PageAnalysis(
            page=page.number,
            width=float(page.rect.width),
            height=height,
            decisions=decisions,
            figure_rects=figures,
            table_rects=tables,
        )

    def _front_matter(
        self, pno: int, blocks: list[TextBlock], stats: dict[int, BlockStats]
    ) -> dict[int, tuple[BlockKind, SkipReason | None]]:
        """1 ページ目の Abstract 見出しより前（タイトル・著者・所属・日付）を判定する。

        その範囲で最も大きい文字の行をタイトルとして訳し、それ以外（著者・所属など）は残す。
        Abstract の見出しが見つからない論文では何もしない（従来の判定に任せる）。
        """
        if pno != 0 or self.options.first_page > 0:
            return {}
        idx = next((i for i, b in enumerate(blocks) if _ABSTRACT_HEADING.match(stats[b.index].text)), None)
        if idx is None or idx == 0:
            return {}
        front = [b for b in blocks[:idx] if not stats[b.index].rotated and stats[b.index].words]
        if not front:
            return {}
        title_size = max(stats[b.index].size for b in front)
        result: dict[int, tuple[BlockKind, SkipReason | None]] = {}
        for b in front:
            if stats[b.index].size >= title_size - 0.5:
                result[b.index] = (BlockKind.TITLE, None)
            else:
                result[b.index] = (BlockKind.BODY, SkipReason.AUTHORS)
        self.seen_title = self.seen_abstract = True
        return result

    # -- 種類 ------------------------------------------------------------

    def _kind(self, b: TextBlock, st: BlockStats, page_height: float) -> BlockKind:
        body = self.body_size
        if _CAPTION.match(st.text):
            return BlockKind.CAPTION
        if b.page == 0 and st.size >= body * 1.35:
            return BlockKind.TITLE
        if (st.all_bold or st.size >= body * 1.15) and st.words <= 18 and len(b.lines) <= 3:
            return BlockKind.HEADING
        if len(b.lines) == 1 and SECTION_NAME.match(st.text):
            return BlockKind.HEADING  # 太字でなくても "ABSTRACT" "References" などは見出し
        if st.size <= body * 0.9 and b.bbox[1] > page_height * 0.6:
            return BlockKind.FOOTNOTE
        return BlockKind.BODY

    # -- 除外判定 ---------------------------------------------------------

    def _skip(
        self,
        b: TextBlock,
        st: BlockStats,
        kind: BlockKind,
        figures: list,
        tables: list,
        tabular: bool,
    ) -> SkipReason | None:
        opts = self.options
        if b.page < opts.first_page or (opts.last_page is not None and b.page > opts.last_page):
            return SkipReason.OUT_OF_RANGE
        if st.rotated:
            return SkipReason.ROTATED
        # ページ上端の柱（"Smith et al." や雑誌名など）
        if b.bbox[3] < self._page_height * 0.07 and len(b.lines) == 1 and (st.words <= 4 or "et al" in st.text):
            return SkipReason.RUNNING_HEADER

        if b.page == 0 and not self.seen_abstract:
            if _ABSTRACT.match(st.text):
                self.seen_abstract = True
            elif kind == BlockKind.TITLE:
                self.seen_title = True
                return None
            elif self.seen_title and st.words <= 15 and not _SENTENCE.search(st.text):
                return SkipReason.AUTHORS

        # 参考文献セクションの開始・終了
        if _REFERENCES.match(st.text):
            self.in_references = True
            return None  # 見出し自体は訳す
        if self.in_references:
            if kind in (BlockKind.HEADING, BlockKind.TITLE) and not st.text.startswith("["):
                self.in_references = False
            elif opts.skip_references:
                return SkipReason.REFERENCES

        fig_overlap = max((overlap_ratio(b.bbox, inflate(r, 4)) for r in figures), default=0.0)
        if kind == BlockKind.CAPTION:
            if fig_overlap >= 0.9:
                return SkipReason.FIGURE
        else:
            if fig_overlap >= 0.5:
                return SkipReason.FIGURE
            # 図の近くにある小さな文字は図中のラベル
            near_fig = any(overlap_ratio(b.bbox, inflate(r, 20)) > 0 for r in figures)
            if near_fig and st.size < self.body_size * 0.85 and st.words <= 8:
                return SkipReason.FIGURE
            # 図のすぐ上下・左右にある短い 1 行（図のタイトルや軸ラベル）
            adjacent = any(overlap_ratio(b.bbox, inflate(r, 12)) > 0 for r in figures)
            if (
                adjacent
                and kind not in (BlockKind.HEADING, BlockKind.TITLE)
                and len(b.lines) == 1
                and st.words <= 8
                and not st.text.rstrip().endswith((".", ":"))
            ):
                return SkipReason.FIGURE
        if kind != BlockKind.CAPTION and (
            tabular
            or any(overlap_ratio(b.bbox, r) >= 0.5 for r in tables)
            or has_column_gaps(b, st.size)
        ):
            return SkipReason.TABLE

        if st.chars and st.math_chars / st.chars > 0.5:
            return SkipReason.MATH
        if _looks_like_equation(b, st):
            return SkipReason.MATH
        if st.cjk > max(3, st.letters * 0.3):
            return SkipReason.ALREADY_JAPANESE
        if st.words == 0:
            return SkipReason.NO_WORDS
        if st.chars and st.mono_chars / st.chars > 0.5:
            return SkipReason.CODE
        if _PSEUDOCODE.match(st.text):
            return SkipReason.CODE
        if st.letters + st.digits and st.letters / (st.letters + st.digits) < 0.4 and st.words < 4:
            return SkipReason.NUMERIC

        if kind == BlockKind.CAPTION and not opts.translate_captions:
            return SkipReason.CAPTION_DISABLED
        if kind == BlockKind.FOOTNOTE and not opts.translate_footnotes:
            return SkipReason.FOOTNOTE_DISABLED
        return None
