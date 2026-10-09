"""翻訳 API に渡すマークアップの生成と解析。

保護対象（インライン数式・引用番号など）は空要素 ``<x id="N"/>`` で表す。
DeepL（tag_handling=xml）も Google（format=html）もタグを保持したまま
訳文の語順に合わせて移動させるので、日本語として自然な位置に数式が置かれる。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

_TAG = re.compile(r"""<x\s+id\s*=\s*["']?(\d+)["']?\s*/?>(?:\s*</x>)?""", re.IGNORECASE)
_STRAY_CLOSE = re.compile(r"</x\s*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"</?[a-zA-Z][^<>]*>")


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def placeholder(run_id: int) -> str:
    return f'<x id="{run_id}"/>'


@dataclass(frozen=True)
class Token:
    """訳文を構成する要素。text か run_id のどちらか一方を持つ。"""

    text: str = ""
    run_id: int | None = None


def parse(markup: str, known_ids: set[int]) -> list[Token]:
    """訳文マークアップをテキストと保護要素の列に分解する。

    - 翻訳で失われた保護要素は末尾に補う（数式が消えることを防ぐ）
    - 重複した保護要素は最初の 1 つだけ残す
    - 未知のタグは除去する
    """
    tokens: list[Token] = []
    seen: set[int] = set()
    pos = 0
    for m in _TAG.finditer(markup):
        _append_text(tokens, markup[pos : m.start()])
        rid = int(m.group(1))
        if rid in known_ids and rid not in seen:
            tokens.append(Token(run_id=rid))
            seen.add(rid)
        pos = m.end()
    _append_text(tokens, markup[pos:])
    missing = sorted(known_ids - seen)
    for rid in missing:
        tokens.append(Token(text=" "))
        tokens.append(Token(run_id=rid))
    return tokens


def _append_text(tokens: list[Token], raw: str) -> None:
    if not raw:
        return
    raw = _STRAY_CLOSE.sub("", raw)
    raw = _ANY_TAG.sub("", raw)
    text = html.unescape(raw)
    if text:
        tokens.append(Token(text=text))


def to_plain(markup: str) -> str:
    """プレースホルダを除いた素の文字列（文字数見積もりやログ用）。"""
    return html.unescape(_ANY_TAG.sub("", markup))
