"""画面での注釈: 付ける・直す・消して戻す・付箋・右クリック・移動・PDF 出力・同期で変わったとき（通信なし）。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

import pymupdf  # noqa: E402
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from test_gui import pump, window  # noqa: E402, F401  (同じ画面の部品を使う)


@pytest.fixture()
def reader(window, paper_path):  # noqa: F811
    app, w = window
    assert w.open_file(paper_path)
    pump(app, 0.3)
    return app, w


def select_words(view, page=0, first=0, last=3):
    """ページの first〜last 番目の単語をドラッグで選ぶ。"""
    words = view._page_words(page)
    w = view._pages[page]

    def at(word):
        c = pymupdf.Rect(word[:4])
        r = w._page_rect()
        z = view.zoom()
        return QPointF(r.x() + (c.x0 + c.x1) / 2 * z, r.y() + (c.y0 + c.y1) / 2 * z)

    a, b = at(words[first]), at(words[last])
    view.press(page, w.to_page(a), a)
    view.drag(page, w.to_page(b), b)
    view.release(page, w.to_page(b), b)


def key(view, k):
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier))


def test_selecting_text_and_picking_a_color_saves_a_highlight(reader):
    app, w = reader
    view = w.preview.original
    select_words(view)
    assert not view.bar.isHidden()
    view.bar.color_buttons["green"].click()
    saved = w.library.annotations(w.paper.id)
    assert len(saved) == 1 and saved[0].kind == "highlight" and saved[0].color == "green"
    assert saved[0].page == 0 and saved[0].quote and saved[0].quads
    assert w.annotations == saved and view.annotations_on(0) == saved  # 原文に描く
    assert w.preview.translated.annotations_on(0) == saved  # 訳文の側にも渡す（余白の帯に使う）
    assert w._tool_color == "green"  # 選んだ色が、以後の既定の色になる
    assert w.side_panel.annotation_list is not None


def test_comment_button_saves_and_opens_the_popover(reader):
    app, w = reader
    view = w.preview.original
    select_words(view)
    view.bar.comment_btn.click()
    saved = w.library.annotations(w.paper.id)
    assert len(saved) == 1 and saved[0].kind == "highlight"
    assert w._popover is not None and w._popover.isVisible() and w._popover.annotation.id == saved[0].id
    w._popover.comment.setPlainText("あとで確かめる")
    w._popover.flush()
    assert w.library.annotations(w.paper.id)[0].comment == "あとで確かめる"
    assert w.annotations[0].comment == "あとで確かめる"  # 画面の印も更新される


def test_selection_across_pages_makes_one_annotation_per_page(reader):
    app, w = reader
    view = w.preview.original
    view.select_all()
    w._annotate_selection("underline")
    saved = w.library.annotations(w.paper.id)
    assert [a.page for a in saved] == list(range(view.page_count())) and {a.kind for a in saved} == {"underline"}


def test_shortcuts_need_a_selection(reader):
    app, w = reader
    view = w.preview.original
    w._annotate_selection("highlight")
    assert w.library.annotations(w.paper.id) == []
    assert "選んでから" in w.status.text() or "選んでから" in w._status_message
    select_words(view)
    w._annotation_actions[0].trigger()  # 選んだ文字にマーカーを付ける
    select_words(view, first=4, last=6)
    w._annotation_actions[1].trigger()  # コメントを付ける: 吹き出しが開く
    kinds = [a.kind for a in w.library.annotations(w.paper.id)]
    assert kinds == ["highlight", "highlight"] and w._popover is not None
    assert not view.has_selection()


def test_click_edit_delete_and_undo(reader):
    app, w = reader
    view = w.preview.original
    select_words(view)
    w._annotate_selection("highlight")
    a = w.annotations[0]
    pos = view._pages[0].annotation_rects(a)[0].center()
    from PySide6.QtCore import QPoint

    w._on_annotation_clicked(a.id, QPoint(10, 10))
    assert w._popover is not None and w._popover.isVisible()
    w._popover._set_color("purple")
    assert w.library.annotations(w.paper.id)[0].color == "purple" and w.annotations[0].color == "purple"
    w._popover.close()

    view.select_annotation(a.id)  # 選んで Delete
    key(view, Qt.Key.Key_Delete)
    assert w.library.annotations(w.paper.id) == [] and w.annotations == [] and view.annotations_on(0) == []
    assert not w.undo_wrap.isHidden() and not w.undo_bar.isHidden()
    w.undo_bar.undo_btn.click()  # 元に戻す
    assert [x.id for x in w.library.annotations(w.paper.id)] == [a.id] and [x.id for x in w.annotations] == [a.id]
    assert w.undo_wrap.isHidden()
    assert pos is not None


def test_popover_delete_shows_the_undo_bar(reader):
    app, w = reader
    select_words(w.preview.original)
    w._annotate_selection("highlight")
    w._edit_annotation(w.annotations[0])
    w._popover.delete()
    assert w.library.annotations(w.paper.id) == [] and not w.undo_bar.isHidden()


def test_sticky_note_tool_places_a_note_and_opens_it(reader):
    app, w = reader
    view = w.preview.original
    w._set_tool_kind("note")
    w.marker_btn.setChecked(True)
    assert view.tool() == "note"
    page = view._pages[0]
    r = page._page_rect()
    p = QPointF(r.x() + 300 * view.zoom(), r.y() + 400 * view.zoom())
    view.press(0, page.to_page(p), p)
    view.release(0, page.to_page(p), p)
    notes = w.library.annotations(w.paper.id)
    assert len(notes) == 1 and notes[0].kind == "note" and notes[0].page == 0
    assert notes[0].quads[0][0] == pytest.approx(300, abs=1) and notes[0].quads[0][1] == pytest.approx(400, abs=1)
    assert w._popover is not None and w._popover.annotation.id == notes[0].id  # すぐコメントを書ける
    key(view, Qt.Key.Key_Escape)  # Esc で道具を止める
    assert not w.marker_btn.isChecked() and view.tool() == ""


def test_marker_tool_marks_on_selecting_and_remembers_kind_and_color(reader):
    app, w = reader
    view = w.preview.original
    w._set_tool_color("blue")
    w._set_tool_kind("underline")
    w.marker_btn.setChecked(True)
    select_words(view)
    a = w.library.annotations(w.paper.id)
    assert len(a) == 1 and a[0].kind == "underline" and a[0].color == "blue"
    w.marker_btn.setChecked(False)
    select_words(view, first=5, last=7)
    assert len(w.library.annotations(w.paper.id)) == 1  # 道具を止めたら、選んでも付かない
    w.show_library()
    assert not w.marker_btn.isChecked() and w.annotations == []  # 論文を閉じたら、道具も印も下げる


def test_context_menu_items_depend_on_what_is_under_the_pointer(reader):
    app, w = reader
    view = w.preview.original
    pt = pymupdf.Point(300, 400)

    def labels(index=0, point=pt):
        return [a.text() for a in w._annotation_menu_items(index, point)]

    assert labels() == ["ここに付箋を付ける"]
    select_words(view)
    assert labels() == ["マーカーを付ける", "下線を付ける", "コメントを付ける"]
    w._annotate_selection("highlight")
    a = w.annotations[0]
    mid = pymupdf.Point((a.quads[0][0] + a.quads[0][2]) / 2, (a.quads[0][1] + a.quads[0][3]) / 2)
    assert labels(0, mid) == ["注釈を編集…", "注釈を削除"]
    w._annotation_menu_items(0, mid)[1].trigger()
    assert w.library.annotations(w.paper.id) == []
    w._annotation_menu_items(0, pt)[0].trigger()  # ここに付箋を付ける
    assert [x.kind for x in w.library.annotations(w.paper.id)] == ["note"]
    # 訳文の表示のときは出さない
    w.view_mode.set_current("original")
    assert w.preview.active_view() is view


def test_list_click_goes_to_the_annotation_and_flashes(reader):
    app, w = reader
    view = w.preview.original
    view.select_all()
    w._annotate_selection("highlight")
    last = w.library.annotations(w.paper.id)[-1]
    view.go_to_page(0)
    pump(app, 0.1)
    w.side_panel.annotationActivated.emit(last.id, last.page)
    pump(app, 0.2)
    assert view.current_page() == last.page and view.flashing_id() == last.id


def test_library_list_click_opens_the_paper_at_the_annotation(reader):
    app, w = reader
    select_words(w.preview.original)
    w._annotate_selection("highlight")
    a = w.annotations[0]
    w.show_library()
    w.library_view.reveal(a.paper_id)
    pump(app, 0.2)
    w.library_view.side_panel.annotationActivated.emit(a.id, a.page)
    pump(app, 0.4)
    assert w.screen_mode == "reader" and w.paper.id == a.paper_id
    assert w.preview.original.flashing_id() == a.id


def test_changes_from_the_library_and_sync_are_redrawn(reader):
    app, w = reader
    view = w.preview.original
    a = w.library.add_annotation(w.paper.id, "highlight", 0, [(50.0, 90.0, 120.0, 105.0)], quote="x", color="pink")
    pump(app, 0.1)
    assert [x.id for x in w.annotations] == [a.id] and view.annotations_on(0)  # 合図（LibraryEvent）で読み直す
    w.library.update_annotation(a.id, comment="同期で届いたコメント")
    pump(app, 0.1)
    assert w.annotations[0].comment == "同期で届いたコメント"
    from spectra.library.sync import RemoteChange

    w.library.delete_annotation(a.id)
    w._on_remote_changes([RemoteChange("annotations", w.paper.sha256, w.paper.id)])
    assert w.annotations == [] and view.annotations_on(0) == []


def test_other_papers_annotation_events_do_not_redraw_this_paper(reader):
    app, w = reader
    w._on_annotations_changed(w.paper.id + 99)
    assert w.annotations == []


def test_translated_selection_asks_for_the_original(reader):
    app, w = reader
    w.view_mode.set_current("original")
    w._on_original_requested(2)
    pump(app, 0.2)
    assert w.view_mode.current() == "original" and w.preview.original.current_page() == 2
    assert "原文に付けます" in w.status.text() or "原文に付けます" in w._status_message


def test_marker_menu_and_actions(reader):
    app, w = reader
    assert w.marker_btn.isVisible() and not w.marker_btn.isChecked()
    assert w.marker_menu_btn.isVisible()
    assert [a.text() for a in w.marker_menu_btn.menu().actions() if a.isCheckable()][:3] == ["マーカー", "下線", "付箋（クリックした位置に置く）"]
    w._kind_actions["note"].trigger()
    assert w._tool_kind == "note"
    w._color_actions["pink"].trigger()
    assert w._tool_color == "pink" and w.preview.original.current_color() == "pink"
    w._annotation_actions[3].trigger()  # メニューの「マーカーの道具」
    assert w.marker_btn.isChecked() and w.preview.original.tool() == "note"
    w.show_library()
    assert not w.marker_btn.isVisibleTo(w) and not w.marker_menu_btn.isVisibleTo(w)


# -- PDF 出力 ---------------------------------------------------------------------------


def _translate(app, w):
    w.start_translation()
    pump(app, 60, until=lambda: not w._busy())
    assert w.result is not None


def test_export_includes_annotations_only_when_asked(reader):
    from spectra.core.options import OutputMode

    app, w = reader
    _translate(app, w)
    select_words(w.preview.original)
    w._annotate_selection("highlight")
    w._on_note_placed(0, 300.0, 400.0)
    w._popover.close()
    sha_before = w.paper.sha256

    def annots(data):
        doc = pymupdf.open(stream=data, filetype="pdf")
        return [(a.type[1], pno) for pno, page in enumerate(doc) for a in page.annots()]

    plain = w._output_bytes(OutputMode.INTERLEAVE)
    assert annots(plain) == []  # 既定では書かない
    with_notes = annots(w._output_bytes(OutputMode.INTERLEAVE, annotations=True))
    kinds = {k for k, _p in with_notes}
    assert "Highlight" in kinds and "Text" in kinds
    side = annots(w._output_bytes(OutputMode.SIDE_BY_SIDE, annotations=True))
    assert {k for k, _p in side} >= {"Highlight", "Text"}
    translated_only = annots(w._output_bytes(OutputMode.TRANSLATED, annotations=True))
    assert translated_only  # 翻訳のみは、訳文の余白の帯と付箋
    assert w._output_bytes(OutputMode.TRANSLATED, annotations=False) == w.translated_pdf
    # ライブラリの PDF は変えない
    assert w.library.get(w.paper.id).sha256 == sha_before
    assert annots(w.paper.pdf_path.read_bytes()) == []


def test_export_dialog_gets_the_annotation_count(reader, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from spectra.ui import main_window as mw

    app, w = reader
    _translate(app, w)
    select_words(w.preview.original)
    w._annotate_selection("highlight")
    seen = {}

    class FakeDialog:
        def __init__(self, current, pages, parent=None, annotation_count=None):
            seen["count"] = annotation_count

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw, "ExportDialog", FakeDialog)
    w.save_dialog()
    assert seen["count"] == 1
