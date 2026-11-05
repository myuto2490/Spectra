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
    assert p.quote.text() == "<b>text</b>" and p.quote.textFormat().name == "PlainText"  # HTML として解釈しない
    assert p.comment.toPlainText() == "memo" and not p.merged.isHidden()
    note = AnnotationPopover(Ann("n", kind="note", quote=""))
    assert all(b.isHidden() for b in note.kind_buttons.values()) and note.quote.isHidden()  # 付箋は種類を切り替えない


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
    assert row.quote.textFormat().name == "PlainText" and row.comment.textFormat().name == "PlainText"
    assert row.quote.maximumHeight() < 60 and row.comment.maximumHeight() < 80  # 引用は 2 行、コメントは 3 行まで
    assert "連結" in row.accessibleName() or any("連結" in c.text() for c in row.findChildren(type(row.page)))


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
    assert panel.detail.tabs.sizeHint().width() <= 324  # 4 つのタブが右のパネルの幅に収まる
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
