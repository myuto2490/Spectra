"""文書全体の解析：抽出 → 判定 → セグメント化。"""

from __future__ import annotations

import re
from collections.abc import Callable

import pymupdf

from .classify import (
    DocumentClassifier,
    estimate_body_font,
    estimate_body_size,
    find_running_headers,
)
from .extract import TEXT_FLAGS, extract_blocks
from .fonts import find_text_fonts, has_numbered_adv_fonts, is_math_font, text_fonts
from .models import (
    LINE_ART_CHAR,
    LINE_ART_FONT,
    DocumentAnalysis,
    PageAnalysis,
    RestoreGlyph,
    SkipReason,
    Span,
    erase_rect,
    inflate,
    rect_area,
    rect_intersect,
    rect_union,
)
from .options import AnalysisOptions
from .segment import build_segments

ProgressFn = Callable[[int, int], None]


def page_range(doc: pymupdf.Document, options: AnalysisOptions) -> range:
    last = doc.page_count - 1 if options.last_page is None else min(options.last_page, doc.page_count - 1)
    return range(max(0, options.first_page), last + 1)


def analyze_document(
    doc: pymupdf.Document,
    options: AnalysisOptions | None = None,
    progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> DocumentAnalysis:
    options = options or AnalysisOptions()
    pages = page_range(doc, options)
    with text_fonts(_document_text_fonts(doc, pages)):
        return _analyze(doc, options, pages, progress, should_cancel)


def _document_text_fonts(doc: pymupdf.Document, pages: range) -> frozenset[str]:
    """名前が記号フォントと区別できない Elsevier のフォント（AdvP4DF60E など）のうち、本文を組んでいるもの。"""
    if not any(has_numbered_adv_fonts(f[3] for f in doc.get_page_fonts(pno)) for pno in pages):
        return frozenset()
    spans = (
        (s["font"], s["text"])
        for pno in pages
        for b in doc[pno].get_text("dict", flags=TEXT_FLAGS)["blocks"]
        for ln in b.get("lines", ())
        for s in ln["spans"]
    )
    return find_text_fonts(spans)


def _analyze(
    doc: pymupdf.Document,
    options: AnalysisOptions,
    pages: range,
    progress: ProgressFn | None,
    should_cancel: Callable[[], bool] | None,
) -> DocumentAnalysis:
    blocks = {pno: extract_blocks(doc[pno]) for pno in pages}
    all_blocks = [b for bl in blocks.values() for b in bl]
    body_size = estimate_body_size(all_blocks)
    body_font = estimate_body_font(all_blocks)
    classifier = DocumentClassifier(options, body_size, body_font)
    classifier.running_headers = find_running_headers(blocks)
    analyses: list[PageAnalysis] = []
    for i, pno in enumerate(pages):
        if should_cancel and should_cancel():
            raise InterruptedError
        analyses.append(classifier.analyze_page(doc[pno], blocks[pno]))
        if progress:
            progress(i + 1, len(pages))
    for pa in analyses:
        drop_duplicate_layers(pa)
        mark_figure_and_table_text(pa, doc[pa.page])
        guard_overlaps(pa, doc[pa.page])
        attach_line_art(pa, doc[pa.page])
    segments = build_segments(
        [(pa.width, pa.decisions) for pa in analyses],
        merge=options.merge_split_paragraphs,
        body_font=body_font,
    )
    return DocumentAnalysis(pages=analyses, segments=segments, body_size=body_size)


_FRAGMENT_REASONS = (SkipReason.MATH, SkipReason.NO_WORDS, SkipReason.NUMERIC)


def drop_duplicate_layers(pa: PageAnalysis) -> None:
    """同じ位置に重ねた文字の層（スキャンに二重に付けた OCR テキストなど）は、最初の 1 つだけ訳す。

    重なった層の文字は、最初の段落を消すときに一緒に消える。
    """
    kept: list = []
    for d in list(pa.decisions):
        if d.translate and any(k.translate and _same_place(d.block.bbox, k.block.bbox) for k in kept):
            pa.decisions.remove(d)
        else:
            kept.append(d)


def _same_place(a, b) -> bool:
    return rect_area(rect_intersect(a, b)) >= max(rect_area(a), rect_area(b)) * 0.8


def guard_overlaps(pa: PageAnalysis, page: pymupdf.Page | None = None) -> None:
    """消す範囲が翻訳対象外の文字に重なる段落を処理する（図表・数式を守ることを優先する）。

    - 重なりは文字の並び（スパン）単位で判定する（独立数式の外枠は分数などで縦に大きく広がるため）
    - 重なる相手が 1〜3 文字の小さな数式の断片なら、段落に取り込んで訳文と一緒に動かす
    - 重なりが字形の枠だけで、実際の字形（大きな Σ や括弧の上半分など）が段落の外にあるなら、訳したうえで
      消去に巻き込まれる字形を pa.restore に記録する（描画時に原文から切り抜いて戻す）。page が必要
    - それ以外の重なりがある段落は訳さない
    1 つを訳さないことにすると、その段落が新たに他の段落の障害になり得るため、変化が無くなるまで繰り返す。
    """
    ink = _InkFinder(page)
    changed = True
    while changed:
        changed = False
        kept = [d for d in pa.decisions if not d.translate]
        for d in pa.decisions:
            if not d.translate:
                continue
            # 描画時と同じく、重なる行はスパン単位で消すので、判定もスパン単位で行う
            rects = [erase_rect(s.bbox) for s in d.block.spans if s.text.strip()]
            hits = [k for k in kept if _overlaps(rects, k)]
            if not hits:
                continue
            # 字形の枠だけが重なる（実際の字形は段落の外にある）相手は、取り込まずにその場に残し、消えたら戻す。
            # これを許すのは文章の段落だけ（数式の断片を本文と取り違えたブロックは、従来どおり訳さない）
            prose = _prose(d)
            clear = [
                k for k in hits
                if prose and not _is_paragraph_tail(k, d) and ink.clear(_hit_spans(rects, k), d.block.bbox, rects)
            ]
            merge = [k for k in hits if k not in clear and (_is_fragment(k) or _is_paragraph_tail(k, d))]
            others = [k for k in hits if k not in clear and k not in merge]
            if others:
                d.skip = SkipReason.OVERLAP
                changed = True
                break
            if merge:
                for k in merge:
                    d.block.lines.extend(k.block.lines)
                    d.block.lines.sort(key=lambda ln: (round(ln.baseline, 1), ln.bbox[0]))
                    d.block.bbox = rect_union(d.block.bbox, k.block.bbox)
                    pa.decisions.remove(k)
                changed = True
                break
    # 訳すことにした段落の消去に巻き込まれる字形を記録する
    pa.restore = []
    kept = [d for d in pa.decisions if not d.translate]
    for d in pa.decisions:
        if not d.translate:
            continue
        rects = [erase_rect(s.bbox) for s in d.block.spans if s.text.strip()]
        for k in kept:
            for s in _hit_spans(rects, k):
                for ch, box, origin in ink.glyphs(s):
                    pa.restore.append(
                        RestoreGlyph(ink=box, span=s.bbox, block_index=d.block.index, text=ch, origin=origin)
                    )


# ---- 図・表の中の文字（本文として訳さない） ---------------------------------------------

_TABLE_CAPTION = re.compile(r"^\s*table\s*([A-Z]?\d+|[ivxlc]+)", re.IGNORECASE)


def mark_figure_and_table_text(pa: PageAnalysis, page: pymupdf.Page) -> None:
    """図・表として検出されなかった図表の中の文字を、訳さないことにする（訳す側に変えることはしない）。

    - 語と語の間に長い矢印・線が引かれた 1 行（"measurand ──→ measurement(s)" のような図の中のラベル）
    - "TABLE x" の見出しのすぐ下の横線と、同じ幅の下の横線に挟まれた、表の行（罫線の少ない表）
    - 表のすぐ上下に切り離された、表と同じ左端・同じ列の位置を持つ 1 行（表の最後の行など）
    """
    try:
        drawings = page.get_cdrawings()
    except Exception:
        return
    rules = [_hrule(d) for d in drawings]
    rules = [r for r in rules if r is not None]
    _mark_diagram_lines(pa, drawings)
    _mark_ruled_tables(pa, rules)
    _join_table_rows(pa)


def _hrule(d: dict) -> tuple[float, float, float] | None:
    """横線なら (左端, 右端, 高さの位置)。線（太さのある直線）と、細い塗りの四角を横線とみなす。"""
    x0, y0, x1, y1 = d["rect"]
    if x1 - x0 < 20:
        return None
    if d.get("type") == "s" and y1 - y0 < 0.5 or d.get("type") == "f" and y1 - y0 < 1.5:
        return (x0, x1, (y0 + y1) / 2)
    return None


def _mark_diagram_lines(pa: PageAnalysis, drawings: list[dict]) -> None:
    for d in pa.decisions:
        if not d.translate or len(d.block.lines) != 1:
            continue
        ln = d.block.lines[0]
        spans = [s for s in ln.spans if s.text.strip()]
        size = max((s.size for s in spans), default=10.0)
        top, bottom = ln.bbox[1], ln.bbox[3]
        for a, b in zip(spans, spans[1:]):
            g0, g1 = a.bbox[2] - 1, b.bbox[0] + 1
            if g1 - g0 < size * 2:
                continue
            for dr in drawings:
                x0, y0, x1, y1 = dr["rect"]
                if g0 <= x0 and x1 <= g1 and x1 - x0 >= size * 2 and top <= y0 and y1 <= bottom:
                    d.skip = SkipReason.FIGURE
                    break
            if d.skip is not None:
                break


def _mark_ruled_tables(pa: PageAnalysis, rules: list[tuple[float, float, float]]) -> None:
    for cap in pa.decisions:
        if not _TABLE_CAPTION.match(cap.block.text):
            continue
        cb = cap.block.bbox
        size = max((s.size for s in cap.block.spans), default=10.0)
        # 見出し（と、そのすぐ下の表題の行）の下、数行以内にある横線を表の上端とする
        tops = sorted((r for r in rules if cb[3] - 1 <= r[2] <= cb[3] + size * 4), key=lambda r: r[2])
        if not tops:
            continue
        x0, x1, top = tops[0]
        same = sorted((r for r in rules if r[2] > top + size and abs(r[0] - x0) < 1.5 and abs(r[1] - x1) < 1.5), key=lambda r: r[2])
        if not same:
            continue
        bottom = same[-1][2]
        # 横線の間にあるブロック。文章の段落（4 行以上、または文が続くもの）が 1 つでもあれば表とみなさない
        inside = [
            d for d in pa.decisions
            if d is not cap and x0 - 2 <= d.block.bbox[0] and d.block.bbox[2] <= x1 + 2
            and top - size * 0.5 <= d.block.bbox[1] and d.block.bbox[3] <= bottom + size * 0.5
        ]
        if not inside or any(len(d.block.lines) > 3 for d in inside):
            continue
        # 訳すと列がつながって崩れる、複数の列を 1 行にまとめたブロックだけを訳さない（1 つのセルは従来どおり訳す）。
        # 列の間が狭い行も、他の行で分かった列の位置から始まる部分があれば、複数の列をまとめた行とみなす
        columns = {x for d in inside for x in _column_starts(d.block)}
        for d in inside:
            if d.translate and (_spans_columns(d.block) or _starts_at_column(d.block, columns)):
                d.skip = SkipReason.TABLE


def _column_starts(block) -> list[float]:
    """列の間ほどの大きな空き（字の 1.5 倍以上）の後に始まる部分の左端。"""
    starts = []
    for ln in block.lines:
        spans = [s for s in ln.spans if s.text.strip()]
        size = max((s.size for s in spans), default=10.0)
        starts += [b.bbox[0] for a, b in zip(spans, spans[1:]) if b.bbox[0] - a.bbox[2] > size * 1.5]
    return starts


def _spans_columns(block) -> bool:
    return bool(_column_starts(block))


def _starts_at_column(block, columns: set[float]) -> bool:
    """行の途中に、表の列の位置（columns）から始まる部分があるか。"""
    for ln in block.lines:
        spans = [s for s in ln.spans if s.text.strip()]
        for a, b in zip(spans, spans[1:]):
            if b.bbox[0] - a.bbox[2] > 0.5 and any(abs(b.bbox[0] - c) <= 2 for c in columns):
                return True
    return False


def _join_table_rows(pa: PageAnalysis) -> None:
    tables = [d for d in pa.decisions if d.skip == SkipReason.TABLE and len(d.block.lines) >= 2]
    for d in pa.decisions:
        if not d.translate or len(d.block.lines) != 1:
            continue
        ln = d.block.lines[0]
        spans = [s for s in ln.spans if s.text.strip()]
        size = max((s.size for s in spans), default=10.0)
        starts = [s.bbox[0] for a, s in zip(spans, spans[1:]) if s.bbox[0] - a.bbox[2] > size * 1.5]
        if not starts:
            continue
        for t in tables:
            tb = t.block.bbox
            touching = -size <= d.block.bbox[1] - tb[3] <= size or -size <= tb[1] - d.block.bbox[3] <= size
            if not touching or abs(d.block.bbox[0] - tb[0]) > 2:
                continue
            # 表の各行の、2 列目以降の始まりの位置と揃っているか
            cols = {round(s.bbox[0]) for tl in t.block.lines for s in tl.spans[1:] if s.text.strip()}
            if all(any(abs(x - c) <= 2 for c in cols) for x in starts):
                d.skip = SkipReason.TABLE
                break


def attach_line_art(pa: PageAnalysis, page: pymupdf.Page) -> None:
    """訳す段落の行で、語と語の間の空きに描かれた小さなベクター図形を、仮のスパンとして行に加える。

    元の組版で記号の代わりに描かれた図形（黒い四角・線で描いた記号など）は、訳文に置き換えると元の位置に残って
    訳文と重なる。段落中の数式と同じく保護要素として扱い、訳文の中の位置へ原文の見た目のまま移す。
    図・表・罫線を巻き込まないよう、行の高さに収まり、語の間の空きの内側にある、小さな図形だけを対象にする。
    """
    lines = [ln for d in pa.decisions if d.translate for ln in d.block.lines]
    if not lines:
        return
    try:
        drawings = page.get_cdrawings()  # get_drawings より速い（範囲と線の太さだけを使う）
    except Exception:
        return
    inks = [_ink_rect(d) for d in drawings]
    blocked = list(pa.figure_rects) + list(pa.table_rects)
    for ln in lines:
        spans = [s for s in ln.spans if s.text.strip()]
        if len(spans) < 2:
            continue
        size = max(s.size for s in spans)
        height = ln.bbox[3] - ln.bbox[1]
        added = []
        for a, b in zip(spans, spans[1:]):
            g0, g1 = a.bbox[2] - 0.5, b.bbox[0] + 0.5
            if g1 - g0 < 1:
                continue
            # 数式の中（数式フォントや斜体の変数名の間。下線で描いた "sky_trans_ν" の "_" など）の図形は、
            # 数式と一緒に 1 つの保護要素として移す既存の処理に任せる（ここで分けると数式が途切れる）
            if any(is_math_font(s.font) or s.is_italic for s in (a, b)):
                continue
            found = [
                r for r in inks
                if g0 <= r[0] and r[2] <= g1
                and ln.bbox[1] - 1 <= r[1] and r[3] <= ln.bbox[3] + 1
                and r[2] - r[0] <= size * 4 and r[3] - r[1] <= height
            ]
            if not found or len(found) > 4:
                continue
            box = (min(r[0] for r in found), min(r[1] for r in found), max(r[2] for r in found), max(r[3] for r in found))
            if any(rect_area(rect_intersect(inflate(box, 1), f)) > 0 for f in blocked):
                continue
            added.append(Span(LINE_ART_CHAR, LINE_ART_FONT, size, 0, 0, box, (box[0], ln.baseline)))
        if added:
            ln.spans = sorted([*ln.spans, *added], key=lambda s: s.bbox[0])


def _ink_rect(d: dict) -> tuple[float, float, float, float]:
    """図形の描かれる範囲（線は太さの分だけ広げる）。"""
    x0, y0, x1, y1 = d["rect"]
    w = (d.get("width") or 0.0) / 2 if d.get("color") is not None else 0.0
    return (x0 - w, y0 - w, x1 + w, y1 + w)


_PROSE_WORD = re.compile(r"[A-Za-z]{3,}")


def _prose(d) -> bool:
    """文章の段落らしいか（3 文字以上の英単語が 6 語以上）。"""
    return len(_PROSE_WORD.findall(d.block.text)) >= 6


def _overlaps(rects: list, other) -> bool:
    return bool(_hit_spans(rects, other))


def _hit_spans(rects: list, other) -> list:
    hits = []
    for s in other.block.spans:
        if not s.text.strip():
            continue
        sb = inflate(s.bbox, -0.5)
        if any(rect_area(rect_intersect(r, sb)) > 0.5 for r in rects):
            hits.append(s)
    return hits


class _InkFinder:
    """スパンの実際の字形の範囲（フォントの枠ではなく、字の輪郭から求めた範囲）を調べる。"""

    def __init__(self, page: pymupdf.Page | None):
        self.page = page
        self._cache: dict = {}

    def glyphs(self, span) -> list[tuple[str, tuple[float, float, float, float], tuple[float, float]]]:
        """スパンの字形ごとの (文字, 実際の範囲, 原点)。調べられなければ空。"""
        if self.page is None:
            return []
        key = (span.bbox, span.text)
        if key not in self._cache:
            self._cache[key] = self._find(span)
        return self._cache[key]

    def of(self, span) -> tuple[float, float, float, float] | None:
        boxes = [box for _, box, _ in self.glyphs(span)]
        if not boxes:
            return None
        return (min(r[0] for r in boxes), min(r[1] for r in boxes), max(r[2] for r in boxes), max(r[3] for r in boxes))

    def _find(self, span):
        try:
            clip = pymupdf.Rect(inflate(span.bbox, 1.0))
            data = self.page.get_text("rawdict", flags=TEXT_FLAGS | pymupdf.TEXT_ACCURATE_BBOXES, clip=clip)
        except Exception:
            return []
        want = {c for c in span.text if not c.isspace()}
        zone = inflate(span.bbox, 0.5)
        found = []
        for b in data.get("blocks", ()):
            for ln in b.get("lines", ()):
                for s in ln["spans"]:
                    for c in s.get("chars", ()):
                        x, y = c["origin"]
                        if c["c"] in want and zone[0] <= x <= zone[2] and zone[1] <= y <= zone[3]:
                            found.append((c["c"], tuple(c["bbox"]), (x, y)))
        # 1 文字でも範囲を測れない（幅や高さが 0 の Type3 フォントの字など）、または見つからない字があれば、
        # 一部の字だけで「重ならない」と判定しないよう、スパン全体を測れないものとする
        if len(found) != sum(1 for c in span.text if not c.isspace()):
            return []
        if any(r[2] <= r[0] or r[3] <= r[1] for _, r, _ in found):
            return []
        return found

    def clear(self, spans: list, para: tuple, erase: list) -> bool:
        """spans の実際の字形が、段落の範囲にも消す範囲にも重ならないか（字形の枠だけが重なっているか）。"""
        if not spans:
            return False
        zone = inflate(para, 0.5)
        for s in spans:
            box = self.of(s)
            if box is None or rect_area(rect_intersect(box, zone)) > 0:
                return False
            if any(rect_area(rect_intersect(box, r)) > 0 for r in erase):
                return False
        return True


def _is_paragraph_tail(k, para) -> bool:
    """段落から切り離された、上付き・下付きの数値を含む段落の最後の行（例: "18.64 (Orton 1992)."）。

    段落のすぐ下（または重なる位置）にあって段の幅に収まる、数値・数式中心の短い 1 行は段落の一部とみなす。
    等号を含んで中央に置かれた行は独立した数式の可能性が高いので取り込まない。
    """
    if k.skip not in _FRAGMENT_REASONS or len(k.block.lines) != 1 or k.block.char_count > 40:
        return False
    kb, pb = k.block.bbox, para.block.bbox
    size = max((s.size for s in para.block.spans), default=10.0)
    within = kb[0] >= pb[0] - 2 and kb[2] <= pb[2] + 2
    below = pb[3] - size <= kb[1] <= pb[3] + size * 0.8
    text = k.block.text
    centered = abs((kb[0] + kb[2]) / 2 - (pb[0] + pb[2]) / 2) < size and kb[0] - pb[0] > size * 2
    display_math = ("=" in text or "≈" in text) and centered
    return within and below and not display_math


def _is_fragment(d) -> bool:
    return d.skip in _FRAGMENT_REASONS and len(d.block.lines) == 1 and d.block.char_count <= 3
