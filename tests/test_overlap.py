"""訳文が見出しや別の段落と重なる・訳されないまま残る不具合の再発防止（Icarus 186 の Hesman et al. など）。"""

import pymupdf

from spectra.core.extract import extract_blocks
from spectra.core.models import BlockKind, DocumentAnalysis, Segment
from spectra.core.options import RenderOptions
from spectra.core.qa import _overlapping_lines
from spectra.core.render import Renderer, _overlapping


def _split_math_line_page():
    """段落の最終行が "and 2.2⁺⁰·⁶₋₀.₄ in the lower …" で、上付きの直後の空白だけが上付きの高さにある。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for i, t in enumerate(
        [
            "The calculated abundances in this work are estimated to be",
            "within the expected range for both of the regions, and the",
            "results agree with the previous measurements of the CO line",
        ]
    ):
        page.insert_text((60, 100 + i * 12), t, fontname="tiro", fontsize=10)
    y = 136
    page.insert_text((60, y), "and 2.2", fontname="tiro", fontsize=10)
    x = 60 + pymupdf.get_text_length("and 2.2", "tiro", 10)
    page.insert_text((x, y - 4), "+0.6", fontname="tiro", fontsize=7.5)
    page.insert_text((x, y + 3), "-0.4", fontname="tiro", fontsize=7.5)
    x2 = x + pymupdf.get_text_length("+0.6", "tiro", 7.5)
    font = pymupdf.Font("tiro")
    tw = pymupdf.TextWriter(page.rect)
    tw.append((x2, y - 3.6), " ", font=font, fontsize=10)  # 空白だけが上付きの高さ
    tw.append((x2 + font.text_length(" ", 10), y), "in the lower and upper regions, respectively.", font=font, fontsize=10)
    tw.write_text(page)
    page.insert_text((60, y + 26), "5. Discussion", fontname="tibo", fontsize=10)
    return doc


def test_leading_space_at_superscript_height_does_not_lift_the_baseline():
    # PyMuPDF はスパンの基準点を先頭の文字（上付きの高さの空白）にするので、本文の文字の位置で取り直す
    blocks = extract_blocks(_split_math_line_page()[0])
    line = next(ln for b in blocks for ln in b.lines if "in the lower" in ln.text)
    span = next(s for s in line.spans if "in the lower" in s.text)
    assert abs(span.origin[1] - 136.0) < 0.5
    assert abs(line.baseline - 136.0) < 0.5


def _renderer(segments=()):
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    return Renderer(doc, DocumentAnalysis(pages=[], segments=list(segments), body_size=10.0), RenderOptions())


def test_frames_of_one_paragraph_do_not_overlap_each_other():
    # 1 行が数式で左右のブロックに分かれると、続きのブロックの枠が前の枠の最終行の高さから始まる
    seg = Segment(id="s", kind=BlockKind.BODY, source="x", runs={}, regions=[])
    r = _renderer()
    earlier = [(301.7, 612.7, 552.9, 670.4)]
    clipped = r._clip_to_neighbors(seg, 0, (301.7, 656.8, 552.8, 694.1), earlier)
    assert clipped[1] >= 670.4
    # 左右に離れた枠（別の段）は動かさない
    assert r._clip_to_neighbors(seg, 0, (40, 656.8, 280, 694.1), earlier)[1] == 656.8


def test_overlapping_translations_are_found():
    from spectra.core.layout import LayoutResult

    a = Segment(id="a", kind=BlockKind.BODY, source="x", runs={}, regions=[])
    b = Segment(id="b", kind=BlockKind.BODY, source="y", runs={}, regions=[])
    c = Segment(id="c", kind=BlockKind.BODY, source="z", runs={}, regions=[])
    la = LayoutResult([], 1.0, True, 2, used=[(0, (300, 610, 553, 672))])
    lb = LayoutResult([], 1.0, True, 1, used=[(0, (300, 664, 360, 676))])  # a の最終行に食い込む
    lc = LayoutResult([], 1.0, True, 3, used=[(0, (300, 680, 553, 720)), (1, (40, 600, 280, 700))])
    assert _overlapping([(a, la), (b, lb), (c, lc)]) == {"a", "b"}
    # 同じ段落の 2 つの枠が重なる場合も見つける
    ld = LayoutResult([], 1.0, True, 3, used=[(0, (300, 610, 553, 672)), (0, (300, 660, 553, 694))])
    assert _overlapping([(a, ld)]) == {"a"}


def test_qa_finds_translated_lines_drawn_on_top_of_each_other():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((300, 660), "下層領域と上層領域においてそれぞれ", fontname="japan", fontsize=9)
    page.insert_text((300, 661), "る。5. 考察", fontname="japan", fontsize=9)  # 前の行に重ねて描いた
    page.insert_text((300, 700), "これは次の段落の一行目である。", fontname="japan", fontsize=9)
    page.insert_text((300, 711), "これは次の段落の二行目である。", fontname="japan", fontsize=9)  # 詰めた行送りは重なりではない
    assert _overlapping_lines(page, [tuple(page.rect)]) == 1
    # 書き換えていない範囲（原文のまま）の行は数えない
    assert _overlapping_lines(page, []) == 0
