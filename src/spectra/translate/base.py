"""翻訳エンジンの共通基盤：バッチ分割・リトライ・キャンセル。"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import ClassVar

import httpx

from ..core.options import TranslateOptions
from .errors import CancelledError, NetworkError, RateLimitError, ServiceError, TranslatorError

log = logging.getLogger(__name__)

USER_AGENT = "Spectra (+https://github.com/myuto2490/Spectra)"
RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)
MAX_RETRY_AFTER = 60.0


@dataclass(frozen=True)
class Usage:
    """今月の使用量。limit が None のときは API から取得できない。"""

    used: int
    limit: int | None

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)


class Translator(ABC):
    engine_id: ClassVar[str]
    display_name: ClassVar[str]
    max_items: ClassVar[int] = 50
    max_chars: ClassVar[int] = 20_000
    supports_formality: ClassVar[bool] = False
    retry_delays: ClassVar[tuple[float, ...]] = RETRY_DELAYS

    def __init__(
        self,
        api_key: str = "",
        options: TranslateOptions | None = None,
        client: httpx.Client | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.api_key = api_key.strip()
        self.options = options or TranslateOptions()
        self._client = client
        self._owns_client = client is None
        self.cancel_event = cancel_event or threading.Event()
        self.sleep: Callable[[float], None] = self._interruptible_sleep

    # -- サブクラスで実装 ----------------------------------------------------

    @abstractmethod
    def _translate_batch(self, texts: list[str]) -> list[str | None]:
        """1 リクエスト分を翻訳する。texts はマークアップ文字列。

        訳せなかった項目は None を返す（その段落は原文のまま一切変更しない）。
        """

    def usage(self) -> Usage | None:
        return None

    def verify(self) -> Usage | None:
        """API キーが有効か確認する。無効なら例外を送出する。"""
        return self.usage()

    def cache_namespace(self) -> str:
        """キャッシュキーに含める、訳文に影響する設定。"""
        o = self.options
        return f"{self.engine_id}|{o.source_lang}|{o.target_lang}"

    # -- 公開 API --------------------------------------------------------

    def translate(self, texts: list[str], on_batch: Callable[[int], None] | None = None) -> list[str | None]:
        """任意件数を翻訳する。on_batch は完了した件数を通知する。"""
        out: list[str | None] = []
        for batch in self.batches(texts):
            self._check_cancel()
            result = self._with_retry(lambda b=batch: self._translate_batch(b))
            if len(result) != len(batch):
                raise ServiceError(f"翻訳結果の件数が一致しません（{len(batch)} 件中 {len(result)} 件）")
            out.extend(result)
            if on_batch:
                on_batch(len(batch))
        return out

    def batches(self, texts: list[str]) -> Iterator[list[str]]:
        batch: list[str] = []
        chars = 0
        for t in texts:
            if batch and (len(batch) >= self.max_items or chars + len(t) > self.max_chars):
                yield batch
                batch, chars = [], 0
            batch.append(t)
            chars += len(t)
        if batch:
            yield batch

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
        self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- 内部 ------------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(60.0, connect=10.0),
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise CancelledError

    def _interruptible_sleep(self, seconds: float) -> None:
        if self.cancel_event.wait(seconds):
            raise CancelledError

    def _with_retry(self, fn):
        attempt = 0
        while True:
            try:
                return fn()
            except httpx.TimeoutException as e:
                err: TranslatorError = NetworkError("タイムアウトしました")
                cause: Exception = e
                retry_after = None
            except httpx.TransportError as e:
                err, cause, retry_after = NetworkError(), e, None
            except TranslatorError as e:
                if not e.retryable:
                    raise
                err, cause = e, e
                retry_after = getattr(e, "retry_after", None)
            if attempt >= len(self.retry_delays):
                raise err from cause
            delay = min(retry_after, MAX_RETRY_AFTER) if retry_after else self.retry_delays[attempt]
            log.info("%s: retry %d after %.1fs (%s)", self.engine_id, attempt + 1, delay, type(err).__name__)
            self.sleep(delay)
            self._check_cancel()
            attempt += 1


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def rate_limited(response: httpx.Response) -> RateLimitError:
    err = RateLimitError()
    err.retry_after = retry_after_seconds(response)  # type: ignore[attr-defined]
    return err


def mask(key: str) -> str:
    """ログ用に API キーを伏せる。"""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"
