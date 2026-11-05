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


def test_key_command_with_labels(monkeypatch, capsys):
    from spectra.config.secrets import SecretStore, slot
    from spectra.config.settings import Settings

    for var in ("SPECTRA_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    entered = iter(["k-first", "k-lab"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(entered))
    assert main(["key", "set"]) == 0  # 名前なしは 1 本目（以前からの保存場所）
    assert Settings.load().api_keys == []
    assert main(["key", "set", "--label", "研究室"]) == 0
    keys = Settings.load().api_keys
    assert [k["label"] for k in keys] == ["", "研究室"] and keys[0]["id"] == "default"
    store = SecretStore()
    assert store.get("gemini", use_env=False) == "k-first" and store.get(slot("gemini", keys[1]["id"]), use_env=False) == "k-lab"
    capsys.readouterr()
    assert main(["key", "status"]) == 0
    out = capsys.readouterr().out
    assert "1. Gemini: 設定済み" in out and "2. 研究室: 設定済み" in out and "k-lab" not in out
    assert main(["key", "delete", "--label", "ない"]) == 1
    assert main(["key", "delete", "--label", "研究室"]) == 0
    assert [k["id"] for k in Settings.load().api_keys] == ["default"]
    assert SecretStore().get(slot("gemini", keys[1]["id"]), use_env=False) == ""
