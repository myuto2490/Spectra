"""注釈を、出力する PDF に標準の PDF 注釈として書き込む（docs/ANNOTATIONS.md §5）。

注釈の位置は原文のページの座標で持っている。出力の形式ごとに、原文のページがどこに来るかに合わせて書く。
ライブラリの PDF には書き込まない（出力するファイルだけ）。
"""

from __future__ import annotations

import pymupdf

from .options import OutputMode

# PDF に書くときの色（表示の色はテーマで決めるが、ファイルには固定の色を書く）
COLORS: dict[str, tuple[float, float, float]] = {
    "yellow": (1.0, 0.85, 0.2),
    "green": (0.45, 0.85, 0.4),
    "blue": (0.4, 0.7, 1.0),
    "pink": (1.0, 0.55, 0.75),
    "purple": (0.75, 0.55, 1.0),
}
BAR_X = 6.0  # 訳文の余白の帯の左端（pt）
BAR_WIDTH = 4.0


def apply_annotations(doc: pymupdf.Document, annotations: list, mode: OutputMode, pages: range | None = None) -> int:
    """annotations（library.Annotation のリスト）を doc に書き、書いた件数を返す。

    doc は core.export.build_output が作った文書。pages はそのときのページの範囲（省略すると全ページ）。
    - 翻訳のみ: 訳文のページの左の余白に同じ色の帯（中身に引用とコメント）。付箋はそのまま付箋
    - 交互対訳: 原文のページ（偶数番目）に、マーカー・下線・付箋
    - 左右対訳: 左半分（原文）に、マーカー・下線・付箋
    """
    order = list(pages) if pages is not None else list(range(doc.page_count))
    where = {pno: i for i, pno in enumerate(order)}
    written = 0
    for a in annotations:
        if a.page not in where:
            continue  # 出力の範囲の外
        i = where[a.page]
        if mode == OutputMode.INTERLEAVE:
            target = 2 * i
        elif mode == OutputMode.SIDE_BY_SIDE:
            target = i
        else:
            target = a.page  # 翻訳のみは、訳文の文書そのもの（ページの番号は原文と同じ）
        if not 0 <= target < doc.page_count:
            continue
        page = doc[target]
        if mode == OutputMode.TRANSLATED and a.kind != "note":
            annot = _bar(page, a)
        else:
            annot = _mark(page, a)
        annot.set_colors(stroke=COLORS.get(a.color, COLORS["yellow"]))
        annot.set_info(content=_content(a, mode))
        annot.update()
        written += 1
    return written


def _mark(page: pymupdf.Page, a) -> pymupdf.Annot:
    rects = [pymupdf.Rect(q) for q in a.quads]
    if a.kind == "note":
        return page.add_text_annot(pymupdf.Point(rects[0].x0, rects[0].y0), a.comment or a.quote or "", icon="Comment")
    if a.kind == "underline":
        return page.add_underline_annot(rects)
    return page.add_highlight_annot(rects)


def _bar(page: pymupdf.Page, a) -> pymupdf.Annot:
    """訳文では文字の位置が原文と違うので、同じ高さの左の余白に帯を描く（段落の場所は原文と同じ）。"""
    _x0, y0, _x1, y1 = a.rect
    annot = page.add_rect_annot(pymupdf.Rect(BAR_X, y0, BAR_X + BAR_WIDTH, max(y1, y0 + 4)))
    annot.set_colors(fill=COLORS.get(a.color, COLORS["yellow"]))
    annot.set_border(width=0)
    return annot


def _content(a, mode: OutputMode) -> str:
    """注釈の中身（ほかの PDF ビューアで表示される文）。訳文の帯には、原文の引用も入れる。"""
    if mode == OutputMode.TRANSLATED and a.kind != "note" and a.quote:
        return f"「{a.quote}」\n{a.comment}".strip()
    return a.comment
