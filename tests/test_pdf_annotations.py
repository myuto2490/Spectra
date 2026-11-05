"""PDF 表示の注釈（印の描画と判定・選んだ範囲の帯・マーカーの道具・訳文の余白の帯・移動と強調）のテスト。画面は出さない。"""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

import pymupdf  # noqa: E402
from PySide6.QtCore import (  # noqa: E402
    QEvent,  # noqa: E402
    QPointF,
    Qt,
)
from PySide6.QtGui import QKeyEvent  # noqa: E402

from spectra.library.models import Annotation  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from spectra.ui.app import create_app

    return create_app([])


def pump(app, seconds=0.1):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def plain_doc(pages: int = 4) -> pymupdf.Document:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=400, height=500)
        page.insert_text((50, 100), f"alpha beta gamma delta page{i + 1}", fontsize=14)
        page.insert_text((50, 130), "second line here", fontsize=14)
    return doc


def ann(id="a1", kind="highlight", page=0, quads=((50.0, 88.0, 150.0, 104.0),), color="yellow", comment="", quote="alpha beta"):
    return Annotation(
        id=id, paper_id=1, kind=kind, page=page, quads=tuple(quads), quote=quote, color=color,
        comment=comment, created_at=1.0, updated_at=1.0,
    )


@pytest.fixture()
def view(app):
    from spectra.ui.pdf_view import PdfView

    v = PdfView()
    v.resize(700, 600)
    v.show()
    v.set_document(plain_doc())
    v.set_annotation_mode("marks")
    pump(app)
    yield v
    v.close()


def pos_of(view, index, x, y):
    w = view._pages[index]
    r = w._page_rect()
    return QPointF(r.x() + x * view.zoom(), r.y() + y * view.zoom())


def drag(view, index, start, end):
    w = view._pages[index]
    a, b = pos_of(view, index, *start), pos_of(view, index, *end)
    view.press(index, w.to_page(a), a)
    view.drag(index, w.to_page(b), b)
    view.release(index, w.to_page(b), b)


def click(view, index, x, y, annotation_id=""):
    w = view._pages[index]
    p = pos_of(view, index, x, y)
    a = w.annotation_at(p)
    view.press(index, w.to_page(p), p)
    view.release(index, w.to_page(p), p, None, a.id if a else annotation_id)
    return a


# -- 描画の場所と判定 ------------------------------------------------------------------


def test_marks_and_bands_have_different_shapes(view):
    h = ann("h", "highlight", comment="あとで読む")
    u = ann("u", "underline", quads=((50.0, 118.0, 120.0, 134.0),), color="green")
    n = ann("n", "note", quads=((200.0, 200.0, 200.0, 200.0),), color="pink")
    view.set_annotations([h, u, n])
    w = view._pages[0]
    rects = w.annotation_rects(h)
    assert len(rects) == 2  # 文字の上の矩形 + コメントの目印
    assert rects[0].width() == pytest.approx(100 * view.zoom())  # 原文の座標のとおりに描く
    assert len(w.annotation_rects(u)) == 1
    note = w.annotation_rects(n)[0]
    assert note.width() == 16 and note.left() == pytest.approx(w._page_rect().x() + 200 * view.zoom())

    view.set_annotation_mode("bands")  # 訳文: 左の余白に、同じ高さの帯
    band = w.annotation_rects(h)[0]
    assert band.width() == 5 and band.top() == pytest.approx(w._page_rect().y() + 88 * view.zoom())
    assert band.height() == pytest.approx(16 * view.zoom())
    assert len(w.annotation_rects(h)) == 1


def test_annotation_at_and_tooltip_text(view):
    from spectra.ui.pdf_view import _annotation_summary

    a = ann("a", comment="図 2 を見る", quote="alpha beta")
    view.set_annotations([a])
    w = view._pages[0]
    assert w.annotation_at(pos_of(view, 0, 100, 96)).id == "a"
    assert w.annotation_at(pos_of(view, 0, 100, 300)) is None
    assert view.annotation_at_point(0, pymupdf.Point(100, 96)).id == "a"
    assert view.annotation_at_point(1, pymupdf.Point(100, 96)) is None  # 別のページ
    assert _annotation_summary(a) == "「alpha beta」\n図 2 を見る"
    assert _annotation_summary(ann(quote="x" * 200)).count("x") == 79  # 長い引用は 80 文字に切る
    assert _annotation_summary(ann(quote="", comment="")) == "注釈"
    view.set_annotation_mode("")
    assert view.annotations_on(0) == []  # 描かないモード


def test_later_annotation_is_on_top(view):
    view.set_annotations([ann("old"), ann("new")])
    assert view._pages[0].annotation_at(pos_of(view, 0, 100, 96)).id == "new"


# -- クリック・選ぶ・消す ----------------------------------------------------------------


def test_click_on_a_mark_opens_it_and_delete_asks_to_remove(view, app):
    view.set_annotations([ann("a")])
    got, deleted = [], []
    view.annotationClicked.connect(lambda i, p: got.append(i))
    view.annotationDeleteRequested.connect(deleted.append)
    click(view, 0, 100, 96)
    assert got == ["a"] and view.selected_annotation() == "a"
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier))
    assert deleted == ["a"]
    click(view, 0, 300, 400)  # 何もないところをクリックすると、選びを外す
    assert view.selected_annotation() == ""
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier))
    assert deleted == ["a"]  # 選んでいなければ何もしない


def test_clicking_a_link_still_works_beside_marks(app):
    from spectra.ui.pdf_view import PdfView

    doc = plain_doc(2)
    doc[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(50, 300, 150, 320), "uri": "https://example.com"})
    v = PdfView()
    v.resize(700, 600)
    v.show()
    v.set_document(doc)
    v.set_annotation_mode("marks")
    v.set_annotations([ann("a")])
    pump(app)
    urls = []
    v.linkRequested.connect(urls.append)
    click(v, 0, 100, 310)
    assert urls == ["https://example.com"]
    v.close()


def test_set_annotations_drops_a_selection_that_no_longer_exists(view):
    view.set_annotations([ann("a")])
    view.select_annotation("a")
    view.set_annotations([])
    assert view.selected_annotation() == ""


# -- 選んだ文字 → 小さな帯 -----------------------------------------------------------------


def test_selection_parts_split_by_page(view):
    drag(view, 0, (52, 96), (140, 96))
    parts = view.selection_parts()
    assert len(parts) == 1 and parts[0][0] == 0
    page, quads, quote = parts[0]
    assert quads and all(len(q) == 4 for q in quads) and quote.startswith("alpha")
    view.select_all()
    parts = view.selection_parts()
    assert [p[0] for p in parts] == [0, 1, 2, 3]  # ページごとに 1 件ずつ
    assert "second line here" in parts[1][2]
    view.clear_selection()
    assert view.selection_parts() == []


def test_selection_bar_shows_after_selecting_and_hides_on_click(view, app):
    assert view.bar.isHidden()
    drag(view, 0, (52, 96), (140, 96))
    pump(app, 0.05)
    assert not view.bar.isHidden()
    assert view.bar.height() > 0 and view.bar.geometry().right() <= view.viewport().width()
    assert not view.bar.underline_btn.isHidden() and view.bar.original_btn.isHidden()
    w = view._pages[0]
    p = pos_of(view, 0, 300, 400)
    view.press(0, w.to_page(p), p)  # 別のところを押すと、帯は消える
    assert view.bar.isHidden()


def test_bar_buttons_request_annotations_and_clear_the_selection(view):
    reqs, colors = [], []
    view.annotationRequested.connect(lambda k, c, w: reqs.append((k, c, w)))
    view.colorPicked.connect(colors.append)
    drag(view, 0, (52, 96), (140, 96))
    view.bar.color_buttons["green"].click()
    assert reqs == [("highlight", "green", False)] and colors == ["green"] and view.current_color() == "green"
    assert not view.has_selection() and view.bar.isHidden()  # 付けたら選びを外す
    drag(view, 0, (52, 96), (140, 96))
    view.bar.underline_btn.click()
    drag(view, 0, (52, 96), (140, 96))
    view.bar.comment_btn.click()
    assert reqs[1:] == [("underline", "green", False), ("highlight", "green", True)]  # 色は直前に選んだもの
    drag(view, 0, (52, 96), (140, 96))
    view.bar.copy_btn.click()
    from PySide6.QtGui import QGuiApplication

    assert QGuiApplication.clipboard().text().startswith("alpha") and not view.has_selection()


def test_translated_view_asks_to_use_the_original(app):
    from spectra.ui.pdf_view import PdfView

    v = PdfView()
    v.resize(700, 600)
    v.show()
    v.set_document(plain_doc())
    v.set_annotation_mode("bands")
    pump(app)
    asked = []
    v.originalRequested.connect(asked.append)
    drag(v, 1, (52, 96), (140, 96))
    assert not v.bar.isHidden() and v.bar.underline_btn.isHidden() and not v.bar.original_btn.isHidden()
    v.bar.original_btn.click()
    assert asked == [1] and not v.has_selection()
    v.close()


# -- マーカーの道具 ----------------------------------------------------------------------


def test_marker_tool_annotates_on_release_and_note_tool_places_on_click(view):
    reqs, notes, cancelled = [], [], []
    view.annotationRequested.connect(lambda k, c, w: reqs.append((k, c, w)))
    view.notePlaced.connect(lambda p, x, y: notes.append((p, round(x), round(y))))
    view.toolCancelled.connect(lambda: cancelled.append(1))
    drag(view, 0, (52, 96), (140, 96))  # 道具を使っていなければ、帯が出るだけ
    assert reqs == [] and not view.bar.isHidden()
    view.clear_selection()

    view.set_tool("highlight")
    view.set_current_color("pink")
    drag(view, 0, (52, 96), (140, 96))
    assert reqs == [("highlight", "pink", False)] and not view.has_selection()
    view.set_tool("underline")
    drag(view, 1, (52, 96), (140, 96))
    assert reqs[-1] == ("underline", "pink", False)

    view.set_tool("note")
    click(view, 0, 200, 300)
    assert notes == [(0, 200, 300)]
    view.set_annotations([ann("a")])
    got = []
    view.annotationClicked.connect(lambda i, p: got.append(i))
    click(view, 0, 100, 96)
    assert got == ["a"] and len(notes) == 1  # 印の上では付箋を置かない

    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    assert cancelled == [1]
    view.set_tool("")
    view.set_tool("bogus")
    assert view.tool() == ""


def test_tool_does_nothing_in_the_translated_view(app):
    from spectra.ui.pdf_view import PdfView

    v = PdfView()
    v.resize(700, 600)
    v.show()
    v.set_document(plain_doc())
    v.set_annotation_mode("bands")
    v.set_tool("highlight")
    pump(app)
    reqs = []
    v.annotationRequested.connect(lambda k, c, w: reqs.append(1))
    drag(v, 0, (52, 96), (140, 96))
    assert reqs == []  # 付けるのは原文だけ
    v.close()


def test_escape_clears_a_selection(view):
    drag(view, 0, (52, 96), (140, 96))
    assert view.has_selection()
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    assert not view.has_selection() and view.bar.isHidden()


# -- 移動と強調 ------------------------------------------------------------------------


def test_go_to_annotation_scrolls_and_flashes(view, app):
    view.set_annotations([ann("far", page=3, quads=((50.0, 400.0, 150.0, 416.0),))])
    assert view.go_to_annotation("far")
    pump(app, 0.1)
    assert view.current_page() == 3 and view.flashing_id() == "far"
    assert not view.go_to_annotation("missing") and not view.go_to_annotation("")
    view._end_flash()
    assert view.flashing_id() == ""


def test_preview_pair_draws_marks_on_the_original_and_bands_on_the_translation(app):
    from spectra.ui.pdf_view import PreviewPair

    pair = PreviewPair()
    pair.resize(1100, 600)
    pair.show()
    pair.original.set_document(plain_doc())
    pair.translated.set_document(plain_doc())
    pair.set_annotations([ann("a", page=2, quads=((50.0, 400.0, 150.0, 416.0),))])
    pump(app)
    assert pair.original.annotation_mode() == "marks" and pair.translated.annotation_mode() == "bands"
    assert pair.original.annotations_on(2) and pair.translated.annotations_on(2)
    # 訳文だけを表示しているときは、訳文の側で移動して強調する
    pair.set_mode("translated")
    pump(app, 0.1)
    assert pair.go_to_annotation("a")
    assert pair.translated.flashing_id() == "a"
    pair.set_mode("both")
    pump(app, 0.1)
    assert pair.go_to_annotation("a") and pair.original.flashing_id() == "a" and pair.translated.flashing_id() == "a"
    # 付ける操作は原文からだけ届く
    reqs = []
    pair.annotationRequested.connect(lambda k, c, w: reqs.append(k))
    pair.original.annotationRequested.emit("highlight", "yellow", False)
    pair.translated.annotationRequested.emit("highlight", "yellow", False)
    assert reqs == ["highlight"]
    pair.close()
