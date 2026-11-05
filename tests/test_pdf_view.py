"""PDF 表示（ページ移動・ズーム・検索・文字の選択・リンク・ページの一覧と目次）のテスト。画面は出さない。"""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

import pymupdf  # noqa: E402
from pdf_factory import make_paper  # noqa: E402
from PySide6.QtCore import QPointF  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from spectra.ui.app import create_app

    return create_app([])


def pump(app, seconds=0.1):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def plain_doc(pages: int = 5, words_per_page: str = "alpha beta gamma delta") -> pymupdf.Document:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=400, height=500)
        page.insert_text((50, 100), f"{words_per_page} page{i + 1}", fontsize=14)
        page.insert_text((50, 130), "second line here", fontsize=14)
    return doc


@pytest.fixture()
def view(app):
    from spectra.ui.pdf_view import PdfView

    v = PdfView()
    v.resize(700, 600)
    v.show()
    v.set_document(plain_doc())
    pump(app)
    yield v
    v.close()


def click_point(view, index, x, y):
    """ページ index の中の座標 (x, y)（pt）に対応するページ部品の位置。"""
    w = view._pages[index]
    r = w._page_rect()
    return QPointF(r.x() + x * view.zoom(), r.y() + y * view.zoom())


# -- ページの移動 ---------------------------------------------------------------


def test_go_to_page_clamps_and_reports(view, app):
    seen = []
    view.pageChanged.connect(lambda i, n: seen.append((i, n)))
    assert view.page_count() == 5
    assert view.go_to_page(3) == 3
    pump(app)
    assert view.current_page() == 3 and seen[-1] == (3, 5)
    assert view.go_to_page(99) == 4  # 範囲の外は端に寄せる
    assert view.go_to_page(-5) == 0


def test_next_and_previous_page(view, app):
    view.go_to_page(1)
    pump(app)
    view.next_page()
    pump(app)
    assert view.current_page() == 2
    view.verticalScrollBar().setValue(view.verticalScrollBar().value() + 120)  # ページの途中
    view.previous_page()  # まず今のページの先頭へ
    pump(app)
    assert view.current_page() == 2
    view.previous_page()
    pump(app)
    assert view.current_page() == 1


def test_go_to_page_without_document(app):
    from spectra.ui.pdf_view import PdfView

    v = PdfView()
    assert v.go_to_page(3) == 0 and v.outline() == [] and v.page_count() == 0
    assert v.search("x") == 0 and v.selected_text() == ""


# -- ズーム ---------------------------------------------------------------------


def test_fit_width_and_fit_page(view, app):
    view.fit_width()
    pump(app)
    assert view.fit_mode() == "width"
    width_zoom = view.zoom()
    view.fit_page()
    pump(app)
    assert view.fit_mode() == "page"
    assert view.zoom() <= width_zoom + 1e-6  # ページ全体は、幅に合わせるより小さいか同じ
    page_h = 500 * view.zoom()
    assert page_h <= view.viewport().height()
    view.set_zoom(1.5)
    assert view.fit_mode() == "" and abs(view.zoom() - 1.5) < 1e-6


def test_zoom_is_clamped(view):
    from spectra.ui.pdf_view import MAX_ZOOM, MIN_ZOOM

    view.set_zoom(100)
    assert view.zoom() == MAX_ZOOM
    view.set_zoom(0.001)
    assert view.zoom() == MIN_ZOOM


def test_zoom_around_pointer_keeps_the_point_in_place(view, app):
    view.set_zoom(1.0)
    pump(app)
    view.verticalScrollBar().setValue(300)
    anchor = QPointF(200, 150)
    before = (view.verticalScrollBar().value() + anchor.y()) / view.zoom()
    view.set_zoom(2.0, anchor=anchor)
    pump(app)
    after = (view.verticalScrollBar().value() + anchor.y()) / view.zoom()
    assert abs(before - after) < 2.0


def test_view_state_roundtrip(view, app):
    view.set_zoom(1.25)
    pump(app)
    view.go_to_page(2)
    view.verticalScrollBar().setValue(view.verticalScrollBar().value() + 80)
    pump(app)
    state = view.view_state()
    assert state["fit"] == "none" and state["page"] == 2 and abs(state["zoom"] - 1.25) < 1e-3

    from spectra.ui.pdf_view import PdfView

    other = PdfView()
    other.resize(700, 600)
    other.show()
    other.set_document(plain_doc())
    pump(app)
    other.restore_view_state(state)
    pump(app)
    assert other.current_page() == 2
    assert abs(other.zoom() - 1.25) < 1e-3
    assert abs(other.view_state()["ratio"] - state["ratio"]) < 0.02
    other.close()


def test_restore_view_state_ignores_bad_values(view):
    before = view.view_state()
    for bad in (None, "x", {"zoom": "abc"}, {"page": None}, []):
        view.restore_view_state(bad)
    assert view.view_state()["page"] == before["page"]
    view.restore_view_state({"zoom": 1.0, "fit": "bogus", "page": 999, "ratio": 5})  # 範囲の外は寄せる
    assert 0 <= view.current_page() < view.page_count()


# -- 検索 -----------------------------------------------------------------------


def test_search_counts_and_steps(view, app):
    seen = []
    view.searchChanged.connect(lambda cur, total: seen.append((cur, total)))
    assert view.search("alpha") == 5
    assert seen[-1] == (1, 5)
    view.find_next()
    assert seen[-1] == (2, 5)
    pump(app)
    assert view.current_page() in (0, 1)  # 2 番目の一致のページへ移った
    view.find_previous()
    view.find_previous()
    assert seen[-1] == (5, 5)  # 先頭から戻ると最後へ回る
    assert view.hits_on(0)  # 色で示す一致がある
    view.clear_search()
    assert seen[-1] == (0, 0) and view.hit_count() == 0 and view.hits_on(0) == []


def test_search_starts_from_the_current_page(view, app):
    view.go_to_page(3)
    pump(app)
    view.search("beta")
    assert view.hit_index() == 3  # 今のページ以降の最初の一致


def test_search_without_matches_and_blank(view):
    assert view.search("zzzz-not-here") == 0 and view.hit_index() == -1
    assert view.search("   ") == 0
    view.find_next()  # 一致がなくても落ちない


def test_search_is_case_insensitive(view):
    assert view.search("ALPHA") == 5


def test_new_document_resets_search(view):
    view.search("alpha")
    view.set_document(plain_doc(2))
    assert view.hit_count() == 0 and view.search_text() == ""


# -- 文字の選択とコピー -------------------------------------------------------------


def drag_select(view, index, start, end):
    w = view._pages[index]
    a, b = click_point(view, index, *start), click_point(view, index, *end)
    view.press(index, w.to_page(a), a)
    view.drag(index, w.to_page(b), b)
    view.release(index, w.to_page(b), b)


def test_select_words_and_copy(view, app):
    drag_select(view, 0, (52, 96), (140, 96))  # "alpha beta gamma" のあたり
    assert view.has_selection()
    text = view.selected_text()
    assert text.startswith("alpha") and "beta" in text
    assert view.selection_on(0)
    assert view.copy_selection()
    from PySide6.QtGui import QGuiApplication

    assert QGuiApplication.clipboard().text() == text


def test_selection_across_lines_joins_with_newline(view):
    drag_select(view, 0, (52, 96), (120, 126))
    assert "\n" in view.selected_text()


def test_click_without_drag_clears_selection(view):
    drag_select(view, 0, (52, 96), (140, 96))
    assert view.has_selection()
    w = view._pages[0]
    p = click_point(view, 0, 300, 400)  # 何もないところ
    view.press(0, w.to_page(p), p)
    view.release(0, w.to_page(p), p)
    assert not view.has_selection() and view.selected_text() == ""


def test_select_all(view):
    view.select_all()
    text = view.selected_text()
    assert text.count("second") == 5 and "page5" in text


# -- リンク ---------------------------------------------------------------------


def test_links_open_externally_or_jump(app):
    from spectra.ui.pdf_view import PdfView

    doc = plain_doc(3)
    doc[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(50, 200, 150, 220), "uri": "https://example.com/x"})
    doc[0].insert_link(
        {"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(50, 250, 150, 270), "page": 2, "to": pymupdf.Point(0, 100)}
    )
    v = PdfView()
    v.resize(700, 600)
    v.show()
    v.set_document(doc)
    pump(app)
    urls = []
    v.linkRequested.connect(urls.append)
    w = v._pages[0]
    p = click_point(v, 0, 100, 210)
    v.press(0, w.to_page(p), p)
    v.release(0, w.to_page(p), p)
    assert urls == ["https://example.com/x"]  # 開く前に、画面側が確認できるよう通知だけする
    p = click_point(v, 0, 100, 260)
    v.press(0, w.to_page(p), p)
    v.release(0, w.to_page(p), p)
    pump(app)
    assert v.current_page() == 2
    # ドラッグしたあとの release ではリンクを開かない
    urls.clear()
    v.go_to_page(0)
    pump(app)
    a, b = click_point(v, 0, 60, 205), click_point(v, 0, 140, 215)
    v.press(0, w.to_page(a), a)
    v.drag(0, w.to_page(b), b)
    v.release(0, w.to_page(b), b)
    assert urls == []
    v.close()


def test_cursor_over_link_and_text(app, view):
    from PySide6.QtCore import Qt

    view._doc[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(50, 200, 150, 220), "uri": "https://e.com"})
    view._links.clear()
    assert view.cursor_at(0, pymupdf.Point(100, 210)).shape() == Qt.CursorShape.PointingHandCursor
    assert view.cursor_at(0, pymupdf.Point(60, 98)).shape() == Qt.CursorShape.IBeamCursor
    assert view.cursor_at(0, pymupdf.Point(350, 450)).shape() == Qt.CursorShape.ArrowCursor


# -- 目次とページの一覧 ----------------------------------------------------------------


def test_outline_and_nav_pane(app):
    from spectra.ui.pdf_view import NavPane, PdfView

    doc = plain_doc(4)
    doc.set_toc([[1, "Intro", 1], [2, "Background", 2], [1, "Method", 3]])
    v = PdfView()
    v.set_document(doc)
    assert v.outline() == [(1, "Intro", 0), (2, "Background", 1), (1, "Method", 2)]

    pane = NavPane()
    pane.resize(180, 500)
    pane.show()
    pane.set_document(doc, v.outline())
    pump(app, 0.2)
    assert pane.has_outline() and pane.thumbnails.count() == 4
    assert not pane.thumbnails.item(0).icon().isNull()  # 見えているページの縮小画像
    pane.show_outline()
    assert pane.showing_outline()
    got = []
    pane.pageActivated.connect(got.append)
    method = pane.outline_tree.topLevelItem(1)
    pane.outline_tree.itemClicked.emit(method, 0)
    assert got == [2]
    pane.thumbnails.itemClicked.emit(pane.thumbnails.item(3))
    assert got == [2, 3]
    pane.set_current_page(1)
    assert pane.thumbnails.currentRow() == 1

    pane.set_document(plain_doc(2), [])  # しおりがない PDF では一覧だけ
    assert not pane.has_outline() and not pane.showing_outline()
    pane.show_outline()
    assert not pane.showing_outline()
    pane.close()


# -- 原文と訳文 ---------------------------------------------------------------------


def test_preview_pair_searches_the_visible_side(app):
    from spectra.ui.pdf_view import PreviewPair

    pair = PreviewPair()
    pair.resize(1000, 600)
    pair.show()
    pair.original.set_document(plain_doc(3))
    pair.translated.set_document(plain_doc(2, "omega psi chi"))
    pair.set_mode("original")
    pump(app)
    seen = []
    pair.searchChanged.connect(lambda cur, total: seen.append((cur, total)))
    assert pair.search("alpha") == 3 and seen[-1] == (1, 3)
    pair.set_mode("translated")  # 見えているほうで探し直す
    pump(app)
    assert pair.original.hit_count() == 0
    assert seen[-1] == (0, 0)  # 訳文には "alpha" がない
    assert pair.search("omega") == 2
    pair.find_next()
    assert seen[-1] == (2, 2)
    pair.clear_search()
    assert pair.translated.hit_count() == 0
    pair.close()


def test_make_paper_search_roundtrip(app):
    """合成論文（2 段組・数式）でも検索と選択が働く。"""
    from spectra.ui.pdf_view import PdfView

    v = PdfView()
    v.resize(800, 700)
    v.show()
    v.set_document(make_paper())
    pump(app)
    assert v.search("neural") >= 1
    v.select_all()
    assert len(v.selected_text()) > 100
    v.close()


# -- 見ていた場所の記憶（表示前・幅に合わせているとき） ------------------------------------------------


def test_restore_before_the_view_is_shown(app):
    from spectra.ui.pdf_view import PdfView

    v = PdfView()  # まだ表示していない（読書画面を開く前）
    v.set_document(plain_doc())
    v.restore_view_state({"zoom": 1.5, "fit": "none", "page": 3, "ratio": 0.25})
    v.resize(700, 600)
    v.show()
    pump(app, 0.3)
    assert v.current_page() == 3 and abs(v.zoom() - 1.5) < 1e-3
    assert v.view_state()["ratio"] == pytest.approx(0.25, abs=0.02)
    v.close()


def test_restore_keeps_the_position_when_fitting_to_width(view, app):
    view.restore_view_state({"zoom": 1.0, "fit": "width", "page": 2, "ratio": 0.5})
    pump(app, 0.3)
    assert view.fit_mode() == "width" and view.current_page() in (2, 3)
    state = view.view_state()
    assert state["page"] == 2 and state["ratio"] == pytest.approx(0.5, abs=0.05)
    # 一度戻したあと、ウィンドウの大きさを変えても、位置を勝手に戻し直さない
    view.verticalScrollBar().setValue(0)
    view.resize(650, 600)
    pump(app, 0.3)
    assert view.current_page() == 0


def test_switching_view_mode_keeps_an_explicit_zoom(app):
    from spectra.ui.pdf_view import PreviewPair

    pair = PreviewPair()
    pair.resize(1000, 600)
    pair.show()
    pair.original.set_document(plain_doc(3))
    pair.translated.set_document(plain_doc(3))
    pump(app)
    pair.set_zoom(1.3)
    pair.set_mode("both")
    pump(app, 0.2)
    assert abs(pair.original.zoom() - 1.3) < 1e-3  # 倍率を決めていれば、表示を切り替えても変えない
    pair.original.fit_width()
    pair.set_mode("original")
    pump(app, 0.2)
    assert pair.original.fit_mode() == "width"
    pair.close()


# -- 検索の帯 ----------------------------------------------------------------------


def test_find_bar_flow(app):
    from PySide6.QtGui import (
        QGuiApplication,  # noqa: F401  (Shift の状態は実機では押している間だけなので使わない)
    )

    from spectra.ui.pdf_view import FindBar

    bar = FindBar()
    bar.show()
    queries, steps = [], []
    bar.queryChanged.connect(queries.append)
    bar.nextRequested.connect(lambda: steps.append("next"))
    bar.previousRequested.connect(lambda: steps.append("prev"))
    closed = []
    bar.closed.connect(lambda: closed.append(1))

    bar.edit.setText("alpha")
    pump(app, 0.4)  # 打つのをやめて少しすると探す
    assert queries == ["alpha"]
    bar.edit.returnPressed.emit()  # 同じ語で Enter なら次へ
    assert steps == ["next"] and queries == ["alpha"]
    bar.edit.setText("beta")
    bar.edit.returnPressed.emit()  # 打った直後の Enter は、まず探す
    assert queries == ["alpha", "beta"]
    bar.prev_btn.click()
    assert steps[-1] == "prev"

    bar.set_count(2, 5)
    assert bar.count.text() == "2 / 5 件"
    bar.set_count(0, 0)
    assert bar.count.text() == "見つかりません"
    bar.edit.setText("")
    bar.set_count(0, 0)
    assert bar.count.text() == ""

    bar.close_bar()
    assert closed == [1] and bar.isHidden()
    bar.open()
    assert not bar.isHidden()
    bar.close()


# -- 右クリックのメニュー ----------------------------------------------------------------


def _labels(actions):
    return [a.text() if a is not None else "-" for a in actions]


def test_context_menu_items_and_enabled_state(view):
    pt = pymupdf.Point(100, 300)
    actions = view.context_actions(0, pt)
    assert _labels(actions) == ["コピー", "すべてを選択", "-", "PDF 内を検索…"]
    copy, select_all, _sep, find = actions
    assert not copy.isEnabled()  # 何も選んでいなければ、コピーは押せない
    assert select_all.isEnabled() and find.isEnabled()
    drag_select(view, 0, (52, 96), (140, 96))
    assert view.context_actions(0, pt)[0].isEnabled()


def test_context_menu_actions_work(view, app):
    from PySide6.QtGui import QGuiApplication

    asked = []
    view.findRequested.connect(asked.append)
    menu = view.build_context_menu(0, pymupdf.Point(100, 300))
    by_text = {a.text(): a for a in menu.actions() if not a.isSeparator()}
    assert [a.isSeparator() for a in menu.actions()] == [False, False, True, False]

    by_text["PDF 内を検索…"].trigger()  # 何も選んでいなければ、空で開く
    assert asked == [""]
    by_text["すべてを選択"].trigger()
    assert view.has_selection()
    drag_select(view, 0, (52, 96), (80, 96))  # 1 語を選んでから開くと、その語が検索欄に入る
    selected = view.selected_text()
    menu = view.build_context_menu(0, pymupdf.Point(100, 300))
    {a.text(): a for a in menu.actions() if not a.isSeparator()}["PDF 内を検索…"].trigger()
    assert asked[-1] == selected and len(selected.split()) <= 3
    {a.text(): a for a in menu.actions() if not a.isSeparator()}["コピー"].trigger()
    assert QGuiApplication.clipboard().text() == selected


def test_long_selection_is_not_put_in_the_find_box(view):
    view.select_all()
    asked = []
    view.findRequested.connect(asked.append)
    view.context_actions(0, pymupdf.Point(0, 0))[3].trigger()
    assert asked == [""]  # 100 文字を超える選択は、検索欄に入れない


def test_context_menu_can_be_extended_with_providers(view):
    from PySide6.QtGui import QAction

    seen = []

    def marker(index, pt):
        seen.append((index, round(pt.x), round(pt.y)))
        a = QAction("マーカーを付ける", view)
        return [a]

    def nothing(index, pt):
        return []  # 何も足さないときは、区切り線も入れない

    view.add_context_provider(marker)
    view.add_context_provider(nothing)
    view.add_context_provider(lambda i, p: [QAction("コメントを付ける", view), None, QAction("ほか", view)])
    labels = _labels(view.context_actions(2, pymupdf.Point(12, 34)))
    assert labels == ["コピー", "すべてを選択", "-", "PDF 内を検索…", "-", "マーカーを付ける", "-", "コメントを付ける", "-", "ほか"]
    assert seen == [(2, 12, 34)]  # 右クリックしたページと位置が渡る


def test_context_menu_is_shown_from_the_page_widget(view, monkeypatch):
    """ページを右クリックすると、そのページと位置でメニューを作って出す（本物のメニューは開かない）。"""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QContextMenuEvent

    shown = []

    class FakeMenu:
        def exec(self, pos):
            shown.append(("exec", pos))

    def build(index, pt):
        shown.append(("build", index, round(pt.x), round(pt.y)))
        return FakeMenu()

    monkeypatch.setattr(view, "build_context_menu", build)
    w = view._pages[1]
    r = w._page_rect()
    at = QPoint(int(r.x() + 100 * view.zoom()), int(r.y() + 50 * view.zoom()))
    w.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, at, at))
    assert shown[0] == ("build", 1, 100, 50)  # 右クリックしたページと、ページの中の位置
    assert shown[1][0] == "exec"


def test_preview_pair_forwards_find_requests_and_providers(app):
    from PySide6.QtGui import QAction

    from spectra.ui.pdf_view import PreviewPair

    pair = PreviewPair()
    pair.resize(1000, 600)
    pair.show()
    pair.original.set_document(plain_doc(2))
    pair.translated.set_document(plain_doc(2))
    pump(app)
    got = []
    pair.findRequested.connect(got.append)
    pair.add_context_provider(lambda i, p: [QAction("マーカーを付ける", pair)])
    for view in (pair.original, pair.translated):  # 原文・訳文のどちらでも
        assert "マーカーを付ける" in _labels(view.context_actions(0, pymupdf.Point(1, 1)))
        view.context_actions(0, pymupdf.Point(1, 1))[3].trigger()
    assert got == ["", ""]
    pair.close()
