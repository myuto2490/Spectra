"""利用可能な翻訳エンジンの一覧。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .base import Translator
from .deepl import DeepLTranslator
from .dummy import DummyTranslator
from .google import GoogleTranslator


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
    "deepl": EngineInfo(
        engine_id="deepl",
        name="DeepL API Free",
        description="自然で読みやすい訳文。論文の翻訳に最もおすすめです。",
        key_url="https://www.deepl.com/pro-api",
        key_label="認証キー",
        free_quota="毎月 50 万文字まで無料",
    ),
    "google": EngineInfo(
        engine_id="google",
        name="Google Cloud Translation",
        description="Google Cloud の翻訳 API（Basic）。幅広い分野の用語に対応します。",
        key_url="https://console.cloud.google.com/apis/library/translate.googleapis.com",
        key_label="API キー",
        free_quota="毎月 50 万文字まで無料（請求先アカウントの登録が必要）",
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
    "deepl": DeepLTranslator,
    "google": GoogleTranslator,
    "dummy": DummyTranslator,
}


def public_engines() -> list[EngineInfo]:
    """UI に表示するエンジン。擬似翻訳は開発者向け（SPECTRA_DEV=1）のときだけ表示する。"""
    ids = ["deepl", "google"] + (["dummy"] if os.environ.get("SPECTRA_DEV") else [])
    return [ENGINES[i] for i in ids]


def create(engine_id: str, **kwargs) -> Translator:
    try:
        cls = _CLASSES[engine_id]
    except KeyError:
        raise ValueError(f"unknown engine: {engine_id}") from None
    return cls(**kwargs)
