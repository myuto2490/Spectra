"""Microsoft Translator（Azure AI Translator, v3）。無料プラン F0 は毎月 200 万文字まで無料。

https://learn.microsoft.com/azure/ai-services/translator/text-translation/reference/v3/translate
"""

from __future__ import annotations

import contextlib

import httpx

from ..core.options import TranslateOptions
from .base import Translator, Usage, rate_limited
from .errors import (
    AuthenticationError,
    MissingKeyError,
    QuotaExceededError,
    ServiceError,
    TranslatorError,
)

ENDPOINT = "https://api.cognitive.microsofttranslator.com"


class MicrosoftTranslator(Translator):
    engine_id = "microsoft"
    display_name = "Microsoft Translator"
    max_items = 100  # API の上限は 1,000 件
    max_chars = 40_000  # API の上限は 1 リクエスト 50,000 文字（タグを含む）

    def __init__(self, api_key: str = "", options: TranslateOptions | None = None, region: str = "", **kwargs):
        super().__init__(api_key, options, **kwargs)
        self.region = region.strip()

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise MissingKeyError
        headers = {"Ocp-Apim-Subscription-Key": self.api_key}
        # 「グローバル」以外のリージョンで作成したリソースはリージョンの指定が必要
        if self.region and self.region.lower() != "global":
            headers["Ocp-Apim-Subscription-Region"] = self.region
        return headers

    def _translate_batch(self, texts: list[str]) -> list[str]:
        params = {
            "api-version": "3.0",
            "from": self.options.source_lang.lower(),
            "to": self.options.target_lang.lower(),
            "textType": "html",  # <x id="0"/> のようなタグを保持する
        }
        r = self.client.post(f"{ENDPOINT}/translate", params=params, headers=self._headers(), json=[{"Text": t} for t in texts])
        self._raise_for_status(r)
        return [item["translations"][0]["text"] for item in r.json()]

    def verify(self) -> Usage | None:
        # 使用量を取得する API が無いため、短い文を 1 回翻訳してキーとリージョンを確認する
        self._translate_batch(["Hello"])
        return None

    @staticmethod
    def _raise_for_status(r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        code, message = 0, ""
        with contextlib.suppress(ValueError, AttributeError, TypeError):
            err = r.json().get("error", {})
            code, message = int(err.get("code", 0)), err.get("message", "")
        if r.status_code == 401:
            hint = "Azure ポータルの「キーとエンドポイント」でキーとリージョンを確認してください。"
            if code == 401015 or "region" in message.lower():
                raise AuthenticationError("リージョンの指定が正しくありません。", hint=hint)
            raise AuthenticationError(hint=hint)
        if r.status_code == 403 and code == 403001:
            raise QuotaExceededError("無料プラン（F0）の今月の上限に達しました。")
        if r.status_code == 429:
            raise rate_limited(r)
        if r.status_code >= 500:
            raise ServiceError(f"HTTP {r.status_code}")
        raise TranslatorError(f"Microsoft がリクエストを拒否しました（HTTP {r.status_code}）{': ' + message if message else ''}")
