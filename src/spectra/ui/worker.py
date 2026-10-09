"""時間のかかる処理をバックグラウンドで実行し、結果を GUI スレッドへ通知する。"""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)


class _Signals(QObject):
    finished = Signal(object)
    failed = Signal(object)
    progress = Signal(object)


class Task(QRunnable):
    """fn(report, cancel_event) を実行する。report(x) で進捗を通知できる。"""

    def __init__(self, fn: Callable[[Callable[[object], None], threading.Event], object]):
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.cancel_event = threading.Event()
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn(self._emit_progress, self.cancel_event)
        except BaseException as e:  # noqa: BLE001 - GUI へそのまま伝える
            log.debug("task failed:\n%s", traceback.format_exc())
            self._emit(self.signals.failed, e)
        else:
            self._emit(self.signals.finished, result)

    def _emit_progress(self, value: object) -> None:
        self._emit(self.signals.progress, value)

    @staticmethod
    def _emit(signal, value: object) -> None:
        # アプリ終了時など、受け手が既に破棄されている場合は通知しない
        try:
            signal.emit(value)
        except RuntimeError:
            log.debug("signal receiver is gone")

    def cancel(self) -> None:
        self.cancel_event.set()


# 実行中のタスクへの参照。保持しないと完了通知が届く前に Python 側で解放されてしまう
_active: set[Task] = set()


def start(
    fn: Callable[[Callable[[object], None], threading.Event], object],
    on_done: Callable[[object], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    on_progress: Callable[[object], None] | None = None,
) -> Task:
    task = Task(fn)
    if on_done:
        task.signals.finished.connect(on_done)
    if on_error:
        task.signals.failed.connect(on_error)
    if on_progress:
        task.signals.progress.connect(on_progress)
    _active.add(task)
    task.signals.finished.connect(lambda _r: _active.discard(task))
    task.signals.failed.connect(lambda _e: _active.discard(task))
    QThreadPool.globalInstance().start(task)
    return task
