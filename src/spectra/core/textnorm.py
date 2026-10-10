"""訳文の表記を日本語の論文として自然な形に整える。

- 全角括弧の内側の余分な空白を取る：「（ Moses, 1992 ）」→「（Moses, 1992）」
- 中身が英数字・記号だけの全角括弧は半角にする：「（Moses, 1992）」→「(Moses, 1992)」
  （半角括弧と和文の間には組版時に 1/4 em の空きが入る）
"""

from __future__ import annotations

import re

_TAG = r'<x id="\d+"/>'
_INNER_SPACE_OPEN = re.compile(r"（[ 　]+")
_INNER_SPACE_CLOSE = re.compile(r"[ 　]+）")
# 中身に和文（かな・漢字・全角記号）を含まない全角括弧
_LATIN_PAREN = re.compile(r"（((?:[\x20-\x7E -ɏͰ-Ͽ‐-‧′-″−]|" + _TAG + r")+?)）")


def normalize_translation(markup: str) -> str:
    text = _INNER_SPACE_OPEN.sub("（", markup)
    text = _INNER_SPACE_CLOSE.sub("）", text)
    return _LATIN_PAREN.sub(lambda m: "(" + m.group(1).strip() + ")", text)
