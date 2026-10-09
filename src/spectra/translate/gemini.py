"""Gemini API による翻訳。Google AI Studio で発行した API キーで、クレジットカードの登録なしに無料枠で使える。

https://ai.google.dev/api/generate-content

- 段落を JSON の配列でまとめて送り、同じ件数・順序の JSON で受け取る（構造化出力）
- 数式などの保護要素 ``<x id="N"/>`` は、そのまま残して語順に合う位置へ置くよう指示する
- 無料枠は 1 分・1 日あたりの回数に上限があるため、429 では指定された時間だけ待って再試行する
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass
from typing import ClassVar

import httpx

from ..core.markup import placeholder
from ..core.options import Formality, TranslateOptions
from .base import Translator, Usage
from .errors import (
    AuthenticationError,
    MissingKeyError,
    QuotaExceededError,
    RateLimitError,
    ServiceError,
    TranslatorError,
)

log = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
PROMPT_VERSION = 1  # 指示文を変えたら上げる（キャッシュを分けるため）



@dataclass(frozen=True)
class ModelInfo:
    name: str
    description: str
    thinking: tuple[str, ...]  # 試す思考レベル（最小から）。対応していなければ次を試し、最後は指定なし
    fallback: bool = True  # 混雑・上限時の自動切り替え先に含めるか
    caution: str = ""


# 表示順。翻訳は深い推論を必要としないため、思考は各モデルで指定できる最小限にする（速度と無料枠の節約）
MODELS: dict[str, ModelInfo] = {
    "gemini-3.5-flash-lite": ModelInfo(
        "Gemini 3.5 Flash-Lite", "標準。速く、無料枠の回数にも余裕があります（おすすめ）", ("minimal",)
    ),
    "gemini-3.1-flash-lite": ModelInfo("Gemini 3.1 Flash-Lite", "軽量。最も軽く、混雑時の代わりに向いています", ("minimal",)),
    "gemini-3.5-flash": ModelInfo("Gemini 3.5 Flash", "バランス型。訳文の自然さを少し重視する場合に", ("minimal", "low")),
    "gemini-3.6-flash": ModelInfo("Gemini 3.6 Flash", "高品質。専門用語の多い論文に", ("minimal", "low")),
    "gemini-3.7-flash": ModelInfo("Gemini 3.7 Flash", "高品質。長く複雑な文の多い論文に", ("minimal", "low")),
    "gemini-3.8-flash": ModelInfo(
        "Gemini 3.8 Flash",
        "最高品質",
        ("low",),
        fallback=False,
        caution="混雑しやすく、無料枠をすぐに使い切ることがあります。通常は他のモデルをおすすめします。",
    ),
}
DEFAULT_MODEL = "gemini-3.5-flash-lite"
# 自動切り替えの順序（3.8 Flash は含めない）
FALLBACK_ORDER = [m for m, info in MODELS.items() if info.fallback]

_RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"id": {"type": "INTEGER"}, "text": {"type": "STRING"}},
        "required": ["id", "text"],
    },
}
# 返ってきたタグの表記ゆれ（<x id='0'></x> など）も受け付ける
_TAG = re.compile(r"""<x\s+id\s*=\s*["']?(\d+)["']?\s*/?>(?:\s*</x>)?""", re.IGNORECASE)
_BLOCKED = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "OTHER", "LANGUAGE"}


def system_prompt(options: TranslateOptions) -> str:
    style = "です・ます調" if options.formality == Formality.POLITE else "である調"
    return f"""あなたは学術論文を専門とする翻訳者です。英語の論文の段落を、日本語の学術論文として自然で正確な文章に翻訳してください。

入力は JSON の配列で、各要素は {{"id": 番号, "text": 原文}} です。
出力も JSON の配列とし、すべての要素について {{"id": 同じ番号, "text": 訳文}} を、入力と同じ件数・同じ順序で返してください。

翻訳の規則:
1. 文体は{style}で統一する。
2. 原文の意味を省略・追加せずに訳す。各要素は独立した段落・見出し・キャプションなので、要素をまたいで文を移動しない。
3. <x id="0"/> のようなタグは数式・記号・引用番号を表す。タグは一字一句変えずに、すべて 1 回ずつ訳文に含め、日本語の語順で自然な位置に置く。タグの中身を推測して書き換えない。
4. 専門用語は分野で一般的な日本語訳を使う。定訳のない用語、手法名・モデル名・データセット名・人名・製品名は原語のまま残す。
5. 略語（例: CNN, BLEU）、変数名、URL、メールアドレス、コードは原文のまま残す。
6. 見出しの章番号（例: 3.2）や「Figure 1:」などの番号は残し、「Figure」は「図」、「Table」は「表」と訳す。
7. 句読点は「、」「。」を使い、英数字は半角のままにする。
8. 訳文以外の説明や注記は書かない。"""


class ModelUnavailableError(TranslatorError):
    title = "このモデルは現在使えません"
    retryable = False


class GeminiTranslator(Translator):
    engine_id = "gemini"
    display_name = "Gemini API"
    max_items = 30
    max_chars = 15_000  # 1 回にまとめて送り、リクエスト回数を節約する（応答が長くなりすぎない範囲）
    supports_formality = True
    # 無料枠の 1 分あたりの上限に当たった場合に備え、長めに待つ（RetryInfo があればそれを優先）
    retry_delays: ClassVar[tuple[float, ...]] = (5.0, 10.0, 20.0, 40.0, 60.0)
    # 切り替え先のモデルがある場合は、待ち続けずに早めに切り替える
    fallback_retry_delays: ClassVar[tuple[float, ...]] = (5.0, 10.0)

    def __init__(
        self,
        api_key: str = "",
        options: TranslateOptions | None = None,
        model: str = DEFAULT_MODEL,
        auto_fallback: bool = True,
        **kwargs,
    ):
        super().__init__(api_key, options, **kwargs)
        self.requested_model = model if model in MODELS else DEFAULT_MODEL
        self.model = self.requested_model
        self.auto_fallback = auto_fallback
        self.notices: list[str] = []  # 自動切り替えなど、利用者に伝える出来事
        self._tried: set[str] = {self.model}
        self._thinking_index: dict[str, int] = {}

    def cache_namespace(self) -> str:
        # 自動切り替え後の訳文も、選んだモデルの訳文として再利用する
        formality = "polite" if self.options.formality == Formality.POLITE else "plain"
        return f"{super().cache_namespace()}|{self.requested_model}|{formality}|p{PROMPT_VERSION}"

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise MissingKeyError(hint="設定 → 翻訳エンジン で Gemini API キーを入力してください。")
        return {"x-goog-api-key": self.api_key}

    # -- 翻訳（自動切り替えつき） -----------------------------------------------

    def translate(self, texts: list[str], on_batch=None) -> list[str | None]:
        out: list[str | None] = []
        for batch in self.batches(texts):
            self._check_cancel()
            result = self._translate_with_fallback(batch)
            if len(result) != len(batch):
                raise ServiceError(f"翻訳結果の件数が一致しません（{len(batch)} 件中 {len(result)} 件）")
            out.extend(result)
            if on_batch:
                on_batch(len(batch))
        return out

    def _next_model(self) -> str | None:
        if not self.auto_fallback:
            return None
        return next((m for m in FALLBACK_ORDER if m not in self._tried), None)

    def _translate_with_fallback(self, batch: list[str]) -> list[str | None]:
        while True:
            self.retry_delays = self.fallback_retry_delays if self._next_model() else type(self).retry_delays
            try:
                return self._with_retry(lambda: self._translate_batch(batch))
            except (QuotaExceededError, RateLimitError, ServiceError, ModelUnavailableError) as e:
                nxt = self._next_model()
                if nxt is None:
                    raise
                reason = {
                    QuotaExceededError: "1 日の無料枠の上限に達した",
                    RateLimitError: "回数の上限に達した",
                    ServiceError: "混雑している",
                    ModelUnavailableError: "使えなかった",
                }.get(type(e), "使えなかった")
                note = f"{MODELS[self.model].name} が{reason}ため、{MODELS[nxt].name} に切り替えて翻訳しました。"
                log.info("gemini: %s", note)
                if note not in self.notices:
                    self.notices.append(note)
                self.model = nxt
                self._tried.add(nxt)

    def _translate_batch(self, texts: list[str]) -> list[str | None]:
        items = [{"id": i, "text": _unescape(t)} for i, t in enumerate(texts)]
        try:
            result = self._request(items)
        except _BlockedError:
            result = None
        if result is not None and len(result) == len(texts):
            return result
        # 件数が合わない・ブロックされた場合は分割してやり直す（1 件でも訳せなければ None）
        if len(texts) == 1:
            log.warning("gemini: could not translate one item")
            return [None]
        mid = len(texts) // 2
        return self._translate_batch(texts[:mid]) + self._translate_batch(texts[mid:])

    def _request(self, items: list[dict]) -> list[str | None] | None:
        config: dict = {
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
            "maxOutputTokens": 32768,
        }
        levels = MODELS[self.model].thinking
        idx = self._thinking_index.get(self.model, 0)
        if idx < len(levels):
            config["thinkingConfig"] = {"thinkingLevel": levels[idx]}
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt(self.options)}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(items, ensure_ascii=False)}]}],
            "generationConfig": config,
        }
        url = f"{API_BASE}/models/{self.model}:generateContent"
        r = self.client.post(url, headers=self._headers(), json=body)
        if r.status_code == 400 and "thinkingConfig" in config and "thinking" in r.text.lower():
            # その思考レベルに対応していないモデルは、次のレベル（最後は指定なし）で送り直す
            self._thinking_index[self.model] = idx + 1
            return self._request(items)
        self._raise_for_status(r)
        return self._parse(r.json(), len(items))

    @staticmethod
    def _parse(data: dict, expected: int) -> list[str | None] | None:
        if data.get("promptFeedback", {}).get("blockReason"):
            raise _BlockedError
        candidates = data.get("candidates") or []
        if not candidates:
            raise _BlockedError
        cand = candidates[0]
        if cand.get("finishReason") in _BLOCKED:
            raise _BlockedError
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
        try:
            arr = json.loads(text)
        except ValueError:
            log.warning("gemini: response is not valid JSON (finishReason=%s)", cand.get("finishReason"))
            return None
        if not isinstance(arr, list):
            return None
        by_id: dict[int, str] = {}
        for obj in arr:
            if isinstance(obj, dict) and isinstance(obj.get("id"), int) and isinstance(obj.get("text"), str):
                by_id.setdefault(obj["id"], obj["text"])
        if len(by_id) != expected or set(by_id) != set(range(expected)):
            return None
        return [_escape_text(by_id[i]) for i in range(expected)]

    # -- 確認 ------------------------------------------------------------

    def verify(self) -> Usage | None:
        """モデル情報の取得でキーを確認する（無料枠の回数を消費しない）。"""
        r = self.client.get(f"{API_BASE}/models/{self.model}", headers=self._headers())
        self._raise_for_status(r)
        return None

    @staticmethod
    def _raise_for_status(r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        status, message, reasons, retry_after, per_day = "", "", set(), None, False
        with contextlib.suppress(ValueError, AttributeError, TypeError):
            err = r.json().get("error", {})
            status, message = err.get("status", ""), err.get("message", "")
            for d in err.get("details", []) or []:
                if d.get("reason"):
                    reasons.add(d["reason"])
                if d.get("retryDelay"):
                    retry_after = float(str(d["retryDelay"]).rstrip("s") or 0)
                for v in d.get("violations", []) or []:
                    if "perday" in (v.get("quotaId", "") + v.get("quotaMetric", "")).lower():
                        per_day = True
        low = message.lower()
        if "API_KEY_INVALID" in reasons or "api key not valid" in low or r.status_code == 401:
            raise AuthenticationError(hint="Google AI Studio で発行した API キーを確認してください。")
        if r.status_code == 403:
            raise AuthenticationError(message or "アクセスが拒否されました。", hint="API キーの制限や、Gemini API が使える地域かを確認してください。")
        if r.status_code == 404:
            raise ModelUnavailableError(message, hint="設定で別のモデルを選んでください。")
        if r.status_code == 429 or status == "RESOURCE_EXHAUSTED":
            if per_day or "per day" in low:
                raise QuotaExceededError(
                    "Gemini API の無料枠の 1 日あたりの上限に達しました。",
                    hint="上限は日本時間の 16 時（夏時間は 17 時）ごろに戻ります。翻訳済みの段落はキャッシュに保存されているので、続きから翻訳できます。"
                    "設定で別のモデルを選ぶと、別の枠で続けられる場合があります。",
                )
            err = RateLimitError("無料枠の 1 分あたりの上限に達したため、少し待ってから再開します。")
            err.retry_after = retry_after  # type: ignore[attr-defined]
            raise err
        if r.status_code >= 500:
            raise ServiceError(f"HTTP {r.status_code}（Gemini が混み合っています）")
        raise TranslatorError(f"Gemini がリクエストを拒否しました（HTTP {r.status_code}）{': ' + message if message else ''}")


class _BlockedError(Exception):
    """安全フィルタなどで応答が得られなかった。"""


def _unescape(markup: str) -> str:
    """送信用：XML のエスケープを戻し、タグはそのまま残す。"""
    return markup.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _escape_text(text: str) -> str:
    """受信した訳文を、タグ以外の部分だけ XML エスケープしたマークアップに戻す。"""
    out, pos = [], 0
    for m in _TAG.finditer(text):
        out.append(text[pos : m.start()].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        out.append(placeholder(int(m.group(1))))
        pos = m.end()
    out.append(text[pos:].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(out)
