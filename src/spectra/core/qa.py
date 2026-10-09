"""レイアウト保持の自動検査。

翻訳前後の PDF を比較し、図表やレイアウトが崩れていないことを確かめる。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

from .extract import TEXT_FLAGS
from .models import DocumentAnalysis, Rect, inflate, rect_area, rect_intersect

log = logging.getLogger(__name__)

PIXEL_TOLERANCE = 2.0  # 平均画素差（0-255）の許容値
QA_DPI = 60


@dataclass
class QAIssue:
    page: int
    kind: str  # images / drawings / text / pixels
    message: str


@dataclass
class QAReport:
    issues: list[QAIssue] = field(default_factory=list)
    pages_checked: int = 0
    regions_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues


def _norm(text: str) -> Counter:
    return Counter(re.sub(r"\s+", "", text))


def _drawing_count(page: pymupdf.Page) -> int:
    try:
        return len(page.get_drawings())
    except Exception:
        return -1


def _mean_diff(a: pymupdf.Pixmap, b: pymupdf.Pixmap) -> float:
    if (a.width, a.height, a.n) != (b.width, b.height, b.n):
        return 255.0
    sa, sb = a.samples, b.samples
    if sa == sb:
        return 0.0
    total = sum(abs(x - y) for x, y in zip(sa, sb))
    return total / max(1, len(sa))


def check(
    before: pymupdf.Document,
    after: pymupdf.Document,
    analysis: DocumentAnalysis,
    written: dict[int, list[Rect]] | None = None,
    pixels: bool = True,
) -> QAReport:
    """before（原文）と after（翻訳後、ページ構成は同じ）を比較する。

    written: ページごとの訳文の描画範囲。画素比較から除外する。
    """
    report = QAReport()
    written = written or {}
    for pa in analysis.pages:
        pno = pa.page
        pb, pf = before[pno], after[pno]
        report.pages_checked += 1

        if len(pb.get_image_info()) != len(pf.get_image_info()):
            report.issues.append(QAIssue(pno, "images", "画像の数が変化しました"))
        db, df = _drawing_count(pb), _drawing_count(pf)
        if db >= 0 and df >= 0 and db != df:
            report.issues.append(QAIssue(pno, "drawings", f"図形の数が変化しました（{db} → {df}）"))

        for d in pa.decisions:
            if d.translate:
                continue
            clip = pymupdf.Rect(inflate(d.block.bbox, -0.5))
            if clip.is_empty:
                continue
            want = _norm(d.block.text)
            got = _norm(pf.get_text("text", clip=clip, flags=TEXT_FLAGS))
            missing = want - got
            if missing and sum(missing.values()) > max(1, sum(want.values()) * 0.02):
                report.issues.append(
                    QAIssue(pno, "text", f"翻訳対象外の文字が失われました: {''.join(missing.elements())[:20]!r}")
                )

        if not pixels:
            continue
        mine = written.get(pno, [])
        for r in list(pa.figure_rects) + list(pa.table_rects):
            rr = inflate(r, 1)
            if any(rect_area(rect_intersect(rr, w)) > 0 for w in mine):
                continue
            clip = pymupdf.Rect(rr) & pb.rect
            if clip.is_empty or clip.width < 2 or clip.height < 2:
                continue
            report.regions_checked += 1
            diff = _mean_diff(pb.get_pixmap(clip=clip, dpi=QA_DPI), pf.get_pixmap(clip=clip, dpi=QA_DPI))
            if diff > PIXEL_TOLERANCE:
                report.issues.append(QAIssue(pno, "pixels", f"図表の見た目が変化しました（差分 {diff:.1f}）"))
    for issue in report.issues:
        log.warning("QA page %d [%s] %s", issue.page + 1, issue.kind, issue.message)
    return report
