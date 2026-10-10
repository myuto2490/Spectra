import pymupdf

from spectra.core.redact import erase_text


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
