"""翻訳対象ブロックを翻訳単位（Segment）に変換する。"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, replace

from .fonts import base_name, decode_symbol_font, is_math_font, is_math_span, is_sans_font
from .markup import escape, placeholder, to_plain
from .models import (
    Align,
    BlockDecision,
    BlockKind,
    InlinePiece,
    InlineRun,
    Line,
    Region,
    Segment,
    Span,
    TextBlock,
    TextStyle,
)

# 数式の前後に付く記号・数字（CMR の "=" "6" "(" など）
_OPERATOR_ONLY = re.compile(r"^[\s0-9=+\-−×·/()\[\]{},.;:|<>≤≥≠≈∼~^_′'!*]+$")
_CITATION = re.compile(r"\[\d+(?:\s*[,–\-]\s*\d+)*\]")
_MATH_ITALIC = re.compile(r"^(CMMI|CMBX?MI|LMMathItalic|txmi|pxmi|ntxmi|MTMI)|Math.?Italic", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?:。．！？：]\s*(<x id=\"\d+\"/>)?\s*$")


@dataclass
class _Piece:
    """セグメント化途中の要素：通常テキストか保護対象スパンか。"""

    span: Span
    text: str
    protected: bool
    baseline: float
    exact: bool = True  # span の bbox がこの text の範囲と一致するか（部分文字列なら False）


# ---- 書式の推定 ------------------------------------------------------------


def _dominant(values: list[tuple[object, int]], default):
    c: Counter = Counter()
    for v, w in values:
        c[v] += w
    return c.most_common(1)[0][0] if c else default


def block_style(block: TextBlock, kind: BlockKind, page_width: float) -> TextStyle:
    plain = [s for s in block.spans if s.text.strip() and not is_math_font(s.font)] or block.spans
    size = _dominant([(round(s.size, 2), len(s.text.strip())) for s in plain], plain[0].size)
    color = _dominant([(s.color, len(s.text.strip())) for s in plain], 0)
    bold_chars = sum(len(s.text.strip()) for s in plain if s.is_bold)
    total = sum(len(s.text.strip()) for s in plain) or 1
    font = _dominant([(base_name(s.font), len(s.text.strip())) for s in plain], "")

    baselines = [ln.baseline for ln in block.lines]
    gaps = [b - a for a, b in zip(baselines, baselines[1:]) if b - a > size * 0.5]
    pitch = statistics.median(gaps) if gaps else size * 1.22

    xs = [ln.bbox[0] for ln in block.lines]
    first_indent = 0.0
    if len(block.lines) >= 2:
        diff = xs[0] - min(xs[1:])
        if diff > size * 0.5:
            first_indent = diff

    return TextStyle(
        size=size,
        color=color,
        bold=bold_chars >= total * 0.6,
        serif=not is_sans_font(font),
        line_pitch=pitch,
        first_indent=first_indent,
        align=_detect_align(block, kind, page_width, size),
    )


def _detect_align(block: TextBlock, kind: BlockKind, page_width: float, size: float) -> Align:
    x0, _, x1, _ = block.bbox
    center = (x0 + x1) / 2
    lines = block.lines
    if len(lines) == 1:
        # ページ中央に置かれ、左余白より内側から始まる 1 行は中央揃え（タイトル・見出し・短いキャプション）
        centered = abs(center - page_width / 2) < page_width * 0.03 and x0 > page_width * 0.12
        if centered:
            return Align.CENTER
        return Align.LEFT
    centers = [(ln.bbox[0] + ln.bbox[2]) / 2 for ln in lines]
    lefts = [ln.bbox[0] for ln in lines[1:]]
    if max(centers) - min(centers) < size * 0.6 and max(lefts) - min(lefts) > size * 0.6:
        return Align.CENTER
    rights = [ln.bbox[2] for ln in lines[:-1]]
    if len(rights) >= 1 and max(rights) - min(rights) < size * 0.8:
        return Align.JUSTIFY
    return Align.LEFT


# ---- 保護対象の検出 ---------------------------------------------------------


def _pieces_for_line(line: Line, base_size: float, body_font: str, has_math: bool) -> list[_Piece]:
    baseline = line.baseline
    pieces: list[_Piece] = []
    prev: Span | None = None
    for span in line.spans:
        # 座標上は離れているのに空白文字が無い場合（見出し番号と題名など）は空白を補う
        if (
            prev is not None
            and span.bbox[0] - prev.bbox[2] > base_size * 0.2
            and not prev.text[-1:].isspace()
            and not span.text[:1].isspace()
        ):
            pieces.append(_Piece(span, " ", False, baseline, exact=False))
        prev = span
        raised = abs(span.origin[1] - baseline) > base_size * 0.15
        small = span.size < base_size * 0.8
        math = is_math_font(span.font) or (
            is_math_span(span.font, span.text, body_font) and (has_math or len(span.text.strip()) <= 4)
        )
        if math or (raised and small):
            pieces.append(_Piece(span, span.text, True, baseline))
            continue
        # 引用番号は保護（色と表記を保つ）
        pos = 0
        whole = not _CITATION.search(span.text)
        for m in _CITATION.finditer(span.text):
            if m.start() > pos:
                pieces.append(_Piece(span, span.text[pos : m.start()], False, baseline, exact=False))
            pieces.append(_Piece(span, m.group(0), True, baseline, exact=False))
            pos = m.end()
        if pos < len(span.text):
            pieces.append(_Piece(span, span.text[pos:], False, baseline, exact=whole))

    # 数式に隣接する記号・数字だけのスパン（CMR10 の "= 6" など）を数式に取り込む
    changed = True
    while changed:
        changed = False
        for i, p in enumerate(pieces):
            if p.protected or not _OPERATOR_ONLY.match(p.text) or not p.text.strip():
                continue
            left = i > 0 and pieces[i - 1].protected and not _CITATION.fullmatch(pieces[i - 1].text)
            right = i + 1 < len(pieces) and pieces[i + 1].protected and not _CITATION.fullmatch(pieces[i + 1].text)
            cm_font = base_name(p.span.font).upper().startswith(("CMR", "LMROMAN", "CMBX"))
            if (left and right) or ((left or right) and (cm_font or len(p.text.strip()) <= 1)):
                p.protected = True
                changed = True
    return pieces


def _inline_piece(p: _Piece, base_size: float) -> InlinePiece:
    s = p.span
    return InlinePiece(
        text=decode_symbol_font(s.font, p.text),
        size_ratio=s.size / base_size if base_size else 1.0,
        rise_ratio=(p.baseline - s.origin[1]) / base_size if base_size else 0.0,
        italic=bool(_MATH_ITALIC.search(base_name(s.font))) or s.is_italic,
        bold=s.is_bold,
        color=s.color,
        math=is_math_font(s.font),
    )


# ---- マークアップ化 ---------------------------------------------------------


@dataclass
class _Unit:
    """1 ブロック分の翻訳単位候補。"""

    decision: BlockDecision
    markup: str
    runs: dict[int, InlineRun]
    region: Region


def _join(markup: str, nxt: str) -> str:
    if not markup:
        return nxt
    if markup.endswith("-") and len(markup) >= 2 and markup[-2].isalpha() and nxt[:1].islower():
        return markup[:-1] + nxt  # 行末ハイフネーションの解除
    if markup.endswith((" ", " ")) or nxt.startswith(" "):
        return markup + nxt
    return markup + " " + nxt


def build_unit(decision: BlockDecision, page_width: float, body_font: str = "") -> _Unit | None:
    block = decision.block
    style = block_style(block, decision.kind, page_width)
    runs: dict[int, InlineRun] = {}
    markup = ""
    has_math = any(is_math_font(s.font) for s in block.spans)
    pending: list[_Piece] = []

    def flush() -> str:
        """溜めた保護要素を 1 つのプレースホルダにして返す。"""
        if not pending:
            return ""
        rid = len(runs)
        trailing_space = pending[-1].text[-1:].isspace()
        runs[rid] = _make_run(pending, style.size, block.page)
        pending.clear()
        return placeholder(rid) + (" " if trailing_space else "")

    for line in block.lines:
        line_markup = ""
        for p in _pieces_for_line(line, style.size, body_font, has_math):
            if p.protected:
                # 空白は数式の一部として保持しつつ、前後の空白は外へ出す
                if not pending and p.text[:1].isspace():
                    line_markup += " "
                pending.append(p)
                continue
            if pending and not p.text.strip():
                pending.append(p)
                continue
            line_markup += flush() + escape(p.text)
        line_markup += flush()
        markup = _join(markup, re.sub(r"\s+", " ", line_markup).strip())

    markup = re.sub(r"\s+", " ", markup).strip()
    if not to_plain(markup).strip():
        return None
    region = Region(
        page=block.page,
        rect=block.bbox,
        line_rects=[ln.bbox for ln in block.lines],
        span_rects=[[s.bbox for s in ln.spans if s.text.strip()] for ln in block.lines],
        style=style,
        source_chars=len(to_plain(markup)),
        block_index=block.index,
        first_baseline=block.lines[0].baseline,
    )
    return _Unit(decision, markup, runs, region)


def _make_run(pieces: list[_Piece], base_size: float, page: int) -> InlineRun:
    """保護要素を作る。前後の空白は落とし（訳文側で間隔を入れ直す）、可能なら原文の横位置を記録する。"""
    items = list(pieces)
    while items and not items[0].text.strip():
        items.pop(0)
    while items and not items[-1].text.strip():
        items.pop()
    if not items:
        return InlineRun(())
    inline = [_inline_piece(p, base_size) for p in items]
    inline[0] = replace(inline[0], text=inline[0].text.lstrip())
    inline[-1] = replace(inline[-1], text=inline[-1].text.rstrip())
    if all(p.exact for p in items) and base_size > 0:
        visible = [p for p in items if p.text.strip()]
        x0 = min(p.span.bbox[0] for p in visible)
        x1 = max(p.span.bbox[2] for p in visible)
        inline = [
            replace(ip, dx_ratio=(p.span.bbox[0] - x0) / base_size) if p.text.strip() else ip
            for ip, p in zip(inline, items)
        ]
        # 空白だけの要素は位置を持たないので除く
        inline = [ip for ip in inline if ip.text.strip()]
        y0 = min(p.span.bbox[1] for p in visible)
        y1 = max(p.span.bbox[3] for p in visible)
        return InlineRun(
            tuple(inline),
            width_ratio=(x1 - x0) / base_size,
            page=page,
            bbox=(x0, y0, x1, y1),
            origin=(x0, items[0].baseline),
            base_size=base_size,
        )
    return InlineRun(tuple(inline))


# ---- 段・ページを跨ぐ段落の結合 -----------------------------------------------


def _continues(prev: _Unit, nxt: _Unit) -> bool:
    if prev.decision.kind != BlockKind.BODY or nxt.decision.kind != BlockKind.BODY:
        return False
    # 結合するのは段の切り替わり（次が上方にある）かページ跨ぎのときだけ。
    # 同じ段で下に続く場合は間に数式などがあるため、結合すると訳文の位置が入れ替わる恐れがある
    p_rect, n_rect = prev.region.rect, nxt.region.rect
    if prev.region.page == nxt.region.page and n_rect[1] >= p_rect[3] - 1:
        return False
    if nxt.region.page - prev.region.page > 1:
        return False
    if _SENTENCE_END.search(prev.markup):
        return False
    head = to_plain(nxt.markup).lstrip()
    if not head or not (head[0].islower() or nxt.markup.startswith("<x")):
        return False
    a, b = prev.region.style.size, nxt.region.style.size
    return abs(a - b) <= max(0.3, a * 0.05)


def _title_continues(prev: _Unit, nxt: _Unit) -> bool:
    a, b = prev.region, nxt.region
    if a.page != b.page or abs(a.style.size - b.style.size) > 0.6:
        return False
    gap = b.rect[1] - a.rect[3]
    return -a.style.size * 0.5 <= gap <= a.style.size * 1.5


def _renumber(markup: str, runs: dict[int, InlineRun], offset: int) -> tuple[str, dict[int, InlineRun]]:
    if not offset:
        return markup, runs
    new = re.sub(r'<x id="(\d+)"/>', lambda m: placeholder(int(m.group(1)) + offset), markup)
    return new, {rid + offset: r for rid, r in runs.items()}


def build_segments(
    decisions_by_page: list[tuple[float, list[BlockDecision]]],
    merge: bool = True,
    body_font: str = "",
) -> list[Segment]:
    """ページ順・ブロック順に翻訳単位を作る。

    decisions_by_page: (ページ幅, そのページの判定結果) の列
    """
    units: list[_Unit] = []
    for width, decisions in decisions_by_page:
        for d in decisions:
            if not d.translate:
                continue
            u = build_unit(d, width, body_font)
            if u is not None:
                units.append(u)

    segments: list[Segment] = []
    last_body: Segment | None = None
    last_unit: _Unit | None = None
    last_title: tuple[Segment, _Unit] | None = None
    for u in units:
        # 複数行のタイトルが行ごとに別のブロックになっている場合は 1 つの文として訳す
        if u.decision.kind == BlockKind.TITLE and last_title is not None and _title_continues(last_title[1], u):
            seg, _ = last_title
            markup, runs = _renumber(u.markup, u.runs, len(seg.runs))
            seg.source = _join(seg.source, markup)
            seg.runs.update(runs)
            seg.regions.append(u.region)
            last_title = (seg, u)
            continue
        if merge and last_body is not None and last_unit is not None and _continues(last_unit, u):
            markup, runs = _renumber(u.markup, u.runs, len(last_body.runs))
            last_body.source = _join(last_body.source, markup)
            last_body.runs.update(runs)
            last_body.regions.append(u.region)
            last_unit = u
            continue
        b = u.decision.block
        seg = Segment(
            id=f"p{b.page}-b{b.index}",
            kind=u.decision.kind,
            source=u.markup,
            runs=dict(u.runs),
            regions=[u.region],
        )
        segments.append(seg)
        last_title = (seg, u) if u.decision.kind == BlockKind.TITLE else None
        if u.decision.kind == BlockKind.BODY:
            last_body, last_unit = seg, u
        elif u.decision.kind in (BlockKind.HEADING, BlockKind.TITLE):
            last_body = last_unit = None
    return segments
