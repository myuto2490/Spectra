"""Google Cloud Translation API (Basic / v2)。毎月 50 万文字まで無料。

https://cloud.google.com/translate/docs/reference/rest/v2/translate
"""

from __future__ import annotations

import httpx

from .base import Translator, Usage, rate_limited
from .errors import (
    AuthenticationError,
    MissingKeyError,
    QuotaExceededError,
    ServiceError,
    TranslatorError,
)

BASE_URL = "https://translation.googleapis.com/language/translate/v2"


class GoogleTranslator(Translator):
    engine_id = "google"
    display_name = "Google Cloud Translation"
    max_items = 100  # API の上限は 128
    max_chars = 5_000  # 推奨される 1 リクエストあたりの上限

    def _params(self) -> dict[str, str]:
        if not self.api_key:
            raise MissingKeyError
        return {"key": self.api_key}

    def _translate_batch(self, texts: list[str]) -> list[str]:
        # HTML として送ると <x id="0"/> のようなタグが保持される（エンティティはパース側で戻す）
        body = {
            "q": texts,
            "source": self.options.source_lang.lower(),
            "target": self.options.target_lang.lower(),
            "format": "html",
        }
        r = self.client.post(BASE_URL, params=self._params(), json=body)
        self._raise_for_status(r)
        return [t["translatedText"] for t in r.json()["data"]["translations"]]

    def usage(self) -> Usage | None:
        return None  # API キーでは使用量を取得できない

    def verify(self) -> Usage | None:
        # 対応言語一覧の取得は文字数を消費せずにキーの有効性を確認できる
        r = self.client.get(f"{BASE_URL}/languages", params={**self._params(), "target": "ja"})
        self._raise_for_status(r)
        return None

    @staticmethod
    def _raise_for_status(r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        reason, message = "", ""
        try:
            err = r.json().get("error", {})
            message = err.get("message", "")
            errors = err.get("errors") or [{}]
            reason = errors[0].get("reason", "") or (err.get("details") or [{}])[0].get("reason", "")
        except (ValueError, AttributeError, IndexError):
            pass
        code = r.status_code
        low = f"{reason} {message}".lower()
        if "api key" in low or "keyinvalid" in low or "api_key_invalid" in low or code == 401:
            raise AuthenticationError(hint="Google Cloud Console で発行した API キーを確認してください。")
        if "has not been used" in low or "disabled" in low or "service_disabled" in low or "accessnotconfigured" in low:
            raise AuthenticationError(
                "このプロジェクトで Cloud Translation API が有効になっていません。",
                hint="Google Cloud Console で「Cloud Translation API」を有効にしてください。",
            )
        if "dailylimitexceeded" in low or "quotaexceeded" in low or "billing" in low:
            raise QuotaExceededError(message)
        if code == 429 or "ratelimitexceeded" in low:
            raise rate_limited(r)
        if code == 403:
            raise AuthenticationError(message or "アクセスが拒否されました。")
        if code >= 500:
            raise ServiceError(f"HTTP {code}")
        raise TranslatorError(f"Google がリクエストを拒否しました（HTTP {code}）{': ' + message if message else ''}")
