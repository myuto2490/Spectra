"""更新の通知：版の比較・応答の解釈・通信の条件（ネットワークは使わない）。"""

import os
import time

import httpx
import pytest

from spectra import update
from spectra.update import UpdateCheckError, UpdateInfo, check_for_update, is_newer, parse_version

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

URL = "https://github.com/myuto2490/Spectra/releases/tag/v0.5.0"


def release(tag="v0.5.0", url=URL, **kw) -> dict:
    return {"tag_name": tag, "html_url": url, "draft": False, "prerelease": False, **kw}


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def reply(body, status=200):
    return client(lambda r: httpx.Response(status, json=body))


# ---- 版の比較 ---------------------------------------------------------------


def test_parse_version():
    assert parse_version("0.4.0") == (0, 4, 0)
    assert parse_version("v0.10.2") == (0, 10, 2)
    assert parse_version(" 1.2 ") == (1, 2)
    for bad in ("", "latest", "0.4.0rc1", "0.4.0-beta", "1.2.3.4.5", "v", "0..1"):
        assert parse_version(bad) is None


def test_is_newer_compares_numbers_not_text():
    assert is_newer("0.10.0", "0.9.0")
    assert is_newer("v0.4.0", "0.3.0")
    assert not is_newer("0.3.0", "0.3.0")
    assert not is_newer("0.2.9", "0.3.0")
    assert not is_newer("0.3", "0.3.0")  # 桁数が違っても同じ版
    assert is_newer("0.3.1", "0.3")


def test_is_newer_ignores_unreadable_versions():
    assert not is_newer("nightly", "0.3.0")
    assert not is_newer("0.4.0rc1", "0.3.0")  # プレリリースの表記は通知しない
    assert not is_newer("0.4.0", "dev")
    assert is_newer("0.4.0", "0.4.0a1") is False  # 今が 0.4.0 の試作版なら、同じ数字の版は新しくない


# ---- 応答の解釈 -------------------------------------------------------------


def test_new_release_is_reported():
    info = check_for_update("0.3.0", client=reply(release()))
    assert info == UpdateInfo("0.5.0", URL)


def test_same_or_older_release_is_ignored():
    assert check_for_update("0.5.0", client=reply(release())) is None
    assert check_for_update("0.6.0", client=reply(release())) is None


def test_prerelease_and_draft_are_ignored():
    assert check_for_update("0.3.0", client=reply(release(prerelease=True))) is None
    assert check_for_update("0.3.0", client=reply(release(draft=True))) is None
    assert check_for_update("0.3.0", client=reply(release(tag="v0.5.0rc1"))) is None


def test_foreign_url_is_replaced_by_the_releases_page():
    for bad in ("https://evil.example/download", "http://github.com/myuto2490/Spectra/releases/x", "javascript:alert(1)", None, 5):
        info = check_for_update("0.3.0", client=reply(release(url=bad)))
        assert info is not None and info.url == update.RELEASES_URL
    assert update.is_release_url(URL)
    assert not update.is_release_url("https://github.com/myuto2490/Spectra/releases/ x")
    assert not update.is_release_url("https://github.com/myuto2490/Spectra-evil/releases/")


def test_skipped_version_is_not_reported_but_newer_one_is():
    assert check_for_update("0.3.0", client=reply(release()), skipped="0.5.0") is None
    assert check_for_update("0.3.0", client=reply(release()), skipped="v0.5.0") is None
    assert check_for_update("0.3.0", client=reply(release("v0.6.0")), skipped="0.5.0") is not None


def test_no_release_yet_is_not_an_error():
    assert check_for_update("0.3.0", client=client(lambda r: httpx.Response(404, json={"message": "Not Found"}))) is None


def test_request_is_plain_and_sends_nothing_identifying():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=release())

    check_for_update("0.3.0", client=client(handler))
    (req,) = seen
    assert str(req.url) == update.LATEST_URL
    assert req.method == "GET" and not req.content
    assert req.headers["user-agent"] == update.USER_AGENT
    assert set(req.headers) <= {"host", "user-agent", "accept", "accept-encoding", "connection"}


# ---- 失敗 -------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_http_errors_raise(status):
    with pytest.raises(UpdateCheckError):
        check_for_update("0.3.0", client=client(lambda r: httpx.Response(status)))


def test_network_errors_raise():
    def boom(request):
        raise httpx.ConnectError("down")

    def slow(request):
        raise httpx.ReadTimeout("slow")

    for handler in (boom, slow):
        with pytest.raises(UpdateCheckError):
            check_for_update("0.3.0", client=client(handler))


def test_malformed_responses_raise():
    with pytest.raises(UpdateCheckError):
        check_for_update("0.3.0", client=client(lambda r: httpx.Response(200, content=b"<html>")))
    with pytest.raises(UpdateCheckError):
        check_for_update("0.3.0", client=reply([1, 2]))
    with pytest.raises(UpdateCheckError):
        check_for_update("0.3.0", client=client(lambda r: httpx.Response(200, content=b"x" * (update.MAX_BODY + 1))))
    # 必要な項目がない応答は、通知しないだけ
    assert check_for_update("0.3.0", client=reply({"foo": 1})) is None
    assert check_for_update("0.3.0", client=reply({"tag_name": 5})) is None


def test_redirects_are_not_followed():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    with pytest.raises(UpdateCheckError):
        check_for_update("0.3.0", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert calls == [update.LATEST_URL]


# ---- いつ確認するか ---------------------------------------------------------


def test_startup_check_only_when_on_and_a_day_has_passed():
    now = 1_000_000.0
    day = update.CHECK_INTERVAL
    assert update.should_check_on_startup("on", 0, now)  # まだ確認したことがない
    assert update.should_check_on_startup("on", now - day, now)
    assert not update.should_check_on_startup("on", now - day + 1, now)
    assert not update.should_check_on_startup("on", now - 60, now)
    assert update.should_check_on_startup("on", now + 3600, now)  # 時計が戻っていた
    for answer in ("", "off", "unknown"):  # 未回答・オフでは通信しない
        assert not update.should_check_on_startup(answer, 0, now)


def test_is_due_uses_real_clock_by_default():
    assert update.is_due(time.time() - update.CHECK_INTERVAL - 5)
    assert not update.is_due(time.time())


# ---- 画面の部品 -------------------------------------------------------------


@pytest.fixture()
def app():
    pytest.importorskip("PySide6.QtWidgets")
    from spectra.ui.app import create_app

    return create_app([])


def test_banner_buttons(app, monkeypatch):
    from spectra.ui import update_prompt
    from spectra.ui.update_prompt import UpdateBanner

    opened = []
    monkeypatch.setattr(update_prompt, "open_release_page", lambda url: opened.append(url) or True)
    b = UpdateBanner()
    events = []
    b.downloadRequested.connect(lambda u: events.append(("download", u)))
    b.dismissed.connect(lambda: events.append(("later",)))
    b.skipRequested.connect(lambda v: events.append(("skip", v)))

    b.show_update(UpdateInfo("0.5.0", URL), current="0.3.0")
    assert b.isVisible() and "0.5.0" in b._title.text() and "0.3.0" in b._body.text()
    b.download_btn.click()
    assert opened == [URL] and events[-1] == ("download", URL)
    b.later_btn.click()
    assert not b.isVisible() and events[-1] == ("later",)
    b.show_update(UpdateInfo("0.5.0", URL))
    b.skip_btn.click()
    assert not b.isVisible() and events[-1] == ("skip", "0.5.0")


def test_open_release_page_refuses_foreign_urls(app, monkeypatch):
    from PySide6.QtGui import QDesktopServices

    from spectra.ui.update_prompt import open_release_page

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda u: opened.append(u.toString()) or True)
    assert not open_release_page("https://evil.example/")
    assert not open_release_page("file:///etc/passwd")
    assert opened == []
    assert open_release_page(URL) and opened == [URL]


def test_ask_enable_update_check(app, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from spectra.ui.update_prompt import ask_enable_update_check

    def run(label_to_click):
        def fake_exec(box):
            for b in box.buttons():
                if b.text() == label_to_click:
                    box._clicked = b
            return 0

        monkeypatch.setattr(QMessageBox, "exec", fake_exec)
        monkeypatch.setattr(QMessageBox, "clickedButton", lambda box: getattr(box, "_clicked", None))
        return ask_enable_update_check()

    assert run("確認する") is True
    assert run("確認しない") is False
    assert run("(閉じた)") is False  # ボタンを押さずに閉じたらオフ


def test_start_check_runs_in_background(app):
    from spectra.ui import update_prompt

    found, none, errors = [], [], []
    t = update_prompt.start_check(found.append, errors.append, lambda: none.append(1), current="0.3.0", client=reply(release()))
    _pump(app, lambda: found or errors)
    assert found == [UpdateInfo("0.5.0", URL)] and not errors and not none

    found.clear()
    update_prompt.start_check(found.append, errors.append, lambda: none.append(1), current="0.5.0", client=reply(release()))
    _pump(app, lambda: none)
    assert none and not found

    update_prompt.start_check(found.append, errors.append, current="0.3.0", client=client(lambda r: httpx.Response(500)))
    _pump(app, lambda: errors)
    assert isinstance(errors[0], UpdateCheckError)
    assert t is not None


def _pump(app, until, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end and not until():
        app.processEvents()
        time.sleep(0.01)
    assert until(), "background task did not finish"
