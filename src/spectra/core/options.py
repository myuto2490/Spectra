"""翻訳ジョブの設定値。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OutputMode(str, Enum):
    TRANSLATED = "translated"  # 翻訳のみ
    SIDE_BY_SIDE = "side_by_side"  # 左右対訳
    INTERLEAVE = "interleave"  # 交互対訳


class Formality(str, Enum):
    DEFAULT = "default"
    PLAIN = "prefer_less"  # である調
    POLITE = "prefer_more"  # です・ます調


@dataclass(frozen=True)
class AnalysisOptions:
    """どのテキストを翻訳対象とするか。"""

    first_page: int = 0  # 0 始まり
    last_page: int | None = None  # 含む。None で最終ページまで
    translate_captions: bool = True
    translate_footnotes: bool = True
    skip_references: bool = True
    merge_split_paragraphs: bool = True


@dataclass(frozen=True)
class RenderOptions:
    """訳文の組版設定。"""

    font_path: str | None = None  # None なら内蔵フォント
    bold_font_path: str | None = None  # 見出し用の太字（無ければ擬似ボールド）
    font_is_serif: bool = False
    text_scale: float = 0.95  # 原文サイズに対する和文の基準倍率
    min_scale: float = 0.45  # これ以上は縮小しない（それでも収まらなければ末尾を省略せず更に縮小）
    output_mode: OutputMode = OutputMode.TRANSLATED


@dataclass(frozen=True)
class TranslateOptions:
    source_lang: str = "EN"
    target_lang: str = "JA"
    formality: Formality = Formality.DEFAULT
