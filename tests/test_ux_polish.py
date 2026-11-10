"""使用感の改善（v0.4.0 の後）のテスト。通信はしない。"""

import datetime as dt
import logging
import sys
import threading
import time
from pathlib import Path

import pytest
from test_gui import _distinct_paper, pump, window  # noqa: F401 - window はフィクスチャ

from spectra.translate.errors import QuotaExceededError, reset_time_text
from spectra.translate.registry import ENGINES


@pytest.fixture()
def dialogs(monkeypatch):
    """QMessageBox を表示せずに、文言とボタンを記録する。choose に入れた名前のボタンを押したことにする。"""
    from PySide6.QtWidgets import QMessageBox

    state = {"shown": [], "choose": ""}

    def fake_exec(box):
        state["shown"].append((box.text(), box.informativeText(), [b.text() for b in box.buttons()]))
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(
        QMessageBox, "clickedButton", lambda box: next((b for b in box.buttons() if b.text() == state["choose"]), None)
    )
    return state


# -- API キーの案内 ------------------------------------------------------------------


def test_missing_key_dialog_explains_how_to_get_one(window, dialogs):  # noqa: F811
    _app, w = window
    w.settings.engine = "gemini"
    dialogs["choose"] = "キャンセル"
    assert w._api_key() is None
    title, body, buttons = dialogs["shown"][0]
    assert "API キーが必要です" in title and "無料" in body and "API キーを作成" in body
    assert not body.startswith("で使えます")  # 以前は、無料枠の説明が空のとき「で使えます。」とだけ出ていた
    assert buttons == ["設定を開く", "キャンセル"]


def test_translation_continues_after_the_key_is_saved_in_settings(window, dialogs, monkeypatch):  # noqa: F811
    """案内から設定を開いてキーを入れたら、もう一度「翻訳」を押さなくても、そのまま続ける。"""
    _app, w = window
    w.settings.engine = "gemini"
    dialogs["choose"] = "設定を開く"
    opened = []

    def fake_settings(tab="engines", focus=""):
        opened.append(tab)
        w.secrets.set("gemini", "test-key")  # 設定の画面で、キーを貼り付けて閉じた

    monkeypatch.setattr(w, "show_settings", fake_settings)
    keys = w._api_key()
    assert opened == ["engines"] and keys and keys[0].read() == "test-key"


def test_nothing_starts_if_settings_is_closed_without_a_key(window, dialogs, monkeypatch):  # noqa: F811
    _app, w = window
    w.settings.engine = "gemini"
    dialogs["choose"] = "設定を開く"
    monkeypatch.setattr(w, "show_settings", lambda tab="engines", focus="": None)
    assert w._api_key() is None


def test_settings_shows_the_steps_only_until_a_key_is_saved(window):  # noqa: F811
    _app, w = window
    from spectra.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(w.settings, w.secrets, w.cache, w, sync=w.sync)
    assert not dlg.key_steps.isHidden() and dlg.key_steps.text() == ENGINES["gemini"].key_steps
    w.secrets.set("gemini", "test-key")
    dlg._refresh_key_steps()
    assert dlg.key_steps.isHidden()
    dlg.deleteLater()


def test_empty_library_says_the_key_is_free(window):  # noqa: F811
    _app, w = window
    w.settings.engine = "gemini"
    w.refresh_engine_status()
    assert "無料" in w.library_view.engine_notice.text()


# -- 上限が戻る時刻 ------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="Windows では実行中にタイムゾーンを切り替えられない")
def test_reset_time_is_a_concrete_local_time(monkeypatch):
    from zoneinfo import ZoneInfo

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        tokyo = ZoneInfo("Asia/Tokyo")
        summer_morning = dt.datetime(2026, 7, 1, 9, 0, tzinfo=tokyo).timestamp()
        assert reset_time_text(summer_morning) == "上限は今日の 16:00 ごろに戻ります。"
        summer_evening = dt.datetime(2026, 7, 1, 20, 0, tzinfo=tokyo).timestamp()
        assert reset_time_text(summer_evening) == "上限は明日の 16:00 ごろに戻ります。"
        winter = dt.datetime(2026, 12, 1, 9, 0, tzinfo=tokyo).timestamp()
        assert reset_time_text(winter) == "上限は今日の 17:00 ごろに戻ります。"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_quota_message_names_the_time_instead_of_asking_the_user_to_work_it_out():
    one = QuotaExceededError().user_message()
    assert "ごろに戻ります" in one and "夏時間" not in one and "キャッシュ" in one
    every = QuotaExceededError(all_keys=True).user_message()
    assert "すべての API キー" in every and "ごろに戻ります" in every
    assert QuotaExceededError(hint="自分の案内").user_message().endswith("自分の案内")


# -- 予期しないエラー ----------------------------------------------------------------


def test_unexpected_error_offers_to_open_the_log_folder(window, dialogs, monkeypatch):  # noqa: F811
    _app, w = window
    opened = []
    monkeypatch.setattr("spectra.ui.main_window.QDesktopServices.openUrl", lambda url: opened.append(url.toLocalFile()))
    dialogs["choose"] = "ログフォルダを開く"
    w._alert("予期しないエラーが発生しました", "RuntimeError: boom", log_button=True)
    assert sorted(dialogs["shown"][0][2]) == sorted(["ログフォルダを開く", "閉じる"])  # 並びは OS の流儀で変わる
    from spectra.config import paths

    assert [Path(o) for o in opened] == [paths.log_dir()] and paths.log_dir().is_dir()  # 区切り文字（/ と \）の違いは無視
    dialogs["choose"] = "閉じる"
    w._alert("予期しないエラーが発生しました", "RuntimeError: boom", log_button=True)
    assert len(opened) == 1
    w._alert("ふつうの警告", "本文")  # 既定では、ボタンを足さない
    assert "ログフォルダを開く" not in dialogs["shown"][-1][2]


def test_unhandled_exceptions_are_logged(monkeypatch, caplog):
    from spectra.ui import app as app_module

    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    monkeypatch.setattr(threading, "excepthook", threading.__excepthook__)
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: None)  # テストの出力にトレースバックを出さない
    app_module._install_excepthook()
    with caplog.at_level(logging.CRITICAL, logger="spectra.unhandled"):
        try:
            raise RuntimeError("slot failed")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        t = threading.Thread(target=lambda: 1 / 0, name="worker-x")
        t.start()
        t.join()
    text = caplog.text
    assert "slot failed" in text and "ZeroDivisionError" in text and "worker-x" in text


# -- 追加できなかったファイルの理由 ---------------------------------------------------------


def test_failed_additions_are_listed_with_their_reasons(window, tmp_path, dialogs, monkeypatch):  # noqa: F811
    import pymupdf

    _app, w = window
    good = _distinct_paper(tmp_path / "good.pdf", "A readable paper")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"this is not a pdf")
    locked = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret")
    doc.save(str(locked), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    banners = []
    monkeypatch.setattr(w, "_show_banner", lambda kind, title, body="", *a, **kw: banners.append((kind, title, body)))
    w.add_files([good, broken, locked])
    assert not dialogs["shown"]  # 複数の追加では、ファイルごとのダイアログは出さない
    kind, title, body = banners[-1]
    assert kind == "warning" and title == "1 件の論文を追加しました"
    assert "2 件は追加できませんでした" in body
    assert "パスワードで保護されています: locked.pdf" in body and "壊れているか、PDF ではありません: broken.pdf" in body


def test_one_unexpected_failure_does_not_stop_the_rest(window, tmp_path, dialogs, monkeypatch):  # noqa: F811
    _app, w = window
    first = _distinct_paper(tmp_path / "a.pdf", "First paper to add")
    second = _distinct_paper(tmp_path / "b.pdf", "Second paper to add")
    real = w.library.add

    def flaky(path, *a, **kw):
        if path.name == "a.pdf":
            raise RuntimeError("unexpected")
        return real(path, *a, **kw)

    monkeypatch.setattr(w.library, "add", flaky)
    papers = w.add_files([first, second])
    assert [p.file_name for p in papers] == ["b.pdf"]
