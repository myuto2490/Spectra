"""画面から見た本文検索：索引づくり（裏で、低い優先度で、差分だけ）と、本文の問い合わせ。

- 起動の数秒後に、ライブラリと索引の差分を取って索引にする。ライブラリの変更・同期・取得のたびに、
  少し待ってから差分を取り直す（続けて来た変更はまとめる）。
- 1 本ずつ索引にし、その間に少し譲る（UI と翻訳を重くしない）。キャンセルできる。
- 問い合わせも裏で行い、古い問い合わせの結果は捨てる。
- 索引を使えなくなったら（壊れた・ディスクがいっぱい など）、本文の検索をやめ、書誌の検索だけに退く。
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ..config import paths
from ..config.settings import Settings
from ..library import Library
from ..library.fulltext import (
    ORIGINAL,
    TRANSLATED,
    FulltextBusy,
    FulltextIndex,
    FulltextUnavailable,
    Source,
    Stats,
    delete_index_files,
    query_terms,
)
from . import worker

log = logging.getLogger(__name__)

STARTUP_DELAY_MS = 5_000  # 起動の直後は待ってから索引づくりを始める
CHANGE_DELAY_MS = 2_000  # ライブラリの変更から差分を取るまでの待ち
QUERY_DELAY_MS = 250  # 入力から問い合わせまでの待ち
YIELD_SECONDS = 0.05  # 1 本ごとに譲る時間
BUSY_RETRY_MS = 30_000  # ほかの Spectra が索引を使っていたとき、索引づくりをやり直すまでの待ち


def sources_for(library: Library, *, translations: bool = True) -> list[Source]:
    """索引にする PDF の一覧（ゴミ箱の論文も含める。戻したときに作り直さずに済むように）。"""
    out = []
    for p in [*library.papers(), *library.trashed_papers()]:
        out.append(Source(p.sha256, ORIGINAL, p.pdf_path))
        if translations and p.translation is not None:
            out.append(Source(p.sha256, TRANSLATED, p.translated_path))
    return out


def run_pass(index: FulltextIndex, sources: list[Source], report, cancel: threading.Event, *, pause: float = YIELD_SECONDS) -> int:
    """差分を索引にする。索引にした数を返す。report((済み, 全部))。"""
    index.prune({(s.sha256, s.kind) for s in sources})
    todo = index.stale(sources)
    if not todo:
        return 0
    done = 0
    report((0, len(todo)))
    for s in todo:
        if cancel.is_set():
            break
        index.index(s, cancel)
        done += 1
        report((done, len(todo)))
        if pause:
            time.sleep(pause)
    if done >= 20:
        index.optimize()
    return done


class FulltextController(QObject):
    progressChanged = Signal()  # 索引づくりの進み具合・状態が変わった
    resultsReady = Signal(str, object)  # (問い合わせの文字列, dict[sha256, Match] か None)
    _changed = Signal()  # ライブラリの変更（どのスレッドからでも送れる）

    def __init__(self, settings: Settings, library: Library, parent=None, *, index_path: Path | None = None,
                 startup_delay_ms: int = STARTUP_DELAY_MS, change_delay_ms: int = CHANGE_DELAY_MS,
                 query_delay_ms: int = QUERY_DELAY_MS, pause: float = YIELD_SECONDS):
        super().__init__(parent)
        self.settings = settings
        self.library = library
        self.index_path = index_path or paths.fulltext_index_file()
        self.pause = pause
        self.index: FulltextIndex | None = None
        self.unavailable: str = ""  # 使えない理由（空なら使える）
        self.progress: tuple[int, int] | None = None  # 索引づくりの (済み, 全部)。していなければ None
        self._pass: worker.Task | None = None
        self._pass_gen = 0
        self._idle = threading.Event()  # 索引づくりのスレッドが動いていない
        self._idle.set()
        self._dirty = False
        self._closed = False
        self._query_text = ""
        self._query_gen = 0
        self._query_task: worker.Task | None = None
        self._pass_timer = QTimer(self, singleShot=True, interval=change_delay_ms, timeout=self._start_pass)
        self._query_timer = QTimer(self, singleShot=True, interval=query_delay_ms, timeout=self._start_query)
        self._changed.connect(lambda: self.schedule())
        self._unlisten = library.add_listener(self._on_library_event)
        if self.enabled:
            self._pass_timer.start(startup_delay_ms)

    # -- 状態 ----------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.fulltext_search)

    @property
    def available(self) -> bool:
        return self.enabled and not self.unavailable

    @property
    def indexing(self) -> bool:
        return self._pass is not None

    def _open(self) -> FulltextIndex:
        """索引を開く（GUI のスレッドでも裏のスレッドでも呼ばれる）。オフ・終了の後は開かない（ファイルを作らない）。"""
        if self._closed or not self.enabled:
            raise FulltextUnavailable("fulltext search is off")
        if self.index is None:
            self.index = FulltextIndex(self.index_path)
        return self.index

    def _fail(self, e: BaseException) -> None:
        log.warning("fulltext search is unavailable: %s", e)
        self.unavailable = str(e) or type(e).__name__
        self.progressChanged.emit()

    def stats(self) -> Stats | None:
        """索引の件数と大きさ（設定の画面に出す）。使えなければ None。"""
        if not self.enabled:
            return None
        try:
            return self._open().stats()
        except FulltextBusy:
            return None
        except FulltextUnavailable as e:
            if self.enabled and not self._closed:
                self._fail(e)
            return None

    # -- 索引づくり ----------------------------------------------------------

    def _on_library_event(self, event) -> None:
        if getattr(event, "kind", "") in ("added", "translation", "removed"):
            with contextlib.suppress(RuntimeError):  # 終了の途中
                self._changed.emit()

    def schedule(self, delay_ms: int | None = None) -> None:
        """少し待ってから差分を取る（同期の後・取得の後などにも呼ぶ）。"""
        if self._closed or not self.available:
            return
        if self._pass is not None:
            self._dirty = True
            return
        if delay_ms is None:
            if not self._pass_timer.isActive():
                self._pass_timer.start()
        else:
            self._pass_timer.start(delay_ms)

    def _start_pass(self) -> None:
        if self._closed or not self.available or self._pass is not None:
            return
        self._dirty = False
        translations = bool(self.settings.fulltext_translations)
        self._pass_gen += 1
        gen = self._pass_gen
        self._idle.clear()

        def run(report, cancel):
            try:
                QThread.currentThread().setPriority(QThread.Priority.LowPriority)
                sources = sources_for(self.library, translations=translations)
                return run_pass(self._open(), sources, report, cancel, pause=self.pause)
            finally:
                self._idle.set()

        self._pass = worker.start(
            run,
            lambda n: gen == self._pass_gen and self._pass_done(n),
            lambda e: gen == self._pass_gen and self._pass_failed(e),
            lambda v: gen == self._pass_gen and self._pass_progress(v),
        )

    def _pass_progress(self, value) -> None:
        self.progress = value
        self.progressChanged.emit()

    def _pass_done(self, count) -> None:
        cancelled = self._pass is not None and self._pass.cancel_event.is_set()
        self._pass = None
        self.progress = None
        self.progressChanged.emit()
        if count and self._query_text:
            self.query(self._query_text, immediate=True)  # 索引が増えたので、今の検索を問い合わせ直す
        if self._dirty and not cancelled:
            self.schedule()

    def _pass_failed(self, e) -> None:
        self._pass = None
        self.progress = None
        if isinstance(e, FulltextBusy):
            log.info("fulltext index is busy, retrying later: %s", e)
            self.progressChanged.emit()
            self.schedule(BUSY_RETRY_MS)
        elif isinstance(e, FulltextUnavailable):
            if self.enabled and not self._closed:
                self._fail(e)
        else:
            log.warning("fulltext indexing failed: %r", e)
            self.progressChanged.emit()

    def cancel(self) -> None:
        """今の索引づくりを止める（次の変更までは再開しない）。"""
        self._pass_timer.stop()
        if self._pass is not None:
            self._pass.cancel()
            self._dirty = False

    # -- 設定 ----------------------------------------------------------------

    def apply_settings(self) -> None:
        """本文検索のオン・オフ、訳文を含めるか、が変わったとき。"""
        if not self.enabled:
            self._stop_pass()
            self._close_index()
            self.progressChanged.emit()
            self.resultsReady.emit(self._query_text, None)
            return
        self.unavailable = ""
        self.schedule(0)
        self.progressChanged.emit()

    def rebuild(self) -> None:
        """索引を消して作り直す。"""
        self._reset(delete=False)
        self.unavailable = ""
        self.schedule(0)
        self.progressChanged.emit()

    def delete_index(self) -> None:
        """索引のファイルを消す（呼び出し側が本文検索をオフにする）。"""
        self._reset(delete=True)
        self.progressChanged.emit()

    def _reset(self, *, delete: bool) -> None:
        self._stop_pass()
        try:
            if delete:
                self._close_index()
                delete_index_files(self.index_path)  # 開かずに消す（空の索引を作らない）
            else:
                index = self.index or FulltextIndex(self.index_path)
                self.index = None
                index.clear()
                self.index = index
        except FulltextBusy as e:
            log.warning("fulltext index is busy: %s", e)
        except FulltextUnavailable as e:
            self._fail(e)

    def _stop_pass(self) -> None:
        """索引づくりを止め、スレッドが索引から手を離すまで待つ（知らせは捨てる）。"""
        self.cancel()
        self._pass_gen += 1
        self._pass = None
        self.progress = None
        if not self._idle.wait(10):
            log.warning("fulltext indexing did not stop in time")

    def _close_index(self) -> None:
        if self.index is not None:
            self.index.close()
            self.index = None

    # -- 問い合わせ ----------------------------------------------------------

    def query(self, text: str, *, immediate: bool = False) -> None:
        """本文を探す。結果は resultsReady で返す（空の文字列なら、すぐ空の結果）。"""
        self._query_text = text
        self._query_gen += 1
        if self._query_task is not None:
            self._query_task.cancel()
            self._query_task = None
        if not query_terms(text) or not self.available:
            self._query_timer.stop()
            self.resultsReady.emit(text, None)
            return
        if immediate:
            self._query_timer.stop()
            self._start_query()
        else:
            self._query_timer.start()

    def _start_query(self) -> None:
        if self._closed or not self.available:
            return
        text, gen = self._query_text, self._query_gen
        terms = query_terms(text)

        def run(_report, cancel):
            return self._open().search(terms, cancel=cancel)

        def done(found):
            if gen == self._query_gen:
                self._query_task = None
                self.resultsReady.emit(text, found)

        def failed(e):
            if gen == self._query_gen:
                self._query_task = None
                if isinstance(e, FulltextBusy):
                    log.info("fulltext query skipped: %s", e)  # その回だけ書誌の検索に退く
                elif isinstance(e, FulltextUnavailable):
                    if self.enabled and not self._closed:
                        self._fail(e)
                else:
                    log.warning("fulltext query failed: %r", e)
                self.resultsReady.emit(text, None)

        self._query_task = worker.start(run, done, failed)

    # -- 終了 ----------------------------------------------------------------

    def close(self) -> None:
        self._closed = True
        self._unlisten()
        self._pass_timer.stop()
        self._query_timer.stop()
        if self._pass is not None:
            self._pass.cancel()
        if self._query_task is not None:
            self._query_task.cancel()
        if self._idle.wait(3):
            self._close_index()
