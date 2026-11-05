"""ライブラリの論文に対するバックグラウンド処理：翻訳キューと書誌情報の照会。

翻訳は API の回数制限と無料枠を考えて 1 本ずつ順に実行する。
キューはアプリを閉じると破棄する（翻訳済みの段落はキャッシュに残るので、やり直しても無料枠はほとんど使わない）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

from ..core.options import AnalysisOptions, RenderOptions, TranslateOptions
from ..library import Library, LookupFailed
from . import worker

log = logging.getLogger(__name__)


@dataclass
class TranslationRequest:
    """翻訳 1 件分の指示。設定はキューに入れた時点の値を使う。"""

    paper_id: int
    title: str
    engine: str
    model: str
    api_key: str  # キーを 1 本だけ直接渡すとき（キーの目録を使うなら "" にして keys を渡す）
    auto_fallback: bool
    translate_options: TranslateOptions
    analysis_options: AnalysisOptions
    render_options: RenderOptions
    verify: bool = True
    cache: object = None  # TranslationCache（キャッシュを使わない設定なら None）
    allow_partial: bool = False
    options: dict = field(default_factory=dict)  # ライブラリに記録する設定（表示用）
    keys: list = field(default_factory=list)  # config.secrets.KeyRef のリスト（使う順）。空なら api_key を使う
    key_auto_switch: bool = True  # 1 日の上限に達したら、次のキーに切り替える


@dataclass(frozen=True)
class TranslationNotice:
    """翻訳の途中の出来事（キーの自動切り替えなど）。進捗の代わりに report で送ると、notice シグナルで届く。"""

    text: str


Runner = Callable[[TranslationRequest, Callable[[object], None], threading.Event], object]


class TranslationQueue(QObject):
    """翻訳を 1 件ずつ順に実行する。"""

    changed = Signal()  # 待ち行列・実行中の論文が変わった
    progress = Signal(int, object)  # (paper_id, pipeline.Progress)
    finished = Signal(object, object)  # (TranslationRequest, JobResult)
    failed = Signal(object, object)  # (TranslationRequest, 例外)
    notice = Signal(int, str)  # (paper_id, 翻訳の途中の出来事。キーの切り替えなど)

    def __init__(self, runner: Runner, parent: QObject | None = None):
        super().__init__(parent)
        self._runner = runner
        self._pending: list[TranslationRequest] = []
        self._current: TranslationRequest | None = None
        self._task: worker.Task | None = None
        self.last_progress: object | None = None

    # -- 状態 ------------------------------------------------------------

    def is_active(self) -> bool:
        return self._current is not None or bool(self._pending)

    def current(self) -> TranslationRequest | None:
        return self._current

    def pending(self) -> list[TranslationRequest]:
        return list(self._pending)

    def state(self, paper_id: int) -> str | None:
        """running / queued / None"""
        if self._current is not None and self._current.paper_id == paper_id:
            return "running"
        if any(r.paper_id == paper_id for r in self._pending):
            return "queued"
        return None

    # -- 操作 ------------------------------------------------------------

    def enqueue(self, req: TranslationRequest, front: bool = False) -> bool:
        """待ち行列に加える。既に待っている・翻訳中の論文なら加えずに False を返す。"""
        if self.state(req.paper_id) is not None:
            return False
        if front:
            self._pending.insert(0, req)
        else:
            self._pending.append(req)
        self.changed.emit()
        self._start_next()
        return True

    def remove(self, paper_id: int) -> None:
        """待っている論文は取り除き、翻訳中の論文はキャンセルする。"""
        if self._current is not None and self._current.paper_id == paper_id:
            if self._task is not None:
                self._task.cancel()
                self.changed.emit()  # 「キャンセルしています…」をすぐに表示する
            return
        before = len(self._pending)
        self._pending = [r for r in self._pending if r.paper_id != paper_id]
        if len(self._pending) != before:
            self.changed.emit()

    def clear_pending(self) -> list[TranslationRequest]:
        """待っている論文をすべて取り除き、取り除いたものを返す（翻訳中の論文はそのまま）。"""
        dropped, self._pending = self._pending, []
        if dropped:
            self.changed.emit()
        return dropped

    def cancel_all(self) -> None:
        self.clear_pending()
        if self._task is not None:
            self._task.cancel()
            self.changed.emit()

    def is_cancelling(self) -> bool:
        return self._task is not None and self._task.cancel_event.is_set()

    # -- 実行 ------------------------------------------------------------

    def _start_next(self) -> None:
        if self._current is not None or not self._pending:
            return
        req = self._current = self._pending.pop(0)
        self.last_progress = None

        def run(report, cancel):
            return self._runner(req, report, cancel)

        self._task = worker.start(
            run,
            lambda result: self._on_done(req, result),
            lambda err: self._on_failed(req, err),
            lambda p: self._on_progress(req, p),
        )
        self.changed.emit()

    def _on_progress(self, req: TranslationRequest, p: object) -> None:
        if isinstance(p, TranslationNotice):  # 進捗の表示は変えずに、出来事だけを知らせる
            if req is self._current:
                self.notice.emit(req.paper_id, p.text)
            return
        if req is self._current:
            self.last_progress = p
            self.progress.emit(req.paper_id, p)

    def _finish(self) -> None:
        self._current = None
        self._task = None
        self.last_progress = None

    def _on_done(self, req: TranslationRequest, result: object) -> None:
        self._finish()
        self.finished.emit(req, result)
        self.changed.emit()
        self._start_next()

    def _on_failed(self, req: TranslationRequest, err: BaseException) -> None:
        self._finish()
        self.failed.emit(req, err)
        self.changed.emit()
        self._start_next()


class LookupQueue(QObject):
    """DOI・arXiv ID から書誌情報を照会し、ライブラリを更新する（1 件ずつ順に）。"""

    updated = Signal(int)  # 書誌情報（または照会の状態）が変わった論文
    changed = Signal()

    def __init__(self, library: Library, parent: QObject | None = None):
        super().__init__(parent)
        self.library = library
        self._pending: list[int] = []
        self._current: int | None = None

    def is_active(self) -> bool:
        return self._current is not None or bool(self._pending)

    def is_waiting(self, paper_id: int) -> bool:
        return paper_id == self._current or paper_id in self._pending

    def enqueue(self, ids: list[int]) -> None:
        added = [i for i in ids if not self.is_waiting(i)]
        if not added:
            return
        self._pending.extend(added)
        self.changed.emit()
        self._start_next()

    def _start_next(self) -> None:
        if self._current is not None or not self._pending:
            return
        pid = self._current = self._pending.pop(0)
        worker.start(lambda _r, _c: lookup_paper(self.library, pid), lambda _r: self._done(pid), lambda e: self._failed(pid, e))

    def _done(self, pid: int) -> None:
        self._current = None
        self.updated.emit(pid)
        self.changed.emit()
        self._start_next()

    def _failed(self, pid: int, err: BaseException) -> None:
        # 通信の失敗（LookupFailed）は pending のまま残し、あとで（次回の起動時にも）取得し直す
        log.warning("metadata lookup failed for paper %s: %s", pid, err)
        if not isinstance(err, LookupFailed):
            try:
                self.library.set_meta_status(pid, "failed")
            except Exception:
                log.debug("could not record lookup failure", exc_info=True)
        self._done(pid)


def lookup_paper(library: Library, paper_id: int, client=None) -> bool:
    """1 件の論文の書誌情報を照会して保存する。見つかれば True。"""
    from ..library.lookup import lookup

    paper = library.get(paper_id)
    if paper is None:
        return False
    md = paper.metadata
    if not (md.doi or md.arxiv_id):
        library.set_meta_status(paper_id, "failed")
        return False
    found = lookup(md, client)
    if found is None:
        library.set_meta_status(paper_id, "failed")
        return False
    library.update_metadata(paper_id, found.merged(md), status="done")
    return True
