"""文書全体の解析：抽出 → 判定 → セグメント化。"""

from __future__ import annotations

from collections.abc import Callable

import pymupdf

from .classify import DocumentClassifier, estimate_body_font, estimate_body_size
from .extract import extract_blocks
from .models import DocumentAnalysis, PageAnalysis
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
    segments = build_segments(
        [(pa.width, pa.decisions) for pa in analyses],
        merge=options.merge_split_paragraphs,
        body_font=body_font,
    )
    return DocumentAnalysis(pages=analyses, segments=segments, body_size=body_size)
