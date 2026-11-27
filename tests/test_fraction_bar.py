"""段落中の分数の横線が分子・分母の文字より長くても、数式と一緒に移す（元の位置に取り消し線のように残さない）。"""

import pymupdf

from spectra.core.layout import PlacedRun
from spectra.core.models import InlinePiece, InlineRun
from spectra.core.render import Renderer

RUN_BOX = (181.0, 365.3, 208.6, 384.0)  # 分子・分母の文字の範囲
BAR = ((194.1, 372.0), (210.4, 372.0))  # 横線は文字より右へ 1.8pt はみ出す


def _renderer_with_bar(extra=()):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.draw_line(*BAR, width=0.558)
    for a, b in extra:
        page.draw_line(a, b, width=0.558)
    r = Renderer.__new__(Renderer)
    r.doc = doc
    r._extent = {}
    piece = InlinePiece("hc", 0.7, 0.3, True, False, 0, True, 0.0)
    run = InlineRun((piece,), width_ratio=2.8, page=0, bbox=RUN_BOX, origin=(181.0, 375.0), base_size=9.96)
    return r, run, PlacedRun(0, 0, 300.0, 500.0, 1.0)


def test_a_fraction_bar_slightly_wider_than_the_formula_moves_with_it():
    r, run, pr = _renderer_with_bar()
    found = r._collect_inline_vectors([(run, pr)])
    assert [tuple(round(v, 1) for v in d["rect"]) for d, _, _ in found] == [(194.1, 372.0, 210.4, 372.0)]
    box = r._extent[id(run)]
    assert box[2] >= 210.4 and box[0] == RUN_BOX[0]  # 切り抜きは横線まで広がる（左右だけ）
    assert box[1] == RUN_BOX[1] and box[3] == RUN_BOX[3]


def test_lines_far_from_the_formula_are_not_taken():
    far = (((215.0, 372.0), (240.0, 372.0)), ((181.0, 395.0), (208.0, 395.0)))  # 右に離れた線、下の別の行の線
    r, run, pr = _renderer_with_bar(far)
    found = r._collect_inline_vectors([(run, pr)])
    assert [tuple(round(v, 1) for v in d["rect"]) for d, _, _ in found] == [(194.1, 372.0, 210.4, 372.0)]
