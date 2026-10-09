import pymupdf
import pytest

from spectra.cli import main, parse_pages


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECTRA_HOME", str(tmp_path / "home"))


@pytest.mark.parametrize(("spec", "expected"), [(None, (0, None)), ("3", (2, 2)), ("2-5", (1, 4)), ("4-", (3, None)), ("-2", (0, 1))])
def test_parse_pages(spec, expected):
    assert parse_pages(spec) == expected


@pytest.mark.parametrize("spec", ["0", "5-2", "a"])
def test_parse_pages_invalid(spec):
    with pytest.raises(SystemExit):
        parse_pages(spec)


def test_translate_with_dummy(paper_path, capsys):
    out = paper_path.with_name("out.pdf")
    assert main(["translate", str(paper_path), "--engine", "dummy", "-o", str(out), "--mode", "side-by-side"]) == 0
    assert pymupdf.open(out)[0].rect.width == pytest.approx(1190)
    assert "保存しました" in capsys.readouterr().out
    # 既存ファイルは --force なしでは上書きしない
    assert main(["translate", str(paper_path), "--engine", "dummy", "-o", str(out)]) == 1


def test_missing_key_message(paper_path, capsys):
    assert main(["translate", str(paper_path), "--engine", "gemini", "-o", str(paper_path.with_name("x.pdf"))]) == 1
    assert "API キーが設定されていません" in capsys.readouterr().err


def test_estimate(paper_path, capsys):
    assert main(["estimate", str(paper_path), "--engine", "dummy"]) == 0
    assert "翻訳する文字数" in capsys.readouterr().out


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "Spectra" in capsys.readouterr().out
