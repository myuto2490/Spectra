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
