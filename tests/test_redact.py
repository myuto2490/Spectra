import pymupdf
import pytest

from spectra.core.redact import erase_text, resolve_line_feeds, resolve_line_feeds_in


def _page_with_form(text_in_form: bool) -> tuple[pymupdf.Document, pymupdf.Page]:
    """図の部品（フォーム XObject）を貼り込んだページを作る。"""
    src = pymupdf.open()
    sp = src.new_page(width=200, height=100)
    sp.draw_rect(pymupdf.Rect(10, 10, 190, 90), color=(0, 0, 1), fill=(0.8, 0.9, 1))
    if text_in_form:
        sp.insert_text((20, 50), "Body text inside a form", fontsize=10)
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.show_pdf_page(pymupdf.Rect(100, 150, 300, 250), src, 0)  # 部品として貼り込む
    page.insert_text((40, 60), "Body text on the page", fontsize=10)
    return doc, page


def test_forms_are_not_duplicated_and_figure_is_unchanged():
    doc, page = _page_with_form(text_in_form=False)
    before = page.get_pixmap(clip=pymupdf.Rect(100, 150, 300, 250), dpi=72).samples
    n0 = doc.xref_length()
    line = next(b for b in page.get_text("dict")["blocks"] if b["type"] == 0)["bbox"]
    erase_text(page, [tuple(line)])
    assert "Body text on the page" not in page.get_text()
    assert doc.xref_length() - n0 < 10  # 部品の複製を作らない
    assert page.get_pixmap(clip=pymupdf.Rect(100, 150, 300, 250), dpi=72).samples == before


def test_text_inside_forms_is_still_erased():
    doc, page = _page_with_form(text_in_form=True)
    words = page.get_text("words")
    target = next(w for w in words if w[4] == "inside")
    erase_text(page, [(target[0], target[1], target[2], target[3])])
    assert "inside" not in page.get_text()


def test_page_level_resources_with_several_forms_are_restored_exactly():
    """資源がページ自身に書かれ、部品が複数ある場合（redaction が名前を付け替える）も図が二重にならない。"""
    figs = pymupdf.open()
    for color in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
        fp = figs.new_page(width=100, height=100)
        for i in range(5):
            fp.draw_line((10, 10 + i * 15), (90, 20 + i * 15), color=color)
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=400)
    for i in range(3):
        page.show_pdf_page(pymupdf.Rect(20 + i * 120, 200, 120 + i * 120, 300), figs, i)
    page.insert_text((40, 60), "Body text on the page", fontsize=10)
    drawings = len(page.get_drawings())
    figure_area = pymupdf.Rect(0, 150, 400, 400)
    before = page.get_pixmap(clip=figure_area, dpi=72).samples
    line = next(b for b in page.get_text("dict")["blocks"] if b["type"] == 0)["bbox"]
    erase_text(page, [tuple(line)])
    assert "Body text" not in page.get_text()
    assert len(page.get_drawings()) == drawings
    assert page.get_pixmap(clip=figure_area, dpi=72).samples == before


# ---- 行送り（TL）を使う改行命令 ---------------------------------------------------
# MuPDF 1.28 は redaction で描画命令を書き直すとき、T*・'・" の行送りを取り違え、
# 消していない後ろの文字までずらしたり消したりする。消す前に同じ意味の命令へ置き換える。


def _page_with_stream(body: bytes, width: float = 300, height: float = 200) -> tuple[pymupdf.Document, pymupdf.Page]:
    """描画命令を直接書いた最小のページ（/F1 は Helvetica）。"""
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    page.insert_text((10, 10), "x", fontname="helv")  # フォント資源を作る
    font = page.get_fonts()[0][4]
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, body.replace(b"/F1", b"/" + font.encode()))
    doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")
    return doc, page


def _words(page: pymupdf.Page) -> list[tuple[str, float, float]]:
    return [(w[4], round(w[0], 1), round(w[1], 1)) for w in page.get_text("words")]


def _erase_words(page: pymupdf.Page, names: set[str], shrink: float = 0.3) -> None:
    rects = []
    for w in page.get_text("words"):
        if w[4] in names:
            h = (w[3] - w[1]) * shrink
            rects.append((w[0], w[1] + h, w[2], w[3] - h))
    erase_text(page, rects)


_LINE_FEED_CASES = {
    # 上付きを Td で置いたあと、行送りと ' で下付きを置く（TeX の添字。消すと後ろの行が上下にずれた）
    "quote": b"BT /F1 10 Tf 1 0 0 1 20 150 Tm (Hello)Tj 30 8 Td (sup)Tj 16 TL (sub)' 20 8 Td (after)Tj -70 -20 Td (Next)Tj ET",
    # T* で改行（消すと改行ごと落ち、下付きが消えた）
    "tstar": b"BT /F1 10 Tf 1 0 0 1 20 150 Tm (Hello)Tj 30 8 Td (sup)Tj 16 TL T* (sub)Tj 20 8 Td (after)Tj -70 -20 Td (Next)Tj ET",
    # " で改行（何も消さなくても、書き直すと後ろの文字がすべて消えた）
    "dquote": b"BT /F1 10 Tf 1 0 0 1 20 150 Tm 12 TL (Hello)Tj 0 0 (two)\" (three)' ET",
}


@pytest.mark.parametrize("case", sorted(_LINE_FEED_CASES))
@pytest.mark.parametrize("erase", [("Hello",), ("sup",), ("Hello", "sup")])
def test_erasing_keeps_text_placed_with_line_feeds_in_place(case, erase):
    doc, page = _page_with_stream(_LINE_FEED_CASES[case])
    before = _words(page)
    erase = set(erase) & {w[0] for w in before}
    if not erase:
        pytest.skip("消す語が無い組み合わせ")
    _erase_words(page, erase)
    assert _words(page) == [w for w in before if w[0] not in erase]


def test_resolve_line_feeds_in_expands_operators():
    stream = b"BT 12 TL (a)Tj T* (b)' 1 2 (c)\" ET"
    out = resolve_line_feeds_in(stream)
    assert out == b"BT 12 TL (a)Tj 0 -12 Td  0 -12 Td (b)Tj 1 Tw 2 Tc 0 -12 Td (c)Tj ET"


def test_resolve_line_feeds_in_tracks_td_and_graphics_state():
    # TD は行送りも設定する。q/Q は行送りを保存・復元する。BT・Tm では初期化されない
    stream = b"BT 0 -5 TD q 7 TL Q ET BT 1 0 0 1 0 0 Tm T* ET"
    assert resolve_line_feeds_in(stream) == b"BT 0 -5 TD q 7 TL Q ET BT 1 0 0 1 0 0 Tm 0 -5 Td  ET"


def test_resolve_line_feeds_in_leaves_strings_and_inline_images_alone():
    stream = b"BT 3 TL (T* \\) ' \") Tj <54> ' ET BI /W 1 /H 1 ID \x00T*'\" EI T*"
    out = resolve_line_feeds_in(stream)
    assert out == b"BT 3 TL (T* \\) ' \") Tj 0 -3 Td <54> Tj ET BI /W 1 /H 1 ID \x00T*'\" EI 0 -3 Td "


def test_resolve_line_feeds_in_gives_up_when_leading_is_unknown():
    # 図の部品（フォーム XObject）は呼び出し側の行送りを引き継ぐので、自分で設定していなければ置き換えない
    assert resolve_line_feeds_in(b"BT (a)' ET", leading=None) is None
    assert resolve_line_feeds_in(b"BT 4 TL (a)' ET", leading=None) == b"BT 4 TL 0 -4 Td (a)Tj ET"
    assert resolve_line_feeds_in(b"BT (unterminated ET") is None


def test_pages_without_line_feeds_are_not_rewritten():
    doc, page = _page_with_stream(b"BT /F1 10 Tf 1 0 0 1 20 150 Tm (Hello)Tj 0 -12 Td (World)Tj ET")
    contents = page.get_contents()
    assert resolve_line_feeds(page) is False
    assert page.get_contents() == contents


def test_text_with_line_feeds_inside_forms_is_erased_without_moving_the_rest():
    src = pymupdf.open()
    sp = src.new_page(width=300, height=200)
    sp.insert_text((10, 10), "x", fontname="helv")
    font = sp.get_fonts()[0][4]
    body = _LINE_FEED_CASES["quote"].replace(b"/F1", b"/" + font.encode())
    xref = src.get_new_xref()
    src.update_object(xref, "<<>>")
    src.update_stream(xref, body)
    src.xref_set_key(sp.xref, "Contents", f"{xref} 0 R")
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.show_pdf_page(page.rect, src, 0)  # 本文ごと部品として貼り込む
    before = _words(page)
    _erase_words(page, {"sup"}, shrink=0)  # 消し残りを見つけて部品の中まで消すよう、語の範囲全体を指定する
    assert _words(page) == [w for w in before if w[0] != "sup"]
