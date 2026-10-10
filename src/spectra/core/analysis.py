"""文書全体の解析：抽出 → 判定 → セグメント化。"""

from __future__ import annotations

from collections.abc import Callable

import pymupdf

from .classify import DocumentClassifier, estimate_body_font, estimate_body_size
from .extract import extract_blocks
from .models import (
    DocumentAnalysis,
    PageAnalysis,
    SkipReason,
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
    blocks = {pno: extract_blocks(doc[pno]) for pno in pages}
    all_blocks = [b for bl in blocks.values() for b in bl]
    body_size = estimate_body_size(all_blocks)
    body_font = estimate_body_font(all_blocks)
    classifier = DocumentClassifier(options, body_size, body_font)
    analyses: list[PageAnalysis] = []
    for i, pno in enumerate(pages):
        if should_cancel and should_cancel():
            raise InterruptedError
        analyses.append(classifier.analyze_page(doc[pno], blocks[pno]))
        if progress:
            progress(i + 1, len(pages))
    for pa in analyses:
        guard_overlaps(pa)
    segments = build_segments(
        [(pa.width, pa.decisions) for pa in analyses],
        merge=options.merge_split_paragraphs,
        body_font=body_font,
    )
    return DocumentAnalysis(pages=analyses, segments=segments, body_size=body_size)


_FRAGMENT_REASONS = (SkipReason.MATH, SkipReason.NO_WORDS, SkipReason.NUMERIC)


def guard_overlaps(pa: PageAnalysis) -> None:
    """消す範囲が翻訳対象外の文字に重なる段落を処理する（図表・数式を守ることを優先する）。

    - 重なりは文字の並び（スパン）単位で判定する（独立数式の外枠は分数などで縦に大きく広がるため）
    - 重なる相手が 1〜3 文字の小さな数式の断片なら、段落に取り込んで訳文と一緒に動かす
    - それ以外の重なりがある段落は訳さない
    1 つを訳さないことにすると、その段落が新たに他の段落の障害になり得るため、変化が無くなるまで繰り返す。
    """
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
            if all(_is_fragment(k) for k in hits):
                for k in hits:
                    d.block.lines.extend(k.block.lines)
                    d.block.lines.sort(key=lambda ln: (round(ln.baseline, 1), ln.bbox[0]))
                    d.block.bbox = rect_union(d.block.bbox, k.block.bbox)
                    pa.decisions.remove(k)
            else:
                d.skip = SkipReason.OVERLAP
            changed = True
            break


def _overlaps(rects: list, other) -> bool:
    for s in other.block.spans:
        if not s.text.strip():
            continue
        sb = inflate(s.bbox, -0.5)
        if any(rect_area(rect_intersect(r, sb)) > 0.5 for r in rects):
            return True
    return False


def _is_fragment(d) -> bool:
    return d.skip in _FRAGMENT_REASONS and len(d.block.lines) == 1 and d.block.char_count <= 3
