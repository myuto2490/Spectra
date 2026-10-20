"""訳文の表記を日本語の論文として自然な形に整える。

- 全角括弧の内側の余分な空白を取る：「（ Moses, 1992 ）」→「（Moses, 1992）」
- 中身が英数字・記号だけの全角括弧は半角にする：「（Moses, 1992）」→「(Moses, 1992)」
  （半角括弧と和文の間には組版時に 1/4 em の空きが入る）
- 翻訳エンジンが数式などを MathML・HTML のタグで書いてしまった場合は、タグを取って中身だけ残す：
  「<math><mi>p</mi></math>」→「p」
- 翻訳エンジンの出力が崩れて生じる、結合文字（アクセント記号など）の連続を取り除く
"""

from __future__ import annotations

import re

_TAG = r'<x id="\d+"/>'
_INNER_SPACE_OPEN = re.compile(r"（[ 　]+")
_INNER_SPACE_CLOSE = re.compile(r"[ 　]+）")
# 中身に和文（かな・漢字・全角記号）を含まない全角括弧
_LATIN_PAREN = re.compile(r"（((?:[\x20-\x7E -ɏͰ-Ͽ‐-‧′-″−]|" + _TAG + r")+?)）")
# マークアップの上ではエスケープされている（&lt;mi&gt;）
_LEAKED_TAG = re.compile(
    r"&lt;/?(?:math|mi|mo|mn|ms|mtext|mrow|msub|msup|msubsup|mfrac|msqrt|mroot|mover|munder|munderover|mspace"
    r"|mstyle|mfenced|mtable|mtr|mtd|semantics|annotation|sub|sup|i|em|strong|span|br)\b(?:(?!&gt;).)*?/?&gt;",
    re.IGNORECASE,
)
_COMBINING_RUN = re.compile(r"[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]{3,}")


def is_degenerate(text: str) -> bool:
    """翻訳エンジンの出力が崩れている（結合文字が延々と続くなど）か。"""
    return _COMBINING_RUN.search(text) is not None


def normalize_translation(markup: str) -> str:
    text = _LEAKED_TAG.sub("", markup)
    text = _COMBINING_RUN.sub("", text)
    text = _INNER_SPACE_OPEN.sub("（", text)
    text = _INNER_SPACE_CLOSE.sub("）", text)
    return _LATIN_PAREN.sub(lambda m: "(" + m.group(1).strip() + ")", text)
