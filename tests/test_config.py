import json
import os
import stat
import sys

import pytest

from spectra.config import paths
from spectra.config.fonts import resolve_font
from spectra.config.secrets import SecretStore
from spectra.config.settings import Settings
from spectra.core.options import Formality, OutputMode


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECTRA_HOME", str(tmp_path))
    for var in ("SPECTRA_DEEPL_API_KEY", "DEEPL_AUTH_KEY", "SPECTRA_GOOGLE_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_paths_follow_spectra_home(isolated_home):
    assert paths.settings_file().is_relative_to(isolated_home)
    assert paths.fonts_dir().is_relative_to(isolated_home)


def test_settings_roundtrip():
    s = Settings(engine="google", output_mode="side_by_side", skip_references=False, text_scale=0.9)
    s.save()
    loaded = Settings.load()
    assert loaded.engine == "google" and loaded.output_mode == "side_by_side"
    assert loaded.skip_references is False and loaded.text_scale == 0.9


def test_settings_tolerates_broken_values():
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text(
        json.dumps({"engine": 3, "output_mode": "nope", "text_scale": 5, "skip_references": "yes", "unknown": 1}),
        encoding="utf-8",
    )
    s = Settings.load()
    assert s.engine == "deepl" and s.output_mode == "translated"
    assert s.text_scale == 1.1 and s.skip_references is True


def test_settings_tolerates_corrupt_file():
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text("{broken", encoding="utf-8")
    assert Settings.load() == Settings()


def test_settings_to_core_options():
    s = Settings(formality="prefer_more", output_mode="interleave", translate_captions=False)
    assert s.translate_options().formality == Formality.POLITE
    assert s.render_options().output_mode == OutputMode.INTERLEAVE
    assert s.analysis_options(1, 3).translate_captions is False


def test_secret_store_file_fallback(isolated_home):
    store = SecretStore(use_keyring=False)
    assert not store.secure
    store.set("deepl", "  abc:fx  ")
    assert store.get("deepl") == "abc:fx"
    assert store.source("deepl") == "設定ファイル"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(store.fallback_file).st_mode) == 0o600
    store.delete("deepl")
    assert store.get("deepl") == "" and not store.fallback_file.exists()


def test_secret_store_env_has_priority(monkeypatch):
    store = SecretStore(use_keyring=False)
    store.set("google", "stored")
    monkeypatch.setenv("GOOGLE_API_KEY", "from-env")
    assert store.get("google") == "from-env"
    assert store.get("google", use_env=False) == "stored"
    assert "環境変数" in store.source("google")


def test_resolve_font_falls_back_to_builtin(tmp_path):
    assert resolve_font("noto-serif").regular is None  # 未ダウンロード
    f = tmp_path / "MyMincho.ttf"
    f.write_bytes(b"x")
    r = resolve_font("custom", str(f))
    assert r.regular == str(f) and r.serif
