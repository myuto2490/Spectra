"""設定の「翻訳エンジン」のキーの一覧と、「詳細」の更新の確認（通信しない）。"""

import json

import pytest

from spectra.config.secrets import SecretStore, slot


@pytest.fixture()
def dialog(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECTRA_HOME", str(tmp_path / "home"))
    for var in ("SPECTRA_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    from spectra.config.settings import Settings
    from spectra.translate.cache import TranslationCache
    from spectra.ui.app import create_app
    from spectra.ui.settings_dialog import KeyField, SettingsDialog

    app = create_app([])
    monkeypatch.setattr(KeyField, "verify", lambda self: None)  # ネットワークに出ない
    settings, secrets = Settings(), SecretStore(use_keyring=False)
    cache = TranslationCache(tmp_path / "cache.sqlite3")

    def make():
        return SettingsDialog(settings, secrets, cache)

    yield app, make, settings, secrets
    cache.close()


def test_keys_can_be_added_named_reordered_and_removed(dialog, tmp_path):
    app, make, settings, secrets = dialog
    d = make()
    assert list(d.key_fields) == ["gemini"] and not d.key_fields["gemini"].up_btn.isVisibleTo(d)
    first = d.key_fields["gemini"]
    first.edit.setText("k-first")
    first._save()
    assert secrets.get("gemini", use_env=False) == "k-first"  # 以前からの保存場所のまま

    d._add_key()
    assert len(settings.api_keys) == 2 and settings.api_keys[0]["id"] == "default"
    new_id = settings.api_keys[1]["id"]
    second = d.key_fields[slot("gemini", new_id)]
    second.edit.setText("k-second")
    second._save()
    second.name.setText("研究室")
    second.name.editingFinished.emit()
    assert settings.api_keys[1]["label"] == "研究室"
    assert secrets.get(slot("gemini", new_id), use_env=False) == "k-second"

    d._move_key(new_id, -1)
    assert [e["id"] for e in settings.api_keys] == [new_id, "default"]
    assert list(d.key_fields)[0] == slot("gemini", new_id) and d.key_fields[slot("gemini", new_id)].up_btn.isEnabled() is False

    d.key_fields[slot("gemini", new_id)]._delete()  # 2 つ以上あるときは、一覧から外してキーも消す
    assert [e["id"] for e in settings.api_keys] == ["default"]
    assert secrets.get(slot("gemini", new_id), use_env=False) == ""
    saved = (tmp_path / "home" / "config" / "settings.json").read_text(encoding="utf-8")
    assert json.loads(saved)["api_keys"] == settings.api_keys
    assert "k-first" not in saved and "k-second" not in saved  # 設定ファイルにキーの中身を書かない
    d.close()


def test_exhausted_key_is_marked_in_the_list(dialog):
    app, make, settings, secrets = dialog
    from spectra.config.quota import QuotaBook

    other = "2" * 32
    settings.api_keys = [{"id": "default", "provider": "gemini", "label": ""}, {"id": other, "provider": "gemini", "label": "予備"}]
    secrets.set("gemini", "k1")
    secrets.set(slot("gemini", other), "k2")
    QuotaBook().mark("default", "gemini-3.5-flash-lite")
    d = make()
    assert "今日の上限" in d.key_fields["gemini"].status.text()
    assert "今日の上限" not in d.key_fields[slot("gemini", other)].status.text()
    d.key_auto_switch.setChecked(False)
    assert settings.key_auto_switch is False
    d.close()


def test_update_check_setting_and_check_now(dialog, monkeypatch):
    app, make, settings, secrets = dialog
    from spectra import update
    from spectra.ui import update_prompt

    d = make()
    assert settings.update_check == "ask" and not d.update_check.isChecked()  # 未回答はオフとして表示
    d.update_check.setChecked(True)
    assert settings.update_check == "on"
    d.update_check.setChecked(False)
    assert settings.update_check == "off"

    calls = []

    def fake_start(on_found, on_error=None, on_none=None, **kw):
        calls.append(kw)
        on_found(update.UpdateInfo("9.9.9", "https://github.com/myuto2490/Spectra/releases/tag/v9.9.9"))

    monkeypatch.setattr(update_prompt, "start_check", fake_start)
    opened = []
    monkeypatch.setattr(update_prompt, "open_release_page", lambda url: opened.append(url) or True)
    d.check_for_update_now()
    assert "9.9.9" in d.update_status.text() and not d.download_btn.isHidden()
    assert opened == []  # 勝手にブラウザを開かない
    d.download_btn.click()
    assert opened == ["https://github.com/myuto2490/Spectra/releases/tag/v9.9.9"]

    def failing_start(on_found, on_error=None, on_none=None, **kw):
        on_error(update.UpdateCheckError("つながりません"))

    monkeypatch.setattr(update_prompt, "start_check", failing_start)
    d.check_for_update_now()
    assert "つながりません" in d.update_status.text()
    d.close()
