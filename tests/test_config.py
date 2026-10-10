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
    # 未ダウンロードのフォントを選んでいる場合は、同梱の Noto Sans JP（日本語の字形）を使う
    fallback = resolve_font("noto-serif")
    assert fallback.regular and fallback.regular.endswith("NotoSansJP-Regular.otf")
    assert fallback.bold and fallback.bold.endswith("NotoSansJP-Bold.otf")
    f = tmp_path / "MyMincho.ttf"
    f.write_bytes(b"x")
    r = resolve_font("custom", str(f))
    assert r.regular == str(f) and r.serif


def test_spectra_home_disables_os_keyring():
    assert not SecretStore().secure  # SPECTRA_HOME が設定されている（conftest）


class _FakeKeyring:
    """macOS のキーチェーンの代わり。読み書きの回数を数える。"""

    def __init__(self, value: str):
        self.store = {("Spectra", "gemini-api-key"): value}
        self.reads = 0
        self.writes = 0

    def get_password(self, service, account):
        self.reads += 1
        return self.store.get((service, account))

    def set_password(self, service, account, value):
        self.writes += 1
        self.store[(service, account)] = value

    def delete_password(self, service, account):
        self.store.pop((service, account), None)


def test_keychain_is_read_once_and_reclaimed_once(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = _FakeKeyring("AIzaFromDevBuild")
    store = SecretStore(use_keyring=False)
    store._keyring = fake  # 別のアプリ（開発中の Python）が保存した項目がある状態

    assert store.get("gemini") == "AIzaFromDevBuild"
    assert store.get("gemini") == "AIzaFromDevBuild"
    assert fake.reads == 1  # 2 回目以降は覚えたキーを使う
    assert fake.writes == 1  # このアプリで保存し直して持ち主になる

    # 次の起動（同じアプリ）：読み出しは 1 回、保存し直しはしない
    store2 = SecretStore(use_keyring=False)
    store2._keyring = fake
    assert store2.get("gemini") == "AIzaFromDevBuild"
    assert fake.writes == 1

    # アップデートなどでアプリが変わると、もう一度だけ保存し直す
    monkeypatch.setattr(SecretStore, "_identity", staticmethod(lambda: "new-app|0.1.1"))
    store3 = SecretStore(use_keyring=False)
    store3._keyring = fake
    store3.get("gemini")
    assert fake.writes == 2


def test_delete_clears_cached_key():
    store = SecretStore(use_keyring=False)
    store.set("gemini", "AIzaKey")
    assert store.get("gemini") == "AIzaKey"
    store.delete("gemini")
    assert store.get("gemini") == ""
