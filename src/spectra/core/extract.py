"""PDF ページからテキストブロック・図領域・表領域を取り出す。"""

from __future__ import annotations

import logging
import math
import re
import statistics

import pymupdf

from .fonts import is_math_font
from .models import Line, Rect, Span, TextBlock, overlap_ratio, rect_area, rect_union

log = logging.getLogger(__name__)

# 合字は展開し（ﬁ → fi）、画像はテキスト抽出に含めない
TEXT_FLAGS = (
    pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE
) & ~pymupdf.TEXT_PRESERVE_LIGATURES


_EQUATION_NUMBER = re.compile(r"\S\s*\(\d{1,3}[a-z]?\)\s*$")


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
    # 先に 1 行を組み立て直してから（数式で高くなった行の形が決まってから）段落として結合する
    rows: list[TextBlock] = []
    for b in blocks:
        prev = rows[-1] if rows else None
        if prev is not None and _math_on_last_line(prev, b):
            # 根号や分数だけのブロック（行の上下にはみ出している）：前の行の一部として取り込む
            last = prev.lines[-1]
            last.spans = sorted(last.spans + [s for ln in b.lines for s in ln.spans], key=lambda s: s.bbox[0])
            last.bbox = rect_union(last.bbox, b.bbox)
            prev.bbox = rect_union(prev.bbox, b.bbox)
        elif prev is not None and _continues_same_line(prev, b):
            # 分数などで 1 行が左右のブロックに分断されている：同じ行としてつなぎ直す
            prev.lines = _merge_visual_lines(prev.lines + [Line(list(ln.spans), ln.bbox, ln.direction) for ln in b.lines])
            prev.bbox = rect_union(prev.bbox, b.bbox)
        else:
            rows.append(b)
    out: list[TextBlock] = []
    for b in rows:
        prev = out[-1] if out else None
        if prev is not None and _continues_paragraph(prev, b):
            prev.lines = prev.lines + b.lines
            prev.bbox = rect_union(prev.bbox, b.bbox)
        else:
            out.append(b)
    return out


def _continues_same_line(a: TextBlock, b: TextBlock) -> bool:
    """a の最後の行と b の最初の行が、同じベースラインで左右に並んでいる。"""
    if _is_rotated(a) or _is_rotated(b):
        return False
    last, first = a.lines[-1], b.lines[0]
    size = _line_size(last)
    same_baseline = abs(first.baseline - last.baseline) <= max(1.5, size * 0.15)
    # 分数の分子と分母のように、境目で少し重なることがある
    to_the_right = first.bbox[0] >= last.bbox[2] - size * 1.5 or _overlap_is_math(last, first)
    to_the_right = to_the_right and first.bbox[0] - last.bbox[2] < size * 6
    to_the_right = to_the_right and first.bbox[0] > last.bbox[0] + (last.bbox[2] - last.bbox[0]) * 0.5
    # 行頭・行末の数式（σ² など）は本文と文字の大きさが違う
    same_size = abs(_line_size(first) - size) <= max(0.6, size * 0.06)
    same_size = same_size or _all_math(last, _line_size(first)) or _all_math(first, size)
    return same_baseline and to_the_right and same_size


def _math_like(span: Span, size: float) -> bool:
    """数式の部品とみなすスパン（数式フォント、または本文より小さい分子・分母・添字）。"""
    return not span.text.strip() or is_math_font(span.font) or span.size < size * 0.8


def _is_equation_line(ln: Line, size: float) -> bool:
    """独立数式の行：数式フォントを含み、文章の単語（2 文字以上の英字）が無いか、式番号で終わり長い単語が無い。"""
    if not any(is_math_font(s.font) for s in ln.spans):
        return False
    text = "".join(s.text for s in ln.spans if not _math_like(s, size))
    if not re.search(r"[A-Za-z]{2,}", text):
        return True
    return bool(_EQUATION_NUMBER.search(ln.text)) and not re.search(r"[A-Za-z]{4,}", text)


def _all_math(ln: Line, size: float) -> bool:
    return all(_math_like(s, size) for s in ln.spans)


def _overlapping_spans(a: Line, b: Line) -> list[Span]:
    """a と b の文字のうち、左右に重なっているもの（空白は除く）。"""
    sa = [s for s in a.spans if s.text.strip()]
    sb = [s for s in b.spans if s.text.strip()]
    out: list[Span] = []
    for s in sa:
        for t in sb:
            if min(s.bbox[2], t.bbox[2]) - max(s.bbox[0], t.bbox[0]) >= 1.0:
                out += [s, t]
    return out


def _overlap_is_math(a: Line, b: Line) -> bool:
    """左右に重なっている文字が、数式の部品だけか（分子と分母が上下に並んでいる）。"""
    over = _overlapping_spans(a, b)
    size = max(_line_size(a), _line_size(b))
    return bool(over) and all(_math_like(s, size) for s in over)


def _math_on_last_line(a: TextBlock, b: TextBlock) -> bool:
    """b が数式の部品だけでできていて、a の最後の行のすぐ右で、その行の上下にはみ出す位置にある。"""
    if _is_rotated(a) or _is_rotated(b):
        return False
    last = a.lines[-1]
    size = _line_size(last)
    if not all(_math_like(s, size) for s in b.spans):
        return False
    base = last.baseline
    x0, y0, x1, y1 = b.bbox
    beside = last.bbox[0] < x0 <= last.bbox[2] + size * 2
    within = base - size * 2.2 <= y0 and y1 <= base + size * 1.2 and y0 < last.bbox[1]
    return beside and within


def _continues_paragraph(a: TextBlock, b: TextBlock) -> bool:
    if _is_rotated(a) or _is_rotated(b):
        return False
    size_a, size_b = _line_size(a.lines[-1]), _line_size(b.lines[0])
    if abs(size_a - size_b) > max(0.6, size_a * 0.06):
        return False
    size = size_a
    last, first = a.lines[-1], b.lines[0]
    if _is_equation_line(first, size) or _is_equation_line(last, size):
        return False  # 段落の前後の独立数式
    # 縦方向：すぐ下に続いていること。複数行あるなら行送りもそろっていること
    gap = first.bbox[1] - last.bbox[3]
    # 分数などで行が高くなると、前後のブロックが上下に重なることがある（負の隙間も許す）
    if gap < -size * 1.5 or gap > size * 1.5:
        return False
    if len(a.lines) >= 2:
        pitch = a.lines[-1].baseline - a.lines[-2].baseline
        step = first.baseline - last.baseline
        # 根号や分数で高くなった行は、行送りが広がる（行の矩形どうしが接していれば続きとみなす）
        tall = max(first.bbox[3] - first.bbox[1], last.bbox[3] - last.bbox[1]) > size * 1.6
        if abs(step - pitch) > max(2.0, pitch * 0.25) and not (tall and step > pitch and gap <= size * 0.6):
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
    # 上から順に見る（取り込んだ断片のように順序が前後していても、同じ高さの行どうしを照らし合わせる）
    for ln in sorted(lines, key=lambda ln: ln.bbox[1]):
        if ln.direction != (1.0, 0.0) and abs(ln.direction[1]) > 1e-3:
            merged.append(ln)
            continue
        target = None
        for cand in reversed(merged[-3:]):
            if abs(cand.direction[1]) > 1e-3:
                continue
            # 文字どうしが左右に重ならなければ同じ行。分子と分母のように上下に並ぶ数式の部品は、重なっていても同じ行
            side_by_side = not _overlapping_spans(ln, cand) or _overlap_is_math(ln, cand)
            if side_by_side and _vertically_aligned(ln.bbox, cand.bbox):
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
    # 先頭の見出しの行を 1 行ずつ切り離す（見出しらしい行が続く PDF でも再帰が深くならないよう、繰り返しで行う）
    heads: list[list[Line]] = []
    while len(lines) > 1 and _heading_first_line(lines):
        heads.append([lines[0]])
        lines = lines[1:]
    if len(lines) <= 1:
        return [*heads, lines]
    gaps = [lines[i + 1].bbox[1] - lines[i].bbox[3] for i in range(len(lines) - 1)]
    typical_gap = statistics.median(gaps) if gaps else 0.0
    parts: list[list[Line]] = [*heads, [lines[0]]]
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
    from .classify import squeeze_letter_spacing

    text = squeeze_letter_spacing(first.text.strip())
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


def read_drawings(page: pymupdf.Page) -> list[dict]:
    """ページのベクター描画を読む（重い処理なので、ページごとに 1 回だけ呼んで使い回す）。"""
    try:
        return page.get_drawings()
    except Exception:  # 壊れたコンテンツストリーム
        log.warning("page %d: failed to read drawings", page.number, exc_info=True)
        return []


def find_figure_rects(page: pymupdf.Page, drawings: list[dict] | None = None) -> list[Rect]:
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

    if drawings is None:
        drawings = read_drawings(page)
    if not drawings:
        return rects
    for r, members in cluster_drawings(drawings, _to_rect(page.rect)):
        w, h = r[2] - r[0], r[3] - r[1]
        if w < 24 or h < 24 or rect_area(r) > page_area * 0.8:
            continue
        # 単純な枠（強調ボックス・背景色）は図ではない。中の文章は翻訳する
        if len(members) <= 2 and all(_is_plain_box(d) for d in members):
            continue
        rects.append(r)
    return rects


MAX_GRID_CELLS = 2_000_000  # cluster_drawings のマス目の総数の上限（A0 の 3 pt 角で約 90 万）


def cluster_drawings(drawings: list[dict], page_rect: Rect, cell: float = 3.0) -> list[tuple[Rect, list[dict]]]:
    """近くにあるベクター描画をまとめる（図の領域の検出）。

    ページを cell pt 角のマス目に区切り、描画が占めるマスとその隣（許容誤差）を塗って、
    つながったマスの塊ごとにまとめる。描画の数に比例する時間で済むため、
    数万〜数十万の図形を含む散布図のページでも速い（PyMuPDF の cluster_drawings は二乗に近い）。
    """
    x0, y0, x1, y1 = page_rect
    if not all(math.isfinite(v) for v in page_rect):
        return []
    # 極端に大きなページ（細工された PDF など）でマス目がメモリを使い切らないよう、マスを粗くして総数を抑える
    while (max(0.0, x1 - x0) / cell + 2) * (max(0.0, y1 - y0) / cell + 2) > MAX_GRID_CELLS:
        cell *= 2
    cols = max(1, int((x1 - x0) / cell) + 2)
    rows = max(1, int((y1 - y0) / cell) + 2)
    grid = [0] * (cols * rows)  # 0: 空き、それ以外: 描画あり

    def cells(r: Rect) -> tuple[int, int, int, int]:
        c0 = min(cols - 1, max(0, int((r[0] - x0) / cell) - 1))
        c1 = min(cols - 1, max(0, int((r[2] - x0) / cell) + 1))
        r0 = min(rows - 1, max(0, int((r[1] - y0) / cell) - 1))
        r1 = min(rows - 1, max(0, int((r[3] - y0) / cell) + 1))
        return c0, r0, c1, r1

    boxes = []
    for d in drawings:
        r = _to_rect(d["rect"])
        boxes.append(r)
        c0, r0, c1, r1 = cells(r)
        w, h = c1 - c0 + 1, r1 - r0 + 1
        if w * h > 4 * (w + h) and w > 2 and h > 2:
            # 大きな矩形（枠線・背景）は外周だけを塗る（中の文章の領域まで図にしないため）
            for c in range(c0, c1 + 1):
                grid[r0 * cols + c] = grid[r1 * cols + c] = 1
            for rr in range(r0, r1 + 1):
                grid[rr * cols + c0] = grid[rr * cols + c1] = 1
        else:
            for rr in range(r0, r1 + 1):
                base = rr * cols
                for c in range(c0, c1 + 1):
                    grid[base + c] = 1

    # つながったマスに番号を付ける
    label = [0] * len(grid)
    comp_box: list[list[float]] = []
    current = 0
    for start in range(len(grid)):
        if not grid[start] or label[start]:
            continue
        current += 1
        stack = [start]
        label[start] = current
        while stack:
            i = stack.pop()
            r, c = divmod(i, cols)
            for j in (i - 1 if c else -1, i + 1 if c < cols - 1 else -1, i - cols, i + cols):
                if 0 <= j < len(grid) and grid[j] and not label[j]:
                    label[j] = current
                    stack.append(j)
        comp_box.append([])

    members: dict[int, list[int]] = {}
    for k, r in enumerate(boxes):
        cx = min(cols - 1, max(0, int(((r[0] + r[2]) / 2 - x0) / cell)))
        cy = min(rows - 1, max(0, int(((r[1] + r[3]) / 2 - y0) / cell)))
        lab = label[cy * cols + cx]
        if not lab:
            # 大きな枠の中心は塗っていないので、外周のマスで所属を決める
            c0, r0, _c1, _r1 = cells(r)
            lab = label[r0 * cols + c0]
        if lab:
            members.setdefault(lab, []).append(k)

    out: list[tuple[Rect, list[dict]]] = []
    for idxs in members.values():
        bb = boxes[idxs[0]]
        for k in idxs[1:]:
            bb = rect_union(bb, boxes[k])
        out.append((bb, [drawings[k] for k in idxs]))
    return out


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


def find_table_rects(page: pymupdf.Page, drawings: list[dict] | None = None) -> list[Rect]:
    """罫線で囲まれた表の領域。罫線のほとんど無いページでは検出を省く（重い処理のため）。"""
    if drawings is not None:
        strokes = sum(1 for d in drawings for it in d.get("items", []) if it[0] in ("l", "re"))
        # 罫線の表は多くても数百本。数千を超える図形はグラフなどの図なので、表の検出は省く
        # （罫線の無い表と同じく、列の空きや行の揃いからの判定は引き続き行う）
        if strokes < 4 or len(drawings) > 3000:
            return []
    try:
        tables = page.find_tables()
    except Exception:
        log.debug("page %d: table detection failed", page.number, exc_info=True)
        return []
    return [_to_rect(t.bbox) for t in tables.tables if rect_area(_to_rect(t.bbox)) > 0]
