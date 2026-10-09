"""DeepL API（Free / Developer / Pro）。Free プランは新規受付を終了している。

https://developers.deepl.com/docs/api-reference/translate
"""

from __future__ import annotations

import contextlib

import httpx

from ..core.options import Formality
from .base import Translator, Usage, rate_limited
from .errors import (
    AuthenticationError,
    MissingKeyError,
    QuotaExceededError,
    ServiceError,
    TranslatorError,
)

FREE_URL = "https://api-free.deepl.com"
PRO_URL = "https://api.deepl.com"


class DeepLTranslator(Translator):
    engine_id = "deepl"
    display_name = "DeepL API"
    max_items = 50  # API の上限
    max_chars = 30_000  # リクエスト本文 128 KiB 未満に収める
    supports_formality = True

    @property
    def base_url(self) -> str:
        # Free プランのキーは末尾が ":fx"
        return FREE_URL if self.api_key.endswith(":fx") else PRO_URL

    def cache_namespace(self) -> str:
        return f"{super().cache_namespace()}|{self.options.formality.value}"

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise MissingKeyError
        return {"Authorization": f"DeepL-Auth-Key {self.api_key}"}

    def _translate_batch(self, texts: list[str]) -> list[str]:
        body: dict = {
            "text": texts,
            "source_lang": self.options.source_lang.upper(),
            "target_lang": self.options.target_lang.upper(),
            "tag_handling": "xml",
            "ignore_tags": ["x"],
            "preserve_formatting": True,
        }
        if self.options.formality != Formality.DEFAULT:
            body["formality"] = self.options.formality.value
        r = self.client.post(f"{self.base_url}/v2/translate", json=body, headers=self._headers())
        self._raise_for_status(r)
        data = r.json()
        return [t["text"] for t in data["translations"]]

    def usage(self) -> Usage:
        r = self.client.get(f"{self.base_url}/v2/usage", headers=self._headers())
        self._raise_for_status(r)
        data = r.json()
        return Usage(used=int(data.get("character_count", 0)), limit=data.get("character_limit"))

    @staticmethod
    def _raise_for_status(r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        message = ""
        with contextlib.suppress(ValueError, AttributeError):
            message = r.json().get("message", "")
        code = r.status_code
        if code in (401, 403):
            raise AuthenticationError(hint="DeepL の API キー（Free プランは末尾が「:fx」）を確認してください。")
        if code == 456:
            raise QuotaExceededError
        if code == 429:
            raise rate_limited(r)
        if code >= 500:
            raise ServiceError(f"HTTP {code}")
        raise TranslatorError(f"DeepL がリクエストを拒否しました（HTTP {code}）{': ' + message if message else ''}")
