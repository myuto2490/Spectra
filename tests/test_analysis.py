import re

import pymupdf
import pytest

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


def test_grid_clustering_groups_nearby_drawings_and_is_fast():
    import time

    from spectra.core.extract import cluster_drawings

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    shape = page.new_shape()
    # 散布図：5,000 個の点（それぞれ別の図形）が 2 つの領域に密集している
    for i in range(2500):
        for ox, oy in ((60, 60), (360, 400)):
            shape.draw_circle((ox + (i % 50) * 2, oy + (i // 50) * 2), 0.6)
            shape.finish(color=(0, 0, 1))
    shape.commit()
    drawings = page.get_drawings()
    t = time.time()
    clusters = cluster_drawings(drawings, (0, 0, 595, 842))
    assert time.time() - t < 3
    big = [r for r, _m in clusters if r[2] - r[0] > 80]
    assert len(big) == 2


def test_paragraph_tail_line_with_superscripts_is_absorbed():
    """上付き・下付きで段落から切り離された最後の行は段落の一部とみなし、独立した数式は取り込まない。"""
    from spectra.core.analysis import _is_paragraph_tail
    from spectra.core.models import BlockDecision, Line, Span, TextBlock

    def block(text, bbox, size=10.0, lines=1):
        span = Span(text, "Times-Roman", size, 0, 0, bbox, (bbox[0], bbox[3] - 2))
        return TextBlock(0, 0, bbox, [Line([span], bbox) for _ in range(lines)])

    para = BlockDecision(block("The mixing ratio is constrained by", (318, 289, 569, 432), lines=13), BlockKind.BODY)
    tail = BlockDecision(block("18.64 (Orton 1992).", (361, 429, 467, 443)), BlockKind.BODY, SkipReason.NUMERIC)
    assert _is_paragraph_tail(tail, para)

    display = BlockDecision(block("x = 1.0 (3)", (400, 436, 488, 448)), BlockKind.BODY, SkipReason.MATH)
    assert not _is_paragraph_tail(display, para)

    elsewhere = BlockDecision(block("18.64 (Orton 1992).", (60, 600, 160, 612)), BlockKind.BODY, SkipReason.NUMERIC)
    assert not _is_paragraph_tail(elsewhere, para)


def test_centered_titles_are_centered_even_when_wide():
    """ページいっぱいに広がる中央揃えのタイトルも、2 行目も、中央揃えで組む。"""

    def draw(page):
        for y, text in ((80, "A Remarkably Long Title About Planetary Atmospheres and Their Winds"), (100, "Observed by Radio Telescopes")):
            w = pymupdf.get_text_length(text, fontname="tibo", fontsize=16)
            page.insert_text(((595 - w) / 2, y), text, fontname="tibo", fontsize=16)
        page.insert_text((60, 140), "Alice Example", fontname="tiro", fontsize=10)
        page.insert_text((280, 180), "ABSTRACT", fontname="tibo", fontsize=10)
        page.insert_text((60, 200), "We present observations of the atmosphere and discuss them.", fontname="tiro", fontsize=10)

    a = analyze_document(_one_page(draw))
    title = next(s for s in a.segments if s.kind == BlockKind.TITLE)
    assert len(title.regions) == 2
    assert {r.style.align.value for r in title.regions} == {"center"}


def test_duplicate_text_layers_are_translated_once():
    # スキャンに OCR テキストを二重に付けた PDF（同じ位置に、少しずれて同じ文字が重なる）
    from pdf_factory import make_scanned

    doc = make_scanned()
    page = doc[0]
    for i, line in enumerate(["Learning Robust Representations for Testing", "with Synthetic Papers"]):
        w = pymupdf.get_text_length(line, fontname="tibo", fontsize=17)
        page.insert_text(((595 - w) / 2, 73.5 + i * 20), line, fontname="tibo", fontsize=17, render_mode=3)
    a = analyze_document(doc)
    titles = [s for s in a.segments if "Learning Robust" in s.source]
    assert len(titles) == 1


def test_wordless_block_in_front_matter_is_not_a_title():
    # 表紙の図の中に OCR が拾った記号の断片
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((150, 100), "Inverse Methods for Sounding", fontname="tibo", fontsize=24)
    page.insert_text((400, 300), "; - ' A", fontname="tibo", fontsize=20)
    page.insert_text((60, 400), "Abstract", fontname="tibo", fontsize=12)
    page.insert_text((60, 420), "We study inverse methods for remote sounding of the atmosphere.", fontname="tiro", fontsize=10)
    a = analyze_document(doc)
    assert _find(a, "Inverse Methods").translate
    assert _find(a, "; - ' A").skip == SkipReason.NO_WORDS


# ---- Elsevier の古い組版（Adv* の記号フォント）の段落中の数式 ----------------------

_R, _I = "AdvGulliv-R", "AdvGulliv-I"  # 本文・斜体
_SYM, _EXT, _PUNCT, _GREEK = "AdvP4C4E74", "AdvP4C4E46", "AdvP4C4E51", "AdvPSMP10"  # = − ／根号／小数点／ν


def _span(text, font, size, x0, x1, base):
    from spectra.core.models import Span

    return Span(text, font, size, 0, 0, (x0, base - size * 0.75, x1, base + size * 0.25), (x0, base))


def _line(*spans):
    from spectra.core.models import Line, rect_union

    bbox = spans[0].bbox
    for s in spans[1:]:
        bbox = rect_union(bbox, s.bbox)
    return Line(list(spans), bbox)


def _block(*lines):
    from spectra.core.models import TextBlock, rect_union

    bbox = lines[0].bbox
    for ln in lines[1:]:
        bbox = rect_union(bbox, ln.bbox)
    return TextBlock(0, 0, bbox, list(lines))


def test_elsevier_symbol_fonts_are_math_fonts():
    from spectra.core.fonts import is_math_font

    for name in ("AdvP4C4E74", "ABCDEF+AdvP4C4E46", "AdvPSMP10", "AdvPSSym", "AdvMTSY", "AdvPi2"):
        assert is_math_font(name), name
    for name in ("AdvGulliv-R", "AdvGulliv-I", "AdvPSUnv", "AdvOT596495f2", "AdvTT825c8005"):
        assert not is_math_font(name), name


def _noise_paragraph():
    """'... of the noise as / noise_ν = √(1.1 − sky_trans_ν) / sky_trans_ν. We used ... / an assumed ...'

    PyMuPDF は根号・分子（R）と、分母から続く文（D）を別のブロックとして返す。
    """
    para = _block(
        _line(_span("of the Dewar window and an observation of dark sky, respectively.", _R, 8.1, 311.5, 562.6, 541.2)),
        _line(_span("We then calculated the predicted spectral variance of the noise as", _R, 8.1, 311.5, 562.5, 551.7)),
    )
    lhs = _block(
        _line(_span("noise", _I, 8.1, 311.5, 332.0, 569.7), _span("m", _GREEK, 6.4, 332.0, 335.0, 570.9), _span("¼", _SYM, 8.1, 336.0, 342.0, 569.7))
    )
    radical = _block(
        _line(_span("p", _SYM, 8.1, 344.2, 350.9, 559.6), _span("ffiffiffiffiffiffiffiffiffiffiffi", _EXT, 8.1, 350.9, 388.0, 559.6)),
        _line(
            _span("1", _R, 5.2, 350.9, 353.5, 565.1),
            _span(":", _PUNCT, 5.2, 353.5, 354.8, 565.1),
            _span("1", _R, 5.2, 354.8, 357.4, 565.1),
            _span("\x03", _SYM, 5.2, 357.4, 361.5, 565.1),
            _span("sky trans", _I, 5.2, 361.5, 385.0, 565.1),
            _span("m", _GREEK, 4.9, 385.0, 387.5, 565.9),
        ),
    )
    rest = _block(
        _line(
            _span("sky trans", _I, 5.2, 353.5, 380.0, 572.4),
            _span("m", _GREEK, 4.9, 380.0, 382.5, 573.2),
            _span(".", _R, 8.1, 388.5, 390.5, 569.7),
            _span(" We used 1.1 in this equation to account for", _R, 8.1, 390.5, 562.5, 569.7),
        )
    )
    after = _block(
        _line(_span("an assumed window plus telescope emissivity of 10%. (This 10% va-", _R, 8.1, 311.5, 562.6, 584.4)),
        _line(_span("lue is an empirical result. When we observe at a wavelength where", _R, 8.1, 311.5, 562.6, 594.9)),
    )
    return [para, lhs, radical, rest, after]


def test_inline_radical_and_fraction_are_rebuilt_into_one_line():
    from spectra.core.extract import _merge_paragraph_lines

    blocks = _merge_paragraph_lines(_noise_paragraph())
    assert len(blocks) == 1  # 根号で行送りが広がった行も、前後の段落とつながる
    row = blocks[0].lines[2]
    assert len(blocks[0].lines) == 5
    assert row.text.startswith("noise") and row.text.endswith("account for")
    assert row.baseline == pytest.approx(569.7)  # 根号の上線ではなく本文の文字の位置


def test_stacked_fraction_parts_on_one_line():
    """分子と分母は左右に重なっていても同じ行。文字が重ならない語は順序によらず同じ行にまとめる。"""
    from spectra.core.extract import _merge_visual_lines

    numerator = _line(
        _span("from our flat-field via,", _R, 8.1, 311.5, 400.1, 518.9),
        _span(" sky trans", _I, 8.1, 400.1, 437.4, 518.9),
        _span("m", _GREEK, 6.4, 437.3, 440.2, 520.3),
        _span("¼", _SYM, 8.1, 442.9, 449.2, 519.1),
        _span("black", _I, 5.2, 451.5, 463.8, 515.3),
        _span("\x03", _SYM, 5.2, 466.5, 470.6, 515.3),
        _span("sky", _I, 5.2, 470.6, 478.2, 515.3),
    )
    denominator = _line(_span("black", _I, 5.2, 458.8, 471.0, 521.6))
    rest = _line(_span(". Black and sky are", _R, 8.1, 480.9, 562.6, 518.9))
    merged = _merge_visual_lines([denominator, rest, numerator])  # 断片を取り込んだときのように順序が前後する
    assert len(merged) == 1

    text = _line(_span("Typically,", _R, 10, 58.9, 105.0, 443.0), _span("P", _I, 10, 190.0, 199.2, 443.0))
    words = _line(_span("the raw power", _R, 10, 108.9, 185.0, 443.1))
    assert len(_merge_visual_lines([text, words])) == 1
    other = _line(_span("the next line of the paragraph", _R, 10, 58.9, 185.0, 445.0))
    assert len(_merge_visual_lines([text, other])) == 2  # 本文の文字どうしが重なる行は別の行


def test_display_equation_is_not_merged_into_paragraph():
    from spectra.core.extract import _merge_paragraph_lines

    intro = _block(_line(_span("Table 3. We also report the reduced chi-squared of the best fit:", _R, 8.1, 32.7, 283.8, 523.0)))
    equation = _block(_line(_span("v", _GREEK, 10.4, 33.1, 40.0, 545.0), _span("¼", _SYM, 8.4, 45.0, 52.0, 545.0), _span("1", _R, 8.4, 60.0, 65.0, 541.0)))
    assert len(_merge_paragraph_lines([intro, equation])) == 2

    numbered = _block(_line(_span("F", _GREEK, 8.1, 42.5, 50.0, 716.0), _span(" = Kn dq/dz ,", _I, 8.1, 50.0, 140.0, 716.0), _span("(7)", _R, 8.1, 280.0, 293.6, 716.0)))
    where = _block(
        _line(_span("where q is the CO mole ratio, n is the atmospheric num-", _R, 8.1, 42.5, 293.6, 739.0)),
        _line(_span("ber density, and the gradient in the CO mole ratio is", _R, 8.1, 42.6, 293.6, 750.9)),
    )
    assert len(_merge_paragraph_lines([numbered, where])) == 2


def test_inline_math_with_variable_names_is_one_movable_run():
    from spectra.core.extract import _merge_paragraph_lines
    from spectra.core.models import BlockDecision
    from spectra.core.segment import build_unit

    block = _merge_paragraph_lines(_noise_paragraph())[0]
    unit = build_unit(BlockDecision(block, BlockKind.BODY), 595, body_font=_R)
    assert "noise as <x" in unit.markup and '"/>. We used 1.1' in unit.markup  # 文末の "." は数式に含めない
    run = next(r for r in unit.runs.values() if r.text.startswith("noise"))  # 変数名 noise_ν も数式の一部
    assert run.bbox is not None  # 元の見た目のまま移せる（分子・分母・根号が 1 つの塊）
    assert run.bbox[1] < 554 and run.bbox[3] > 573  # 根号の上端から分母の下端まで


def test_display_equation_fragments_are_not_translated():
    from spectra.core.classify import BlockStats, _looks_like_equation, has_column_gaps

    # χ² = Σ_{j=1}^{nspectral} …：総和の上限は斜体の単語
    upper = _block(_line(_span("v", _GREEK, 10.4, 42.5, 50.0, 700.0), _span("¼", _SYM, 8.4, 52.0, 58.0, 700.0), _span("nspectral", _I, 5.4, 62.4, 84.1, 692.0)))
    assert _looks_like_equation(upper, BlockStats(upper, _R))
    # y_j − F_j(x_{i+1})：1 文字の変数を字間の空いた単語とみなさない
    numerator = _block(_line(*[_span(t, f, s, 91.6 + 4 * i, 95.6 + 4 * i, 697.0) for i, (t, f, s) in enumerate(
        [("y", _I, 8.4), ("j", _I, 5.4), ("\x03", _SYM, 8.4), ("F", _I, 8.4), ("j", _I, 5.4), ("ð", _SYM, 8.4), ("x", _I, 8.4), ("i", _I, 5.4)]
    )]))
    st = BlockStats(numerator, _R)
    assert st.math_chars / st.chars > 0.5
    # 式番号で終わる独立数式の右端
    tail = _block(_line(_span("s′ exp(−τ s′) ds′", "TimesNewRomanPS-ItalicMT", 10, 208.0, 280.0, 640.0), _span("(7)", "TimesNewRomanPSMT", 10, 288.4, 300.1, 640.0)))
    assert _looks_like_equation(tail, BlockStats(tail, "TimesNewRomanPSMT"))
    # 1 行だけの表の見出し（列の間が 2 か所以上空いている）
    header = _block(_line(_span("Wavelength", _R, 8, 51.0, 90.0, 95.0), _span("Intensity", _R, 8, 150.0, 190.0, 95.0), _span("Width", _R, 8, 260.0, 290.0, 95.0)))
    assert has_column_gaps(header, 8)


def _skip_of(block, body_font="Times-Roman"):
    from spectra.core.classify import BlockStats, DocumentClassifier
    from spectra.core.options import AnalysisOptions

    classifier = DocumentClassifier(AnalysisOptions(), 10.0, body_font)
    return classifier._skip(block, BlockStats(block, body_font), BlockKind.BODY, [], [], False)


def test_txfonts_equation_fragments_are_not_translated():
    """txfonts（rtxmi・rtxr）で組まれた独立数式の断片（分数の分子など）を本文と判定しない。"""
    it, rm, mi, sy = "Times-Italic", "Times-Roman", "rtxmi", "txsy"
    # 分子の dL_λ(x + dx, y + dy, …)：微分 "dL" "dx" は単語ではない
    spans = [("dL", it, 10), ("λ", mi, 7), ("(", rm, 10), ("x", it, 10), (" +", "rtxr", 10), (" dx", it, 10), (",", mi, 10), (" y", it, 10), (" +", "rtxr", 10), (" dy", it, 10), (")", rm, 10)]
    numerator = _block(_line(*[_span(t, f, s, 200 + 10 * i, 208 + 10 * i, 630.0) for i, (t, f, s) in enumerate(spans)]))
    assert _skip_of(numerator) == SkipReason.MATH
    # μ E_Sun：立体の小さな添字 "Sun" は単語ではない
    sun = _block(_line(_span("µ", mi, 10, 210.0, 216.0, 712.0), _span("Sun", rm, 7, 216.2, 226.0, 714.0), _span("E", it, 10, 226.5, 233.0, 712.0), _span("0", rm, 7, 233.2, 236.0, 714.0)))
    assert _skip_of(sun) == SkipReason.MATH
    # 2 行の分数 dE↓ / 2
    frac = _block(_line(_span("dE", it, 10, 218.0, 230.0, 210.0), _span("↓", sy, 7, 230.2, 236.0, 206.0)), _line(_span("2", rm, 10, 226.0, 231.0, 222.0)))
    assert _skip_of(frac) == SkipReason.MATH


def test_subscripted_variables_in_a_sentence_are_still_translated():
    """文中の E_Sun のような添字付きの変数があっても、文章は訳す。"""
    it, rm = "Times-Italic", "Times-Roman"
    words = "The solar irradiance at the top of the atmosphere is written as"
    line = _line(_span(words, rm, 10, 72.0, 380.0, 300.0), _span("E", it, 10, 383.0, 390.0, 300.0), _span("Sun", rm, 7, 390.2, 401.0, 302.0), _span(" and is assumed constant.", rm, 10, 401.5, 520.0, 300.0))
    assert _skip_of(_block(line)) is None


def _run(*items, base=300.0):
    """(文字列, フォント, 大きさ, 前との隙間, ベースラインのずれ) を左から並べた 1 行。"""
    spans, x = [], 72.0
    for text, font, size, gap, shift in items:
        width = len(text) * size * 0.5
        spans.append(_span(text, font, size, x + gap, x + gap + width, base + shift))
        x += gap + width
    return _block(_line(*spans))


_SMALL_TEXT_CASES = {
    # 斜体のスモールキャップスの人名（"S" + 小さな "MITH" が同じ高さ）
    "small caps names": _run(("J.", "Times-Roman", 10, 0, 0), (" S", "Times-Italic", 10, 1, 0), ("MITH", "Times-Italic", 7, 0, 0), (" and", "Times-Roman", 10, 0, 0), (" J", "Times-Italic", 10, 1, 0), ("ONES", "Times-Italic", 7, 0, 0)),
    # 斜体の 1 文字のすぐ後に続く、小さな文字の文
    "small sentence after a letter": _run(("Note", "Times-Roman", 10, 0, 0), (" a", "Times-Italic", 10, 0, 0), ("see the appendix for details", "Times-Roman", 7, 1, 0)),
    # 記号 "∗" のすぐ後に続く、小さな文字の脚注
    "footnote after an asterisk": _run(("∗", "txsy", 10, 0, 0), ("Corresponding author address", "Times-Roman", 7, 0.5, 0)),
    # 数式フォントのあるブロックの、斜体の短い英単語（微分の "dx" と取り違えない）
    "italic short words": _run(("di", "Times-Italic", 10, 0, 0), (" de", "Times-Italic", 10, 0, 0), (" do", "Times-Italic", 10, 0, 0), (" µ", "rtxmi", 10, 0, 0)),
    # 合字 "ff" "ffi" を rtxr で組んだ本文
    "ligatures in rtxr": _run(("The", "Times-Roman", 10, 0, 0), (" e", "Times-Roman", 10, 0, 0), ("ﬀ", "rtxr", 10, 0, 0), ("ect", "Times-Roman", 10, 0, 0), (" of", "Times-Roman", 10, 0, 0), (" e", "Times-Roman", 10, 0, 0), ("ﬃ", "rtxr", 10, 0, 0), ("ciency", "Times-Roman", 10, 0, 0)),
}


@pytest.mark.parametrize("name", list(_SMALL_TEXT_CASES))
def test_small_text_next_to_a_letter_is_not_a_subscript(name):
    """同じ高さに並ぶ小さな文字や、空白を含む小さな文は添字ではない（文章として訳す）。"""
    assert _skip_of(_SMALL_TEXT_CASES[name]) is None


def test_caption_covered_by_a_white_figure_background_is_translated():
    """図とキャプションをまとめて覆う白い背景があっても、キャプションは訳す（図の中の文字は原文のまま）。"""
    from pdf_factory import make_figure_on_white_background

    a = analyze_document(make_figure_on_white_background())
    pa = a.pages[0]
    # 白い背景は図の中身ではないが、図の領域としてはキャプションを含んで検出される（この現象の前提）
    cap = _find(a, "Fig. 3.")
    assert any(r[1] <= cap.block.bbox[1] and r[3] >= cap.block.bbox[3] for r in pa.figure_rects)
    assert cap.kind == BlockKind.CAPTION and cap.translate and cap.skip is None
    assert _find(a, "frequency (GHz)").skip == SkipReason.FIGURE  # 軸ラベルは今までどおり原文のまま


def test_caption_over_figure_graphics_is_still_kept():
    """キャプションの文字の下に図の線が通っているときは、図の一部として原文のまま残す。"""
    from pdf_factory import make_figure_on_white_background

    a = analyze_document(make_figure_on_white_background(caption_over_graphic=True))
    assert _find(a, "Fig. 3.").skip == SkipReason.FIGURE


def test_background_fill_is_not_figure_content():
    from spectra.core.classify import _is_background_fill

    white = {"type": "f", "fill": (1.0, 1.0, 1.0), "color": None}
    assert _is_background_fill(white)
    assert _is_background_fill({**white, "fill": (0.98, 0.98, 0.99)})
    assert not _is_background_fill({**white, "fill": (0.9, 0.9, 0.9)})  # 灰色の塗りは図の中身かもしれない
    assert not _is_background_fill({**white, "color": (0, 0, 0)})  # 枠線がある
    assert not _is_background_fill({**white, "type": "s"})
    assert not _is_background_fill({"type": "f", "fill": None, "color": None})


# -- 区切り記号のないキャプション（"Fig. 1 Schematic of …"）と、ページ上端の柱 ----------------------------


def _side_caption_page(label_bold: bool = True, header: bool = True, caption: str | None = None) -> pymupdf.Document:
    """ページ上端に柱、そのすぐ下の左に図の横のキャプション（番号だけが太字で、区切り記号なし）、右に図。下に本文。"""
    from pdf_factory import BODY, BOLD, write_runs

    doc = pymupdf.open()
    page = doc.new_page(width=440, height=666)
    if header:
        page.insert_text((47, 40), "87", fontname=BOLD, fontsize=8)
        page.insert_text((63, 40), "Page 8 of 41", fontname=BODY, fontsize=8)
        page.insert_text((338, 40), "T. Imamura et al.", fontname=BODY, fontsize=8)
    lines = (caption or "Schematic of|cyclostrophic balance, where the|centrifugal force balances the|pressure gradient force.").split("|")
    write_runs(page, 47, 59, [("Fig. 3", BOLD if label_bold else BODY, 8), (" " + lines[0], BODY, 8)])
    for i, text in enumerate(lines[1:], 1):
        page.insert_text((47, 59 + i * 9.5), text, fontname=BODY, fontsize=8)
    for i in range(12):  # 図（曲線の集まり）
        page.draw_bezier((215, 70 + i * 9), (270, 40 + i * 9), (330, 120 + i * 9), (390, 70 + i * 9), color=(0, 0, 1), width=0.8)
    body = "The mean zonal wind is maintained by the transport of angular momentum in the atmosphere of the planet"
    for i in range(12):
        page.insert_text((47, 260 + i * 12), f"{body} {i}." if i == 11 else body, fontname=BODY, fontsize=9.5)
    return doc


def test_caption_without_delimiter_is_translated_when_its_label_is_bold():
    a = analyze_document(_side_caption_page())
    cap = _find(a, "Schematic of")
    assert cap.kind == BlockKind.CAPTION and cap.translate
    assert cap.block.text.startswith("Fig. 3 Schematic")  # 柱と 1 つの段落になっていない
    assert "pressure gradient force" in cap.block.text


def test_running_header_is_not_joined_to_the_caption_below_it():
    a = analyze_document(_side_caption_page())
    head = _find(a, "Page 8 of 41")
    assert len(head.block.lines) == 1 and head.skip == SkipReason.RUNNING_HEADER
    assert "Schematic" not in head.block.text


def test_running_header_with_title_and_page_number_is_skipped():
    from pdf_factory import BODY

    doc = _side_caption_page(header=False)
    page = doc[0]
    page.insert_text((47, 40), "Superrotation in Planetary Atmospheres", fontname=BODY, fontsize=8)
    page.insert_text((337, 40), "Page 5 of 41", fontname=BODY, fontsize=8)
    page.insert_text((385, 40), "87", fontname=BODY, fontsize=8)
    a = analyze_document(doc)
    assert _find(a, "Superrotation in Planetary").skip == SkipReason.RUNNING_HEADER
    assert _find(a, "Schematic of").translate


def test_figure_reference_without_bold_label_is_not_a_caption():
    """本文の "Fig. 3 Shows …"（太字でない）は、キャプションにしない（迷ったら今までどおり）。"""
    a = analyze_document(_side_caption_page(label_bold=False))
    assert _find(a, "Schematic of").kind != BlockKind.CAPTION


@pytest.mark.parametrize(
    "caption",
    ["shows the balance of|forces in the atmosphere|of a slowly rotating planet|such as Venus or Titan."],
)
def test_bold_label_followed_by_a_lowercase_sentence_is_not_a_caption(caption):
    a = analyze_document(_side_caption_page(caption=caption))
    assert _find(a, "shows the balance").kind != BlockKind.CAPTION


def test_first_line_of_a_paragraph_still_joins_the_rest():
    """行送りがそろっていれば、1 行だけのブロックと続きの段落は、今までどおり 1 つの段落になる。"""
    from spectra.core.extract import _continues_paragraph
    from spectra.core.models import Line, Span, TextBlock

    def line(y: float, x1: float = 393.0) -> Line:
        box = (47.0, y, x1, y + 9.0)
        return Line(spans=[Span("word " * 12, "Times", 9.5, 0, 0, box, (47.0, y + 8))], bbox=box)

    def block(lines: list[Line]) -> TextBlock:
        return TextBlock(page=0, index=0, bbox=(47.0, lines[0].bbox[1], 393.0, lines[-1].bbox[3]), lines=lines)

    first = block([line(100)])
    assert _continues_paragraph(first, block([line(112), line(124), line(136, 200.0)]))
    assert not _continues_paragraph(first, block([line(121), line(133), line(145, 200.0)]))  # 行送り 12 に対して 21 離れている


# ---- 本文の Times が番号だけの名前（AdvP4DF60E）の Elsevier の論文 -------------------


def _rename_fonts(doc, name):
    """合成した PDF のフォント（Times）の名前を、Elsevier の組版の名前に付け替える。"""
    for page in doc:
        for xref, *_ in page.get_fonts():
            doc.xref_set_key(xref, "BaseFont", f"/{name}")


def test_numbered_elsevier_body_font_is_text_not_math():
    # Planetary and Space Science (2010) など: 本文の Times が AdvP4DF60E、記号が AdvP4C4E74 のように、名前では区別できない
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    lines = [
        "Infrared heterodyne spectroscopy enables spectral resolving power great",
        "enough to directly measure fully resolved emission line profiles from",
        "the stratosphere of Titan, and to retrieve winds and temperatures.",
    ]
    for i, text in enumerate(lines):
        page.insert_text((60, 100 + i * 12), text, fontname="tiro", fontsize=10)
    for i, text in enumerate(lines):
        page.insert_text((60, 300 + i * 12), text.replace("the", "a"), fontname="tiro", fontsize=10)
    _rename_fonts(doc, "AdvP4DF60E")
    doc = pymupdf.open("pdf", doc.tobytes())
    a = analyze_document(doc)
    assert _find(a, "Infrared heterodyne").translate
    assert _find(a, "a stratosphere of Titan").translate


def test_find_text_fonts_separates_numbered_body_and_symbol_fonts():
    from spectra.core.fonts import find_text_fonts, is_math_font, text_fonts

    words = ["High", "spectral", "resolution", "infrared", "observations", "of", "Titan", "winds"]
    spans = [("ABCDEF+AdvP4DF60E", w) for w in words]
    # 記号フォントの文字（"=" が "¼"、"−" が "\x03"、根号の上線が "ﬃ"、小数点が ":"）は英単語にならない
    spans += [("AdvP4C4E74", t) for t in ("¼", "\x03", "þ", "ð", "Þ", "p")]
    spans += [("AdvP4C4E46", t) for t in ("ffiffiffiffi", "ﬃﬃﬃﬃ", "Z", "c", "X")]
    spans += [("AdvP4C4E51", t) for t in (":", ";", "=", "@")]
    found = find_text_fonts(spans)
    assert found == {"AdvP4DF60E"}
    assert is_math_font("AdvP4DF60E")  # 解析の外では、これまでどおり記号フォントとみなす（迷ったら訳さない）
    with text_fonts(found):
        assert not is_math_font("ABCDEF+AdvP4DF60E")
        for name in ("AdvP4C4E74", "AdvP4C4E46", "AdvP4C4E51", "AdvPSMP10", "AdvPi2"):
            assert is_math_font(name), name
    assert is_math_font("AdvP4DF60E")


def test_elsevier_entity_font_is_kept_as_original_glyph():
    # AdvEls-ent5 などは 1 字ずつの特殊文字のフォントで、"10–15°S" の "°" が "1" として取り出される
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 100
    page.insert_text((60, y), "The winds were measured at 10-15", fontname="tiro", fontsize=10)
    x = 60 + pymupdf.get_text_length("The winds were measured at 10-15", fontname="tiro", fontsize=10)
    page.insert_text((x, y), "1", fontname="cour", fontsize=10)
    page.insert_text((x + 6, y), " South latitude near the equatorial region.", fontname="tiro", fontsize=10)
    page.insert_text((60, y + 12), "These results are compared with the spacecraft retrievals.", fontname="tiro", fontsize=10)
    for xref, _, _, name, *_ in page.get_fonts():
        if "Courier" in name:
            doc.xref_set_key(xref, "BaseFont", "/MNENOH+AdvEls-ent5")
    doc = pymupdf.open("pdf", doc.tobytes())
    a = analyze_document(doc)
    assert _find(a, "measured at").translate
    source = next(s.source for s in a.segments if "measured at" in s.source)
    assert "151" not in source
    assert re.search(r'10-15 ?<x id="0"/> South', source), source


def test_elsevier_symbol_and_garbled_spans_are_math():
    from spectra.core.fonts import is_math_font, is_math_span

    for name in ("MNENOH+AdvEls-ent5", "AdvEls-ent8", "AdvMacMthSyN", "AdvMacMthSy"):
        assert is_math_font(name), name
    for name in ("AdvMacT_R", "AdvUnivers", "AdvGulliver-SCO"):
        assert not is_math_font(name), name
    # 対応の壊れた記号（"∼" が "\x02"、"×" が "\x03"）は、どのフォントでも数式として元の字形のまま残す
    for text in ("\x02", " \x03", "\x04", ""):
        assert is_math_span("Times-Roman", text, "Times-Roman"), repr(text)
    assert not is_math_span("Times-Roman", "South", "Times-Roman")


def _elsevier_table_page(caption_first_line, caption_second_line):
    """Elsevier の表: 番号の行（名前に Bold の無い太字）の下に題名、その下に灰色の地と罫線の表。本文より小さい文字。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for i in range(6):
        page.insert_text(
            (60, 100 + i * 12), "The winds of Titan were measured with infrared heterodyne spectroscopy.", fontname="tiro", fontsize=10
        )
    page.insert_text((60, 200), caption_first_line, fontname="tibo", fontsize=6.5)
    if caption_second_line:
        page.insert_text((60, 208), caption_second_line, fontname="tiro", fontsize=6.5)
    page.draw_rect((60, 212, 540, 264), color=None, fill=(0.93, 0.93, 0.93))  # Elsevier の表の薄い灰色の地
    for y in (214, 226, 262):
        page.draw_line((60, y), (540, y), width=0.5)
    page.insert_text((64, 222), "Date", fontname="tiro", fontsize=6.5)
    page.insert_text((300, 222), "Wind speed (m/s)", fontname="tiro", fontsize=6.5)
    for i, (date, speed) in enumerate([("November 1980", "100"), ("July 3, 1989", "100"), ("October 1995", "210")]):
        page.insert_text((64, 236 + i * 9), date, fontname="tiro", fontsize=6.5)
        page.insert_text((300, 236 + i * 9), speed, fontname="tiro", fontsize=6.5)
    for xref, _, _, name, *_ in page.get_fonts():
        if name == "Times-Bold":
            doc.xref_set_key(xref, "BaseFont", "/MNENAB+AdvTT5a1c2d3e")
    return pymupdf.open("pdf", doc.tobytes())


def test_table_caption_without_delimiter_or_bold_name_is_translated():
    # "Table 1" の後に区切り記号が無く、ラベルのフォント名に Bold が無い（Elsevier の Planetary and Space Science など）
    a = analyze_document(_elsevier_table_page("Table 1", "Titan wind speed: 1981-2005."))
    cap = _find(a, "Titan wind speed")
    assert cap.kind == BlockKind.CAPTION
    assert cap.translate
    assert not _find(a, "November 1980").translate  # 表の中身は訳さない


def test_table_sentence_in_label_font_position_is_not_caption():
    # 番号の後が小文字で続く文（"Table 1 shows …"）は、ラベルの書式でもキャプションにしない
    a = analyze_document(_elsevier_table_page("Table 1", "shows the wind speed of Titan."))
    assert _find(a, "shows the wind speed").kind != BlockKind.CAPTION


def _book_chapter(pages: int = 5) -> pymupdf.Document:
    """余白の広い本の章（概要が無く、柱がページの上端から離れている）。"""
    from pdf_factory import BODY, BOLD, LOREM, write_paragraph

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        if i == 0:
            page.insert_text((140, 200), "6", fontname=BOLD, fontsize=24)
            page.insert_text((140, 230), "Transfer of Things", fontname=BODY, fontsize=18)
            page.insert_text((140, 320), "6.1   Optical Properties of a Mixed Volume", fontname=BOLD, fontsize=12)
            y = 345
        else:
            if i % 2:
                page.insert_text((140, 160), f"{115 + i}", fontname=BODY, fontsize=10)
                page.insert_text((320, 160), "A Primer on Transfer", fontname=BODY, fontsize=10)
            else:
                page.insert_text((140, 160), "Transfer of Things", fontname=BODY, fontsize=10)
                page.insert_text((460, 160), f"{115 + i}", fontname=BODY, fontsize=10)
            y = 172  # 柱のすぐ下から本文（抽出で柱と 1 つのブロックにまとめられやすい）
        write_paragraph(page, (140, 473), y, LOREM, 6)
    return doc


def test_numbered_section_heading_on_the_first_page_is_not_taken_as_authors():
    a = analyze_document(_book_chapter())
    heading = _find(a, "Optical Properties of a Mixed")
    assert heading.translate and heading.kind == BlockKind.HEADING


def test_running_headers_far_from_the_page_edge_are_found_by_repetition():
    a = analyze_document(_book_chapter(7))
    for pa in a.pages[1:]:
        top = min(pa.decisions, key=lambda d: d.block.bbox[1])
        assert top.skip == SkipReason.RUNNING_HEADER, pa.page
        assert len(top.block.lines) == 1  # 下の段落とは別のブロック
        body = [d for d in pa.decisions if d.translate]
        assert body and all("Primer" not in d.block.text and "Things" not in d.block.text for d in body)
    # 1 ページ目の大きな章題は、柱と同じ文言でも訳す
    first = a.pages[0]
    assert any(d.translate and "Transfer of Things" in d.block.text for d in first.decisions)


def test_equation_number_pushed_below_a_long_equation_is_not_merged_into_the_next_paragraph():
    from pdf_factory import BODY, LOREM, write_paragraph

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((200, 300), "R = A + B + C + D + E", fontname="symb", fontsize=10)
    page.insert_text((449, 312), "(6.80)", fontname=BODY, fontsize=10)  # 式の下の行に送られた式番号
    write_paragraph(page, (140, 473), 324, LOREM, 3)
    a = analyze_document(doc)
    number = _find(a, "(6.80)")
    assert not number.translate and len(number.block.lines) == 1
    para = _find(a, "Deep neural networks")
    assert para.translate and "(6.80)" not in para.block.text


def _frag_span(text, font, size, x0, y_base, width=None):
    from spectra.core.models import Span

    w = width if width is not None else len(text) * size * 0.5
    return Span(text, font, size, 0, 0, (x0, y_base - size * 0.8, x0 + w, y_base + size * 0.2), (x0, y_base))


def _fragment_block(lines):
    from spectra.core.models import Line, TextBlock

    ls = []
    for spans in lines:
        box = (min(s.bbox[0] for s in spans), min(s.bbox[1] for s in spans), max(s.bbox[2] for s in spans), max(s.bbox[3] for s in spans))
        ls.append(Line(spans=spans, bbox=box))
    bbox = (min(ln.bbox[0] for ln in ls), min(ln.bbox[1] for ln in ls), max(ln.bbox[2] for ln in ls), max(ln.bbox[3] for ln in ls))
    return TextBlock(page=0, index=0, bbox=bbox, lines=ls)


def test_subscript_words_after_an_accented_variable_are_math():
    """分母 "β_particle ω˜_particle" の添字の単語を本文の単語と数えない（"˜" は別のスパンで置かれる）。"""
    from spectra.core.classify import _subscripts

    after_accent = _frag_span("particle", "Times-Roman", 7, 316, 713, 21)
    block = _fragment_block([[
        _frag_span("β", "rtxmi", 10, 280, 710, 6),
        _frag_span("particle", "Times-Roman", 7, 286, 713, 21),
        _frag_span("ω", "rtxmi", 10, 310, 710, 6),
        _frag_span("˜", "Times-Roman", 10, 310, 708, 4),
        after_accent,
    ]])
    assert id(after_accent) in _subscripts(block)


def test_subscript_on_a_line_split_off_at_the_same_height_is_math():
    """上付きと下付きが重なる位置で行が分けられ、次の行が下付きの単語から始まる（"β^ext" / "_particle + β^ext"）。"""
    from spectra.core.classify import _subscripts

    split = _frag_span("particle", "Times-Roman", 7, 267, 647, 21)
    block = _fragment_block([
        [_frag_span("+", "rtxr", 10, 253, 644, 6), _frag_span("β", "rtxmi", 10, 260, 644, 7), _frag_span("ext", "Times-Roman", 7, 267, 640, 8)],
        [split, _frag_span("+", "rtxr", 10, 291, 644, 6), _frag_span("β", "rtxmi", 10, 297, 644, 7), _frag_span("ext", "Times-Roman", 7, 304, 640, 8)],
    ])
    assert id(split) in _subscripts(block)
    # 下の別の行（前の行の右端から続かない行）の先頭の小さな単語は、添字としない
    below = _frag_span("note", "Times-Roman", 7, 140, 700, 14)
    block = _fragment_block([[_frag_span("β", "rtxmi", 10, 260, 644, 7), _frag_span("ext", "Times-Roman", 7, 267, 640, 8)], [below]])
    assert id(below) not in _subscripts(block)


def test_equation_number_with_a_chapter_prefix_marks_an_equation_fragment():
    from spectra.core.classify import _EQUATION_NUMBER

    assert _EQUATION_NUMBER.search("gas(6.4)")
    assert _EQUATION_NUMBER.search("x (2.10b)")
    assert _EQUATION_NUMBER.search("y (7)")


def test_upright_fragments_inside_a_display_equation_are_math():
    """分数の分子 "hc" や、式の右の "[isotropic] (3.90)" は数式の一部として訳さない。"""
    from pdf_factory import BODY, LOREM, write_paragraph

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    write_paragraph(page, (140, 473), 200, LOREM, 3)
    page.insert_text((300, 300), "hc", fontname=BODY, fontsize=10)  # 分子（立体の英字）
    page.insert_text((290, 314), "l = k + m", fontname="symb", fontsize=10)  # 分母（数式フォント）
    page.insert_text((360, 340), "a = b + c", fontname="symb", fontsize=10)
    page.insert_text((420, 340), "[isotropic] (3.90)", fontname=BODY, fontsize=10)
    write_paragraph(page, (140, 473), 380, LOREM, 3)
    a = analyze_document(doc)
    assert _find(a, "hc").skip == SkipReason.MATH
    assert _find(a, "[isotropic]").skip == SkipReason.MATH
    assert all(d.translate for d in _decisions(a) if "Deep neural" in d.block.text)


def test_capitalized_connective_below_a_display_equation_is_translated():
    """式の真下で左右に重なる 1 語の "Hence" "Thus" も、文のつなぎとして訳す。"""
    from spectra.core.classify import _has_prose_word

    assert _has_prose_word("Hence") and _has_prose_word("Thus") and _has_prose_word("Then")
    assert not _has_prose_word("hc kB T") and not _has_prose_word("dLi") and not _has_prose_word("[isotropic] (3.90)")


def test_prose_word_beside_a_display_equation_is_still_translated():
    """行列の左の "where" や箇条書きの "underconstrained m < n" は、数式と同じ高さでも本文。"""
    from spectra.core.classify import BlockStats, _absorb_equation_fragments
    from spectra.core.models import BlockDecision, TextBlock

    def block(index, spans):
        b = _fragment_block([spans])
        return TextBlock(page=0, index=index, bbox=b.bbox, lines=b.lines)

    matrix = block(0, [_frag_span("S", "rtxmi", 10, 220, 400, 6), _frag_span("0", "rtxr", 10, 240, 400, 6)])
    where = block(1, [_frag_span("where", "Times-Roman", 10, 180, 400, 24)])
    bullet = block(2, [_frag_span("well-posed", "Times-Roman", 10, 180, 412, 45), _frag_span("m", "rtxmi", 10, 228, 412, 6)])
    numerator = block(3, [_frag_span("hc", "Times-Roman", 10, 222, 390, 10)])
    decisions = [
        BlockDecision(matrix, BlockKind.BODY, SkipReason.MATH),
        BlockDecision(where, BlockKind.BODY, None),
        BlockDecision(bullet, BlockKind.BODY, None),
        BlockDecision(numerator, BlockKind.BODY, None),
    ]
    stats = {b.index: BlockStats(b) for b in (matrix, where, bullet, numerator)}
    _absorb_equation_fragments(decisions, stats, 10.0)
    assert decisions[1].translate and decisions[2].translate
    assert decisions[3].skip == SkipReason.MATH


def test_equation_with_a_chapter_number_in_a_text_font_is_math():
    """式と同じ高さの "M or E (3.11)" "[dimensionless] (3.119)" "(B.22) dx" は数式。式と並ばない同じ形の行は訳す。"""
    from spectra.core.classify import BlockStats, _absorb_equation_fragments
    from spectra.core.models import BlockDecision, TextBlock

    def block(index, spans):
        b = _fragment_block([spans])
        return TextBlock(page=0, index=index, bbox=b.bbox, lines=b.lines)

    equation = block(0, [_frag_span("=", "rtxr", 10, 200, 300, 6), _frag_span("∂Φ", "rtxmi", 10, 210, 300, 12)])
    beside = block(1, [_frag_span("M or E", "Times-Roman", 10, 160, 300, 30), _frag_span("(3.11)", "Times-Roman", 10, 440, 300, 24)])
    unit = block(2, [_frag_span("[dimensionless]", "Times-Roman", 10, 240, 300, 60)])
    alone = block(3, [_frag_span("M or E", "Times-Roman", 10, 140, 500, 30), _frag_span("(3.11)", "Times-Roman", 10, 180, 500, 24)])
    decisions = [BlockDecision(equation, BlockKind.BODY, SkipReason.MATH)] + [
        BlockDecision(b, BlockKind.BODY, None) for b in (beside, unit, alone)
    ]
    stats = {b.index: BlockStats(b) for b in (equation, beside, unit, alone)}
    _absorb_equation_fragments(decisions, stats, 10.0)
    assert decisions[1].skip == SkipReason.MATH and decisions[2].skip == SkipReason.MATH
    assert decisions[3].translate


def test_body_line_citing_a_number_without_an_equation_beside_it_is_translated():
    """数式と並ばない 1 行の "Bond albedo (0.77)" "See Eq. (3.4)" "Appendix (A.1)" は本文。"""
    from pdf_factory import BODY, LOREM, write_paragraph

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    write_paragraph(page, (140, 473), 200, LOREM, 3)
    for i, line in enumerate(["Bond albedo (0.77)", "See Eq. (3.4)", "Appendix (A.1)"]):
        page.insert_text((140, 270 + i * 30), line, fontname=BODY, fontsize=10)
    write_paragraph(page, (140, 473), 400, LOREM, 3)
    a = analyze_document(doc)
    for line in ("Bond albedo", "See Eq.", "Appendix"):
        assert _find(a, line).translate, line


def test_table_of_contents_with_leaders_is_not_translated():
    """目次の項目（リーダーとページ番号で終わる行）は訳さない。訳すと番号・リーダー・ページ番号が消える。"""
    from pdf_factory import BODY

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    entries = [("2.1", "The Nature of Light", "11"), ("2.2", "Wave Equation", "13"), ("2.3", "Light Waves in a Vacuum", "18")]
    for i, (num, title, pno) in enumerate(entries):
        # 番号・題名・リーダー・ページ番号が 1 つながりの行（列の間隔が無いので、表とは判定されない）
        page.insert_text((140, 200 + i * 12), f"{num} {title} " + ". " * 12 + pno, fontname=BODY, fontsize=10)
    a = analyze_document(doc)
    assert not _find(a, "Nature of Light").translate


def test_paragraph_with_one_line_ending_in_leader_dots_is_translated():
    """2 行の本文の 1 行目が ". . . . 3" で終わっても、目次ではない。"""
    from pdf_factory import BODY

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((140, 200), "The series continues as 1, 2, . . . . 3", fontname=BODY, fontsize=10)
    page.insert_text((140, 212), "and so on until the remaining terms become negligible in practice.", fontname=BODY, fontsize=10)
    a = analyze_document(doc)
    assert _find(a, "remaining terms").translate


def test_nomenclature_entries_are_translated_one_by_one():
    """記号表（記号と説明の行を広い行間で並べたもの）は、項目ごとに訳す。段の最後と次の段の最初の項目もつなげない。"""
    from pdf_factory import BODY

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    left = [("s", "Stephan-Boltzmann constant"), ("c", "velocity of light"), ("h", "Planck constant"), ("f", "azimuth angle")]
    right = [("r", "mass density"), ("t", "optical depth"), ("w", "single scatter albedo")]
    for col, entries in ((140, left), (320, right)):
        for i, (sym, desc) in enumerate(entries):
            y = 260 + i * 20  # 行間 20pt（文字の 2 倍）
            page.insert_text((col, y), sym, fontname="symb", fontsize=10)
            page.insert_text((col + 14, y), desc, fontname=BODY, fontsize=10)
    a = analyze_document(doc)
    for _, desc in left + right:
        d = _find(a, desc)
        assert d.translate and len(d.block.lines) == 1, desc
    seg = next(s for s in a.segments if "azimuth" in s.source)
    assert "mass density" not in seg.source


def test_double_spaced_paragraph_starting_lines_with_a_is_not_split_as_nomenclature():
    """行間の広い（ダブルスペースの）段落で、行頭が "A small" "a depth" でも、記号表として分けない。"""
    from pdf_factory import BODY

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    lines = [
        "A small fraction of the incident radiation is absorbed by",
        "a depth of the atmosphere that depends on the wavelength",
        "a mass of air that is large compared with the cloud layer",
        "and the remainder is scattered back towards the observer.",
    ]
    for i, line in enumerate(lines):
        page.insert_text((140, 200 + i * 22), line, fontname=BODY, fontsize=10)  # 行間 2.2 倍
    a = analyze_document(doc)
    d = _find(a, "A small fraction")
    assert d.translate and len(d.block.lines) == 4
