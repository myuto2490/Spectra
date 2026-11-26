"""訳す段落の原文を消すと、字形の枠だけが段落にかかる翻訳対象外の字形（大きな Σ など）まで消える。

MuPDF の消去は字形の枠（フォントの上下の余白を含む）で判定するため、字そのものは段落の外にあっても巻き込まれる。
その場合も段落は訳し、消えた字形を原文から切り抜いて戻す。
"""

import pymupdf
import pytest
from pdf_factory import LOREM, write_paragraph

from spectra.core.analysis import guard_overlaps
from spectra.core.extract import TEXT_FLAGS
from spectra.core.models import (
    BlockDecision,
    BlockKind,
    DocumentAnalysis,
    Line,
    PageAnalysis,
    SkipReason,
    Span,
    TextBlock,
)
from spectra.core.options import RenderOptions
from spectra.core.render import Renderer
from spectra.core.segment import build_segments
from spectra.translate.dummy import DummyTranslator

FORMULA_INK = pymupdf.Rect(280, 279, 360, 301)  # 大きな記号の字そのものの範囲（ベースライン 300 より上）


def _page(formula: str = "S+S=S") -> pymupdf.Document:
    """大きな記号の式のすぐ下に段落がある。記号の字形の枠（下の余白）が段落の 1 行目にかかる。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((280, 300), formula, fontname="symb", fontsize=30)
    write_paragraph(page, (140, 473), 312, LOREM, 3)
    return doc


def _analysis(doc: pymupdf.Document, with_page: bool = True) -> DocumentAnalysis:
    page = doc[0]
    lines = []
    for b in page.get_text("dict", flags=TEXT_FLAGS)["blocks"]:
        for ln in b.get("lines", ()):
            spans = [
                Span(s["text"], s["font"], s["size"], s["color"], s["flags"], tuple(s["bbox"]), tuple(s["origin"]))
                for s in ln["spans"]
            ]
            lines.append(Line(spans=spans, bbox=tuple(ln["bbox"])))
    formula = [ln for ln in lines if ln.spans[0].font == "Symbol"]
    body = [ln for ln in lines if ln not in formula]

    def block(i, ls):
        box = (min(x.bbox[0] for x in ls), min(x.bbox[1] for x in ls), max(x.bbox[2] for x in ls), max(x.bbox[3] for x in ls))
        return TextBlock(page=0, index=i, bbox=box, lines=ls)

    decisions = [
        BlockDecision(block(0, formula), BlockKind.BODY, SkipReason.MATH),
        BlockDecision(block(1, body), BlockKind.BODY),
    ]
    pa = PageAnalysis(page=0, width=595, height=842, decisions=decisions)
    guard_overlaps(pa, page if with_page else None)
    segments = build_segments([(pa.width, pa.decisions)])
    for s in segments:
        s.translated = DummyTranslator._fake(s.source)
    return DocumentAnalysis(pages=[pa], segments=segments, body_size=10.0)


def _pixels(doc: pymupdf.Document) -> bytes:
    return doc[0].get_pixmap(clip=FORMULA_INK, dpi=150).samples


def test_the_formula_glyph_box_reaches_into_the_paragraph():
    doc = _page()
    a = _analysis(doc)
    para = a.pages[0].decisions[1]
    assert para.translate  # 字そのものは段落の外なので訳す
    assert a.pages[0].restore and all(g.block_index == 1 for g in a.pages[0].restore)


def test_paragraph_is_kept_without_the_page_to_measure_real_glyphs():
    a = _analysis(_page(), with_page=False)
    assert a.pages[0].decisions[1].skip == SkipReason.OVERLAP


@pytest.mark.parametrize("restore", [True, False])
def test_erased_glyphs_are_put_back_from_the_original(restore):
    original = _page()
    before = _pixels(original)
    doc = pymupdf.open(stream=original.tobytes(), filetype="pdf")
    a = _analysis(original)
    if not restore:
        # 原文の文書が無ければ字形を戻せないので、段落は原文のまま残す（字形も消さない）
        report = Renderer(doc, a, RenderOptions(), source=None).render()
        assert [k.segment_id for k in report.kept_original] == [a.segments[0].id]
        assert "これは" not in doc[0].get_text()
    else:
        Renderer(doc, a, RenderOptions(), source=original).render()
        assert "これは" in doc[0].get_text()  # 段落は訳した
    assert _pixels(doc) == before  # 式の字は元のまま


def test_a_lone_big_symbol_is_left_in_place_rather_than_merged_into_the_paragraph():
    """1〜3 文字の数式の断片は段落に取り込んで一緒に動かすが、字そのものが段落の外にある大きな記号（Σ だけの行）を
    取り込むと、段落の枠が記号まで広がって他の式と重なり、段落を訳せなくなる。その場に残して、消えたら戻す。"""
    a = _analysis(_page("S"))
    formula, para = a.pages[0].decisions
    assert para.translate and formula in a.pages[0].decisions
    assert para.block.bbox[1] > 300  # 段落の枠は記号まで広がらない
    assert a.pages[0].restore
