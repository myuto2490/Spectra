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
from .fonts import find_text_fonts, has_numbered_adv_fonts, text_fonts
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
        drawings = page.get_drawings()
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
    r = d["rect"]
    w = (d.get("width") or 0.0) / 2 if d.get("color") is not None else 0.0
    return (r.x0 - w, r.y0 - w, r.x1 + w, r.y1 + w)


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
