"""API を使わない擬似翻訳。レイアウトの確認とテストに使う。"""

from __future__ import annotations

import re

from .base import Translator, Usage

_TAG = re.compile(r"<x id=\"\d+\"/>")
_SAMPLE = "これはレイアウトを確認するための擬似的な翻訳文です。実際の翻訳では、ここに日本語の訳文が入ります。"


class DummyTranslator(Translator):
    engine_id = "dummy"
    display_name = "レイアウト確認（擬似翻訳）"
    max_items = 200
    max_chars = 200_000

    def _translate_batch(self, texts: list[str]) -> list[str]:
        return [self._fake(t) for t in texts]

    def usage(self) -> Usage:
        return Usage(used=0, limit=None)

    @staticmethod
    def _fake(markup: str) -> str:
        """原文の 6 割程度の長さの日本語風テキストを作り、プレースホルダは順に散りばめる。"""
        tags = _TAG.findall(markup)
        plain_len = len(_TAG.sub("", markup))
        n = max(4, int(plain_len * 0.6))
        body = (_SAMPLE * (n // len(_SAMPLE) + 1))[:n]
        if not tags:
            return body
        step = max(1, len(body) // (len(tags) + 1))
        parts = []
        for i, tag in enumerate(tags):
            parts.append(body[i * step : (i + 1) * step])
            parts.append(tag)
        parts.append(body[len(tags) * step :])
        return "".join(parts)
