"""図・表として検出されなかった図表の中の文字を、本文として訳さない（訳す側に変えることはしない）。"""

from spectra.core.analysis import mark_figure_and_table_text
from spectra.core.models import (
    BlockDecision,
    BlockKind,
    Line,
    PageAnalysis,
    SkipReason,
    Span,
    TextBlock,
)

RM = "Times-Roman"


def _span(text, x0, x1, base, size=10.0):
    return Span(text, RM, size, 0, 0, (x0, base - size * 0.8, x1, base + size * 0.2), (x0, base))


def _line(*spans):
    return Line(spans=list(spans), bbox=(min(s.bbox[0] for s in spans), min(s.bbox[1] for s in spans),
                                         max(s.bbox[2] for s in spans), max(s.bbox[3] for s in spans)))


def _decision(index, *lines, skip=None, kind=BlockKind.BODY):
    box = (min(ln.bbox[0] for ln in lines), min(ln.bbox[1] for ln in lines),
           max(ln.bbox[2] for ln in lines), max(ln.bbox[3] for ln in lines))
    return BlockDecision(TextBlock(page=0, index=index, bbox=box, lines=list(lines)), kind, skip)


class _Page:
    """get_cdrawings だけを持つページ（描画を直接与える）。"""

    def __init__(self, drawings):
        self.drawings = drawings

    def get_cdrawings(self):
        return self.drawings


def _stroke(x0, y0, x1, y1, width=0.5):
    return {"rect": (x0, y0, x1, y1), "type": "s", "width": width, "color": (0, 0, 0)}


def _run(decisions, drawings):
    pa = PageAnalysis(page=0, width=595, height=842, decisions=decisions)
    mark_figure_and_table_text(pa, _Page(drawings))
    return [d.skip for d in pa.decisions]


PARAGRAPH = [
    _line(_span("This is an ordinary paragraph of body text that continues over several lines.", 140, 470, 600)),
    _line(_span("It has no large gaps between its words and must always be translated.", 140, 460, 612)),
]


def test_a_label_line_with_an_arrow_between_its_words_is_part_of_a_figure():
    label = _decision(0, _line(_span("measurand", 180, 220, 450), _span("measurement(s) = estimate", 290, 400, 450)))
    arrow = _stroke(224, 446, 286, 446)
    para = _decision(1, *PARAGRAPH)
    assert _run([label, para], [arrow]) == [SkipReason.FIGURE, None]


def test_lines_without_drawings_in_their_gaps_are_still_translated():
    label = _decision(0, _line(_span("measurand", 180, 220, 450), _span("measurement(s) = estimate", 290, 400, 450)))
    underline = _stroke(180, 452, 400, 452)  # 語の下に引いた線（空きの中ではない）
    assert _run([label], [underline]) == [None]


def test_rows_joining_columns_between_the_rules_under_a_table_caption_are_not_translated():
    caption = _decision(0, _line(_span("TABLE 4.4", 141, 184, 182)), kind=BlockKind.CAPTION)
    header = _decision(1, _line(_span("Moments of Inertia", 238, 320, 210), _span("Examples", 358, 400, 210)))
    cell = _decision(2, _line(_span("Linear Molecules", 143, 213, 222)))  # 1 つのセルは従来どおり訳す
    row = _decision(3, _line(_span("IA = 0, IB = IC", 239, 300, 244), _span("HCl, OCS, CO2", 358, 430, 244)))
    rules = [_stroke(141, 200, 471, 200, 1.0), _stroke(141, 514, 471, 514, 1.0)]
    assert _run([caption, header, cell, row], rules) == [None, SkipReason.TABLE, None, SkipReason.TABLE]


def test_paragraphs_between_rules_keep_the_area_from_being_treated_as_a_table():
    caption = _decision(0, _line(_span("Table 2", 141, 180, 182)), kind=BlockKind.CAPTION)
    row = _decision(1, _line(_span("Planet", 150, 190, 222), _span("Radius", 300, 340, 222)))
    long_para = _decision(2, *[_line(_span(f"Body text line {i} of a long paragraph between two rules.", 141, 470, 300 + 12 * i)) for i in range(5)])
    rules = [_stroke(141, 200, 471, 200, 1.0), _stroke(141, 400, 471, 400, 1.0)]
    assert _run([caption, row, long_para], rules) == [None, None, None]


def test_a_last_row_split_off_below_a_table_joins_the_table():
    rows = [_line(_span(f"Quantity {i}", 167, 260, 250 + 12 * i), _span("g", 338, 344, 250 + 12 * i), _span("9.8 m s", 362, 400, 250 + 12 * i)) for i in range(4)]
    table = _decision(0, *rows, skip=SkipReason.TABLE)
    last = _decision(1, _line(_span("Mass of the earth", 167, 236, 302), _span("ME", 338, 351, 302), _span("5.988 kg", 362, 425, 302)))
    para = _decision(2, *PARAGRAPH)
    assert _run([table, last, para], []) == [SkipReason.TABLE, SkipReason.TABLE, None]
    # 左端や列の位置が揃っていない 1 行は、表の続きとみなさない
    other = _decision(1, _line(_span("A note below the table", 150, 236, 302), _span("see text", 300, 340, 302)))
    assert _run([table, other], []) == [SkipReason.TABLE, None]


def test_a_row_with_a_narrow_gap_that_starts_on_a_table_column_is_not_translated():
    caption = _decision(0, _line(_span("TABLE 4.4", 141, 184, 182)), kind=BlockKind.CAPTION)
    header = _decision(1, _line(_span("Moments of Inertia", 238, 320, 210), _span("Examples", 358, 400, 210)))
    narrow = _decision(2, _line(_span("Oblate IB = IC < IA", 238, 352, 340), _span("BCl3", 358, 378, 340)))  # 列の間が 6pt
    rules = [_stroke(141, 200, 471, 200, 1.0), _stroke(141, 514, 471, 514, 1.0)]
    assert _run([caption, header, narrow], rules) == [None, SkipReason.TABLE, SkipReason.TABLE]


def test_fill_in_blanks_and_form_lines_are_not_taken_for_figures():
    """穴埋めの空欄や記入欄の下線（ベースライン付近の線）は図の矢印ではない。"""
    blank = _decision(0, _line(_span("The boiling point of water is", 140, 260, 450), _span("degrees Celsius.", 320, 390, 450)))
    form = _decision(1, _line(_span("Name:", 140, 170, 480), _span("Date:", 300, 325, 480)))
    lines = [_stroke(264, 451, 316, 451), _stroke(174, 481, 296, 481)]
    assert _run([blank, form], lines) == [None, None]


def test_a_plain_line_between_words_of_a_sentence_is_not_a_figure():
    """矢じりの無い線が文の途中（ラベルでなく、文として終わる行）にあっても図とはしない。"""
    sentence = _decision(0, _line(_span("The value shown in the second figure panel is", 140, 330, 450), _span("smaller than expected.", 380, 470, 450)))
    assert _run([sentence], [_stroke(334, 446, 376, 446)]) == [None]


def test_an_arrow_shape_between_words_marks_a_figure_even_in_a_sentence():
    arrow = {"rect": (334.0, 444.5, 376.0, 447.8), "type": "f", "items": [("re",), ("l",), ("l",)], "color": None, "width": None}
    line = _decision(0, _line(_span("The energy flows from the source", 140, 330, 450), _span("to the detector.", 380, 450, 450)))
    assert _run([line], [arrow]) == [SkipReason.FIGURE]


def test_a_ruled_table_ends_at_its_own_bottom_rule():
    """同じページの 2 つ目の表の下端まで広げない（間の、列のある行を 1 つ目の表に入れない）。"""
    cap1 = _decision(0, _line(_span("Table 1", 141, 180, 182)), kind=BlockKind.CAPTION)
    row1 = _decision(1, _line(_span("Planet", 150, 190, 212), _span("Radius", 300, 340, 212)))
    row1b = _decision(2, _line(_span("Venus", 150, 190, 226), _span("6052 km", 300, 340, 226)))
    between = _decision(3, _line(_span("Notes:", 141, 170, 272), _span("values from the archive", 300, 420, 272)))
    cap2 = _decision(4, _line(_span("Table 2", 141, 180, 292)), kind=BlockKind.CAPTION)
    row2 = _decision(5, _line(_span("Moon", 150, 190, 322), _span("1737 km", 300, 340, 322)))
    rules = [_stroke(141, 200, 471, 200, 1.0), _stroke(141, 240, 471, 240, 1.0),
             _stroke(141, 305, 471, 305, 1.0), _stroke(141, 340, 471, 340, 1.0)]
    skips = _run([cap1, row1, row1b, between, cap2, row2], rules)
    assert skips[3] is None  # 表の外の行は訳す
    assert skips[1] == skips[2] == skips[5] == SkipReason.TABLE


def test_a_note_below_a_table_does_not_join_it_through_font_changes_in_cells():
    """セルの中のフォントの変わり目の位置と偶然揃った表の注は、表の行とみなさない。"""
    rows = [_line(_span(f"Planet {i}", 167, 200, 250 + 12 * i), _span("b", 205, 212, 250 + 12 * i), _span("Jupiter", 300, 340, 250 + 12 * i)) for i in range(4)]
    table = _decision(0, *rows, skip=SkipReason.TABLE)
    note = _decision(1, _line(_span("Source:", 167, 185, 302), _span("planetary fact sheet", 205, 290, 302)))
    assert _run([table, note], []) == [SkipReason.TABLE, None]
