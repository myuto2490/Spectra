"""ライブラリの検索：本文・メモ（ノート・注釈のコメント）にも一致させる。オフスクリーン・作り物のデータ。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pymupdf
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from spectra.config.settings import Settings  # noqa: E402
from spectra.library import Library, Metadata  # noqa: E402
from spectra.library.fulltext import ORIGINAL, TRANSLATED, Match, Snippet  # noqa: E402
from spectra.ui.library_model import BODY, COL, META, NOTE, MatchRole, PaperIdRole  # noqa: E402
from spectra.ui.library_view import LibraryView  # noqa: E402
from spectra.ui.match_delegate import (  # noqa: E402
    MATCH_ROW_HEIGHT,
    ROW_HEIGHT,
    _near_first_hit,
    _segments,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _pdf(path: Path, text: str) -> Path:
    doc = pymupdf.open()
    doc.new_page().insert_text((40, 60), text, fontname="helv", fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture()
def lib(tmp_path):
    library = Library(tmp_path / "library")
    yield library
    library.close()


def _add(lib, tmp_path, name, title, added_at=None):
    p, _ = lib.add(_pdf(tmp_path / f"{name}.pdf", f"{name} body"))
    lib.update_metadata(p.id, Metadata(title=title))
    return lib.get(p.id)


def _rows(view):
    return [view.proxy.index(r, 0).data(PaperIdRole) for r in range(view.proxy.rowCount())]


def _match(sha, terms, pages, snippets=()):
    m = Match(sha, set(terms), set(pages))
    m.snippets = list(snippets)
    return m


def test_body_only_match_is_shown_after_bibliographic_matches_with_snippet(app, lib, tmp_path):
    a = _add(lib, tmp_path, "a", "Radiative transfer primer")
    b = _add(lib, tmp_path, "b", "Unrelated title")
    c = _add(lib, tmp_path, "c", "Another unrelated title")
    view = LibraryView(lib, Settings())
    view.reload()
    view.set_search("radiative")
    assert _rows(view) == [a.id]
    snip = Snippet(TRANSLATED, 4, "…the radiative term…", ((5, 14),))
    found = {
        b.sha256: _match(b.sha256, {0}, {(ORIGINAL, 1)}, [snip]),
        c.sha256: _match(c.sha256, {0}, {(ORIGINAL, 1), (ORIGINAL, 2), (TRANSLATED, 3)}, [snip]),
    }
    view.set_fulltext("radiative", found)
    # 書誌に一致したものが先、本文だけのものは一致したページの多い順
    assert _rows(view) == [a.id, c.id, b.id]
    assert view.proxy._hits[a.id] == (META, 0)
    assert view.proxy._hits[c.id] == (BODY, 3)
    idx = view.proxy.index(1, COL["title"])
    heading, text, spans = idx.data(MatchRole)
    assert heading == "本文に一致・訳文 p.5"
    assert text[spans[0][0] : spans[0][1]] == "radiative"
    assert view.proxy.index(0, COL["title"]).data(MatchRole) is None
    assert view.table.verticalHeader().defaultSectionSize() == MATCH_ROW_HEIGHT
    assert not view.table.horizontalHeader().isSortIndicatorShown()


def test_header_click_overrides_relevance_order(app, lib, tmp_path):
    a = _add(lib, tmp_path, "a", "Zeta radiative")
    b = _add(lib, tmp_path, "b", "Alpha")
    view = LibraryView(lib, Settings())
    view.reload()
    view.set_search("radiative")
    view.set_fulltext("radiative", {b.sha256: _match(b.sha256, {0}, {(ORIGINAL, 0)})})
    assert _rows(view) == [a.id, b.id]
    view.table.horizontalHeader().sectionClicked.emit(COL["title"])
    view.table.sortByColumn(COL["title"], Qt.SortOrder.AscendingOrder)
    assert _rows(view) == [b.id, a.id]  # タイトルの順（Alpha → Zeta）
    assert view.table.horizontalHeader().isSortIndicatorShown()
    view.set_search("")
    assert not view.proxy._user_sorted


def test_results_for_an_older_query_are_ignored(app, lib, tmp_path):
    a = _add(lib, tmp_path, "a", "Nothing here")
    view = LibraryView(lib, Settings())
    view.reload()
    view.set_search("gamma")
    view.set_fulltext("gam", {a.sha256: _match(a.sha256, {0}, {(ORIGINAL, 0)})})
    assert _rows(view) == []
    view.set_fulltext("gamma", None)  # 本文を探せない（索引が使えない）ときは書誌だけ
    assert _rows(view) == []
    assert view.table.verticalHeader().defaultSectionSize() == ROW_HEIGHT


def test_terms_can_match_across_title_and_body(app, lib, tmp_path):
    a = _add(lib, tmp_path, "a", "Ocean color")
    view = LibraryView(lib, Settings())
    view.reload()
    view.set_search("ocean aerosol")
    assert _rows(view) == []
    view.set_fulltext("ocean aerosol", {a.sha256: _match(a.sha256, {1}, {(ORIGINAL, 2)})})
    assert _rows(view) == [a.id]
    assert view.proxy._hits[a.id][0] == BODY


def test_note_and_annotation_comment_matches(app, lib, tmp_path):
    a = _add(lib, tmp_path, "a", "First")
    b = _add(lib, tmp_path, "b", "Second")
    lib.set_note(a.id, "あとで読む：カタカナの検索も確かめる")
    lib.add_annotation(b.id, "note", 0, [(10, 10, 10, 10)], comment="check the aerosol model")
    view = LibraryView(lib, Settings())
    view.reload()
    view.set_search("かたかな")  # ひらがなでカタカナに一致する
    assert _rows(view) == [a.id]
    assert view.proxy._hits[a.id] == (NOTE, 0)
    assert view.matches_for(a.id)[0][0] == "メモに一致"
    view.set_search("aerosol")
    assert _rows(view) == [b.id]
    heading, text, spans = view.matches_for(b.id)[0]
    assert heading == "メモに一致" and text[spans[0][0] : spans[0][1]] == "aerosol"
    lib.add_annotation(a.id, "note", 0, [(10, 10, 10, 10)], comment="aerosol again")
    view.refresh_comments()
    assert set(_rows(view)) == {a.id, b.id}


def test_trashed_papers_stay_hidden_even_if_body_matches(app, lib, tmp_path):
    a = _add(lib, tmp_path, "a", "Title")
    lib.trash(a.id)
    view = LibraryView(lib, Settings())
    view.reload()
    view.set_search("body")
    view.set_fulltext("body", {a.sha256: _match(a.sha256, {0}, {(ORIGINAL, 0)})})
    assert _rows(view) == []


def test_index_status_row(app, lib):
    view = LibraryView(lib, Settings())
    view.set_index_status("本文の索引を作成中 1/3", cancellable=True)
    assert not view.index_status.isHidden() and not view.index_cancel_btn.isHidden()
    got = []
    view.indexCancelRequested.connect(lambda: got.append(1))
    view.index_cancel_btn.click()
    assert got == [1]
    view.set_index_status("")
    assert view.index_status.isHidden()


def test_detail_panel_shows_escaped_matches(app, lib):
    view = LibraryView(lib, Settings())
    view.detail.set_matches([("本文に一致・原文 p.1", "<b>x</b> radiative", ((9, 18),))])
    lb = view.detail.match_labels[0]
    assert "&lt;b&gt;x&lt;/b&gt;" in lb.text() and "<b>radiative</b>" in lb.text()
    assert not view.detail.matches_box.isHidden()
    view.detail.set_matches([])
    assert view.detail.matches_box.isHidden()


def test_segments_split_highlighted_parts():
    assert _segments("abcdef", ((1, 3), (2, 4))) == [("a", False), ("bc", True), ("d", True), ("ef", False)]


def test_main_window_searches_body_end_to_end(app, tmp_path):
    from spectra.ui.main_window import MainWindow

    library = Library(tmp_path / "lib")
    p, _ = library.add(_pdf(tmp_path / "x.pdf", "the quasiparticle spectrum is measured"))
    library.update_metadata(p.id, Metadata(title="Plain title"))
    win = MainWindow(settings=Settings(), library=library, check_updates=False)
    win.fulltext.pause = 0
    win.fulltext.schedule(0)
    end = time.time() + 10
    while time.time() < end and not (win.fulltext.index and win.fulltext.index.known() and not win.fulltext.indexing):
        QCoreApplication.processEvents()
        time.sleep(0.01)
    win.search.setText("quasiparticle")
    end = time.time() + 10
    while time.time() < end and not _rows(win.library_view):
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert _rows(win.library_view) == [p.id]
    heading, text, _ = win.library_view.proxy.index(0, COL["title"]).data(MatchRole)
    assert heading == "本文に一致・原文 p.1" and "quasiparticle" in text
    win.close()
    library.close()


def test_list_snippet_starts_near_the_first_hit():
    text = "…" + "x" * 50 + " aerosol tail"
    a = text.index("aerosol")
    cut, spans = _near_first_hit(text, ((a, a + 7),))
    assert cut.startswith("…") and len(cut) < len(text)
    assert cut[spans[0][0] : spans[0][1]] == "aerosol"
    assert _near_first_hit("short aerosol", ((6, 13),)) == ("short aerosol", ((6, 13),))
