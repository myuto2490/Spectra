"""利用可能な翻訳エンジンの一覧。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .base import Translator
from .dummy import DummyTranslator
from .gemini import DEFAULT_MODEL, GeminiTranslator


@dataclass(frozen=True)
class EngineInfo:
    engine_id: str
    name: str
    description: str
    key_url: str
    key_label: str
    free_quota: str
    needs_key: bool = True


ENGINES: dict[str, EngineInfo] = {
    "gemini": EngineInfo(
        engine_id="gemini",
        name="Gemini API",
        description="Google の生成 AI による翻訳。論文の文体や専門用語を自然に訳します。",
        key_url="https://aistudio.google.com/apikey",
        key_label="API キー",
        free_quota="",
    ),
    "dummy": EngineInfo(
        engine_id="dummy",
        name="レイアウト確認（擬似翻訳）",
        description="API を使わず擬似的な日本語を流し込み、組版だけを確認します。",
        key_url="",
        key_label="",
        free_quota="無制限",
        needs_key=False,
    ),
}

_CLASSES: dict[str, type[Translator]] = {
    "gemini": GeminiTranslator,
    "dummy": DummyTranslator,
}


def public_engines() -> list[EngineInfo]:
    """UI に表示するエンジン。擬似翻訳は開発者向け（SPECTRA_DEV=1）のときだけ表示する。"""
    ids = ["gemini"] + (["dummy"] if os.environ.get("SPECTRA_DEV") else [])
    return [ENGINES[i] for i in ids]


def create(engine_id: str, model: str = DEFAULT_MODEL, auto_fallback: bool = True, **kwargs) -> Translator:
    """翻訳エンジンを作る。model と auto_fallback は Gemini でのみ使う。"""
    try:
        cls = _CLASSES[engine_id]
    except KeyError:
        raise ValueError(f"unknown engine: {engine_id}") from None
    if cls is GeminiTranslator:
        kwargs["model"] = model
        kwargs["auto_fallback"] = auto_fallback
    return cls(**kwargs)
