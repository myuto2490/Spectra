"""注釈の画面部品：吹き出し・削除の取り消し・一覧と絞り込み・4 つ目のタブ・PDF 出力のスイッチ（データは偽物）。"""

import os
import time
from dataclasses import dataclass, replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


@dataclass(frozen=True)
class Ann:
    id: str
    page: int = 0
    kind: str = "highlight"
    color: str = "yellow"
    quote: str = "quoted text"
    comment: str = ""
    quads: tuple = ((10.0, 100.0, 80.0, 112.0),)
    created_at: float = 1.0
    updated_at: float = 1.0
    comment_merged: bool = False


class FakeLibrary:
    """Library の注釈の API だけを持つ偽物。"""

    def __init__(self, items=()):
        self.items = {a.id: a for a in items}
        self.fail = False

    def annotations(self, paper_id):
        return list(self.items.values())

    def update_annotation(self, annotation_id, *, kind=None, color=None, comment=None):
        if self.fail:
            raise OSError("disk full")
        a = self.items[annotation_id]
        fields = {k: v for k, v in (("kind", kind), ("color", color), ("comment", comment)) if v is not None}
        if "comment" in fields:
            fields["comment_merged"] = False
        self.items[annotation_id] = a = replace(a, **fields)
        return a

    def delete_annotation(self, annotation_id):
        return self.items.pop(annotation_id)

    def restore_annotation(self, annotation):
        self.items[annotation.id] = annotation
        return annotation


@pytest.fixture()
def app():
    from spectra.ui.app import create_app

    return create_app([])


def pump(app, until, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end and not until():
        app.processEvents()
        time.sleep(0.01)
    assert until()


# ---- 吹き出し ---------------------------------------------------------------


def test_popover_shows_the_annotation(app):
    from spectra.ui.annotation_popover import AnnotationPopover

    p = AnnotationPopover(Ann("a", color="green", quote="<b>text</b>", comment="memo", comment_merged=True))
    assert p.swatches["green"].isChecked() and p.kind_buttons["highlight"].isChecked()
    assert p.quote.text() == "<b>text</b>"  # プレーンテキストで描く（HTML として解釈しない）
    assert p.comment.toPlainText() == "memo" and not p.merged.isHidden()
    note = AnnotationPopover(Ann("n", kind="note", quote=""))
    assert note.kind_row.isHidden() and note.quote.isHidden()  # 付箋は種類を切り替えない


def test_popover_changes_color_and_kind_through_the_library(app):
    from spectra.ui.annotation_popover import AnnotationPopover

    lib = FakeLibrary([Ann("a")])
    p = AnnotationPopover(lib.items["a"], lib)
    seen = []
    p.changed.connect(seen.append)
    p.swatches["pink"].click()
    p.kind_buttons["underline"].click()
    assert lib.items["a"].color == "pink" and lib.items["a"].kind == "underline"
    assert [(a.color, a.kind) for a in seen] == [("pink", "highlight"), ("pink", "underline")]
    p.swatches["pink"].click()  # 同じ色を押しても何も書かない
    assert len(seen) == 2


def test_popover_autosaves_comment_after_a_pause_and_on_close(app):
    from spectra.ui.annotation_popover import AnnotationPopover

    lib = FakeLibrary([Ann("a", comment="古い")])
    p = AnnotationPopover(lib.items["a"], lib)
    p.show()
    p.comment.setPlainText("新しい")
    assert lib.items["a"].comment == "古い"  # 入力の直後は書かない
    p._timer.setInterval(10)
    p._timer.start()
    pump(app, lambda: lib.items["a"].comment == "新しい")
    p.comment.setPlainText("閉じる前の入力")
    p.hide()  # 閉じるときは待たずに保存する
    assert lib.items["a"].comment == "閉じる前の入力"


def test_popover_keeps_input_when_saving_fails_and_limits_length(app):
    from spectra.ui.annotation_popover import MAX_COMMENT, AnnotationPopover

    lib = FakeLibrary([Ann("a")])
    p = AnnotationPopover(lib.items["a"], lib)
    p.show()
    lib.fail = True
    p.swatches["blue"].click()
    assert "保存できませんでした" in p.status.text() and lib.items["a"].color == "yellow"
    lib.fail = False
    p.comment.setPlainText("あ" * (MAX_COMMENT + 5))
    assert len(p.comment.toPlainText()) == MAX_COMMENT and "超えた分" in p.status.text()
    p.flush()
    assert len(lib.items["a"].comment) == MAX_COMMENT


def test_popover_delete_and_undo_bar(app):
    from spectra.ui.annotation_popover import AnnotationPopover, UndoBar

    lib = FakeLibrary([Ann("a", comment="残したい")])
    p = AnnotationPopover(lib.items["a"], lib)
    p.show()
    removed = []
    p.deleted.connect(removed.append)
    p.comment.setPlainText("消す直前の入力")  # 消す注釈には保存しない
    p.delete_btn.click()
    assert "a" not in lib.items and [a.id for a in removed] == ["a"] and not p.isVisible()

    bar = UndoBar(lib)
    undone, expired = [], []
    bar.undone.connect(undone.append)
    bar.expired.connect(lambda: expired.append(1))
    bar.show_deleted(removed[0])
    assert bar.isVisible() and bar._timer.interval() == 5000
    bar.undo_btn.click()
    assert lib.items["a"].comment == "残したい" and [a.id for a in undone] == ["a"] and not bar.isVisible()

    bar.show_deleted(removed[0])  # 5 秒たてば消え、元には戻らない
    bar._timer.setInterval(10)
    bar._timer.start()
    pump(app, lambda: expired)
    assert not bar.isVisible()


def test_popover_without_a_library_only_signals(app):
    from spectra.ui.annotation_popover import AnnotationPopover

    p = AnnotationPopover(Ann("a"))
    seen, removed = [], []
    p.changed.connect(seen.append)
    p.deleted.connect(removed.append)
    p.swatches["purple"].click()
    assert seen[-1].color == "purple"
    p.delete()
    assert removed[0].id == "a"


# ---- 一覧 -------------------------------------------------------------------


def _items():
    return [
        Ann("c", page=2, color="blue", quote="third", comment="コメントあり"),
        Ann("a", page=0, color="yellow", quote="first"),
        Ann("b2", page=1, color="green", quote="second lower", quads=((10.0, 400.0, 50.0, 410.0),)),
        Ann("b1", page=1, color="green", quote="second upper", comment="上", quads=((10.0, 50.0, 50.0, 60.0),)),
    ]


def test_list_orders_filters_and_counts(app):
    from spectra.ui.annotation_list import AnnotationList

    lst = AnnotationList()
    assert not lst.empty.isHidden() and "まだ注釈がありません" in lst.empty.text() and lst.filters.isHidden()

    lst.set_annotations(_items())
    assert [r.annotation.id for r in lst.rows] == ["a", "b1", "b2", "c"]  # ページ順 → 上から
    assert lst.empty.isHidden() and not lst.filters.isHidden() and lst.count.text() == "4 件"

    lst.swatches["green"].click()
    assert [a.id for a in lst.visible_annotations()] == ["b1", "b2"] and lst.count.text() == "2 件（全 4 件）"
    lst.swatches["blue"].click()  # 複数の色は「どれか」
    assert [a.id for a in lst.visible_annotations()] == ["b1", "b2", "c"]
    lst.only_comments.click()
    assert [a.id for a in lst.visible_annotations()] == ["b1", "c"]
    lst.swatches["green"].click()
    lst.swatches["blue"].click()
    assert [a.id for a in lst.visible_annotations()] == ["b1", "c"]  # 色の絞り込みを外してもコメントの条件は残る
    lst.swatches["yellow"].click()
    assert lst.visible_annotations() == [] and "条件に合う注釈はありません" in lst.empty.text()
    assert len(lst.annotations()) == 4


def test_list_click_activates_and_highlights(app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from spectra.ui.annotation_list import AnnotationList

    lst = AnnotationList()
    lst.resize(320, 600)
    lst.show()
    lst.set_annotations(_items())
    got = []
    lst.annotationActivated.connect(lambda i, p: got.append((i, p)))
    QTest.mouseClick(lst.rows[2], Qt.MouseButton.LeftButton)
    assert got == [("b2", 1)] and lst.rows[2].property("selected") is True and lst.rows[0].property("selected") is not True
    QTest.keyClick(lst.rows[0], Qt.Key.Key_Return)  # キーボードでも移動できる
    assert got[-1] == ("a", 0)
    # 一覧を作り直しても、選んでいた注釈の強調は残る
    lst.set_annotations(_items())
    assert lst.rows[0].property("selected") is True


def test_list_rows_are_plain_text_and_clipped(app):
    from spectra.ui.annotation_list import AnnotationList

    lst = AnnotationList()
    lst.resize(320, 600)
    lst.show()
    long = "line " * 400
    lst.set_annotations([Ann("a", quote="<i>x</i>" + long, comment=long, comment_merged=True)])
    row = lst.rows[0]
    assert row.quote.text().startswith("<i>x</i>")  # プレーンテキスト
    fm_h = row.quote.fontMetrics().lineSpacing()
    assert row.quote.heightForWidth(250) <= fm_h * 2 + 2 and row.comment.heightForWidth(250) <= fm_h * 3 + 2  # 2 行・3 行まで
    assert any("連結" in c.text() for c in row.findChildren(type(row.page)))


def test_color_helper_follows_the_theme(app):
    from spectra.ui.annotation_popover import ANNOTATION_COLORS, annotation_rgb

    for c in ANNOTATION_COLORS:
        assert annotation_rgb(c, dark=False) != annotation_rgb(c, dark=True)
    assert annotation_rgb("nope", dark=False) == annotation_rgb("yellow", dark=False)


# ---- 右のパネルの 4 つ目のタブ ---------------------------------------------------


@pytest.fixture()
def lib_and_paper(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECTRA_HOME", str(tmp_path / "home"))
    from pdf_factory import make_paper

    from spectra.library import Library

    path = tmp_path / "p.pdf"
    make_paper(str(path))
    lib = Library(tmp_path / "lib")
    paper = lib.add(path)[0]
    yield lib, lib.get(paper.id)
    lib.close()


class _LibWithAnnotations:
    """本物の Library に、注釈の API（偽物）を足す。"""

    def __init__(self, lib, items):
        self._lib, self.fake = lib, FakeLibrary(items)

    def __getattr__(self, name):
        return getattr(self._lib, name)

    def annotations(self, paper_id):
        return self.fake.annotations(paper_id)


def test_side_panel_has_four_tabs_and_lists_annotations(app, lib_and_paper):
    from spectra.config.settings import Settings
    from spectra.ui.side_panel import SidePanel

    lib, paper = lib_and_paper
    wrapped = _LibWithAnnotations(lib, _items())
    settings = Settings()
    panel = SidePanel(wrapped, settings, "reader")
    panel.show()
    assert len(panel.detail.tab_buttons) == 4 and [b.text() for b in panel.detail.tab_buttons][-1] == "注釈"
    panel.set_paper(paper)
    assert len(panel.annotation_list.rows) == 4
    got = []
    panel.annotationActivated.connect(lambda i, p: got.append((i, p)))
    panel.set_current_tab("annotations")
    assert panel.current_tab() == "annotations" and settings.side_panel_tab == "annotations"
    assert panel.detail.pages.currentWidget() is panel.annotation_list
    panel.annotation_list.rows[1].clicked.emit("b1", 1)
    assert got == [("b1", 1)]

    # 注釈が変わったら、読み直す
    wrapped.fake.items["z"] = Ann("z", page=5)
    assert len(panel.annotation_list.rows) == 4
    panel.refresh_annotations()
    assert len(panel.annotation_list.rows) == 5
    panel.set_paper(None)  # 何も選んでいなければ空
    assert panel.annotation_list.rows == []


def test_side_panel_without_annotation_api_shows_the_guide(app, lib_and_paper):
    from spectra.config.settings import Settings
    from spectra.ui.side_panel import SidePanel

    lib, paper = lib_and_paper
    panel = SidePanel(lib, Settings(), "library")
    panel.show()
    panel.set_paper(paper)
    panel.set_current_tab("annotations")
    assert panel.annotation_list.rows == [] and not panel.annotation_list.empty.isHidden()


# ---- PDF 出力のダイアログ -------------------------------------------------------


def test_export_dialog_annotation_switch(app):
    from spectra.core.options import OutputMode
    from spectra.ui.export_dialog import ExportDialog

    d = ExportDialog(OutputMode.TRANSLATED, 10, annotation_count=3)
    d.show()
    assert not d.annotations_row.isHidden() and d.annotations_switch.isChecked()  # 既定でオン
    assert d.include_annotations() and "3 件" in d.annotations_hint.text()
    assert "左の余白" in d.annotations_hint.text()  # 翻訳のみでは余白の帯になる
    d.cards[OutputMode.SIDE_BY_SIDE].click()
    assert "左の余白" not in d.annotations_hint.text() and d.selected() == OutputMode.SIDE_BY_SIDE
    d.annotations_switch.click()
    assert not d.include_annotations()

    none = ExportDialog(OutputMode.TRANSLATED, 10, annotation_count=0)  # 注釈がなければ出さない
    none.show()
    assert none.annotations_row.isHidden() and not none.include_annotations()
    unknown = ExportDialog(OutputMode.TRANSLATED, 10)  # 数がわからなければ出す（既定でオン）
    unknown.show()
    assert unknown.include_annotations()


# ---- 実画面で見つかった表示の不具合（欠け・重なり・古い欄の残り） ---------------------------


def test_popover_kind_buttons_show_their_whole_text(app):
    from spectra.ui.annotation_popover import AnnotationPopover

    p = AnnotationPopover(Ann("a"))
    p.show()
    app.processEvents()
    for b in p.kind_buttons.values():
        assert b.width() >= b.sizeHint().width()  # 幅が足りず、文字が「ーカ」のように欠けない
    # 色の丸・種類・削除ボタンは、同じ行に詰めずに、はみ出さずに収まる
    for w in (*p.swatches.values(), p.delete_btn, *p.kind_buttons.values()):
        assert p.rect().contains(w.mapTo(p, w.rect().topLeft())) and p.rect().contains(w.mapTo(p, w.rect().bottomRight()))
    assert p.kind_row.geometry().top() > p.swatches["yellow"].geometry().bottom()


def test_clamped_label_cuts_by_whole_lines(app):
    from spectra.ui.annotation_popover import ClampedLabel

    lb = ClampedLabel("word " * 200, 2)
    lb.resize(200, 100)
    lines = lb._lines(200)
    assert len(lines) == 2 and lines[-1].endswith("…")  # 3 行目以降は、最後の行を省略して表す
    fm_h = lb.fontMetrics().lineSpacing()
    assert lb.heightForWidth(200) == fm_h * 2 + 2  # 行の途中で切れる高さにならない
    short = ClampedLabel("short", 3)
    assert short._lines(200) == ["short"] and short.heightForWidth(200) == fm_h + 2
    assert ClampedLabel("a\nb", 3)._lines(200) == ["a", "b"]  # 改行は行として扱う
    assert not lb.grab().isNull()


def test_list_rebuild_leaves_no_stale_rows(app):
    from spectra.ui.annotation_list import AnnotationList, AnnotationRow

    lst = AnnotationList()
    lst.resize(320, 600)
    lst.show()
    for _ in range(3):
        lst.set_annotations(_items())
    # 古い行は、削除が処理されるのを待たずに、子から外れている（描かれ続けて重ならない）
    assert len(lst.findChildren(AnnotationRow)) == 4 == len(lst.rows)
    lst.swatches["green"].click()
    assert len(lst.findChildren(AnnotationRow)) == 2
    app.processEvents()
    assert sum(1 for r in lst.findChildren(AnnotationRow) if r.isVisible()) == 2
    lst.set_annotations([])
    assert lst.findChildren(AnnotationRow) == []


def test_list_rows_are_not_clipped(app):
    from spectra.ui.annotation_list import AnnotationList

    lst = AnnotationList()
    lst.resize(324, 700)
    lst.show()
    lst.set_annotations(
        [
            Ann("a", quote="short quote", comment="コメント"),
            Ann("b", page=1, kind="underline", quote="word " * 60, comment="comment " * 60),
        ]
    )
    app.processEvents()
    for r in lst.rows:
        need = r.layout().totalHeightForWidth(r.width()) if r.layout().hasHeightForWidth() else r.layout().sizeHint().height()
        assert r.height() >= need - 1, "行の高さが中身より小さく、文字が欠ける"
        assert r.geometry().bottom() <= lst.area.widget().height()
    # 行どうしが重ならない
    geos = [r.geometry() for r in lst.rows]
    assert all(a.bottom() < b.top() for a, b in zip(geos, geos[1:]))


def test_reader_translation_widget_does_not_overlay_other_tabs(app, lib_and_paper):
    """読書画面で翻訳の設定のパネルを差し込んでも、「注釈」など別のタブの上には重ならない。"""
    from PySide6.QtWidgets import QLabel

    from spectra.config.settings import Settings
    from spectra.ui.side_panel import SidePanel

    lib, paper = lib_and_paper
    panel = SidePanel(_LibWithAnnotations(lib, _items()), Settings(), "reader")
    panel.resize(324, 700)
    panel.show()
    extra = QLabel("翻訳の設定のパネル")
    panel.set_translation_widget(extra)
    panel.set_paper(paper)
    for tab in ("annotations", "note", "details"):
        panel.set_current_tab(tab)
        app.processEvents()
        assert not extra.isVisible(), f"{tab} のタブに翻訳の設定のパネルが重なっている"
    panel.set_current_tab("translation")
    assert extra.isVisible()
    panel.set_current_tab("annotations")
    assert not extra.isVisible()
    # 差し込んだままタブを切り替えても、見えている欄は現在のタブのものだけ
    visible_pages = [panel.detail.pages.widget(i) for i in range(panel.detail.pages.count()) if panel.detail.pages.widget(i).isVisible()]
    assert visible_pages == [panel.annotation_list]
    panel.set_translation_widget(None)
    assert not extra.isVisible() and panel.detail.pages.currentWidget() is panel.annotation_list
    panel.set_current_tab("translation")
    assert panel.detail._translation_page.isVisible()


def test_popover_marks_the_selected_kind(app):
    from spectra.ui.annotation_popover import AnnotationPopover

    p = AnnotationPopover(Ann("a", kind="underline"))
    assert p.kind_buttons["underline"].property("primary") is True and not p.kind_buttons["highlight"].property("primary")
    p.kind_buttons["highlight"].click()
    assert p.kind_buttons["highlight"].property("primary") is True and not p.kind_buttons["underline"].property("primary")


# ---- 日本語入力（IME） -------------------------------------------------------------


def _preedit(widget, text):
    from PySide6.QtGui import QInputMethodEvent
    from PySide6.QtWidgets import QApplication

    QApplication.sendEvent(widget, QInputMethodEvent(text, []))


def _commit(widget, text):
    from PySide6.QtGui import QInputMethodEvent
    from PySide6.QtWidgets import QApplication

    ev = QInputMethodEvent()
    ev.setCommitString(text)
    QApplication.sendEvent(widget, ev)


def _press(popover, global_pos):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    ev = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(1, 1), QPointF(global_pos), Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    popover.eventFilter(None, ev)


def test_popover_is_not_a_popup_window_and_accepts_input_methods(app):
    """Qt.Popup は IME の変換候補の窓で閉じたり入力が届かなかったりするので、通常の小窓にする。"""
    from PySide6.QtCore import Qt

    from spectra.ui.annotation_popover import AnnotationPopover

    p = AnnotationPopover(Ann("a"))
    assert p.windowType() == Qt.WindowType.Tool and p.windowType() != Qt.WindowType.Popup
    assert p.comment.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)


def test_popover_comment_takes_japanese_input_and_stays_open_while_composing(app):
    from PySide6.QtCore import QPoint

    from spectra.ui.annotation_popover import AnnotationPopover

    lib = FakeLibrary([Ann("a")])
    p = AnnotationPopover(lib.items["a"], lib)
    p.popup_at(QPoint(100, 100))
    app.processEvents()
    outside = p.frameGeometry().bottomRight() + QPoint(50, 50)

    _preedit(p.comment, "にほんご")  # 変換中（確定前）
    assert p.composing
    _press(p, outside)  # 変換中は、外のクリックでも閉じない（入力中の文字を失わない）
    assert p.isVisible()
    _commit(p.comment, "日本語")  # 確定
    assert not p.composing and p.comment.toPlainText() == "日本語" and p.isVisible()
    _commit(p.comment, "のコメント")
    assert p.comment.toPlainText() == "日本語のコメント"
    p.flush()
    assert lib.items["a"].comment == "日本語のコメント"

    _press(p, outside)  # 変換中でなければ、外のクリックで閉じる
    assert not p.isVisible()


def test_popover_closes_on_escape_and_inside_clicks_keep_it_open(app):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from spectra.ui.annotation_popover import AnnotationPopover

    p = AnnotationPopover(Ann("a"))
    p.popup_at(QPoint(100, 100))
    _press(p, p.frameGeometry().center())  # 中のクリックでは閉じない
    assert p.isVisible()
    _preedit(p.comment, "かんじ")
    QTest.keyClick(p, Qt.Key.Key_Escape)  # 変換中の Esc は、変換の取り消しに使う（吹き出しは閉じない）
    assert p.isVisible()
    _commit(p.comment, "")
    p.comment.composing = False
    QTest.keyClick(p, Qt.Key.Key_Escape)
    assert not p.isVisible()


def test_note_editor_takes_japanese_input(app):
    from spectra.ui.note_editor import NoteEditor

    ed = NoteEditor(None)
    ed.show()
    ed.set_paper(type("P", (), {"id": 1, "note": "", "note_updated_at": None, "note_merged": False})())
    _preedit(ed.edit, "めも")
    _commit(ed.edit, "メモ")
    assert ed.text() == "メモ" and ed.dirty


def test_side_panel_tabs_fit_even_with_a_wider_font(app, lib_and_paper):
    """4 つのタブは、フォントが広くても（Windows の Segoe UI・拡大表示）右のパネルの幅からはみ出さない。"""
    from PySide6.QtGui import QFont

    from spectra.config.settings import Settings
    from spectra.ui.side_panel import SidePanel

    lib, paper = lib_and_paper
    panel = SidePanel(lib, Settings(), "reader")
    panel.show()
    panel.set_paper(paper)

    def check(panel):
        app.processEvents()
        tabs = panel.detail.tabs
        assert tabs.width() == panel.width() == 324
        for b in panel.detail.tab_buttons:
            tl, br = b.mapTo(tabs, b.rect().topLeft()), b.mapTo(tabs, b.rect().bottomRight())
            assert tl.x() >= 0 and br.x() < tabs.width(), f"{b.full_text()} のタブがパネルからはみ出す"
        buttons = panel.detail.tab_buttons
        xs = [(b.mapTo(tabs, b.rect().topLeft()).x(), b.mapTo(tabs, b.rect().bottomRight()).x()) for b in buttons]
        assert all(a[1] < b[0] for a, b in zip(xs, xs[1:])), "タブどうしが重ならない"

    check(panel)  # 普通のフォント
    assert [b.full_text() for b in panel.detail.tab_buttons] == ["論文の詳細", "AI翻訳の詳細", "ノート", "注釈"]
    for size in (12, 14, 18):  # フォントを広くしていくと、省略して収める（macOS だけで通る値にしない）
        f = QFont(panel.font())
        f.setPointSize(size)
        panel.setFont(f)
        for b in panel.detail.tab_buttons:
            b.setFont(f)
        check(panel)
    elided = [b for b in panel.detail.tab_buttons if b.text() != b.full_text()]
    assert elided and all(b.text().endswith("…") and b.toolTip() == b.full_text() for b in elided)  # 省略したら全体をツールチップに出す
    # 元の大きさに戻すと、省略は解ける
    f = QFont(panel.font())
    f.setPointSize(10)
    panel.setFont(f)
    for b in panel.detail.tab_buttons:
        b.setFont(f)
    check(panel)
    assert all(b.text() == b.full_text() and not b.toolTip() for b in panel.detail.tab_buttons)
