"""画面から見た同期：同期の実行（裏で静かに・1 本ずつ）、状態、手元にない PDF の取得のキュー。

core の SyncEngine は、同期フォルダの読み書きをすべて呼んだスレッドで行う（UI のスレッドから呼んではいけない）。
ここではそれを worker で実行し、結果を Qt のシグナルで画面に伝える。
論文ごとの取得の状態は、このクラスの 1 か所に持ち、一覧の記号・フッター・詳細パネルがそれを映す。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from ..config.settings import Settings
from ..library import Library
from ..library.errors import PaperNotFound, RemoteMissing, SyncError, SyncUnavailable
from ..translate.errors import CancelledError
from . import worker

log = logging.getLogger(__name__)

SYNC_INTERVAL_MS = 60_000  # 定期の同期
LOCAL_DEBOUNCE_MS = 2_000  # ローカルの変更から同期までの待ち（続けて来た変更はまとめる）
STALL_SECONDS = 30  # 取得がこの時間まったく進まなければ失敗とみなす

# 同期の状態（サイドバーの状態の行が映す）
OFF, IDLE, SYNCING, ERROR, UNAVAILABLE = "off", "idle", "syncing", "error", "unavailable"


@dataclass
class Download:
    """1 本の論文の取得（原文・訳文）。"""

    paper_id: int
    kinds: list[str]  # まだ取得していないもの（"original" / "translation"）
    purpose: str = ""  # open（取得したら開く）/ keep（このパソコンに保存するだけ）
    state: str = "queued"  # queued / running / waiting（同期先にまだ届いていない）/ failed
    done: int = 0
    total: int = 0  # 分からなければ 0
    error: BaseException | None = None
    last_progress: float = 0.0

    @property
    def percent(self) -> int | None:
        return self.done * 100 // self.total if self.total else None


EngineFactory = Callable[[Settings, Library], object]


def default_engine(settings: Settings, library: Library):
    from ..library.sync import FOLDER_NAME, FolderStorage, SyncEngine, load_device

    return SyncEngine(library, FolderStorage(Path(settings.sync_folder) / FOLDER_NAME), load_device())


def sync_root(settings: Settings) -> Path:
    from ..library.sync import FOLDER_NAME

    return Path(settings.sync_folder) / FOLDER_NAME


class SyncController(QObject):
    stateChanged = Signal()
    remoteChanged = Signal(object)  # list[RemoteChange]
    downloadChanged = Signal(int)  # 論文の取得の状態が変わった
    downloadFinished = Signal(int, str)  # (paper_id, purpose)
    downloadFailed = Signal(int, object)  # (paper_id, 例外)
    synced = Signal(object)  # SyncReport（1 回の同期が終わった）
    _local = Signal()  # ライブラリの変更（どのスレッドからでも送れる）
    _remote = Signal(object)

    def __init__(self, settings: Settings, library: Library, engine_factory: EngineFactory | None = None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.library = library
        self._factory = engine_factory or default_engine
        self.engine = None
        self.state = OFF
        self.last_saved: float | None = None
        self.last_error: BaseException | None = None
        self.last_report = None
        self._running = False
        self._again = False
        self._sync_task: worker.Task | None = None
        self._sync_done = threading.Event()  # 同期のワーカーが終わったら set（閉じるときに待つ）
        self._sync_done.set()
        self._retired: list = []  # 同期の途中で止めたエンジン（同期が終わってから閉じる）
        self._engine_unsub: Callable[[], None] | None = None
        self.downloads: dict[int, Download] = {}
        self._order: list[int] = []
        self._current: int | None = None
        self._task: worker.Task | None = None

        self._debounce = QTimer(self, singleShot=True, interval=LOCAL_DEBOUNCE_MS, timeout=self.sync_now)
        self._periodic = QTimer(self, interval=SYNC_INTERVAL_MS, timeout=self.sync_now)
        self._stall = QTimer(self, interval=1000, timeout=self._check_stall)
        self._local.connect(self._debounce.start)
        self._remote.connect(self.remoteChanged)
        self._library_unsub = library.add_listener(lambda _e: self._local.emit())
        self.configure()

    # -- 設定 ------------------------------------------------------------------

    def enabled(self) -> bool:
        return self.engine is not None

    def configure(self) -> None:
        """設定に合わせて同期を始める・止める（設定を変えたら呼ぶ）。"""
        self._stop_engine()
        if self.settings.sync_enabled and self.settings.sync_folder:
            try:
                self.engine = self._factory(self.settings, self.library)
            except Exception as e:  # 端末 ID が書けないなど
                log.exception("could not start sync")
                self.engine = None
                self._set_state(ERROR, e)
                return
            self._engine_unsub = self.engine.add_remote_listener(self._remote.emit)
            self._periodic.start()
            self._set_state(IDLE)
            self.sync_now()
        else:
            self._set_state(OFF)

    def _stop_engine(self) -> None:
        self._periodic.stop()
        self._debounce.stop()
        if self._engine_unsub is not None:
            self._engine_unsub()
            self._engine_unsub = None
        if self.engine is not None:
            if self._running:
                # 同期の途中で閉じると sync.sqlite3 を書いている最中に閉じてしまうので、キャンセルして終わってから閉じる
                if self._sync_task is not None:
                    self._sync_task.cancel()
                self._retired.append(self.engine)
            else:
                self._close_engine(self.engine)
            self.engine = None
        for pid in list(self.downloads):
            self.cancel_download(pid)

    def close(self, wait: float = 3.0) -> None:
        """アプリを閉じるとき。同期の途中なら、キャンセルして少しだけ終わるのを待つ。"""
        self._stop_engine()
        self._library_unsub()
        if not self._sync_done.wait(wait):
            log.warning("sync did not stop in time")
        self._close_retired()

    @staticmethod
    def _close_engine(engine) -> None:
        try:
            engine.close()
        except Exception:
            log.debug("engine close failed", exc_info=True)

    def _close_retired(self) -> None:
        while self._retired and self._sync_done.is_set():
            self._close_engine(self._retired.pop())

    def rebind(self, library: Library) -> None:
        """ライブラリを開き直したとき（同期前の状態に戻したときなど）に、新しいライブラリにつなぎ直す。"""
        self.library = library
        self._library_unsub = library.add_listener(lambda _e: self._local.emit())
        self.configure()

    def backup_path(self):
        """初回の同期の前に作ったバックアップ（なければ None）。"""
        try:
            return self.engine.backup_path if self.engine is not None else None
        except Exception:
            log.debug("backup_path failed", exc_info=True)
            return None

    # -- 同期 ------------------------------------------------------------------

    def sync_now(self) -> None:
        """裏で同期する。同期中に呼ばれたら、終わってからもう一度だけ行う。"""
        if self.engine is None:
            return
        if self._running:
            self._again = True
            return
        self._running = True
        self._again = False
        engine = self.engine
        self._set_state(SYNCING)
        self._sync_done.clear()

        def run(_r, cancel):
            try:
                return engine.sync_now(cancel)
            finally:
                self._sync_done.set()

        self._sync_task = worker.start(run, self._on_synced, self._on_sync_failed)

    def _on_synced(self, report) -> None:
        self._running = False
        self._sync_task = None
        self._close_retired()
        if self.engine is None:  # 同期の途中で止めた
            return
        self.last_report = report
        self.last_saved = time.time()
        self._set_state(ERROR if getattr(report, "errors", None) else IDLE)
        self.synced.emit(report)
        if self._again:
            self.sync_now()

    def _on_sync_failed(self, err: BaseException) -> None:
        self._running = False
        self._sync_task = None
        self._close_retired()
        if self.engine is None or isinstance(err, CancelledError):
            if self.engine is not None:
                self._set_state(IDLE)
            return
        log.warning("sync failed: %s", err)
        self._set_state(UNAVAILABLE if isinstance(err, SyncUnavailable) else ERROR, err)
        if self._again:
            self.sync_now()

    def _set_state(self, state: str, error: BaseException | None = None) -> None:
        self.state = state
        self.last_error = error
        self.stateChanged.emit()

    def available(self) -> bool:
        """同期先を最後に読めたか（読めないなら、手元にない論文は開けない）。"""
        return self.engine is not None and self.state != UNAVAILABLE

    # -- 手元にあるか ----------------------------------------------------------

    @staticmethod
    def missing_kinds(paper) -> list[str]:
        kinds = []
        if not paper.has_original:
            kinds.append("original")
        if paper.translation is not None and not paper.has_translation_file:
            kinds.append("translation")
        return kinds

    def needs_download(self, paper) -> bool:
        return bool(self.missing_kinds(paper))

    # -- 取得のキュー ------------------------------------------------------------

    def download(self, paper_id: int, purpose: str = "keep") -> bool:
        """手元にない原文・訳文を取得する。既に待っている論文なら、目的だけを「開く」に強める。"""
        d = self.downloads.get(paper_id)
        if d is not None and d.state in ("queued", "running"):
            if purpose == "open":
                d.purpose = "open"
            return False
        paper = self.library.get(paper_id)
        if paper is None or self.engine is None:
            return False
        kinds = self.missing_kinds(paper)
        if not kinds:
            self.downloadFinished.emit(paper_id, purpose)
            return False
        self.downloads[paper_id] = Download(paper_id, kinds, purpose)
        self._order.append(paper_id)
        self.downloadChanged.emit(paper_id)
        self.stateChanged.emit()
        self._start_next()
        return True

    def download_all(self) -> int:
        """手元にないすべての論文を取得する（「このパソコンにすべて取得」）。"""
        n = 0
        for p in self.library.papers():
            if self.needs_download(p) and self.download(p.id, "keep"):
                n += 1
        return n

    def cancel_download(self, paper_id: int) -> None:
        d = self.downloads.get(paper_id)
        if d is None:
            return
        if self._current == paper_id and self._task is not None:
            self._task.cancel()
            return
        self.downloads.pop(paper_id, None)
        if paper_id in self._order:
            self._order.remove(paper_id)
        self.downloadChanged.emit(paper_id)
        self.stateChanged.emit()

    def download_state(self, paper_id: int) -> Download | None:
        return self.downloads.get(paper_id)

    def active_downloads(self) -> int:
        return sum(1 for d in self.downloads.values() if d.state in ("queued", "running"))

    def _start_next(self) -> None:
        if self._current is not None or self.engine is None:
            return
        while self._order:
            pid = self._order.pop(0)
            d = self.downloads.get(pid)
            if d is not None and d.state == "queued":
                break
        else:
            return
        self._current = pid
        d.state = "running"
        d.last_progress = time.monotonic()
        engine = self.engine
        self.downloadChanged.emit(pid)

        def run(report, cancel):
            for kind in list(d.kinds):
                engine.fetch(pid, kind, progress=lambda done, total: report((done, total)), cancel=cancel)
                d.kinds.remove(kind)
            return pid

        self._task = worker.start(run, lambda _r: self._on_fetched(pid), lambda e: self._on_fetch_failed(pid, e), self._on_progress)
        self._stall.start()

    def _on_progress(self, value) -> None:
        d = self.downloads.get(self._current) if self._current is not None else None
        if d is None:
            return
        d.done, d.total = value
        d.last_progress = time.monotonic()
        self.downloadChanged.emit(d.paper_id)

    def _check_stall(self) -> None:
        d = self.downloads.get(self._current) if self._current is not None else None
        if d is None:
            self._stall.stop()
            return
        if time.monotonic() - d.last_progress > STALL_SECONDS and self._task is not None:
            # 30 秒間まったく進まない：同期アプリが止まっているなど。キャンセルして失敗として知らせる
            d.error = TimeoutError("stalled")
            self._task.cancel()

    def _finish_current(self) -> None:
        self._current = None
        self._task = None
        self._stall.stop()

    def _on_fetched(self, pid: int) -> None:
        self._finish_current()
        d = self.downloads.pop(pid, None)
        self.downloadChanged.emit(pid)
        self.downloadFinished.emit(pid, d.purpose if d else "keep")
        self.stateChanged.emit()
        self._start_next()

    def _on_fetch_failed(self, pid: int, err: BaseException) -> None:
        self._finish_current()
        d = self.downloads.get(pid)
        if d is not None and isinstance(d.error, TimeoutError):
            err = StalledError()
        if isinstance(err, (CancelledError, PaperNotFound)):
            self.downloads.pop(pid, None)
        elif d is not None:
            # 同期先にまだ届いていないものは「到着待ち」、それ以外は「失敗」として残し、行と詳細パネルに出す
            d.state = "waiting" if isinstance(err, RemoteMissing) else "failed"
            d.error = err
            if isinstance(err, SyncUnavailable):
                self._set_state(UNAVAILABLE, err)
        self.downloadChanged.emit(pid)
        if not isinstance(err, (CancelledError, PaperNotFound)):
            self.downloadFailed.emit(pid, err)
        self.stateChanged.emit()
        self._start_next()

    def fetch_blocking(self, paper_id: int, cancel: threading.Event) -> None:
        """（ワーカーから）翻訳の前に原文を手元に用意する。"""
        if self.engine is None:
            return
        self.engine.fetch(paper_id, "original", cancel=cancel)

    # -- このパソコンのコピーを削除 -------------------------------------------------

    def evict(
        self,
        paper_ids: list[int],
        on_done: Callable[[], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        """このパソコンのコピーを削除する。同期先にまだ書き出していない論文は、core が RemoteMissing を出して消さない。"""
        engine = self.engine
        if engine is None:
            return

        def run(_r, _c):
            for pid in paper_ids:
                engine.evict(pid)

        def failed(e):
            log.warning("evict failed: %s", e)
            if on_error:
                on_error(e)

        worker.start(run, lambda _r: on_done and on_done(), failed)


class StalledError(SyncError):
    title = "ファイルを取得できませんでした"
    hint = "同期アプリ（Google Drive など）が動いているか確認してください。"
