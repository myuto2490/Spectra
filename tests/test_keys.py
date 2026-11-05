"""複数の API キー: 保存場所・使う順・1 日の上限に達したときの自動切り替え・上限の記録（通信しない）。"""

import datetime as dt
import json

import httpx
import pytest
from test_translators import _model_of, _quota_body, client, echo_handler, no_sleep

from spectra.config import quota as quota_mod
from spectra.config.quota import QuotaBook, next_reset
from spectra.config.secrets import KeyRef, SecretStore, key_chain, slot
from spectra.config.settings import Settings
from spectra.translate import registry
from spectra.translate.errors import AuthenticationError, QuotaExceededError
from spectra.translate.gemini import GeminiTranslator

K1, K2, K3 = "1" * 32, "2" * 32, "3" * 32
PER_DAY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
PER_MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECTRA_HOME", str(tmp_path))
    for var in ("SPECTRA_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def refs(*pairs) -> list[KeyRef]:
    return [KeyRef(key_id, label, lambda v=value: v) for key_id, label, value in pairs]


def server(rules, seen):
    """rules: {(キーの値, モデル): 応答の種類}。指定のない組は訳文を返す。"""

    def handler(req: httpx.Request):
        key, model = req.headers["x-goog-api-key"], _model_of(req)
        seen.append((key, model))
        kind = rules.get((key, model)) or rules.get((key, "*"))
        if kind == "day":
            return httpx.Response(429, json=_quota_body(PER_DAY))
        if kind == "minute":
            return httpx.Response(429, json=_quota_body(PER_MINUTE, "1s"))
        if kind == "invalid":
            return httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid.",
                                                       "details": [{"reason": "API_KEY_INVALID"}]}})
        if kind == "busy":
            return httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE", "message": "overloaded"}})
        return echo_handler()(req)

    return handler


def translator(handler, keys, book=None, **kw) -> GeminiTranslator:
    return no_sleep(GeminiTranslator(client=client(handler), keys=keys, quota=book, **kw))


LAB, MINE, SPARE = ("lab", "研究室", "k-lab"), ("mine", "個人", "k-mine"), ("spare", "予備", "k-spare")


# -- 切り替え ------------------------------------------------------------------------


def test_daily_quota_switches_to_the_next_key_before_the_model(tmp_path):
    seen, notes = [], []
    book = QuotaBook(tmp_path / "quota.json", clock=Clock())
    t = translator(server({("k-lab", "*"): "day"}, seen), refs(LAB, MINE), book)
    t.on_notice = notes.append
    assert t.translate(["a"]) == ["訳:a"]
    assert seen == [("k-lab", "gemini-3.5-flash-lite"), ("k-mine", "gemini-3.5-flash-lite")]  # モデルはそのまま
    assert t.model == "gemini-3.5-flash-lite" and t.key.id == "mine"
    assert notes == ["API キー「研究室」が今日の上限に達したため、「個人」に切り替えました。"] == t.notices
    assert book.until("lab", "gemini-3.5-flash-lite") is not None
    assert "k-lab" not in (tmp_path / "quota.json").read_text(encoding="utf-8")  # キーの中身は書かない


def test_exhausted_key_is_not_tried_again_until_reset(tmp_path):
    clock = Clock()
    book = QuotaBook(tmp_path / "quota.json", clock=clock)
    book.mark("lab", "gemini-3.5-flash-lite")
    seen = []
    t = translator(server({}, seen), refs(LAB, MINE), book)
    assert t.translate(["a"]) == ["訳:a"]
    assert seen == [("k-mine", "gemini-3.5-flash-lite")] and not t.notices  # 上限のキーに問い合わせない
    clock.now = next_reset(clock.now) + 1
    seen.clear()
    translator(server({}, seen), refs(LAB, MINE), book).translate(["a"])
    assert seen == [("k-lab", "gemini-3.5-flash-lite")]  # 上限が戻ったら、また先頭のキーから


def test_all_keys_exhausted_falls_back_to_the_next_model_then_stops(tmp_path):
    seen = []
    book = QuotaBook(tmp_path / "quota.json", clock=Clock())
    rules = {("k-lab", "gemini-3.5-flash-lite"): "day", ("k-mine", "gemini-3.5-flash-lite"): "day"}
    t = translator(server(rules, seen), refs(LAB, MINE), book)
    assert t.translate(["a"]) == ["訳:a"]
    assert seen[-1] == ("k-lab", "gemini-3.1-flash-lite")  # 次のモデルでは、また先頭のキーから
    assert any("Flash-Lite に切り替えて" in n for n in t.notices)

    seen.clear()
    t = translator(server({("k-lab", "*"): "day", ("k-mine", "*"): "day"}, seen), refs(LAB, MINE), QuotaBook(tmp_path / "q2.json", clock=Clock()))
    with pytest.raises(QuotaExceededError) as e:
        t.translate(["a"])
    assert "すべての API キー" in e.value.user_message()


def test_switch_off_uses_only_the_first_key(tmp_path):
    seen = []
    t = translator(server({("k-lab", "*"): "day"}, seen), refs(LAB, MINE), QuotaBook(tmp_path / "q.json", clock=Clock()),
                   key_auto_switch=False, auto_fallback=False)
    with pytest.raises(QuotaExceededError) as e:
        t.translate(["a"])
    assert {k for k, _ in seen} == {"k-lab"}
    assert "すべての API キー" not in e.value.user_message()


def test_per_minute_limit_waits_then_switches_without_recording(tmp_path):
    seen = []
    book = QuotaBook(tmp_path / "q.json", clock=Clock())
    t = translator(server({("k-lab", "*"): "minute"}, seen), refs(LAB, MINE), book, auto_fallback=False)
    assert t.translate(["a"]) == ["訳:a"]
    assert seen.count(("k-lab", "gemini-3.5-flash-lite")) == 1 + len(t.fallback_retry_delays)  # 待って再試行してから
    assert t.key.id == "mine" and "回数の上限" in t.notices[0]
    assert book.exhausted_keys() == {}  # 1 分の上限は記録しない


def test_invalid_key_is_skipped_only_after_switching(tmp_path):
    seen = []
    book = QuotaBook(tmp_path / "q.json", clock=Clock())
    t = translator(server({("k-lab", "*"): "day", ("k-mine", "*"): "invalid"}, seen), refs(LAB, MINE, SPARE), book)
    assert t.translate(["a"]) == ["訳:a"]
    assert t.key.id == "spare" and any("「個人」が正しくない" in n for n in t.notices)
    with pytest.raises(AuthenticationError):  # 先頭のキーが正しくなければ、今までどおり止める
        translator(server({("k-lab", "*"): "invalid"}, []), refs(LAB, MINE), QuotaBook(tmp_path / "q2.json", clock=Clock())).translate(["a"])


def test_server_errors_do_not_switch_keys(tmp_path):
    seen = []
    t = translator(server({("k-lab", "gemini-3.5-flash-lite"): "busy"}, seen), refs(LAB, MINE), QuotaBook(tmp_path / "q.json", clock=Clock()))
    assert t.translate(["a"]) == ["訳:a"]
    assert {k for k, _ in seen} == {"k-lab"} and t.model == "gemini-3.1-flash-lite"


def test_unreadable_key_is_skipped(tmp_path):
    keys = [KeyRef("lab", "研究室", lambda: ""), *refs(MINE)]
    seen = []
    assert translator(server({}, seen), keys, QuotaBook(tmp_path / "q.json", clock=Clock())).translate(["a"]) == ["訳:a"]
    assert seen == [("k-mine", "gemini-3.5-flash-lite")]


def test_keys_do_not_change_the_cache_namespace():
    with_keys = GeminiTranslator(keys=refs(LAB, MINE))
    assert with_keys.cache_namespace() == GeminiTranslator("x").cache_namespace()


def test_registry_passes_keys_only_to_gemini():
    keys = refs(LAB)
    assert registry.create("gemini", keys=keys, quota=None, key_auto_switch=False).keys == keys
    registry.create("dummy", keys=keys, quota=None, key_auto_switch=False)  # 擬似翻訳は受け取らない


# -- 上限の記録 ----------------------------------------------------------------------


def test_next_reset_is_midnight_in_pacific_time():
    zone = pytest.importorskip("zoneinfo").ZoneInfo(quota_mod.RESET_ZONE)
    summer = dt.datetime(2026, 7, 1, 10, 0, tzinfo=zone).timestamp()  # 夏時間: 日本時間の 16 時
    winter = dt.datetime(2026, 12, 1, 23, 59, tzinfo=zone).timestamp()  # 日本時間の 17 時
    jst = dt.timezone(dt.timedelta(hours=9))
    assert dt.datetime.fromtimestamp(next_reset(summer), jst).hour == 16
    assert dt.datetime.fromtimestamp(next_reset(winter), jst).hour == 17
    assert next_reset(winter) - winter == 60


def test_quota_book_ignores_broken_or_stale_records(tmp_path):
    clock = Clock()
    path = tmp_path / "quota.json"
    path.write_text(json.dumps({"lab|m": clock.now + 60, "old|m": clock.now - 1, "far|m": clock.now + 30 * 86400,
                                "nobar": clock.now + 60, "bad|m": "x"}), encoding="utf-8")
    book = QuotaBook(path, clock=clock)
    assert book.exhausted_keys() == {"lab": clock.now + 60}
    book.forget("lab")
    assert book.exhausted_keys() == {}
    path.write_text("{broken", encoding="utf-8")
    assert book.until("lab", "m") is None


# -- 保存場所と使う順 ------------------------------------------------------------------


def test_default_key_keeps_the_old_storage_name():
    assert slot("gemini") == "gemini" and slot("gemini", K1) == f"gemini-{K1}"


def test_key_chain_order_labels_and_legacy_key():
    store = SecretStore(use_keyring=False)
    assert key_chain(Settings(), store) == []
    store.set("gemini", "k-old")  # 以前の版で保存したキー（目録はまだない）
    chain = key_chain(Settings(), store)
    assert [(k.id, k.label, k.read()) for k in chain] == [("default", "Gemini", "k-old")]

    store.set(slot("gemini", K1), "k-lab")
    s = Settings(api_keys=[{"id": K1, "provider": "gemini", "label": "研究室"}, {"id": "default", "provider": "gemini", "label": ""}])
    assert [(k.id, k.label, k.read()) for k in key_chain(s, store)] == [(K1, "研究室", "k-lab"), ("default", "Gemini 2", "k-old")]
    assert "k-lab" not in json.dumps(s.api_keys)


def test_environment_variable_is_the_only_key(monkeypatch):
    store = SecretStore(use_keyring=False)
    store.set("gemini", "k-old")
    monkeypatch.setenv("GEMINI_API_KEY", "k-env")
    chain = key_chain(Settings(api_keys=[{"id": K1, "provider": "gemini", "label": "研究室"}]), store)
    assert [(k.id, k.read()) for k in chain] == [("env", "k-env")] and "GEMINI_API_KEY" in chain[0].label


def test_keys_are_read_only_when_used():
    reads = []
    keys = [KeyRef("lab", "研究室", lambda: reads.append("lab") or "k-lab"), KeyRef("mine", "個人", lambda: reads.append("mine") or "k-mine")]
    t = translator(server({}, []), keys)
    assert reads == []
    t.translate(["a"])
    assert reads == ["lab"]  # 2 本目は、切り替えるまで読まない
