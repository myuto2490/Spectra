"""下線を図形で描いた変数名（"sky_trans_ν" の "_"）の扱い。

段落の行の中の図形を訳文と一緒に動かす処理が、数式の中の線まで別の保護要素に分けると、変数名が途切れて
"sky" だけが本文として訳されていた。数式の中の線は、数式と一緒に動かす既存の処理に任せる。
"""

import pymupdf

from spectra.core import analysis
from spectra.core.markup import to_plain
from spectra.core.models import BlockDecision, BlockKind, Line, PageAnalysis, Span, TextBlock
from spectra.core.segment import build_segments

RM, IT, GREEK = "AdvGulliv-R", "AdvGulliv-I", "AdvPSMP10"


def _span(text, font, size, x0, x1, base):
    return Span(text, font, size, 0, 0, (x0, base - size * 0.8, x1, base + size * 0.2), (x0, base))


def _page_and_analysis():
    """抽出結果どおりのスパン（"sky" と "trans" の間に空白のスパン、その空きに "_" の塗りの四角）。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(pymupdf.Rect(416.41, 518.8, 418.9, 519.08), color=None, fill=(0, 0, 0))  # "_"
    first = Line(
        spans=[
            _span("We then corrected the data from our flat-field via,", RM, 8.08, 230.0, 400.1, 519.08),
            _span(" ", IT, 8.08, 400.1, 404.1, 519.08),
            _span("sky", IT, 8.08, 404.1, 415.9, 519.08),
            _span(" ", IT, 8.08, 415.9, 418.9, 519.08),
            _span("trans", IT, 8.08, 418.9, 437.4, 519.08),
            _span("m", GREEK, 6.41, 437.3, 440.2, 520.27),  # ν（下付き）
            _span(" is the corrected transmission of the night.", RM, 8.08, 444.0, 560.0, 519.08),
        ],
        bbox=(230.0, 512.0, 560.0, 522.0),
    )
    second = Line(
        spans=[_span("This correction removes the residual pattern of the detector rows.", RM, 8.08, 230.0, 500.0, 529.0)],
        bbox=(230.0, 522.0, 500.0, 532.0),
    )
    block = TextBlock(page=0, index=0, bbox=(230.0, 512.0, 560.0, 532.0), lines=[first, second])
    pa = PageAnalysis(page=0, width=595, height=842, decisions=[BlockDecision(block, BlockKind.BODY)])
    return page, pa


def test_an_underscore_inside_a_variable_name_keeps_the_name_together():
    page, pa = _page_and_analysis()
    attach = getattr(analysis, "attach_line_art", None)  # 行の中の図形を動かす処理（無い版では何もしない）
    if attach is not None:
        attach(pa, page)
    seg = build_segments([(pa.width, pa.decisions)], body_font=RM)[0]
    plain = " ".join(to_plain(seg.source).split())
    assert "via," in plain and "via, sky" not in plain  # 変数名の "sky" は本文として訳さない
    assert any("sky" in r.text and "trans" in r.text for r in seg.runs.values())
