"""キャッシュを考慮した一括翻訳。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..core.textnorm import looks_untranslated
from .base import Translator
from .cache import TranslationCache, cache_key
from .errors import CancelledError, NoValidResponseError, TranslatorError

ProgressFn = Callable[[int, int], None]  # (完了文字数, 全文字数)


@dataclass
class TranslationPlan:
    """翻訳前の見積もり。"""

    unique_texts: list[str]
    cached: dict[str, str]
    total_chars: int
    cached_chars: int

    @property
    def pending(self) -> list[str]:
        return [t for t in self.unique_texts if t not in self.cached]

    @property
    def pending_chars(self) -> int:
        return self.total_chars - self.cached_chars


def plan(texts: list[str], namespace: str, cache: TranslationCache | None) -> TranslationPlan:
    unique = list(dict.fromkeys(texts))
    cached: dict[str, str] = {}
    if cache is not None and unique:
        keys = {t: cache_key(namespace, t) for t in unique}
        hits = cache.get_many(list(keys.values()))
        # 以前に英語のまま返された訳（キャッシュに残っている）は使わず、訳し直す
        cached = {t: hits[k] for t, k in keys.items() if k in hits and not looks_untranslated(t, hits[k])}
    total = sum(len(t) for t in unique)
    return TranslationPlan(unique, cached, total, sum(len(t) for t in cached))


class PartialTranslationError(TranslatorError):
    """途中で失敗した。訳せた分は results に入っている（キャッシュにも保存済み）。"""

    def __init__(self, cause: TranslatorError, results: dict[str, str]):
        super().__init__(cause.detail, hint=cause.hint)
        self.title = cause.title
        self.cause = cause
        self.results = results


def translate_texts(
    texts: list[str],
    translator: Translator,
    cache: TranslationCache | None = None,
    progress: ProgressFn | None = None,
) -> dict[str, str]:
    """texts を翻訳し {原文: 訳文} を返す。訳せたバッチから順にキャッシュへ保存する。"""
    namespace = translator.cache_namespace()
    p = plan(texts, namespace, cache)
    results = dict(p.cached)
    done = p.cached_chars
    if progress:
        progress(done, p.total_chars)
    try:
        for batch in translator.batches(p.pending):
            translated = translator.translate(batch)
            # 英語のまま返った項目も、訳せなかったものとして扱う（原文のまま残し、警告に出す）
            new = {s: t for s, t in zip(batch, translated) if t and not looks_untranslated(s, t)}
            results.update(new)
            if cache is not None:
                cache.put_many({cache_key(namespace, s): t for s, t in new.items()})
            done += sum(len(s) for s in batch)
            if progress:
                progress(done, p.total_chars)
    except TranslatorError as e:
        if results and not isinstance(e, (PartialTranslationError, CancelledError)):
            raise PartialTranslationError(e, results) from e
        raise
    if p.pending and not results:
        # 1 件も訳せなかった（原文のままの PDF を「完了」として作らない）
        raise NoValidResponseError("訳せた段落が 1 つもありませんでした。")
    return results
