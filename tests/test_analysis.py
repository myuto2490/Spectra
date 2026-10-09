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
