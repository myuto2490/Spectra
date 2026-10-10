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


def _drawing_keys(page: pymupdf.Page, exclude: list[Rect]) -> Counter | None:
    """図形の位置の一覧。exclude（訳文を書いた段落）の中にある図形（数式の線など）は含めない。"""
    try:
        drawings = page.get_drawings()
    except Exception:
        return None
    zones = [inflate(r, 2) for r in exclude]

    def inside(r) -> bool:
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
        return any(z[0] <= cx <= z[2] and z[1] <= cy <= z[3] for z in zones)

    return Counter(
        tuple(round(v, 1) for v in d["rect"]) for d in drawings if not inside(tuple(d["rect"]))
    )


def _lost_drawings(before: pymupdf.Page, after: pymupdf.Page, exclude: list[Rect]) -> int:
    """元の図形のうち、翻訳後に同じ位置に見つからないものの数。

    段落中の数式を元の見た目のまま移すと、切り抜きで隠れた（見えない）線が増えることがあるため、
    数の比較ではなく「元の図形が残っているか」で判定する。
    """
    a, b = _drawing_keys(before, exclude), _drawing_keys(after, [])
    if a is None or b is None:
        return 0
    missing = a - b
    if not missing:
        return 0
    # 描画命令の書き直しで座標がわずかにずれることがあるので、0.3pt 以内なら同じ図形とみなす
    remaining = list((b - a).elements())
    lost = 0
    for key in missing.elements():
        match = next((i for i, k in enumerate(remaining) if all(abs(x - y) <= 0.3 for x, y in zip(key, k))), None)
        if match is None:
            lost += 1
        else:
            remaining.pop(match)
    return lost


def _mean_diff(a: pymupdf.Pixmap, b: pymupdf.Pixmap) -> float:
    if (a.width, a.height, a.n) != (b.width, b.height, b.n):
        return 255.0
    sa, sb = a.samples, b.samples
    if sa == sb:
        return 0.0
    total = sum(abs(x - y) for x, y in zip(sa, sb))
    return total / max(1, len(sa))


def _image_key(info: dict) -> tuple:
    return (tuple(round(v, 1) for v in info["bbox"]), info["width"], info["height"])


def _lost_images(before: pymupdf.Page, after: pymupdf.Page) -> list[tuple]:
    """翻訳後に見た目から消えた画像。

    PDF の描画命令を書き直すと、同じ位置に重ね描きされた冗長な画像（図を細かな断片で描いた PDF に多い）が
    まとめられて数が減ることがある。その場合は見た目が変わらないため、消えた位置を高解像度で比較し、
    実際に画素が変わったものだけを返す。
    """
    from collections import Counter

    missing = Counter(_image_key(i) for i in before.get_image_info()) - Counter(
        _image_key(i) for i in after.get_image_info()
    )
    lost = []
    for key in missing.elements():
        clip = pymupdf.Rect(key[0]) + (-1, -1, 1, 1)
        clip &= before.rect
        if clip.is_empty:
            continue
        a = before.get_pixmap(clip=clip, dpi=300, alpha=False)
        b = after.get_pixmap(clip=clip, dpi=300, alpha=False)
        if a.samples != b.samples:
            lost.append(key)
    return lost


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

        lost = _lost_images(pb, pf)
        if lost:
            report.issues.append(QAIssue(pno, "images", f"画像が {len(lost)} 個失われました"))
        mine_rects = written.get(pno, [])
        lost_drawings = _lost_drawings(pb, pf, mine_rects)
        if lost_drawings:
            report.issues.append(QAIssue(pno, "drawings", f"図形が {lost_drawings} 個失われました"))

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
