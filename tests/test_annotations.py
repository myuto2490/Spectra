"""注釈（マーカー・下線・付箋とコメント）のライブラリへの保存（docs/ANNOTATIONS.md §2）。"""

import sqlite3

import pytest
from pdf_factory import make_library_paper

from spectra.library import Annotation, Library, LibraryError, PaperNotFound
from spectra.library import models as M
from spectra.library.store import DB_NAME, MIGRATIONS, STATE_DB_NAME, STATE_MIGRATIONS


@pytest.fixture()
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


@pytest.fixture()
def paper(tmp_path, library):
    path = tmp_path / "paper.pdf"
    make_library_paper(str(path), seed=1)
    return library.add(path)[0]


def test_add_update_delete_and_restore(library, paper):
    events = []
    library.add_listener(events.append)
    a = library.add_annotation(paper.id, "highlight", 0, [(72, 100, 300, 112), (72, 114, 200, 126)], quote="the text")
    assert isinstance(a, Annotation) and len(a.id) == 32 and a.color == "yellow" and not a.has_comment
    assert a.rect == (72, 100, 300, 126)
    assert library.annotations(paper.id) == [a]
    assert [(e.kind, e.paper_id, e.sha256) for e in events] == [("annotations", paper.id, paper.sha256)]

    b = library.update_annotation(a.id, color="blue", comment="大事")
    assert (b.color, b.comment, b.kind, b.quads) == ("blue", "大事", "highlight", a.quads) and b.has_comment
    assert library.update_annotation(a.id, color="blue") == b  # 変わらなければ書かない
    assert len(events) == 2

    gone = library.delete_annotation(a.id)
    assert gone == b and library.annotations(paper.id) == []
    back = library.restore_annotation(gone)
    assert back.id == a.id and back.comment == "大事" and library.annotations(paper.id) == [back]


def test_order_is_page_then_top_then_left(library, paper):
    lower = library.add_annotation(paper.id, "underline", 0, [(72, 300, 100, 310)])
    note = library.add_annotation(paper.id, "note", 0, [(500, 50, 500, 50)], comment="付箋")
    upper = library.add_annotation(paper.id, "highlight", 0, [(72, 100, 100, 110)])
    assert [x.id for x in library.annotations(paper.id)] == [note.id, upper.id, lower.id]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "strike"},
        {"color": "red"},
        {"page": -1},
        {"page": 99},  # 論文のページ数を超える
        {"page": True},
        {"quads": []},
        {"quads": [(1, 2, 3)]},
        {"quads": [(1, 2, 3, float("nan"))]},
        {"quads": [(1, 2, 3, 1e9)]},
        {"quote": "x" * (M.MAX_QUOTE + 1)},
        {"comment": "x" * (M.MAX_COMMENT + 1)},
        {"comment": None},
    ],
)
def test_invalid_values_are_rejected(library, paper, kwargs):
    args = {"kind": "highlight", "page": 0, "quads": [(1, 2, 3, 4)], **kwargs}
    extra = {k: v for k, v in kwargs.items() if k in ("quote", "comment", "color")}
    with pytest.raises(ValueError):
        library.add_annotation(paper.id, args["kind"], args["page"], args["quads"], **extra)
    assert library.annotations(paper.id) == []


def test_reversed_rectangles_are_normalized(library, paper):
    a = library.add_annotation(paper.id, "highlight", 0, [(300, 112, 72, 100)])
    assert a.quads == ((72, 100, 300, 112),)


def test_limit_per_paper(library, paper, monkeypatch):
    monkeypatch.setattr("spectra.library.store.MAX_ANNOTATIONS", 2)
    library.add_annotation(paper.id, "highlight", 0, [(1, 2, 3, 4)])
    library.add_annotation(paper.id, "highlight", 0, [(1, 2, 3, 4)])
    with pytest.raises(LibraryError):
        library.add_annotation(paper.id, "highlight", 0, [(1, 2, 3, 4)])


def test_missing_paper_or_annotation(library, paper):
    with pytest.raises(PaperNotFound):
        library.add_annotation(9999, "highlight", 0, [(1, 2, 3, 4)])
    with pytest.raises(PaperNotFound):
        library.update_annotation("0" * 32, color="blue")
    with pytest.raises(PaperNotFound):
        library.delete_annotation("0" * 32)


def test_comment_merged_flag_is_cleared_when_the_comment_is_saved(library, paper):
    from dataclasses import replace

    a = library.add_annotation(paper.id, "highlight", 0, [(1, 2, 3, 4)], comment="A")
    library.sync_put_annotation(replace(a, comment="A\n―\nB", comment_merged=True))
    assert library.update_annotation(a.id, color="green").comment_merged  # 色だけなら残す
    assert not library.update_annotation(a.id, comment="整理した").comment_merged


def test_annotations_survive_trash_reopen_and_go_with_purge(tmp_path, library, paper):
    a = library.add_annotation(paper.id, "highlight", 0, [(1, 2, 3, 4)], comment="残す")
    library.trash(paper.id)
    library.restore(paper.id)
    library.close()
    lib = Library(tmp_path / "lib")
    try:
        assert lib.annotations(paper.id) == [a]
        lib.purge(paper.id)
        assert lib.get_annotation(a.id) is None
    finally:
        lib.close()


def test_annotations_do_not_raise_database_versions(tmp_path, library, paper):
    """表を足すだけなので、v0.3 以前でもライブラリを開ける（版を上げない）。"""
    library.add_annotation(paper.id, "highlight", 0, [(1, 2, 3, 4)])
    library.close()
    for name, expected in ((DB_NAME, len(MIGRATIONS)), (STATE_DB_NAME, len(STATE_MIGRATIONS))):
        db = sqlite3.connect(tmp_path / "lib" / name)
        try:
            assert db.execute("PRAGMA user_version").fetchone()[0] == expected
        finally:
            db.close()


def test_library_pdf_is_not_modified(library, paper):
    """注釈はライブラリの PDF に書き込まない（sha256 が変わると、同期で同じ論文と分からなくなる）。"""
    import hashlib

    before = hashlib.sha256(paper.pdf_path.read_bytes()).hexdigest()
    library.add_annotation(paper.id, "highlight", 0, [(1, 2, 3, 4)], comment="x")
    assert hashlib.sha256(paper.pdf_path.read_bytes()).hexdigest() == before == paper.sha256
