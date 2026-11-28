"""本文の索引（library/fulltext.py）。作り物の最小 PDF だけを使う。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pymupdf
import pytest

from spectra.library import fulltext
from spectra.library.fulltext import (
    BROKEN,
    ENCRYPTED,
    FULL,
    NO_TEXT,
    OK,
    ORIGINAL,
    TOO_LARGE,
    TRANSLATED,
    FulltextIndex,
    FulltextUnavailable,
    Source,
    make_snippet,
    normalize,
    query_terms,
)


def _pdf(path: Path, pages: list[str], *, font: str = "helv", password: str = "", pad: bool = True) -> Path:
    """pad: 「文字がない」とみなされないよう、各ページに埋め草の行を足す。"""
    doc = pymupdf.open()
    for text in pages:
        if pad:
            text += "\nlorem ipsum dolor sit amet"
        page = doc.new_page(width=300, height=400)
        y = 40
        for line in text.split("\n"):
            page.insert_text((20, y), line, fontname=font, fontsize=10)
            y += 14
    if password:
        doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password)
    else:
        doc.save(str(path))
    doc.close()
    return path


@pytest.fixture()
def index(tmp_path):
    ix = FulltextIndex(tmp_path / "cache" / "fulltext.sqlite3")
    yield ix
    ix.close()


def _src(path: Path, sha: str = "a" * 64, kind: str = ORIGINAL) -> Source:
    return Source(sha, kind, path)


# -- 文字の整え方 ------------------------------------------------------------


def test_normalize_joins_hyphenated_words_and_lines():
    assert normalize("radia-\ntive transfer\nequation") == "radiative transfer equation"


def test_normalize_joins_japanese_lines_without_space_and_widens_nothing():
    assert normalize("放射伝達\n方程式　ＡＢＣ") == "放射伝達方程式 ABC"


def test_query_terms_split_on_any_space():
    assert query_terms(" radiative　伝達  ") == ["radiative", "伝達"]


# -- 索引と検索 --------------------------------------------------------------


def test_index_and_find_english_term_with_page_and_snippet(index, tmp_path):
    pdf = _pdf(tmp_path / "a.pdf", ["Introduction to the topic", "The radiative transfer equation is solved"])
    assert index.index(_src(pdf)) == OK
    found = index.search(query_terms("Radiative"))
    m = found["a" * 64]
    assert m.terms == {0}
    assert m.pages == {(ORIGINAL, 1)}
    snip = m.snippets[0]
    assert snip.page == 1
    start, end = snip.spans[0]
    assert snip.text[start:end].lower() == "radiative"


def test_find_japanese_terms_including_one_and_two_characters(index, tmp_path):
    pdf = _pdf(tmp_path / "ja.pdf", ["放射伝達方程式を解く。\n光の散乱"], font="japan")
    assert index.index(_src(pdf, kind=TRANSLATED)) == OK
    for term in ("伝達方程式", "散乱", "光"):
        assert "a" * 64 in index.search([term]), term
    assert index.search(["吸収"]) == {}


def test_words_split_across_lines_are_found(index, tmp_path):
    pdf = _pdf(tmp_path / "a.pdf", ["the radia-\ntive transfer"])
    index.index(_src(pdf))
    assert "a" * 64 in index.search(["radiative"])
    assert "a" * 64 in index.search(["radiative transfer"[:12]])


def test_terms_are_reported_per_term_for_and_across_fields(index, tmp_path):
    pdf = _pdf(tmp_path / "a.pdf", ["alpha beta", "gamma"])
    index.index(_src(pdf))
    m = index.search(["alpha", "gamma", "delta"])["a" * 64]
    assert m.terms == {0, 1}
    assert m.page_count == 2


def test_special_characters_in_terms_do_not_break_queries(index, tmp_path):
    pdf = _pdf(tmp_path / "a.pdf", ['100% of "quoted" a_b and NEAR OR AND'])
    index.index(_src(pdf))
    for term in ['"quoted"', "100%", "a_b", "OR", "%", "_", "NEAR(", "*", "a'b"]:
        index.search([term])  # 例外にならない
    assert "a" * 64 in index.search(["100%"])
    assert "a" * 64 in index.search(["a_b"])
    assert index.search(["ab"]) == {}  # _ は 1 文字の任意文字として扱わない


def test_original_and_translation_are_indexed_separately(index, tmp_path):
    en = _pdf(tmp_path / "en.pdf", ["scattering phase function"])
    ja = _pdf(tmp_path / "ja.pdf", ["散乱の位相関数"], font="japan")
    index.index(_src(en))
    index.index(_src(ja, kind=TRANSLATED))
    assert index.search(["散乱"])["a" * 64].pages == {(TRANSLATED, 0)}
    index.remove("a" * 64, TRANSLATED)
    assert index.search(["散乱"]) == {}
    assert "a" * 64 in index.search(["scattering"])


# -- 差分 --------------------------------------------------------------------


def test_stale_lists_new_and_changed_files_and_skips_missing(index, tmp_path):
    a = _pdf(tmp_path / "a.pdf", ["first version"])
    missing = tmp_path / "not-yet-synced.pdf"
    sources = [_src(a), _src(missing, sha="b" * 64)]
    assert index.stale(sources) == [sources[0]]
    index.index(sources[0])
    assert index.stale(sources) == []
    _pdf(a, ["second version with more words"])
    assert index.stale(sources) == [sources[0]]
    assert index.index(_src(missing, sha="b" * 64)) is None


def test_failed_files_are_not_retried_until_they_change(index, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7\nthis is not really a pdf")
    assert index.index(_src(bad)) == BROKEN
    assert index.stale([_src(bad)]) == []
    bad.write_bytes(b"%PDF-1.7\nstill broken but different")
    assert index.stale([_src(bad)]) == [_src(bad)]


def test_prune_removes_papers_no_longer_in_library(index, tmp_path):
    a = _pdf(tmp_path / "a.pdf", ["alpha"])
    b = _pdf(tmp_path / "b.pdf", ["alpha"])
    index.index(_src(a))
    index.index(_src(b, sha="b" * 64))
    assert index.prune([("a" * 64, ORIGINAL)]) == 1
    assert set(index.search(["alpha"])) == {"a" * 64}


# -- 止まらない --------------------------------------------------------------


def test_scanned_pdf_without_text_is_no_text(index, tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(pymupdf.Rect(10, 10, 200, 200), fill=(0, 0, 0))
    doc.save(str(tmp_path / "scan.pdf"))
    assert index.index(_src(tmp_path / "scan.pdf")) == NO_TEXT


def test_encrypted_pdf_is_skipped(index, tmp_path):
    pdf = _pdf(tmp_path / "lock.pdf", ["secret words"], password="pw")
    assert index.index(_src(pdf)) == ENCRYPTED
    assert index.search(["secret"]) == {}


def test_empty_and_truncated_files_are_broken(index, tmp_path):
    (tmp_path / "empty.pdf").write_bytes(b"")
    assert index.index(_src(tmp_path / "empty.pdf")) == BROKEN
    good = _pdf(tmp_path / "good.pdf", ["some text here"]).read_bytes()
    (tmp_path / "cut.pdf").write_bytes(good[: len(good) // 3])
    assert index.index(_src(tmp_path / "cut.pdf", sha="c" * 64)) in (BROKEN, OK, NO_TEXT)


def test_huge_pdf_is_indexed_up_to_the_limit(index, tmp_path, monkeypatch):
    monkeypatch.setattr(fulltext, "MAX_PAGES", 3)
    pdf = _pdf(tmp_path / "big.pdf", [f"page number {i} marker{i}" for i in range(6)])
    assert index.index(_src(pdf)) == TOO_LARGE
    assert "a" * 64 in index.search(["marker2"])
    assert index.search(["marker4"]) == {}


def test_index_size_limit_stops_adding(tmp_path):
    ix = FulltextIndex(tmp_path / "f.sqlite3", max_bytes=1)
    pdf = _pdf(tmp_path / "a.pdf", ["alpha words"])
    assert ix.index(_src(pdf)) == FULL
    assert ix.search(["alpha"]) == {}
    ix.max_bytes = 10**9
    assert ix.stale([_src(pdf)]) == [_src(pdf)]  # 上限を外せば索引にし直す
    assert ix.index(_src(pdf)) == OK
    ix.close()


def test_cancel_stops_indexing_without_writing(index, tmp_path):
    pdf = _pdf(tmp_path / "a.pdf", ["alpha"])
    cancel = threading.Event()
    cancel.set()
    assert index.index(_src(pdf), cancel) is None
    assert index.known() == {}


# -- 壊れた・版が違う索引 ----------------------------------------------------


def test_corrupt_index_file_is_rebuilt(tmp_path):
    path = tmp_path / "f.sqlite3"
    path.write_bytes(b"garbage" * 1000)
    ix = FulltextIndex(path)
    assert ix.stats().documents == 0
    ix.close()


def test_other_version_is_rebuilt(tmp_path):
    path = tmp_path / "f.sqlite3"
    ix = FulltextIndex(path)
    ix.index(_src(_pdf(tmp_path / "a.pdf", ["alpha"])))
    ix.close()
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version=99")
    db.close()
    ix = FulltextIndex(path)
    assert ix.known() == {}
    ix.close()


def test_errors_while_searching_raise_unavailable(index, tmp_path):
    index.close()
    with pytest.raises(FulltextUnavailable):
        index.search(["alpha"])


def test_disk_full_is_unavailable(index, tmp_path, monkeypatch):
    pdf = _pdf(tmp_path / "a.pdf", ["alpha"])

    class Full:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database or disk is full")

        def executemany(self, *a, **k):
            raise sqlite3.OperationalError("database or disk is full")

        def close(self):
            pass

    index._db = Full()
    with pytest.raises(FulltextUnavailable):
        index.index(_src(pdf))


def test_clear_and_delete_files(index, tmp_path):
    index.index(_src(_pdf(tmp_path / "a.pdf", ["alpha"])))
    index.clear()
    assert index.stats().documents == 0
    index.delete_files()
    assert not index.path.exists()


def test_stats_counts_statuses(index, tmp_path):
    index.index(_src(_pdf(tmp_path / "a.pdf", ["alpha", "beta"])))
    (tmp_path / "bad.pdf").write_bytes(b"x")
    index.index(_src(tmp_path / "bad.pdf", sha="b" * 64))
    st = index.stats()
    assert (st.documents, st.indexed, st.pages) == (2, 1, 2)
    assert st.by_status == {OK: 1, BROKEN: 1}
    assert st.bytes > 0


# -- 抜粋 --------------------------------------------------------------------


def test_snippet_marks_every_match_and_adds_ellipses():
    import re

    text = "word " * 20 + "Target here and target again " + "tail " * 30
    snip, spans = make_snippet(text, re.compile("target", re.IGNORECASE))
    assert snip.startswith("…") and snip.endswith("…")
    assert [snip[a:b] for a, b in spans] == ["Target", "target"]


# -- レビューの指摘 ----------------------------------------------------------


def test_control_characters_in_terms_do_not_break_or_match_everything(index, tmp_path):
    index.index(_src(_pdf(tmp_path / "a.pdf", ["abcd text"])))
    assert index.search(["ab\x00cd"]) == {} or "a" * 64 in index.search(["ab\x00cd"])  # 例外にならない
    assert index.search(["\x00"]) == {}
    assert index.search(["\x07\x1f"]) == {}
    assert query_terms("ab\x00cd \x01") == ["ab", "cd"]


def test_compound_hyphen_at_line_end_is_kept():
    assert normalize("CO2-\nrich air") == "CO2-rich air"
    assert normalize("radia-\ntive") == "radiative"


def test_unstorable_text_marks_only_that_pdf_broken(index, tmp_path, monkeypatch):
    a = _pdf(tmp_path / "a.pdf", ["alpha"])
    b = _pdf(tmp_path / "b.pdf", ["beta words"])
    real = fulltext.extract
    monkeypatch.setattr(fulltext, "extract", lambda p, c=None: (OK, ["bad \ud800 text"]) if p == a else real(p, c))
    assert index.index(_src(a)) == BROKEN
    assert index.index(_src(b, sha="b" * 64)) == OK
    assert "b" * 64 in index.search(["beta"])


def test_lock_held_by_another_process_is_busy_and_does_not_delete_the_index(tmp_path, monkeypatch):
    monkeypatch.setattr(fulltext, "BUSY_TIMEOUT", 0.1)
    path = tmp_path / "f.sqlite3"
    ix = FulltextIndex(path)
    ix.index(_src(_pdf(tmp_path / "a.pdf", ["alpha words"])))
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(fulltext.FulltextBusy):
            ix.index(_src(_pdf(tmp_path / "b.pdf", ["beta"]), sha="b" * 64))
        ix.close()
        reader = FulltextIndex(path)  # WAL なので、書き込み中でも読める
        assert "a" * 64 in reader.search(["alpha"])
        reader.close()
    finally:
        other.execute("ROLLBACK")
        other.close()

    def locked(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(FulltextIndex, "_connect", locked)
    with pytest.raises(fulltext.FulltextBusy):
        FulltextIndex(path)  # 開くときに使用中でも、壊れたとみなして消さない
    monkeypatch.undo()
    ix = FulltextIndex(path)
    assert "a" * 64 in ix.search(["alpha"])
    ix.close()


def test_query_errors_are_busy_not_unavailable(index):
    class Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("fts5: syntax error near")

    index._db = Broken()
    with pytest.raises(fulltext.FulltextBusy):
        index.search(["alpha"])
    index._db = None


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX の権限")
def test_index_files_are_private(tmp_path):
    import os
    import stat

    path = tmp_path / "new-cache" / "fulltext.sqlite3"
    ix = FulltextIndex(path)
    ix.index(_src(_pdf(tmp_path / "a.pdf", ["alpha words"])))
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    for suffix in ("", "-wal", "-shm"):
        p = path.with_name(path.name + suffix)
        if p.exists():
            assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, suffix
    ix.close()
    os.chmod(path, 0o644)  # 以前の版で作った索引も、開くときに直す
    FulltextIndex(path).close()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
