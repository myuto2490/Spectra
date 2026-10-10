"""翻訳 API に渡すマークアップの生成と解析。

保護対象（インライン数式・引用番号など）は空要素 ``<x id="N"/>`` で表す。
段落の中の太字（"Results." のような段落先頭の小見出しなど）は ``<b>…</b>`` で囲み、
訳文でも対応する語を同じタグで囲んでもらう。
翻訳エンジン（Gemini）にはタグを一字一句変えずに残し、日本語の語順に合う位置へ置くよう指示する。
表記ゆれやタグの欠落があっても数式を失わないよう、解析側で補正する。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

_TAG = re.compile(r"""<x\s+id\s*=\s*["']?(\d+)["']?\s*/?>(?:\s*</x>)?""", re.IGNORECASE)
_BOLD = re.compile(r"<(/?)b\s*>", re.IGNORECASE)
_MARK = re.compile(_TAG.pattern + r"|" + _BOLD.pattern, re.IGNORECASE)
BOLD_OPEN, BOLD_CLOSE = "<b>", "</b>"
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
    bold: bool = False


def parse(markup: str, known_ids: set[int]) -> list[Token]:
    """訳文マークアップをテキストと保護要素の列に分解する。

    - 翻訳で失われた保護要素は末尾に補う（数式が消えることを防ぐ）
    - 重複した保護要素は最初の 1 つだけ残す
    - 未知のタグは除去する
    """
    tokens: list[Token] = []
    seen: set[int] = set()
    pos = 0
    bold = False
    for m in _MARK.finditer(markup):
        _append_text(tokens, markup[pos : m.start()], bold)
        pos = m.end()
        if m.group(1) is None:  # <b> / </b>
            bold = m.group(2) == ""
            continue
        rid = int(m.group(1))
        if rid in known_ids and rid not in seen:
            tokens.append(Token(run_id=rid, bold=bold))
            seen.add(rid)
    _append_text(tokens, markup[pos:], bold)
    missing = sorted(known_ids - seen)
    for rid in missing:
        tokens.append(Token(text=" "))
        tokens.append(Token(run_id=rid))
    return tokens


def _append_text(tokens: list[Token], raw: str, bold: bool = False) -> None:
    if not raw:
        return
    raw = _STRAY_CLOSE.sub("", raw)
    raw = _ANY_TAG.sub("", raw)
    text = html.unescape(raw)
    if text:
        tokens.append(Token(text=text, bold=bold))


def to_plain(markup: str) -> str:
    """プレースホルダを除いた素の文字列（文字数見積もりやログ用）。"""
    return html.unescape(_ANY_TAG.sub("", markup))
