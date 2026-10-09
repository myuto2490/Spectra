"""原文テキストの除去と訳文の描画。

除去は redaction で「文字だけ」を対象にし、画像とベクター描画には触れない。
翻訳対象外のブロックは一切変更しない。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

import pymupdf

from .fontbook import FontBook, FontRole
from .layout import Frame, LayoutResult, PlacedGlyphs, PlacedRun, layout
from .markup import parse
from .models import (
    DocumentAnalysis,
    InlineRun,
    PageAnalysis,
    Rect,
    Region,
    Segment,
    inflate,
    rect_area,
    rect_intersect,
)
from .options import RenderOptions

log = logging.getLogger(__name__)

SHRINK_WARNING = 0.7  # これ未満に縮小した段落は報告する


@dataclass
class SegmentReport:
    segment_id: str
    page: int
    scale: float
    fits: bool


@dataclass
class RenderReport:
    segments: list[SegmentReport] = field(default_factory=list)

    @property
    def shrunk(self) -> list[SegmentReport]:
        return [s for s in self.segments if s.scale < SHRINK_WARNING or not s.fits]


def _color(rgb: int) -> tuple[float, float, float]:
    return ((rgb >> 16 & 255) / 255, (rgb >> 8 & 255) / 255, (rgb & 255) / 255)


class PageGeometry:
    """領域拡張の判定に使う、ページ上の障害物（他のテキスト・図・表・罫線）。"""

    def __init__(self, page: pymupdf.Page, pa: PageAnalysis):
        self.page_rect: Rect = tuple(page.rect)  # type: ignore[assignment]
        self.blocks: list[tuple[int, Rect]] = [(d.block.index, d.block.bbox) for d in pa.decisions]
        self.others: list[Rect] = list(pa.figure_rects) + list(pa.table_rects)
        try:
            for d in page.get_drawings():
                r = tuple(d["rect"])
                if rect_area(r) < page.rect.width * page.rect.height * 0.5:
                    self.others.append(r)  # type: ignore[arg-type]
        except Exception:
            log.debug("page %d: drawings unavailable", page.number, exc_info=True)
        xs = [b[1] for b in self.blocks if (b[1][2] - b[1][0]) > 0]
        if xs:
            self.content: Rect = (
                min(r[0] for r in xs),
                min(r[1] for r in xs),
                max(r[2] for r in xs),
                max(r[3] for r in xs),
            )
        else:
            self.content = self.page_rect

    def obstacles(self, exclude_blocks: set[int], own: Rect) -> list[Rect]:
        result = [r for idx, r in self.blocks if idx not in exclude_blocks]
        # 自分を内包する背景の枠などは障害物ではない
        result += [r for r in self.others if rect_area(rect_intersect(r, own)) <= 0]
        return result


def _expand(region: Region, geo: PageGeometry, own_blocks: set[int], horizontal: bool, vertical: bool) -> Rect:
    x0, y0, x1, y1 = region.rect
    size = region.style.size
    obstacles = geo.obstacles(own_blocks, region.rect)
    margin = max(1.5, size * 0.3)

    if horizontal:
        band = [o for o in obstacles if o[1] < y1 - 0.5 and o[3] > y0 + 0.5]
        right_limit = min([o[0] for o in band if o[0] >= x1 - 0.5] + [geo.content[2] + margin]) - margin
        left_limit = max([o[2] for o in band if o[2] <= x0 + 0.5] + [geo.content[0] - margin]) + margin
        if region.style.align.value == "center":
            room = max(0.0, min(right_limit - x1, x0 - left_limit))
            x0, x1 = x0 - room, x1 + room
        else:
            x1 = max(x1, right_limit)

    if vertical:
        below = [o[1] for o in obstacles if o[1] >= y1 - 0.5 and o[0] < x1 - 0.5 and o[2] > x0 + 0.5]
        bottom = min(below + [geo.content[3] + 0.5])
        limit = y1 + region.style.line_pitch * 3
        y1 = max(y1, min(bottom - size * 0.5, limit))
    return (x0, y0, x1, y1)


class Renderer:
    def __init__(self, doc: pymupdf.Document, analysis: DocumentAnalysis, options: RenderOptions):
        self.doc = doc
        self.analysis = analysis
        self.options = options
        self.fonts = FontBook(options.font_path, serif=options.font_is_serif)
        self._geo: dict[int, PageGeometry] = {}
        self._pa = {pa.page: pa for pa in analysis.pages}

    def geometry(self, pno: int) -> PageGeometry:
        if pno not in self._geo:
            self._geo[pno] = PageGeometry(self.doc[pno], self._pa[pno])
        return self._geo[pno]

    # -- 組版 ------------------------------------------------------------

    def _frames(self, seg: Segment, expand: bool) -> list[Frame]:
        frames = []
        own: dict[int, set[int]] = defaultdict(set)
        for r in seg.regions:
            own[r.page].add(r.block_index)
        for i, r in enumerate(seg.regions):
            rect = r.rect
            if expand:
                single_line = len(r.line_rects) == 1 and len(seg.regions) == 1
                last = i == len(seg.regions) - 1
                rect = _expand(r, self.geometry(r.page), own[r.page], horizontal=single_line, vertical=last)
            offset = None if r.first_baseline is None else r.first_baseline - r.rect[1]
            frames.append(Frame(page=r.page, rect=rect, style=r.style, first_baseline_offset=offset))
        return frames

    def layout_segment(self, seg: Segment) -> LayoutResult:
        tokens = parse(seg.translated or "", set(seg.runs))
        opts = self.options
        result = layout(tokens, seg.runs, self._frames(seg, expand=False), self.fonts, opts.text_scale)
        if result.fits and result.scale >= opts.text_scale - 1e-6:
            return result
        expanded = layout(tokens, seg.runs, self._frames(seg, expand=True), self.fonts, opts.text_scale)
        if (expanded.fits, expanded.scale) > (result.fits, result.scale):
            return expanded
        return result

    # -- 描画 ------------------------------------------------------------

    def render(
        self,
        progress: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> RenderReport:
        report = RenderReport()
        by_page: dict[int, list[PlacedGlyphs]] = defaultdict(list)
        erase: dict[int, list[Rect]] = defaultdict(list)
        moves: list[tuple[InlineRun, PlacedRun]] = []
        segments = [s for s in self.analysis.segments if s.translated]
        total = len(segments) + len(self._pa)
        done = 0
        for seg in segments:
            if should_cancel and should_cancel():
                raise InterruptedError
            result = self.layout_segment(seg)
            for g in result.placed:
                by_page[g.page].append(g)
            for r in seg.regions:
                erase[r.page].extend(r.line_rects)
            for pr in result.runs:
                run = seg.runs.get(pr.run_id)
                if run is not None and run.bbox is not None and run.origin is not None:
                    moves.append((run, pr))
            report.segments.append(SegmentReport(seg.id, seg.regions[0].page, result.scale, result.fits))
            done += 1
            if progress:
                progress(done, total)

        # 数式に付随するベクター線（根号の上線・分数の横線など）を数式と一緒に移動する
        vectors = self._collect_inline_vectors(moves)

        for pno in sorted(self._pa):
            if should_cancel and should_cancel():
                raise InterruptedError
            page = self.doc[pno]
            if erase.get(pno):
                self._erase(page, erase[pno])
            originals = [d for d, _, _ in vectors if d["_page"] == pno]
            if originals:
                self._erase_vectors(page, originals)
        for pno in sorted(self._pa):
            page = self.doc[pno]
            if by_page.get(pno):
                self._write(page, by_page[pno])
            for d, run, pr in vectors:
                if pr.page == pno:
                    self._redraw(page, d, run, pr)
            done += 1
            if progress:
                progress(done, total)
        return report

    def _collect_inline_vectors(self, moves: list[tuple[InlineRun, PlacedRun]]) -> list[tuple[dict, InlineRun, PlacedRun]]:
        by_page: dict[int, list[tuple[InlineRun, PlacedRun]]] = defaultdict(list)
        for run, pr in moves:
            if run.page is not None:
                by_page[run.page].append((run, pr))
        found: list[tuple[dict, InlineRun, PlacedRun]] = []
        for pno, items in by_page.items():
            try:
                drawings = self.doc[pno].get_drawings()
            except Exception:
                continue
            taken: set[int] = set()
            for run, pr in items:
                zone = inflate(run.bbox, 0.8)  # type: ignore[arg-type]
                for i, d in enumerate(drawings):
                    r = tuple(d["rect"])
                    if i in taken or not (zone[0] <= r[0] and zone[1] <= r[1] and r[2] <= zone[2] and r[3] <= zone[3]):
                        continue
                    taken.add(i)
                    found.append(({**d, "_page": pno}, run, pr))
        return found

    @staticmethod
    def _erase_vectors(page: pymupdf.Page, drawings: list[dict]) -> None:
        for d in drawings:
            r = pymupdf.Rect(d["rect"])
            w = (d.get("width") or 1.0) / 2 + 0.3
            page.add_redact_annot(pymupdf.Rect(r.x0 - w, r.y0 - w, r.x1 + w, r.y1 + w), fill=False, cross_out=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=pymupdf.PDF_REDACT_TEXT_NONE,
        )

    @staticmethod
    def _redraw(page: pymupdf.Page, d: dict, run: InlineRun, pr: PlacedRun) -> None:
        ox, oy = run.origin  # type: ignore[misc]
        f = pr.factor

        def tp(p) -> pymupdf.Point:
            return pymupdf.Point(pr.x + (p.x - ox) * f, pr.baseline + (p.y - oy) * f)

        shape = page.new_shape()
        for item in d.get("items", []):
            kind = item[0]
            if kind == "l":
                shape.draw_line(tp(item[1]), tp(item[2]))
            elif kind == "re":
                r = pymupdf.Rect(item[1])
                shape.draw_rect(pymupdf.Rect(tp(r.tl), tp(r.br)))
            elif kind == "qu":
                q = item[1]
                shape.draw_quad(pymupdf.Quad(tp(q.ul), tp(q.ur), tp(q.ll), tp(q.lr)))
            elif kind == "c":
                shape.draw_bezier(tp(item[1]), tp(item[2]), tp(item[3]), tp(item[4]))
        width = d.get("width")
        shape.finish(
            color=d.get("color"),
            fill=d.get("fill"),
            width=(width if width is not None else 1.0) * f,
            closePath=bool(d.get("closePath")),
            even_odd=bool(d.get("even_odd")),
            stroke_opacity=d.get("stroke_opacity") or 1,
            fill_opacity=d.get("fill_opacity") or 1,
        )
        shape.commit(overlay=True)

    @staticmethod
    def _erase(page: pymupdf.Page, rects: list[Rect]) -> None:
        for r in rects:
            h = r[3] - r[1]
            # 上下を少し縮めて、隣接する行（独立数式など）の文字を巻き込まないようにする
            shrink = h * 0.18
            rect = pymupdf.Rect(r[0] - 0.2, r[1] + shrink, r[2] + 0.2, r[3] - shrink)
            if rect.is_empty:
                continue
            page.add_redact_annot(rect, fill=False, cross_out=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )

    def _write(self, page: pymupdf.Page, glyphs: list[PlacedGlyphs]) -> None:
        writers: dict[int, pymupdf.TextWriter] = {}
        bold_jp: list[PlacedGlyphs] = []
        for g in glyphs:
            if g.bold and g.role == FontRole.JP:
                bold_jp.append(g)
                continue
            tw = writers.get(g.color)
            if tw is None:
                tw = writers[g.color] = pymupdf.TextWriter(page.rect)
            tw.append((g.x, g.y), g.text, font=self.fonts.font(g.role), fontsize=g.size)
        for color, tw in writers.items():
            tw.write_text(page, color=_color(color))
        if bold_jp:
            # 内蔵和文フォントには太字が無いため、輪郭線を重ねて太らせる
            name = "SpectraJP"
            page.insert_font(fontname=name, fontbuffer=self.fonts.jp.buffer)
            for g in bold_jp:
                c = _color(g.color)
                page.insert_text(
                    (g.x, g.y), g.text, fontname=name, fontsize=g.size,
                    color=c, fill=c, render_mode=2, border_width=0.035,
                )
