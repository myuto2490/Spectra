"""PDF ページからテキストブロック・図領域・表領域を取り出す。"""

from __future__ import annotations

import logging
import re
import statistics

import pymupdf

from .models import Line, Rect, Span, TextBlock, overlap_ratio, rect_area, rect_union

log = logging.getLogger(__name__)

# 合字は展開し（ﬁ → fi）、画像はテキスト抽出に含めない
TEXT_FLAGS = (
    pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE
) & ~pymupdf.TEXT_PRESERVE_LIGATURES


def _to_rect(r) -> Rect:
    return (float(r[0]), float(r[1]), float(r[2]), float(r[3]))


def extract_blocks(page: pymupdf.Page) -> list[TextBlock]:
    """ページのテキストブロックを読み取り、視覚的な行単位に整える。"""
    data = page.get_text("dict", flags=TEXT_FLAGS)
    blocks: list[TextBlock] = []
    for raw in data["blocks"]:
        if raw.get("type") != 0:
            continue
        lines: list[Line] = []
        for raw_line in raw["lines"]:
            spans = [
                Span(
                    text=s["text"],
                    font=s["font"],
                    size=float(s["size"]),
                    color=int(s["color"]),
                    flags=int(s["flags"]),
                    bbox=_to_rect(s["bbox"]),
                    origin=(float(s["origin"][0]), float(s["origin"][1])),
                )
                for s in raw_line["spans"]
                if s["text"]
            ]
            if not spans or not "".join(s.text for s in spans).strip():
                continue
            d = raw_line.get("dir", (1.0, 0.0))
            lines.append(Line(spans=spans, bbox=_to_rect(raw_line["bbox"]), direction=(float(d[0]), float(d[1]))))
        if not lines:
            continue
        for part in _split_block(_merge_visual_lines(lines)):
            blocks.append(_make_block(page.number, len(blocks), part))
    blocks = _absorb_fragments(blocks)
    blocks = _merge_paragraph_lines(blocks)
    for i, b in enumerate(blocks):
        b.index = i
    return blocks


def _make_block(page: int, index: int, lines: list[Line]) -> TextBlock:
    bbox = lines[0].bbox
    for ln in lines[1:]:
        bbox = rect_union(bbox, ln.bbox)
    return TextBlock(page=page, index=index, bbox=bbox, lines=lines)


def _is_rotated(b: TextBlock) -> bool:
    return any(abs(ln.direction[1]) > 1e-3 or ln.direction[0] < 0 for ln in b.lines)


def _merge_paragraph_lines(blocks: list[TextBlock]) -> list[TextBlock]:
    """1 行ずつ別のブロックになっている段落（行間の広い原稿形式の論文など）を 1 つにまとめる。

    左端・右端・文字サイズ・行送りがそろって続いている行だけを結合する。
    前の行が短く終わっている（段落の最終行）場合や、見出しのような短い行の後は結合しない。
    """
    out: list[TextBlock] = []
    for b in blocks:
        prev = out[-1] if out else None
        if prev is not None and _continues_paragraph(prev, b):
            prev.lines = prev.lines + b.lines
            prev.bbox = rect_union(prev.bbox, b.bbox)
        else:
            out.append(b)
    return out


def _continues_paragraph(a: TextBlock, b: TextBlock) -> bool:
    if _is_rotated(a) or _is_rotated(b):
        return False
    size_a, size_b = _line_size(a.lines[-1]), _line_size(b.lines[0])
    if abs(size_a - size_b) > max(0.6, size_a * 0.06):
        return False
    size = size_a
    last, first = a.lines[-1], b.lines[0]
    # 縦方向：すぐ下に続いていること。複数行あるなら行送りもそろっていること
    gap = first.bbox[1] - last.bbox[3]
    if gap < -1 or gap > size * 1.5:
        return False
    if len(a.lines) >= 2:
        pitch = a.lines[-1].baseline - a.lines[-2].baseline
        if abs((first.baseline - last.baseline) - pitch) > max(2.0, pitch * 0.25):
            return False
    # 横方向：同じ段の中で左右がそろっていること（1 行目の字下げは許す）
    left = min(ln.bbox[0] for ln in a.lines[1:]) if len(a.lines) >= 2 else a.bbox[0]
    if not (-size * 1.5 <= first.bbox[0] - left <= size * 1.5 or 0 <= a.bbox[0] - first.bbox[0] <= size * 3):
        return False
    right = a.bbox[2]
    if first.bbox[2] > right + size * 1.5:
        return False
    # 前の行が右端まで届いていない＝段落の最終行（または見出し）
    last_is_short = last.bbox[2] < right - size * 1.5
    last_is_narrow = (last.bbox[2] - last.bbox[0]) < (first.bbox[2] - first.bbox[0]) * 0.6
    return not (last_is_short or last_is_narrow)


def _absorb_fragments(blocks: list[TextBlock]) -> list[TextBlock]:
    """段落の矩形の内側にある小さな断片ブロック（分数・根号・総和記号など）を段落に取り込む。

    取り込まないと、段落を訳文に置き換えたときに断片だけが取り残されて訳文と重なる。
    """
    absorbed: set[int] = set()
    for frag in blocks:
        if any(abs(ln.direction[1]) > 1e-3 for ln in frag.lines):
            continue
        area = rect_area(frag.bbox)
        best = None
        for host in blocks:
            if host is frag or id(host) in absorbed or rect_area(host.bbox) < area * 2:
                continue
            inside = overlap_ratio(frag.bbox, host.bbox) >= 0.6
            if inside and (best is None or rect_area(host.bbox) < rect_area(best.bbox)):
                best = host
        if best is not None:
            merged = _merge_visual_lines(best.lines + [Line(list(ln.spans), ln.bbox, ln.direction) for ln in frag.lines])
            best.lines = merged
            best.bbox = rect_union(best.bbox, frag.bbox)
            absorbed.add(id(frag))
    return [b for b in blocks if id(b) not in absorbed]


def _merge_visual_lines(lines: list[Line]) -> list[Line]:
    """PyMuPDF が別行として返す同一行上の断片（上付き・下付き・字間の広い単語）を 1 行にまとめる。"""
    merged: list[Line] = []
    for ln in lines:
        if ln.direction != (1.0, 0.0) and abs(ln.direction[1]) > 1e-3:
            merged.append(ln)
            continue
        target = None
        for cand in reversed(merged[-3:]):
            if abs(cand.direction[1]) > 1e-3:
                continue
            h_overlap = min(ln.bbox[2], cand.bbox[2]) - max(ln.bbox[0], cand.bbox[0])
            if h_overlap < 1.0 and _vertically_aligned(ln.bbox, cand.bbox):
                target = cand
                break
        if target is None:
            merged.append(Line(spans=list(ln.spans), bbox=ln.bbox, direction=ln.direction))
        else:
            spans = sorted(target.spans + ln.spans, key=lambda s: s.bbox[0])
            target.spans = spans
            target.bbox = rect_union(target.bbox, ln.bbox)
    for ln in merged:
        ln.spans.sort(key=lambda s: s.bbox[0])
    merged.sort(key=lambda ln: (round(ln.bbox[1], 1), ln.bbox[0]))
    return merged


def _vertically_aligned(a: Rect, b: Rect) -> bool:
    """一方の縦方向の中心が他方の縦範囲に入っていれば同じ行とみなす。"""
    amid = (a[1] + a[3]) / 2
    bmid = (b[1] + b[3]) / 2
    return b[1] <= amid <= b[3] or a[1] <= bmid <= a[3]


def _line_size(ln: Line) -> float:
    weights: dict[float, int] = {}
    for s in ln.spans:
        n = len(s.text.strip())
        if n:
            weights[round(s.size, 1)] = weights.get(round(s.size, 1), 0) + n
    return max(weights, key=weights.get) if weights else ln.spans[0].size


def _all_bold(ln: Line) -> bool:
    spans = [s for s in ln.spans if s.text.strip()]
    return bool(spans) and all(s.is_bold for s in spans)


def _split_block(lines: list[Line]) -> list[list[Line]]:
    """1 ブロック内に見出しと本文など性質の異なる行が混在する場合に分割する。"""
    if len(lines) <= 1:
        return [lines]
    gaps = [lines[i + 1].bbox[1] - lines[i].bbox[3] for i in range(len(lines) - 1)]
    typical_gap = statistics.median(gaps) if gaps else 0.0
    if _heading_first_line(lines):
        return [[lines[0]], *_split_block(lines[1:])]
    parts: list[list[Line]] = [[lines[0]]]
    for prev, ln, gap in zip(lines, lines[1:], gaps):
        size_prev, size_cur = _line_size(prev), _line_size(ln)
        size_change = abs(size_prev - size_cur) > max(1.0, 0.12 * size_prev)
        bold_change = _all_bold(prev) != _all_bold(ln) and len(parts[-1]) <= 2 and _all_bold(prev)
        large_gap = gap > max(typical_gap * 2.5, size_cur * 0.9)
        not_below = ln.bbox[1] < prev.bbox[1] - 1  # 別段に飛んだ
        if size_change or bold_change or large_gap or not_below:
            parts.append([ln])
        else:
            parts[-1].append(ln)
    return parts


SECTION_NAME = re.compile(
    r"^\s*((\d+(\.\d+)*|[ivxIVX]+|[A-Z])\.?\s+)?"
    r"(abstract|summary|introduction|related\s+works?|background|preliminaries|methods?|methodology|"
    r"approach|experiments?|experimental\s+setup|evaluation|results?|discussion|conclusions?|"
    r"limitations|future\s+work|acknowledge?ments?|references|bibliography|appendix|appendices|"
    r"keywords?|index\s+terms|observations?|analysis|data)\b[\s.:—-]*$",
    re.IGNORECASE,
)


def _heading_first_line(lines: list[Line]) -> bool:
    """段落の 1 行目が独立した見出し（"Abstract" など）になっているか。"""
    if len(lines) < 2:
        return False
    first, rest = lines[0], lines[1:]
    text = first.text.strip()
    if not text or len(text.split()) > 6:
        return False
    if SECTION_NAME.match(text):
        return True
    widths = sorted(ln.bbox[2] - ln.bbox[0] for ln in rest)
    median = widths[len(widths) // 2]
    first_w = first.bbox[2] - first.bbox[0]
    first_c = (first.bbox[0] + first.bbox[2]) / 2
    rest_c = (min(ln.bbox[0] for ln in rest) + max(ln.bbox[2] for ln in rest)) / 2
    size = _line_size(first)
    # 短く、段落の中央に置かれた行は見出し
    return first_w < median * 0.5 and abs(first_c - rest_c) < size and not text.endswith((".", ","))


def find_figure_rects(page: pymupdf.Page) -> list[Rect]:
    """図とみなす領域（画像・まとまったベクター描画）を返す。"""
    page_area = rect_area(_to_rect(page.rect))
    rects: list[Rect] = []
    for info in page.get_image_info():
        r = _to_rect(info["bbox"])
        area = rect_area(r)
        # ページ全体を覆う背景画像や極小の装飾は図として扱わない
        if area < 100 or area > page_area * 0.8:
            continue
        rects.append(r)

    try:
        drawings = page.get_drawings()
    except Exception:  # 壊れたコンテンツストリーム
        log.warning("page %d: failed to read drawings", page.number, exc_info=True)
        return rects
    if not drawings:
        return rects
    try:
        clusters = page.cluster_drawings(drawings=drawings)
    except Exception:
        clusters = []
    for c in clusters:
        r = _to_rect(c)
        w, h = r[2] - r[0], r[3] - r[1]
        if w < 24 or h < 24 or rect_area(r) > page_area * 0.8:
            continue
        inside = [d for d in drawings if _inside(_to_rect(d["rect"]), r)]
        # 単純な枠（強調ボックス・背景色）は図ではない。中の文章は翻訳する
        if len(inside) <= 2 and all(_is_plain_box(d) for d in inside):
            continue
        rects.append(r)
    return rects


def _inside(inner: Rect, outer: Rect, tol: float = 1.0) -> bool:
    return (
        inner[0] >= outer[0] - tol
        and inner[1] >= outer[1] - tol
        and inner[2] <= outer[2] + tol
        and inner[3] <= outer[3] + tol
    )


def _is_plain_box(drawing: dict) -> bool:
    items = drawing.get("items", [])
    kinds = {it[0] for it in items}
    return kinds <= {"re", "qu"} or (kinds <= {"l"} and len(items) <= 4)


def find_table_rects(page: pymupdf.Page) -> list[Rect]:
    try:
        tables = page.find_tables()
    except Exception:
        log.debug("page %d: table detection failed", page.number, exc_info=True)
        return []
    return [_to_rect(t.bbox) for t in tables.tables if rect_area(_to_rect(t.bbox)) > 0]
