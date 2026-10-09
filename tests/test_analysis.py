import pymupdf

from spectra.core.analysis import analyze_document
from spectra.core.models import BlockKind, SkipReason
from spectra.core.options import AnalysisOptions


def _decisions(analysis):
    return [d for pa in analysis.pages for d in pa.decisions]


def _find(analysis, needle):
    for d in _decisions(analysis):
        if needle in d.block.text:
            return d
    raise AssertionError(f"block containing {needle!r} not found")


def test_body_size(paper):
    assert analyze_document(paper).body_size == 10.0


def test_title_heading_and_abstract_translated(paper):
    a = analyze_document(paper)
    assert _find(a, "Learning Robust").kind == BlockKind.TITLE
    assert _find(a, "Abstract").translate
    assert _find(a, "1 Introduction").kind == BlockKind.HEADING


def test_authors_skipped(paper):
    assert _find(analyze_document(paper), "Alice Example").skip == SkipReason.AUTHORS


def test_display_equation_skipped(paper):
    a = analyze_document(paper)
    eq = [d for d in _decisions(a) if d.block.text.replace(" ", "").startswith("a=b")]
    assert eq and eq[0].skip in (SkipReason.MATH, SkipReason.NO_WORDS)


def test_rotated_stamp_skipped(paper):
    assert _find(analyze_document(paper), "arXiv:").skip == SkipReason.ROTATED


def test_figure_labels_skipped_but_caption_translated(paper):
    a = analyze_document(paper)
    assert _find(a, "training steps").skip == SkipReason.FIGURE
    assert _find(a, "Encoder").skip == SkipReason.FIGURE
    cap = _find(a, "Figure 1:")
    assert cap.kind == BlockKind.CAPTION and cap.translate


def test_table_skipped(paper):
    a = analyze_document(paper)
    assert _find(a, "Baseline").skip == SkipReason.TABLE
    assert _find(a, "Table 1:").translate


def test_references_skipped_until_next_heading(paper):
    a = analyze_document(paper)
    assert _find(a, "References").translate
    assert _find(a, "[1] A. Example").skip == SkipReason.REFERENCES
    assert _find(a, "A Appendix").translate


def test_references_can_be_translated(paper):
    a = analyze_document(paper, AnalysisOptions(skip_references=False))
    assert _find(a, "[1] A. Example").translate


def test_captions_and_footnotes_can_be_disabled(paper):
    a = analyze_document(paper, AnalysisOptions(translate_captions=False, translate_footnotes=False))
    assert _find(a, "Figure 1:").skip == SkipReason.CAPTION_DISABLED
    assert _find(a, "Code is available").skip == SkipReason.FOOTNOTE_DISABLED


def test_page_range(paper):
    a = analyze_document(paper, AnalysisOptions(first_page=1, last_page=1))
    assert {pa.page for pa in a.pages} == {1}
    assert all(r.page == 1 for s in a.segments for r in s.regions)


def test_inline_math_protected(paper):
    a = analyze_document(paper)
    seg = next(s for s in a.segments if "learning rate" in s.source)
    assert '<x id="0"/>' in seg.source and '<x id="1"/>' in seg.source
    assert "momentum" in seg.source
    run0 = seg.runs[0]
    assert run0.pieces[0].math and run0.pieces[0].text == "α"
    # 下付きの t は同じ保護要素に含まれ、ベースラインより下がっている
    assert any(p.rise_ratio < 0 and p.size_ratio < 1 for p in run0.pieces)


def test_split_paragraph_merged_across_columns(paper):
    a = analyze_document(paper)
    seg = next(s for s in a.segments if "learned features" in s.source)
    assert len(seg.regions) == 2
    assert "learned features generalize well" in seg.source
    assert seg.regions[0].rect[0] < seg.regions[1].rect[0]


def test_no_merge_across_headings(paper):
    a = analyze_document(paper)
    seg = next(s for s in a.segments if s.regions[0].page == 2 and s.kind == BlockKind.BODY)
    assert len(seg.regions) == 1


def test_equation_without_math_fonts_is_skipped(paper):
    assert _find(analyze_document(paper), "max(0").skip == SkipReason.MATH


def test_body_starting_with_figure_is_not_caption(paper):
    d = _find(analyze_document(paper), "Figure 1 shows")
    assert d.kind == BlockKind.BODY and d.translate


def test_multi_line_title_is_one_segment(paper):
    a = analyze_document(paper)
    seg = next(s for s in a.segments if s.kind == BlockKind.TITLE)
    assert "Learning Robust Representations for Testing with Synthetic Papers" in seg.source
    assert len(seg.regions) == 2


def _one_page(draw) -> pymupdf.Document:
    doc = pymupdf.open()
    draw(doc.new_page(width=595, height=842))
    return doc


def test_letter_spaced_abstract_heading_and_front_matter():
    def draw(page):
        page.insert_text((60, 60), "Journal", fontname="hebo", fontsize=20)  # 雑誌名（大きいが 1 語）
        page.insert_text((60, 110), "A Study of Planetary Atmospheres", fontname="tibo", fontsize=16)
        page.insert_text((60, 130), "Using Submillimeter Data", fontname="tibo", fontsize=16)
        page.insert_text((60, 160), "Alice Example, Bob Sample", fontname="tiro", fontsize=10)
        page.insert_text((60, 200), "A B S T R A C T", fontname="tibo", fontsize=9)
        y = 220
        for _ in range(4):
            page.insert_text((60, y), "We present new observations of the atmosphere and discuss them here.", fontname="tiro", fontsize=10)
            y += 12

    a = analyze_document(_one_page(draw))
    kinds = {d.block.text.strip()[:12]: (d.kind, d.skip) for d in a.pages[0].decisions}
    assert kinds["Journal"][1] == SkipReason.AUTHORS
    assert kinds["Alice Exampl"][1] == SkipReason.AUTHORS
    title = next(s for s in a.segments if s.kind == BlockKind.TITLE)
    assert "Atmospheres Using Submillimeter" in title.source
    heading = next(s for s in a.segments if s.kind == BlockKind.HEADING)
    assert heading.source == "ABSTRACT"


def test_math_function_names_are_not_words():
    def draw(page):
        page.insert_text((60, 100), "Some body text that is long enough to be a paragraph here.", fontname="tiro", fontsize=10)
        page.insert_text((250, 300), "cos", fontname="tiro", fontsize=10)
        page.insert_text((272, 300), "f", fontname="symb", fontsize=10)
        page.insert_text((520, 300), "(3)", fontname="tiro", fontsize=10)

    a = analyze_document(_one_page(draw))
    eq = next(d for d in a.pages[0].decisions if d.block.text.strip().startswith("cos"))
    assert not eq.translate


def test_body_color_prefers_neutral_over_link_color():
    from spectra.core.models import Span
    from spectra.core.segment import _body_color

    def span(text, color):
        return Span(text, "Times", 10, color, 0, (0, 0, 1, 1), (0, 0))

    blue = 0x0055CC
    spans = [span("Hubbard et al., 1995; Podolak et al., 1995", blue), span("are enriched in heavy", 0x231F20)]
    assert _body_color(spans) == 0x231F20
