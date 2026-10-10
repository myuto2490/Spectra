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
    erase_rect,
    inflate,
    rect_area,
    rect_intersect,
)
from .options import RenderOptions
from .redact import erase_text, stub_forms
from .textnorm import normalize_translation

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


def _used_text(segments: list[Segment]) -> str:
    chars: set[str] = set()
    for seg in segments:
        chars.update(seg.translated or "")
        for run in seg.runs.values():
            chars.update(run.text)
    return "".join(sorted(chars))


def _complement(page: Rect, keep: list[Rect]) -> list[Rect]:
    """page から keep の矩形群を除いた領域を、重ならない矩形の集まりで返す。"""
    ys = sorted({page[1], page[3], *[min(max(k[1], page[1]), page[3]) for k in keep], *[min(max(k[3], page[1]), page[3]) for k in keep]})
    out: list[Rect] = []
    for ya, yb in zip(ys, ys[1:]):
        if yb - ya <= 0:
            continue
        spans = sorted((k[0], k[2]) for k in keep if k[1] < yb and k[3] > ya)
        x = page[0]
        for a, b in spans:
            if a > x:
                out.append((x, ya, a, yb))
            x = max(x, b)
        if x < page[2]:
            out.append((x, ya, page[2], yb))
    return out


def _merge_rects(rects: list[Rect], gap: float = 2.0) -> list[Rect]:
    """同じ行で隣り合う範囲を 1 つにまとめる（消去の指定が多いと処理が非常に遅くなるため）。"""
    rows: dict[tuple[int, int], list[Rect]] = defaultdict(list)
    for r in rects:
        rows[(round(r[1] * 2), round(r[3] * 2))].append(r)
    merged: list[Rect] = []
    for row in rows.values():
        row.sort(key=lambda r: r[0])
        cur = row[0]
        for r in row[1:]:
            if r[0] - cur[2] <= gap:
                cur = (cur[0], min(cur[1], r[1]), max(cur[2], r[2]), max(cur[3], r[3]))
            else:
                merged.append(cur)
                cur = r
        merged.append(cur)
    return merged


class PageGeometry:
    """領域拡張の判定に使う、ページ上の障害物（他のテキスト・図・表・罫線）。"""

    def __init__(self, page: pymupdf.Page, pa: PageAnalysis):
        self.page_rect: Rect = tuple(page.rect)  # type: ignore[assignment]
        self.blocks: list[tuple[int, Rect]] = [(d.block.index, d.block.bbox) for d in pa.decisions]
        self.others: list[Rect] = list(pa.figure_rects) + list(pa.table_rects)
        page_area = page.rect.width * page.rect.height
        self.others += [r for r in pa.drawing_rects if rect_area(r) < page_area * 0.5]
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
    def __init__(
        self,
        doc: pymupdf.Document,
        analysis: DocumentAnalysis,
        options: RenderOptions,
        source: pymupdf.Document | None = None,
    ):
        """source: 翻訳前の元の文書。渡すと、段落中の数式を元の見た目のまま移して配置する。"""
        self.source = source
        self.doc = doc
        self.analysis = analysis
        self.options = options
        self.fonts = FontBook(options.font_path, serif=options.font_is_serif, jp_bold_path=options.bold_font_path)
        self._geo: dict[int, PageGeometry] = {}
        self._kept: dict[int, list[Rect]] = {}
        self._regions_by_page: dict[int, list[tuple[str, Rect]]] = {}
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
            rect = self._clip_to_neighbors(seg, r.page, rect)
            offset = None if r.first_baseline is None else r.first_baseline - r.rect[1]
            frames.append(Frame(page=r.page, rect=rect, style=r.style, first_baseline_offset=offset))
        return frames

    def _clip_to_neighbors(self, seg: Segment, pno: int, rect: Rect) -> Rect:
        """他の段落の枠と重なる部分を削る（訳文どうしが重なって書かれることを防ぐ）。"""
        x0, y0, x1, y1 = rect
        for other_id, o in self._translated_rects(pno):
            if other_id == seg.id or min(x1, o[2]) - max(x0, o[0]) <= 1:
                continue
            if o[1] >= y0 and o[1] < y1:  # 下にある枠に食い込んでいる
                y1 = max(y0 + 1, o[1] - 0.5)
            elif o[3] <= y1 and o[3] > y0 and o[1] < y0:  # 上にある枠が食い込んでいる
                y0 = min(y1 - 1, o[3] + 0.5)
        return (x0, y0, x1, y1)

    def _translated_rects(self, pno: int) -> list[tuple[str, Rect]]:
        if pno not in self._regions_by_page:
            self._regions_by_page[pno] = [
                (s.id, r.rect) for s in self.analysis.segments if s.translated for r in s.regions if r.page == pno
            ]
        return self._regions_by_page[pno]

    def _kept_spans(self, pno: int) -> list[Rect]:
        """翻訳しない文字の範囲（消してはいけない）。"""
        if pno not in self._kept:
            self._kept[pno] = [
                inflate(s.bbox, -0.5)
                for d in self._pa[pno].decisions
                if not d.translate
                for s in d.block.spans
                if s.text.strip()
            ]
        return self._kept[pno]

    def _erase_rects_for(self, region: Region) -> list[Rect]:
        """通常は行単位で消し、翻訳しない文字と重なる行だけスパン単位で消す（指定の数を抑えて高速にする）。"""
        kept = self._kept_spans(region.page)
        out: list[Rect] = []
        spans_per_line = region.span_rects or [[r] for r in region.line_rects]
        for line, spans in zip(region.line_rects, spans_per_line):
            e = erase_rect(line)
            if any(rect_area(rect_intersect(e, k)) > 0.5 for k in kept):
                out.extend(spans)
            else:
                out.append(line)
        return out

    def layout_segment(self, seg: Segment) -> LayoutResult:
        tokens = parse(normalize_translation(seg.translated or ""), set(seg.runs))
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
        transplants: list[tuple[InlineRun, PlacedRun]] = []
        segments = [s for s in self.analysis.segments if s.translated]
        self.fonts.prepare(_used_text(segments))
        total = len(segments) + len(self._pa)
        done = 0
        for seg in segments:
            if should_cancel and should_cancel():
                raise InterruptedError
            result = self.layout_segment(seg)
            movable = {rid for rid, run in seg.runs.items() if self._can_transplant(run)}
            for g in result.placed:
                if g.run_id is not None and g.run_id in movable:
                    continue  # 元の見た目のまま移すので、代用フォントでは描かない
                by_page[g.page].append(g)
            for r in seg.regions:
                erase[r.page].extend(self._erase_rects_for(r))
            for pr in result.runs:
                run = seg.runs.get(pr.run_id)
                if run is not None and run.bbox is not None and run.origin is not None:
                    moves.append((run, pr))
                    if pr.run_id in movable:
                        transplants.append((run, pr))
            report.segments.append(SegmentReport(seg.id, seg.regions[0].page, result.scale, result.fits))
            done += 1
            if progress:
                progress(done, total)

        # 数式に付随するベクター線（根号の上線・分数の横線など）を数式と一緒に移動する
        vectors = self._collect_inline_vectors(moves)
        stamps = self._make_stamps(transplants)

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
                # 元の見た目のまま移す数式は、線も一緒に移るので描き直さない
                if pr.page == pno and not self._can_transplant(run):
                    self._redraw(page, d, run, pr)
            for run, pr in transplants:
                if pr.page == pno:
                    self._transplant(page, run, pr, stamps)
            done += 1
            if progress:
                progress(done, total)
        return report

    def _can_transplant(self, run: InlineRun) -> bool:
        return self.source is not None and run.bbox is not None and run.origin is not None and run.page is not None

    def _make_stamps(self, transplants: list[tuple[InlineRun, PlacedRun]]) -> dict[int, tuple[pymupdf.Document, int]]:
        """数式の部分だけを残した「切り抜き用のページ」を作る。

        元のページをそのまま貼り込むと、切り抜きで隠れた部分（図の大量の図形など）も PDF に残り、
        表示が重くなる。数式の範囲の外にある文字・図形・画像を消したページから移すことで、
        貼り込む中身を数式だけにする。
        """
        by_page: dict[int, list[Rect]] = defaultdict(list)
        for run, _pr in transplants:
            by_page[run.page].append(inflate(run.bbox, 1.0))  # type: ignore[arg-type]
        if not by_page or self.source is None:
            return {}
        stamp_doc = pymupdf.open()
        stamps: dict[int, tuple[pymupdf.Document, int]] = {}
        for pno, keep in sorted(by_page.items()):
            stamp_doc.insert_pdf(self.source, from_page=pno, to_page=pno)
            sp = stamp_doc[-1]
            # 段落中の数式は図の部品（フォーム XObject）の中には無いので、部品は空にしておく
            # （残すと消去処理が部品を複製し、ファイルが膨らんで処理も遅くなる）
            stub_forms(stamp_doc, sp)
            for r in _complement(tuple(sp.rect), keep):
                sp.add_redact_annot(pymupdf.Rect(r), fill=False, cross_out=False)
            sp.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )
            stamps[pno] = (stamp_doc, sp.number)
        return stamps

    def _transplant(
        self, page: pymupdf.Page, run: InlineRun, pr: PlacedRun, stamps: dict[int, tuple[pymupdf.Document, int]]
    ) -> None:
        """元の文書の数式部分を、そのままの見た目で新しい位置へ配置する。

        字形・分数の線・根号のほか、文字コードの対応が分からない記号（Δ など）も正しく再現できる。
        配置した部分の外側は切り抜かれ、文字の抽出にも含まれない。
        """
        x0, y0, x1, y1 = run.bbox  # type: ignore[misc]
        ox, oy = run.origin  # type: ignore[misc]
        f = pr.factor
        target = pymupdf.Rect(pr.x + (x0 - ox) * f, pr.baseline + (y0 - oy) * f, pr.x + (x1 - ox) * f, pr.baseline + (y1 - oy) * f)
        if target.is_empty:
            return
        src_doc, src_page = stamps.get(run.page, (self.source, run.page))  # type: ignore[arg-type]
        try:
            page.show_pdf_page(target, src_doc, src_page, clip=pymupdf.Rect(run.bbox), keep_proportion=False, overlay=True)
        except Exception:
            log.warning("page %d: could not place inline math", page.number + 1, exc_info=True)

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
        erase_text(page, _merge_rects(rects))

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
