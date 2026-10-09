import json

import httpx
import pytest

from spectra.core.options import Formality, TranslateOptions
from spectra.translate import registry
from spectra.translate.base import Usage
from spectra.translate.cache import TranslationCache
from spectra.translate.deepl import FREE_URL, PRO_URL, DeepLTranslator
from spectra.translate.dummy import DummyTranslator
from spectra.translate.errors import (
    AuthenticationError,
    CancelledError,
    MissingKeyError,
    NetworkError,
    QuotaExceededError,
    RateLimitError,
)
from spectra.translate.google import GoogleTranslator
from spectra.translate.service import PartialTranslationError, plan, translate_texts


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def no_sleep(t):
    t.sleep = lambda s: None
    return t


# ---- DeepL ---------------------------------------------------------------


def test_deepl_request_and_response():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"translations": [{"text": f"訳:{t}"} for t in seen["body"]["text"]]})

    t = DeepLTranslator("abc:fx", TranslateOptions(formality=Formality.PLAIN), client=client(handler))
    assert t.translate(['a <x id="0"/>', "b"]) == ['訳:a <x id="0"/>', "訳:b"]
    assert seen["url"] == f"{FREE_URL}/v2/translate"
    assert seen["auth"] == "DeepL-Auth-Key abc:fx"
    assert seen["body"]["tag_handling"] == "xml"
    assert seen["body"]["target_lang"] == "JA"
    assert seen["body"]["formality"] == "prefer_less"


def test_deepl_pro_endpoint():
    assert DeepLTranslator("abc").base_url == PRO_URL


def test_deepl_usage():
    def handler(req):
        return httpx.Response(200, json={"character_count": 1200, "character_limit": 500000})

    u = DeepLTranslator("k:fx", client=client(handler)).usage()
    assert u == Usage(1200, 500000) and u.remaining == 498800


@pytest.mark.parametrize(("status", "exc"), [(403, AuthenticationError), (456, QuotaExceededError)])
def test_deepl_errors(status, exc):
    t = DeepLTranslator("k:fx", client=client(lambda r: httpx.Response(status, json={"message": "x"})))
    with pytest.raises(exc):
        t.translate(["hello"])


def test_missing_key():
    with pytest.raises(MissingKeyError):
        DeepLTranslator("", client=client(lambda r: httpx.Response(200))).translate(["a"])


def test_retry_on_rate_limit_then_success():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"translations": [{"text": "ok"}]})

    t = no_sleep(DeepLTranslator("k:fx", client=client(handler)))
    assert t.translate(["a"]) == ["ok"] and len(calls) == 3


def test_retry_gives_up():
    t = no_sleep(DeepLTranslator("k:fx", client=client(lambda r: httpx.Response(429))))
    with pytest.raises(RateLimitError):
        t.translate(["a"])


def test_network_error_is_retried_and_reported():
    def handler(req):
        raise httpx.ConnectError("boom", request=req)

    t = no_sleep(DeepLTranslator("k:fx", client=client(handler)))
    with pytest.raises(NetworkError):
        t.translate(["a"])


def test_cancel():
    t = DeepLTranslator("k:fx", client=client(lambda r: httpx.Response(200, json={"translations": []})))
    t.cancel_event.set()
    with pytest.raises(CancelledError):
        t.translate(["a"])


def test_batching_respects_limits():
    t = DeepLTranslator("k:fx")
    batches = list(t.batches(["x" * 10] * 120))
    assert [len(b) for b in batches] == [50, 50, 20]
    g = GoogleTranslator("k")
    assert all(sum(map(len, b)) <= 5000 for b in g.batches(["y" * 1000] * 12))


# ---- Google --------------------------------------------------------------


def test_google_request_and_response():
    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": {"translations": [{"translatedText": "訳 &amp; <x id=\"0\"></x>"}]}})

    t = GoogleTranslator("gkey", client=client(handler))
    assert t.translate(['a & <x id="0"/>']) == ['訳 &amp; <x id="0"></x>']
    assert seen["params"]["key"] == "gkey"
    assert seen["body"]["format"] == "html" and seen["body"]["target"] == "ja"


def test_google_invalid_key():
    body = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "errors": [{"reason": "badRequest"}]}}
    t = GoogleTranslator("bad", client=client(lambda r: httpx.Response(400, json=body)))
    with pytest.raises(AuthenticationError):
        t.verify()


def test_google_api_disabled():
    body = {"error": {"code": 403, "message": "Cloud Translation API has not been used in project 1 before or it is disabled."}}
    t = GoogleTranslator("k", client=client(lambda r: httpx.Response(403, json=body)))
    with pytest.raises(AuthenticationError) as e:
        t.translate(["a"])
    assert "有効" in e.value.user_message()


# ---- service / cache -----------------------------------------------------


def test_dummy_keeps_placeholders():
    out = DummyTranslator().translate(['We set <x id="0"/> and <x id="1"/> to one.'])[0]
    assert out.index('<x id="0"/>') < out.index('<x id="1"/>')


def test_cache_avoids_second_request(tmp_path):
    calls = []

    def handler(req):
        texts = json.loads(req.content)["text"]
        calls.append(len(texts))
        return httpx.Response(200, json={"translations": [{"text": "訳" + t} for t in texts]})

    cache = TranslationCache(tmp_path / "c.sqlite3")
    t = DeepLTranslator("k:fx", client=client(handler))
    first = translate_texts(["a", "b", "a"], t, cache)
    assert first == {"a": "訳a", "b": "訳b"} and calls == [2]
    second = translate_texts(["a", "b", "c"], t, cache)
    assert second["c"] == "訳c" and calls == [2, 1]
    p = plan(["a", "b", "c", "d"], t.cache_namespace(), cache)
    assert p.cached_chars == 3 and p.pending == ["d"]


def test_cache_namespace_includes_formality():
    a = DeepLTranslator("k", TranslateOptions(formality=Formality.PLAIN)).cache_namespace()
    b = DeepLTranslator("k", TranslateOptions(formality=Formality.POLITE)).cache_namespace()
    assert a != b


def test_partial_failure_keeps_finished_batches():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) > 1:
            return httpx.Response(456)
        texts = json.loads(req.content)["text"]
        return httpx.Response(200, json={"translations": [{"text": "訳"} for _ in texts]})

    cache = TranslationCache(None)
    t = DeepLTranslator("k:fx", client=client(handler))
    with pytest.raises(PartialTranslationError) as e:
        translate_texts([f"t{i}" for i in range(60)], t, cache)
    assert len(e.value.results) == 50 and isinstance(e.value.cause, QuotaExceededError)
    assert cache.count() == 50


def test_registry():
    assert isinstance(registry.create("deepl", api_key="x"), DeepLTranslator)
    assert [e.engine_id for e in registry.public_engines()][:3] == ["google", "microsoft", "deepl"]
    assert registry.create("microsoft", api_key="k", region="japaneast").region == "japaneast"
    assert not hasattr(registry.create("google", api_key="k", region="x"), "region")
    with pytest.raises(ValueError):
        registry.create("nope")


# ---- Microsoft -------------------------------------------------------------


def test_microsoft_request_and_response():
    from spectra.translate.microsoft import MicrosoftTranslator

    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params)
        seen["headers"] = req.headers
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=[{"translations": [{"text": "訳 <x id=\"0\"></x>", "to": "ja"}]}])

    t = MicrosoftTranslator("mkey", region="japaneast", client=client(handler))
    assert t.translate(['a <x id="0"/>']) == ['訳 <x id="0"></x>']
    assert seen["params"] == {"api-version": "3.0", "from": "en", "to": "ja", "textType": "html"}
    assert seen["headers"]["Ocp-Apim-Subscription-Key"] == "mkey"
    assert seen["headers"]["Ocp-Apim-Subscription-Region"] == "japaneast"
    assert seen["body"] == [{"Text": 'a <x id="0"/>'}]


def test_microsoft_global_resource_has_no_region_header():
    from spectra.translate.microsoft import MicrosoftTranslator

    seen = {}

    def handler(req):
        seen["headers"] = req.headers
        return httpx.Response(200, json=[{"translations": [{"text": "x"}]}])

    MicrosoftTranslator("k", region="global", client=client(handler)).translate(["a"])
    assert "Ocp-Apim-Subscription-Region" not in seen["headers"]


@pytest.mark.parametrize(
    ("status", "code", "exc"),
    [(401, 401000, AuthenticationError), (401, 401015, AuthenticationError), (403, 403001, QuotaExceededError)],
)
def test_microsoft_errors(status, code, exc):
    from spectra.translate.microsoft import MicrosoftTranslator

    body = {"error": {"code": code, "message": "error"}}
    t = MicrosoftTranslator("k", client=client(lambda r: httpx.Response(status, json=body)))
    with pytest.raises(exc):
        t.verify()
