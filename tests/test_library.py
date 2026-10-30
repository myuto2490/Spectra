import sqlite3
import time

import pytest
from pdf_factory import make_library_paper

from spectra.errors import DocumentError
from spectra.library import Author, Library, LibraryError, Metadata, Translation
from spectra.library.bibtex import cite_key, to_bibtex
from spectra.library.store import DB_NAME, MIGRATIONS


@pytest.fixture()
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


@pytest.fixture()
def pdf(tmp_path):
    def make(name="paper.pdf", seed=0, **kw):
        path = tmp_path / name
        make_library_paper(str(path), seed=seed, **kw)
        return path

    return make


def translation(**kw) -> Translation:
    values = dict(
        paper_id=0, model="gemini-3.5-flash-lite", formality="default", first_page=0, last_page=None,
        options={"skip_references": True}, segments=10, untranslated=0, chars=1234, created_at=time.time(),
    )
    return Translation(**{**values, **kw})


def test_default_root_is_library_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SPECTRA_HOME", str(tmp_path / "home"))
    lib = Library()
    try:
        assert lib.root == tmp_path / "home" / "data" / "library"
        assert (lib.root / DB_NAME).exists()
    finally:
        lib.close()


def test_add_copies_and_extracts(library, pdf):
    path = pdf(body=["DOI: 10.1234/jsp.2024.7"])
    paper, existed = library.add(path)
    assert not existed
    assert paper.pdf_path.read_bytes() == path.read_bytes()
    assert paper.pdf_path.parent == library.root / "files" / str(paper.id)
    assert paper.file_name == "paper.pdf" and paper.source_path == str(path.resolve())
    assert paper.page_count == 1
    assert paper.meta_status == "pending"
    assert paper.metadata.title == "Attention Is All You Need"
    assert paper.metadata.doi == "10.1234/jsp.2024.7"
    assert paper.translation is None and not paper.favorite and paper.last_opened_at is None
    assert library.get(paper.id) == paper


def test_add_same_file_twice_returns_existing(library, pdf, tmp_path):
    first, _ = library.add(pdf())
    copy = tmp_path / "renamed.pdf"
    copy.write_bytes(first.pdf_path.read_bytes())
    again, existed = library.add(copy)
    assert existed and again.id == first.id
    assert len(library.papers()) == 1
    assert library.find_by_sha(first.sha256).id == first.id


def test_add_with_given_metadata(library, pdf):
    md = Metadata(title="Given", authors=[Author("Doe", "Jane")], year=2020)
    paper, _ = library.add(pdf(), md)
    assert paper.metadata.title == "Given"
    assert paper.metadata.authors == [Author("Doe", "Jane")]
    assert paper.authors_short == "Doe"


def test_add_rejects_non_pdf(library, tmp_path):
    bad = tmp_path / "x.pdf"
    bad.write_text("not a pdf")
    with pytest.raises(DocumentError):
        library.add(bad)
    assert library.papers() == []


def test_papers_newest_first(library, pdf):
    a, _ = library.add(pdf("a.pdf", seed=1))
    b, _ = library.add(pdf("b.pdf", seed=2))
    assert [p.id for p in library.papers()] == [b.id, a.id]


def test_same_identifier(library, pdf):
    a, _ = library.add(pdf("a.pdf", seed=1, body=["doi:10.1000/shared"]))
    b, _ = library.add(pdf("b.pdf", seed=2, body=["doi:10.1000/shared"]))
    c, _ = library.add(pdf("c.pdf", seed=3))
    assert [p.id for p in library.same_identifier(a)] == [b.id]
    assert library.same_identifier(c) == []


def test_auto_update_keeps_edited_fields(library, pdf):
    paper, _ = library.add(pdf())
    paper = library.update_metadata(paper.id, Metadata(title="My Own Title"), manual_fields={"title"})
    assert paper.metadata.title == "My Own Title"
    assert paper.edited == frozenset({"title"})

    online = Metadata(title="Online Title", year=2017, venue="NeurIPS", source="crossref")
    paper = library.update_metadata(paper.id, online, status="done")
    assert paper.metadata.title == "My Own Title"  # 利用者の編集は上書きしない
    assert (paper.metadata.year, paper.metadata.venue) == (2017, "NeurIPS")
    assert paper.metadata.source == "crossref"
    assert paper.meta_status == "done"


def test_auto_update_does_not_clear_values(library, pdf):
    paper, _ = library.add(pdf(body=["doi:10.1000/keep"]))
    paper = library.update_metadata(paper.id, Metadata(year=2001, source="arxiv"))
    assert paper.metadata.doi == "10.1000/keep"
    assert paper.metadata.title == "Attention Is All You Need"


def test_manual_update_can_clear(library, pdf):
    paper, _ = library.add(pdf(body=["doi:10.1000/clear"]))
    paper = library.update_metadata(paper.id, Metadata(), manual_fields={"doi"})
    assert paper.metadata.doi == ""
    with pytest.raises(ValueError):
        library.update_metadata(paper.id, Metadata(), manual_fields={"bogus"})
    with pytest.raises(ValueError):
        library.update_metadata(paper.id, Metadata(), status="weird")
    with pytest.raises(LibraryError):
        library.update_metadata(9999, Metadata())


def test_flags(library, pdf):
    paper, _ = library.add(pdf())
    library.set_favorite(paper.id, True)
    library.set_meta_status(paper.id, "local")
    library.touch_opened(paper.id)
    paper = library.get(paper.id)
    assert paper.favorite and paper.meta_status == "local" and paper.last_opened_at
    with pytest.raises(ValueError):
        library.set_meta_status(paper.id, "unknown")


def test_remove_deletes_files(library, pdf):
    paper, _ = library.add(pdf())
    library.save_translation(paper.id, b"%PDF-translated", translation())
    folder = paper.pdf_path.parent
    library.remove(paper.id)
    assert library.get(paper.id) is None
    assert not folder.exists()
    # 同じファイルをもう一度取り込める
    again, existed = library.add(pdf())
    assert not existed and again.id != paper.id


def test_collections(library, pdf):
    a, _ = library.add(pdf("a.pdf", seed=1))
    b, _ = library.add(pdf("b.pdf", seed=2))
    nlp = library.create_collection("  NLP ")
    cv = library.create_collection("cv")
    assert nlp.name == "NLP"
    assert [c.name for c in library.collections()] == ["cv", "NLP"]
    library.add_to_collection(nlp.id, [a.id, b.id, a.id])
    library.add_to_collection(cv.id, [a.id])
    assert library.get(a.id).collection_ids == {nlp.id, cv.id}
    library.remove_from_collection(nlp.id, [a.id])
    assert library.get(a.id).collection_ids == {cv.id}
    library.rename_collection(cv.id, "Vision")
    assert [c.name for c in library.collections()] == ["NLP", "Vision"]
    library.delete_collection(cv.id)
    assert library.get(a.id).collection_ids == frozenset()
    assert library.get(b.id).collection_ids == {nlp.id}
    library.remove(b.id)  # 論文を消すと所属も消える
    with pytest.raises(ValueError):
        library.create_collection("  ")


def test_save_translation_replaces(library, pdf):
    paper, _ = library.add(pdf())
    paper = library.save_translation(paper.id, b"%PDF-first", translation(first_page=2, last_page=4))
    assert paper.translated_path.read_bytes() == b"%PDF-first"
    assert not paper.translation.complete
    paper = library.save_translation(paper.id, b"%PDF-second", translation(untranslated=0))
    assert paper.translated_path.read_bytes() == b"%PDF-second"
    assert paper.translation.complete
    assert paper.translation.paper_id == paper.id
    assert paper.translation.options == {"skip_references": True}
    assert not translation(untranslated=3).complete
    library.remove_translation(paper.id)
    paper = library.get(paper.id)
    assert paper.translation is None and not paper.translated_path.exists()


def test_reopen_keeps_data_and_version(tmp_path, pdf):
    lib = Library(tmp_path / "lib")
    paper, _ = lib.add(pdf())
    c = lib.create_collection("Keep")
    lib.add_to_collection(c.id, [paper.id])
    assert lib.schema_version == len(MIGRATIONS)
    lib.close()

    lib = Library(tmp_path / "lib")
    try:
        assert lib.schema_version == len(MIGRATIONS)
        assert lib.get(paper.id).collection_ids == {c.id}
    finally:
        lib.close()


def test_newer_schema_is_rejected(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    db = sqlite3.connect(root / DB_NAME)
    db.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    db.close()
    with pytest.raises(LibraryError, match="新しいバージョン"):
        Library(root)


def test_migration_from_older_version(tmp_path, monkeypatch):
    """既存の DB に、後から追加した移行だけが流れる。"""
    root = tmp_path / "lib"
    Library(root).close()
    monkeypatch.setattr("spectra.library.store.MIGRATIONS", [*MIGRATIONS, "ALTER TABLE papers ADD COLUMN note TEXT"])
    lib = Library(root)
    try:
        assert lib.schema_version == len(MIGRATIONS) + 1
        cols = [r[1] for r in lib._db.execute("PRAGMA table_info(papers)")]
        assert "note" in cols
    finally:
        lib.close()


def test_models_and_bibtex(library, pdf):
    assert Author("Vaswani", "Ashish").display() == "Vaswani, A."
    assert Author("Lecun", "Yann A.").display() == "Lecun, Y. A."
    assert Author("Vaswani", "Ashish").full() == "Ashish Vaswani"
    assert Author("Consortium").display() == "Consortium"
    merged = Metadata(title="A", year=None).merged(Metadata(title="B", year=2020, doi="10.1/x"))
    assert (merged.title, merged.year, merged.doi) == ("A", 2020, "10.1/x")

    paper, _ = library.add(pdf(), Metadata())
    assert paper.display_title == "paper"  # タイトルがなければファイル名
    md = Metadata(
        title="The Attention & Transformers", authors=[Author("Vaswani", "Ashish"), Author("Shazeer", "Noam"),
        Author("Parmar", "Niki")], year=2017, venue="NeurIPS", volume="30", pages="5998-6008",
        doi="10.5555/3295222", arxiv_id="1706.03762",
    )
    paper = library.update_metadata(paper.id, md)
    assert paper.authors_short == "Vaswani et al."
    assert cite_key(paper) == "vaswani2017attention"
    bib = to_bibtex(paper)
    assert bib.startswith("@article{vaswani2017attention,\n")
    assert "  title = {{The Attention \\& Transformers}}" in bib
    assert "  author = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki}" in bib
    assert "  pages = {5998--6008}" in bib
    assert "  eprint = {1706.03762}" in bib
    assert "number" not in bib  # 空の項目は出さない
