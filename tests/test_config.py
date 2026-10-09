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
    for var in ("SPECTRA_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_paths_follow_spectra_home(isolated_home):
    assert paths.settings_file().is_relative_to(isolated_home)
    assert paths.fonts_dir().is_relative_to(isolated_home)


def test_settings_roundtrip():
    s = Settings(engine="dummy", output_mode="side_by_side", skip_references=False)
    s.save()
    loaded = Settings.load()
    assert loaded.engine == "dummy" and loaded.output_mode == "side_by_side"
    assert loaded.skip_references is False


def test_settings_tolerates_broken_values():
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text(
        json.dumps({"engine": 3, "output_mode": "nope", "text_scale": 5, "skip_references": "yes", "unknown": 1}),
        encoding="utf-8",
    )
    s = Settings.load()
    assert s.engine == "gemini" and s.output_mode == "translated"
    assert s.skip_references is True
    assert not hasattr(s, "text_scale")  # 文字の大きさは自動（設定項目なし）


def test_settings_tolerates_corrupt_file():
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text("{broken", encoding="utf-8")
    assert Settings.load() == Settings()


def test_settings_to_core_options():
    s = Settings(formality="prefer_more", output_mode="interleave", translate_captions=False)
    assert Settings.from_dict({"engine": "deepl", "gemini_model": "x"}).engine == "gemini"  # 旧設定の移行
    assert Settings.from_dict({"gemini_model": "x"}).gemini_model == "gemini-3.5-flash-lite"
    # 以前の既定（3.8 Flash）のままの設定は移行し、新しい形式で自分で選んだ場合は尊重する
    assert Settings.from_dict({"gemini_model": "gemini-3.8-flash"}).gemini_model == "gemini-3.5-flash-lite"
    assert Settings.from_dict({"gemini_model": "gemini-3.8-flash", "schema": 2}).gemini_model == "gemini-3.8-flash"
    assert s.translate_options().formality == Formality.POLITE
    assert s.render_options().output_mode == OutputMode.INTERLEAVE
    assert s.analysis_options(1, 3).translate_captions is False


def test_secret_store_file_fallback(isolated_home):
    store = SecretStore(use_keyring=False)
    assert not store.secure
    store.set("gemini", "  AIzaKey  ")
    assert store.get("gemini") == "AIzaKey"
    assert store.source("gemini") == "設定ファイル"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(store.fallback_file).st_mode) == 0o600
    store.delete("gemini")
    assert store.get("gemini") == "" and not store.fallback_file.exists()


def test_secret_store_env_has_priority(monkeypatch):
    store = SecretStore(use_keyring=False)
    store.set("gemini", "stored")
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert store.get("gemini") == "from-env"
    assert store.get("gemini", use_env=False) == "stored"
    assert "環境変数" in store.source("gemini")


def test_resolve_font_falls_back_to_builtin(tmp_path):
    assert resolve_font("noto-serif").regular is None  # 未ダウンロード
    f = tmp_path / "MyMincho.ttf"
    f.write_bytes(b"x")
    r = resolve_font("custom", str(f))
    assert r.regular == str(f) and r.serif


def test_spectra_home_disables_os_keyring():
    assert not SecretStore().secure  # SPECTRA_HOME が設定されている（conftest）
