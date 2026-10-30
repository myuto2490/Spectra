import json

from pdf_factory import make_library_paper

from spectra.cli import main
from spectra.library import Library, LookupFailed, Metadata


def test_library_add_and_list(tmp_path, capsys):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    make_library_paper(str(a), seed=1)
    make_library_paper(str(b), seed=2, title="Second Paper Title")
    assert main(["library", "add", str(a), str(b)]) == 0
    out = capsys.readouterr().out
    assert "追加しました" in out and "Second Paper Title" in out

    assert main(["library", "add", str(a)]) == 0
    assert "既にライブラリにあります" in capsys.readouterr().out

    assert main(["library", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["title"] for r in rows] == ["Second Paper Title", "Attention Is All You Need"]
    assert {r["meta_status"] for r in rows} == {"local"}  # 照会は既定でオフ

    assert main(["library", "list"]) == 0
    assert "Attention Is All You Need" in capsys.readouterr().out


def test_library_list_empty(capsys):
    assert main(["library", "list"]) == 0
    assert "ライブラリは空です" in capsys.readouterr().out


def test_library_add_with_lookup(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_lookup(md, client=None):
        calls.append(md.doi)
        if md.doi == "10.1000/found":
            return Metadata(title="From Crossref", year=2017, source="crossref")
        if md.doi == "10.1000/offline":
            raise LookupFailed("Crossref に接続できません")
        return None

    monkeypatch.setattr("spectra.library.lookup.lookup", fake_lookup)
    paths = []
    for i, doi in enumerate(["10.1000/found", "10.1000/offline", "10.1000/missing"]):
        p = tmp_path / f"{i}.pdf"
        make_library_paper(str(p), seed=i, body=[f"doi:{doi}"])
        paths.append(str(p))
    assert main(["library", "add", "--lookup", *paths]) == 0
    assert calls == ["10.1000/found", "10.1000/offline", "10.1000/missing"]
    assert "接続できません" in capsys.readouterr().err

    lib = Library()
    try:
        status = {p.metadata.doi: (p.meta_status, p.metadata.title) for p in lib.papers()}
    finally:
        lib.close()
    assert status["10.1000/found"] == ("done", "From Crossref")
    assert status["10.1000/offline"][0] == "pending"
    assert status["10.1000/missing"][0] == "failed"


def test_library_add_reports_bad_files(tmp_path, capsys):
    bad = tmp_path / "bad.pdf"
    bad.write_text("nope")
    assert main(["library", "add", "--no-lookup", str(bad), str(tmp_path / "missing.pdf")]) == 1
    err = capsys.readouterr().err
    assert err.count("取り込めませんでした") == 2
