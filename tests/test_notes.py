"""論文ごとのノート: ライブラリへの保存と、同期での比べ方（版ベクトル・同時の編集の連結）。"""

import sqlite3

import pytest
from pdf_factory import make_library_paper

from spectra.library import Library
from spectra.library.models import NOTE_MAX_CHARS
from spectra.library.store import DB_NAME, MIGRATIONS, STATE_DB_NAME, STATE_MIGRATIONS
from spectra.library.sync import files as F
from spectra.library.sync import notes as N

A, B, C = "a" * 32, "b" * 32, "c" * 32


@pytest.fixture()
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


@pytest.fixture()
def pdf(tmp_path):
    def make(name="paper.pdf", seed=0):
        path = tmp_path / name
        make_library_paper(str(path), seed=seed)
        return path

    return make


def _hlc(ms: int, device: str = A) -> str:
    return f"{ms:013d}.000000.{device}"


# -- ライブラリ ------------------------------------------------------------------


def test_note_is_saved_and_reloaded(tmp_path, library, pdf):
    paper, _ = library.add(pdf())
    assert paper.note == "" and paper.note_updated_at is None and not paper.note_merged
    events = []
    library.add_listener(events.append)
    library.set_note(paper.id, "読む前のメモ\n2 行目")
    got = library.get(paper.id)
    assert got.note == "読む前のメモ\n2 行目" and got.note_updated_at is not None
    assert [e.kind for e in events] == ["note"]
    library.set_note(paper.id, "読む前のメモ\n2 行目")  # 変わらなければ保存も合図もしない
    assert len(events) == 1
    assert got.updated_at == paper.updated_at  # 書誌情報の更新時刻は進めない
    library.close()
    reopened = Library(tmp_path / "lib")
    try:
        assert reopened.get(paper.id).note == "読む前のメモ\n2 行目"
    finally:
        reopened.close()


def test_note_length_is_limited(library, pdf):
    paper, _ = library.add(pdf())
    library.set_note(paper.id, "x" * NOTE_MAX_CHARS)
    with pytest.raises(ValueError):
        library.set_note(paper.id, "x" * (NOTE_MAX_CHARS + 1))
    with pytest.raises(ValueError):
        library.set_note(paper.id, None)


def test_note_survives_trash_and_is_removed_with_purge(tmp_path, library, pdf):
    paper, _ = library.add(pdf())
    library.set_note(paper.id, "残す")
    library.trash(paper.id)
    library.restore(paper.id)
    assert library.get(paper.id).note == "残す"
    library.purge(paper.id)
    state = sqlite3.connect(tmp_path / "lib" / STATE_DB_NAME)
    try:
        assert state.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
    finally:
        state.close()


def test_notes_do_not_raise_database_versions(tmp_path, library, pdf):
    """ノートの表は足すだけなので、v0.3 以前でもライブラリを開ける（版を上げない）。"""
    paper, _ = library.add(pdf())
    library.set_note(paper.id, "メモ")
    library.close()
    for name, expected in ((DB_NAME, len(MIGRATIONS)), (STATE_DB_NAME, len(STATE_MIGRATIONS))):
        db = sqlite3.connect(tmp_path / "lib" / name)
        try:
            assert db.execute("PRAGMA user_version").fetchone()[0] == expected
        finally:
            db.close()


def test_merged_flag_is_cleared_by_the_next_save(library, pdf):
    paper, _ = library.add(pdf())
    library.sync_set_note(paper.id, "連結した", merged=True)
    assert library.get(paper.id).note_merged
    library.set_note(paper.id, "連結した")  # 同じ中身でも、利用者が保存したら印を消す
    assert not library.get(paper.id).note_merged


# -- 比べ方 ----------------------------------------------------------------------


def test_later_version_wins_without_merging():
    v1 = N.new_version("一", _hlc(1), A, None)
    v2 = N.new_version("二", _hlc(2, B), B, v1)  # B は v1 を見てから書いた
    assert N.merge([v1, v2]) == (v2, False)
    assert N.merge([v2, v1]) == (v2, False)
    assert N.merge([]) == (None, False)


def test_concurrent_versions_are_concatenated_newest_first():
    base = N.new_version("共通", _hlc(1), A, None)
    a = N.new_version("A の追記", _hlc(5), A, base)
    b = N.new_version("B の追記", _hlc(3, B), B, base)
    merged, flag = N.merge([a, b])
    assert flag and merged["text"] == "A の追記" + N.SEPARATOR + "B の追記"
    assert merged["vv"] == {A: _hlc(5), B: _hlc(3, B)} and merged["hlc"] == _hlc(5)
    assert N.merge([b, a]) == (merged, True)  # 順序によらない
    # 連結した版は両方を含むので、もう一度まとめても連結しない（往復しない）
    assert N.merge([merged, a, b]) == (merged, False)


def test_three_concurrent_versions_and_empty_or_equal_texts():
    a = N.new_version("A", _hlc(1), A, None)
    b = N.new_version("B", _hlc(2, B), B, None)
    c = N.new_version("C", _hlc(3, C), C, None)
    merged, _ = N.merge([c, a, b])
    assert merged["text"] == "C" + N.SEPARATOR + "B" + N.SEPARATOR + "A"
    cleared = N.new_version("", _hlc(4, B), B, None)
    assert N.merge([a, cleared])[0]["text"] == "A"  # 片方が消しただけなら、残っている方
    same = N.new_version("A", _hlc(5, B), B, None)
    assert N.merge([a, same])[0]["text"] == "A"  # 同じ中身は重ねない


def test_note_from_another_device_is_validated():
    good = {"text": "メモ​\n", "hlc": _hlc(1, B), "vv": {B: _hlc(1, B)}}
    assert F.parse_note(good) == {"text": "メモ\n", "hlc": _hlc(1, B), "vv": {B: _hlc(1, B)}}
    for bad in (
        None, "x", {"text": 3, "hlc": _hlc(1), "vv": {A: _hlc(1)}},
        {"text": "x" * (NOTE_MAX_CHARS + 1), "hlc": _hlc(1), "vv": {A: _hlc(1)}},
        {"text": "x", "hlc": "nope", "vv": {A: _hlc(1)}},
        {"text": "x", "hlc": _hlc(1), "vv": {}},
        {"text": "x", "hlc": _hlc(1), "vv": {"../x": _hlc(1)}},
        {"text": "x", "hlc": _hlc(1), "vv": {A: "nope"}},
    ):
        assert F.parse_note(bad) is None, bad
