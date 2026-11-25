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


_TAG = re.compile(r"<[^>]*>")
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
# 訳す語を含まない部分：URL・DOI・メールアドレスと、論文の定型句（著作権表示・連絡先の見出し）
_NOT_WORDS = re.compile(
    r"(?:https?://|www\.)\S+|\bdoi:\s*\S+|\b10\.\d{4,}/\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|\bsee front matter\b|\ball rights reserved\b|\bpublished by\b|\be-?mail(?: address(?:es)?)?\b"
    r"|\bcorresponding authors?\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[A-Za-z]+")
_ENGLISH_WORD = re.compile(r"\b[a-z]{3,}\b")
# 数式によく出る、小文字の関数名・略号（英語の文の語としては数えない）
_MATH_WORDS = frozenset(
    {
        "min", "max", "sin", "cos", "tan", "sec", "csc", "cot", "sinh", "cosh", "tanh", "arcsin",
        "arccos", "arctan", "log", "exp", "lim", "sup", "inf", "det", "arg", "mod", "lat", "lon",
        "long", "deg", "rad", "sgn", "erf",
    }
)
# 英語の文によく出る機能語
_FUNCTION_WORDS = frozenset(
    {
        "a", "an", "the", "of", "in", "on", "at", "to", "for", "from", "by", "with", "as", "and",
        "or", "but", "nor", "is", "are", "was", "were", "be", "been", "being", "has", "have", "had",
        "this", "that", "these", "those", "which", "who", "whom", "whose", "it", "its", "we", "our",
        "they", "their", "than", "then", "there", "here", "not", "no", "can", "may", "might",
        "will", "would", "should", "could", "into", "onto", "over", "under", "between", "through",
        "during", "after", "before", "while", "where", "when", "also", "both", "each", "such",
        "only",
    }
)


def needs_translation(source: str) -> bool:
    """原文に、訳すべき英語の文（または見出し）が含まれるか。

    URL・DOI・メールアドレス・論文の定型句・タグを除いたうえで、次をどちらも満たすときだけ「英語」とみなす：
    - 数式の語（min, cos など）を除いた小文字の単語（3 文字以上）が 3 つ以上
    - 英字の語のうち小文字で始まる語が 4 割以上、または機能語（the, of, is など）が 4 種類以上
      （所属・住所・人名の並びは大文字で始まる語ばかりで、機能語も少ない）
    当てはまらないもの：DOI や URL だけの行、所属とメールアドレス、著作権表示、ORCID、数式の切れ端、
    固有名詞だけの見出しなど。翻訳エンジンが原文のまま返しても、訳せなかったことにはしない。
    """
    text = _NOT_WORDS.sub(" ", _TAG.sub(" ", source))
    prose = [w for w in _ENGLISH_WORD.findall(text) if w not in _MATH_WORDS]
    if len(prose) < 3:
        return False
    words = _WORD.findall(text)
    if sum(1 for w in words if w[0].islower()) / len(words) >= 0.4:
        return True
    return len({w.lower() for w in words} & _FUNCTION_WORDS) >= 4


def looks_untranslated(source: str, translated: str) -> bool:
    """訳文が英語のまま（かなも漢字も無い）で、原文が訳すべき英語の文（needs_translation）だったか。

    翻訳エンジンが原文をそのまま返すことがある。そのまま使うと、訳されないまま「訳した」ことになる。
    固有名詞だけの見出し（"NASA Ames"）や URL・DOI だけの行などは、原文のまま返っても当てはまらない。
    """
    if _JAPANESE.search(translated):
        return False
    return needs_translation(source)


def normalize_translation(markup: str) -> str:
    text = _LEAKED_TAG.sub("", markup)
    text = _COMBINING_RUN.sub("", text)
    text = _INNER_SPACE_OPEN.sub("（", text)
    text = _INNER_SPACE_CLOSE.sub("）", text)
    return _LATIN_PAREN.sub(lambda m: "(" + m.group(1).strip() + ")", text)
