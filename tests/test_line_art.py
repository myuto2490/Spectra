"""段落の行の中（語と語の間）に描かれた小さなベクター図形を、訳文と一緒に動かす。

元の組版で記号の代わりに描かれた黒い四角などは、訳文に置き換えると元の位置に残って訳文と重なっていた。
"""

import pymupdf
import pytest

from spectra.core.analysis import analyze_document, attach_line_art
from spectra.core.models import LINE_ART_FONT
from spectra.core.pipeline import TranslationJob
from spectra.core.render import Renderer
from spectra.translate.dummy import DummyTranslator

BOX_Y = 312.0
BOXES = [(305.747, 317.087), (323.045, 334.385)]  # 太い横線 2 本（黒い四角に見える）。消す範囲が少し重なる間隔


def _doc(boxes=BOXES, width=5.67, table=False) -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((10, 10), " ", fontname="tiro", fontsize=10)  # フォント資源を作る
    font = page.get_fonts()[0][4]
    # 2 行目は "related by" と "negative" の間を 40pt 空けて、その空きに図形を描く（同じ BT の中で Td で移動）
    body = (
        f"BT /{font} 10 Tf 1 0 0 1 60 542 Tm (Atmospheric light paths are defined by the cosine of the zenith angle.)Tj "
        f"0 -12 Td (Hence the atmospheric path and optical depth are related by)Tj 280 0 Td (negative values of the)Tj "
        f"-280 -12 Td (path direction which depends upon the geometry of the problem considered here.)Tj ET"
    ).encode()
    # TeX の出力と同じく、0.1 倍の座標系で線の太さを 1 回だけ指定し、続けて 2 本を引く（q/Q で区切らない）
    y = (842 - (BOX_Y - 3)) * 10
    strokes = " ".join(f"{x0 * 10:.2f} {y:.2f} m {x1 * 10:.2f} {y:.2f} l S" for x0, x1 in boxes)
    body += f" q 0.1 0 0 0.1 0 0 cm 0 G {width * 10:g} w {strokes} Q".encode()
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, body)
    doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")
    if table:
        page.draw_rect(pymupdf.Rect(285, 280, 335, 340), color=(0, 0, 0), width=0.5)
    return doc


def _thick(page: pymupdf.Page) -> list[tuple]:
    return [tuple(round(v) for v in d["rect"]) for d in page.get_drawings() if (d.get("width") or 0) > 3]


def _line_art_spans(doc) -> list:
    a = analyze_document(doc)
    return [s for d in a.pages[0].decisions for ln in d.block.lines for s in ln.spans if s.font == LINE_ART_FONT]


def test_graphics_between_words_become_movable_pieces():
    spans = _line_art_spans(_doc())
    assert len(spans) == 1  # 同じ空きの 2 本は 1 つの塊
    assert spans[0].bbox[0] <= 305 and spans[0].bbox[2] >= 333


@pytest.mark.parametrize(
    "kwargs",
    [
        {"boxes": [(298.0, 338.0)]},  # 4 字幅より長い線（罫線など）は対象にしない
        {"width": 20.0},  # 行の高さに収まらない図形は対象にしない
        {"table": True},  # 表・図の中の図形は対象にしない
    ],
)
def test_large_or_figure_graphics_are_left_alone(kwargs):
    assert _line_art_spans(_doc(**kwargs)) == []


def test_graphics_move_with_the_translation():
    doc = _doc()
    before = _thick(doc[0])
    res = TranslationJob(doc.tobytes(), DummyTranslator(), None, verify=False).run()
    out = pymupdf.open(stream=res.translated_pdf, filetype="pdf")[0]
    after = _thick(out)
    assert len(after) == 2  # 2 本とも訳文の中へ移った（元の見た目のまま）
    assert not set(before) & set(after)  # 元の位置には残っていない


def test_all_moved_vector_lines_are_erased_from_their_original_place():
    """MuPDF は並んだ複数の線を 1 回の消去で消し切れないことがあるので、残ったものを消し直す。"""
    page = _doc()[0]
    targets = [d for d in page.get_drawings() if (d.get("width") or 0) > 3]
    Renderer._erase_vectors(page, targets)
    assert _thick(page) == []


def test_pages_without_translated_text_are_untouched():
    from spectra.core.models import SkipReason

    doc = _doc()
    pa = analyze_document(doc).pages[0]
    for d in pa.decisions:
        d.skip = SkipReason.FIGURE  # すべて訳さない
    before = [[s.font for s in ln.spans] for d in pa.decisions for ln in d.block.lines]
    attach_line_art(pa, doc[0])
    assert [[s.font for s in ln.spans] for d in pa.decisions for ln in d.block.lines] == before
