"""お気に入りのスター（Issue #2）：押せることが分かるよう、枠だけのスターを常に表示し、お気に入りは塗りつぶす。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from test_gui import pump, window  # noqa: E402, F401  (同じ画面の部品を使う)

from spectra.ui import theme  # noqa: E402
from spectra.ui.library_model import COL, FavoriteRole, PaperIdRole  # noqa: E402
from spectra.ui.library_view import star_colors  # noqa: E402


def _add(w, tmp_path, n=2):
    from pdf_factory import make_library_paper

    ids = []
    for i in range(n):
        path = tmp_path / f"p{i}.pdf"
        make_library_paper(str(path), title=f"Synthetic Paper {i}", seed=i).close()
        ids.append(w.add_files([path], open_single=False)[0].id)
    w.library_view.reload()
    return ids


def _cell(view, paper_id, column):
    for row in range(view.proxy.rowCount()):
        idx = view.proxy.index(row, column)
        if idx.data(PaperIdRole) == paper_id:
            return idx
    raise AssertionError(paper_id)


def test_star_cell_roles(window, tmp_path):  # noqa: F811
    """文字の「★」ではなく役割でお気に入りを渡し、ツールチップは操作の名前、読み上げは状態と操作を伝える。"""
    app, w = window
    a, b = _add(w, tmp_path)
    w.library.set_favorite(a, True)
    w.library_view.refresh_paper(a)
    view = w.library_view
    on, off = _cell(view, a, COL["favorite"]), _cell(view, b, COL["favorite"])
    assert on.data(FavoriteRole) is True and off.data(FavoriteRole) is False
    assert not on.data(Qt.ItemDataRole.DisplayRole) and not off.data(Qt.ItemDataRole.DisplayRole)
    assert on.data(Qt.ItemDataRole.ToolTipRole).startswith("お気に入りから外す")
    assert off.data(Qt.ItemDataRole.ToolTipRole).startswith("お気に入りに追加")
    assert off.data(Qt.ItemDataRole.ToolTipRole)  # お気に入りでなくてもツールチップを出す
    assert on.data(Qt.ItemDataRole.AccessibleTextRole) == "お気に入り"
    assert off.data(Qt.ItemDataRole.AccessibleTextRole) == "お気に入りではない"
    assert off.data(Qt.ItemDataRole.AccessibleDescriptionRole) == "クリックでお気に入りに追加"


def test_star_colors():
    t = theme.current()
    stroke, fill = star_colors(False, False, False)
    assert stroke and not fill  # 枠だけ（常に表示）
    assert star_colors(False, False, True)[0] != stroke  # ホバーで強調
    assert star_colors(False, True, False)[0] != stroke  # 選択した行でも見える色
    assert star_colors(True, False, False) == (t.label, t.label)  # 塗りつぶし


@pytest.mark.parametrize("appearance", ["light", "dark"])
def test_outline_is_always_drawn_and_favorite_is_filled(window, tmp_path, appearance):  # noqa: F811
    """お気に入りでない行にも枠のスターが描かれ、中は空いている。お気に入りの行は中まで塗る。"""
    app, w = window
    w.settings.appearance = appearance
    w.apply_appearance()
    a, b = _add(w, tmp_path)
    w.library.set_favorite(a, True)
    view = w.library_view
    view.refresh_paper(a)
    view.table.clearSelection()
    pump(app)
    image = view.table.viewport().grab().toImage()
    t = theme.current()

    def colors(paper_id):
        r = view.table.visualRect(_cell(view, paper_id, COL["favorite"]))
        center = r.center()
        bg = image.pixelColor(r.left() + 2, r.top() + 2)
        ring = {image.pixelColor(center.x() + dx, center.y() + dy).name() for dx in range(-8, 9) for dy in range(-8, 9)}
        return bg.name(), image.pixelColor(center).name(), ring

    bg, mid, ring = colors(b)
    assert ring - {bg}, "お気に入りでない行にもスターの枠を描く"
    assert mid == bg  # 枠だけ
    bg, mid, ring = colors(a)
    assert QColor(mid) == QColor(t.label)  # 塗りつぶし


def test_click_on_star_toggles_and_menu_names_the_action(window, tmp_path):  # noqa: F811
    app, w = window
    (a,) = _add(w, tmp_path, 1)
    view = w.library_view
    pump(app)
    rect = view.table.visualRect(_cell(view, a, COL["favorite"]))
    QTest.mouseClick(view.table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    pump(app)
    assert w.library.get(a).favorite
    view.select_ids([a])
    pump(app)
    assert w.act_favorite.text() == "お気に入りから外す"
    QTest.mouseClick(view.table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    pump(app)
    assert not w.library.get(a).favorite
    assert w.act_favorite.text() == "お気に入りに追加"


def test_star_column_width_survives_sorting(window, tmp_path):  # noqa: F811
    app, w = window
    a, _b = _add(w, tmp_path)
    w.library.set_favorite(a, True)
    view = w.library_view
    view.refresh_paper(a)
    width = view.table.columnWidth(COL["favorite"])
    view.table.sortByColumn(COL["favorite"], Qt.SortOrder.DescendingOrder)
    pump(app)
    assert view.table.columnWidth(COL["favorite"]) == width == 32
    assert view.proxy.index(0, 0).data(PaperIdRole) == a
    w.resize(700, 400)
    pump(app)
    assert view.table.columnWidth(COL["favorite"]) == 32
