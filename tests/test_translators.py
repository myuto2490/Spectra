import json

import httpx
import pytest

from spectra.core.markup import parse
from spectra.core.options import Formality, TranslateOptions
from spectra.translate import registry
from spectra.translate.cache import TranslationCache
from spectra.translate.dummy import DummyTranslator
from spectra.translate.errors import (
    AuthenticationError,
    CancelledError,
    MissingKeyError,
    NetworkError,
    QuotaExceededError,
    RateLimitError,
    ServiceError,
)
from spectra.translate.gemini import (
    API_BASE,
    DEFAULT_MODEL,
    MODELS,
    GeminiTranslator,
    system_prompt,
)
from spectra.translate.service import PartialTranslationError, plan, translate_texts


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def no_sleep(t):
    t.sleep = lambda s: None
    return t


def gemini_reply(items: list[dict], finish: str = "STOP") -> httpx.Response:
    """Gemini の generateContent の応答を模す。"""
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"parts": [{"text": json.dumps(items, ensure_ascii=False)}], "role": "model"},
                    "finishReason": finish,
                }
            ]
        },
    )


def echo_handler(transform=lambda t: "訳:" + t, calls=None):
    """送られた各項目を transform して返すハンドラ。"""

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        items = json.loads(body["contents"][0]["parts"][0]["text"])
        if calls is not None:
            calls.append({"url": str(req.url), "headers": req.headers, "body": body, "items": items})
        return gemini_reply([{"id": it["id"], "text": transform(it["text"])} for it in items])

    return handler


def gemini(handler, **kw) -> GeminiTranslator:
    return no_sleep(GeminiTranslator("gkey", client=client(handler), **kw))


# ---- Gemini：リクエストと応答 ---------------------------------------------------


def test_request_shape():
    calls = []
    t = gemini(echo_handler(calls=calls))
    out = t.translate(['a &amp; b <x id="0"/>', "c"])
    assert out == ['訳:a &amp; b <x id="0"/>', "訳:c"]
    c = calls[0]
    assert c["url"] == f"{API_BASE}/models/{DEFAULT_MODEL}:generateContent"
    assert c["headers"]["x-goog-api-key"] == "gkey"
    # 送信時は XML エスケープを戻し、タグはそのまま
    assert c["items"] == [{"id": 0, "text": 'a & b <x id="0"/>'}, {"id": 1, "text": "c"}]
    cfg = c["body"]["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    assert cfg["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert "である調" in c["body"]["systemInstruction"]["parts"][0]["text"]


def test_polite_style_and_cache_namespace():
    plain = GeminiTranslator("k")
    polite = GeminiTranslator("k", TranslateOptions(formality=Formality.POLITE))
    assert "です・ます調" in system_prompt(polite.options)
    assert plain.cache_namespace() != polite.cache_namespace()
    assert GeminiTranslator("k", model="gemini-3.6-flash").cache_namespace() != plain.cache_namespace()


def test_unknown_model_falls_back_to_default():
    assert GeminiTranslator("k", model="nope").model == DEFAULT_MODEL


def test_tag_variants_are_normalized():
    t = gemini(echo_handler(lambda s: "訳 <x id='0'></x> と a<b"))
    out = t.translate(['x <x id="0"/>'])[0]
    assert out == '訳 <x id="0"/> と a&lt;b'
    tokens = parse(out, {0})
    assert [tk.run_id for tk in tokens if tk.run_id is not None] == [0]
    assert "".join(tk.text for tk in tokens) == "訳  と a<b"


def test_thoughts_are_ignored():
    def handler(req):
        items = json.loads(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "考え中", "thought": True},
            {"text": json.dumps([{"id": i["id"], "text": "訳"} for i in items])},
        ]}, "finishReason": "STOP"}]})

    assert gemini(handler).translate(["a"]) == ["訳"]


def test_count_mismatch_is_retried_by_splitting():
    calls = []

    def handler(req):
        items = json.loads(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        calls.append(len(items))
        if len(items) > 1:
            return gemini_reply([{"id": 0, "text": "1 件だけ"}])  # 件数が足りない
        return gemini_reply([{"id": 0, "text": "訳" + items[0]["text"]}])

    assert gemini(handler).translate(["a", "b", "c"]) == ["訳a", "訳b", "訳c"]
    assert calls[0] == 3


def test_blocked_item_becomes_none():
    def handler(req):
        items = json.loads(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        if any(i["text"] == "bad" for i in items):
            return httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]})
        return gemini_reply([{"id": i["id"], "text": "訳"} for i in items])

    assert gemini(handler).translate(["ok", "bad"]) == ["訳", None]


def test_degenerate_item_is_retried_alone():
    garbage = "".join(chr(c) for c in range(0x0334, 0x0350))
    calls = []

    def handler(req):
        items = json.loads(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        calls.append(len(items))
        if len(items) > 1:
            return gemini_reply([{"id": i["id"], "text": "訳" + (garbage if i["text"] == "b" else "")} for i in items])
        return gemini_reply([{"id": 0, "text": "訳し直し"}])

    assert gemini(handler).translate(["a", "b"]) == ["訳", "訳し直し"]
    assert calls == [2, 1]


def test_thinking_config_dropped_if_unsupported():
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append("thinkingConfig" in body["generationConfig"])
        if "thinkingConfig" in body["generationConfig"]:
            return httpx.Response(400, json={"error": {"code": 400, "message": "thinking level is not supported"}})
        items = json.loads(body["contents"][0]["parts"][0]["text"])
        return gemini_reply([{"id": i["id"], "text": "訳"} for i in items])

    assert gemini(handler).translate(["a"]) == ["訳"]
    assert calls == [True, False]


# ---- Gemini：エラー ------------------------------------------------------------


def test_invalid_key():
    body = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT",
                      "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}}
    with pytest.raises(AuthenticationError):
        gemini(lambda r: httpx.Response(400, json=body)).verify()


def test_verify_does_not_generate():
    seen = []

    def handler(req):
        seen.append((req.method, str(req.url)))
        return httpx.Response(200, json={"name": f"models/{DEFAULT_MODEL}"})

    assert gemini(handler).verify() is None
    assert seen == [("GET", f"{API_BASE}/models/{DEFAULT_MODEL}")]


def _quota_body(quota_id: str, delay: str = "12s") -> dict:
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota exceeded", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
    ]}}


def test_daily_quota_is_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, json=_quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier"))

    with pytest.raises(QuotaExceededError) as e:
        gemini(handler, auto_fallback=False).translate(["a"])
    assert len(calls) == 1
    message = e.value.user_message()
    assert message.startswith("無料枠の 1 日あたりの上限に達しました")
    assert "今月" not in message


def test_per_minute_limit_waits_retry_delay_then_succeeds():
    calls, waits = [], []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json=_quota_body("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "12s"))
        items = json.loads(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        return gemini_reply([{"id": i["id"], "text": "訳"} for i in items])

    t = GeminiTranslator("k", client=client(handler), auto_fallback=False)
    t.sleep = waits.append
    assert t.translate(["a"]) == ["訳"]
    assert waits == [12.0]


def test_rate_limit_gives_up():
    t = gemini(lambda r: httpx.Response(429, json=_quota_body("PerMinute")), auto_fallback=False)
    with pytest.raises(RateLimitError):
        t.translate(["a"])


def test_missing_key():
    with pytest.raises(MissingKeyError):
        GeminiTranslator("", client=client(lambda r: httpx.Response(200))).translate(["a"])


def test_network_error_is_retried_and_reported():
    def handler(req):
        raise httpx.ConnectError("boom", request=req)

    with pytest.raises(NetworkError):
        gemini(handler).translate(["a"])


def test_cancel():
    t = gemini(echo_handler())
    t.cancel_event.set()
    with pytest.raises(CancelledError):
        t.translate(["a"])


def test_batching_respects_limits():
    t = GeminiTranslator("k")
    batches = list(t.batches(["x" * 10] * 60))
    assert t.model == DEFAULT_MODEL == "gemini-3.5-flash-lite"
    assert [len(b) for b in batches] == [30, 30]
    assert all(sum(map(len, b)) <= t.max_chars for b in t.batches(["y" * 3000] * 10))


# ---- service / cache -----------------------------------------------------


def test_dummy_keeps_placeholders():
    out = DummyTranslator().translate(['We set <x id="0"/> and <x id="1"/> to one.'])[0]
    assert out.index('<x id="0"/>') < out.index('<x id="1"/>')


def test_cache_avoids_second_request(tmp_path):
    calls = []
    cache = TranslationCache(tmp_path / "c.sqlite3")
    t = gemini(echo_handler(calls=calls))
    first = translate_texts(["a", "b", "a"], t, cache)
    assert first == {"a": "訳:a", "b": "訳:b"} and len(calls) == 1
    second = translate_texts(["a", "b", "c"], t, cache)
    assert second["c"] == "訳:c" and len(calls) == 2 and calls[1]["items"] == [{"id": 0, "text": "c"}]
    p = plan(["a", "b", "c", "d"], t.cache_namespace(), cache)
    assert p.cached_chars == 3 and p.pending == ["d"]


def test_untranslatable_items_are_not_cached():
    def handler(req):
        items = json.loads(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        if len(items) == 1 and items[0]["text"] == "bad":
            return httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]})
        if len(items) > 1:
            return httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]})
        return gemini_reply([{"id": 0, "text": "訳"}])

    cache = TranslationCache(None)
    results = translate_texts(["ok", "bad"], gemini(handler), cache)
    assert results == {"ok": "訳"} and cache.count() == 1


def test_partial_failure_keeps_finished_batches():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) > 1:
            return httpx.Response(429, json=_quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
        return echo_handler()(req)

    cache = TranslationCache(None)
    with pytest.raises(PartialTranslationError) as e:
        translate_texts([f"t{i}" for i in range(40)], gemini(handler, auto_fallback=False), cache)
    assert len(e.value.results) == 30 and isinstance(e.value.cause, QuotaExceededError)
    assert cache.count() == 30


def test_registry():
    assert [e.engine_id for e in registry.public_engines()][0] == "gemini"
    t = registry.create("gemini", api_key="x", model="gemini-3.6-flash", auto_fallback=False)
    assert isinstance(t, GeminiTranslator) and t.model == "gemini-3.6-flash" and not t.auto_fallback
    assert isinstance(registry.create("dummy"), DummyTranslator)
    with pytest.raises(ValueError):
        registry.create("deepl")


# ---- 自動切り替え -------------------------------------------------------------


def _model_of(req: httpx.Request) -> str:
    return str(req.url).split("/models/")[1].split(":")[0]


def test_fallback_when_overloaded():
    seen = []

    def handler(req):
        model = _model_of(req)
        seen.append(model)
        if model == "gemini-3.5-flash-lite":
            return httpx.Response(503, json={"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}})
        return echo_handler()(req)

    t = gemini(handler)
    assert t.translate(["a"]) == ["訳:a"]
    assert t.model == "gemini-3.1-flash-lite"
    assert seen.count("gemini-3.5-flash-lite") == 3  # 2 回だけ再試行してから切り替える
    assert t.notices and "混雑" in t.notices[0]


def test_fallback_on_daily_quota_and_cache_namespace_is_stable():
    def handler(req):
        if _model_of(req) == "gemini-3.5-flash-lite":
            return httpx.Response(429, json=_quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
        return echo_handler()(req)

    t = gemini(handler)
    before = t.cache_namespace()
    assert t.translate(["a"]) == ["訳:a"]
    assert t.cache_namespace() == before


def test_fallback_never_uses_38_flash():
    seen = []

    def handler(req):
        seen.append(_model_of(req))
        return httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE"}})

    with pytest.raises(ServiceError):
        gemini(handler).translate(["a"])
    assert "gemini-3.8-flash" not in seen
    assert set(seen) == {m for m in MODELS if MODELS[m].fallback}


def test_38_flash_can_still_be_chosen_explicitly():
    seen = []

    def handler(req):
        seen.append(_model_of(req))
        return echo_handler()(req)

    gemini(handler, model="gemini-3.8-flash").translate(["a"])
    assert seen == ["gemini-3.8-flash"]
    assert MODELS["gemini-3.8-flash"].caution


def test_thinking_levels_are_tried_in_order():
    sent = []

    def handler(req):
        cfg = json.loads(req.content)["generationConfig"]
        sent.append(cfg.get("thinkingConfig", {}).get("thinkingLevel"))
        if cfg.get("thinkingConfig", {}).get("thinkingLevel") == "minimal":
            return httpx.Response(400, json={"error": {"code": 400, "message": "thinking level minimal is not supported"}})
        return echo_handler()(req)

    gemini(handler, model="gemini-3.6-flash").translate(["a"])
    assert sent == ["minimal", "low"]


def test_bold_tags_survive_round_trip():
    calls = []
    t = gemini(echo_handler(lambda s: s.replace("Results.", "結果。"), calls=calls))
    out = t.translate(["<b>Results.</b> We find &lt;1 <x id=\"0\"/>"])[0]
    assert calls[0]["items"][0]["text"] == '<b>Results.</b> We find <1 <x id="0"/>'
    assert out == '<b>結果。</b> We find &lt;1 <x id="0"/>'
