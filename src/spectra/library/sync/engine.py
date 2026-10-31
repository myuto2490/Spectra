"""同期の本体。ライブラリと同期フォルダの間で、書き出しと取り込みを行う。

約束:
- 同期フォルダの読み書きは、UI のスレッドから決して行わない（すべてワーカーから呼ぶ）。
- PDF は手元のライブラリにコピーし、sha256 を検証してから使う。
- 時刻の記録（HLC）は sync.sqlite3 に閉じ込め、UI からは見えない。

（骨組み）同期そのものはまだ行わない。公開 API の形だけを先に固める。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ...translate.errors import CancelledError
from ..errors import PaperNotFound, RemoteMissing, SyncUnavailable
from ..store import Library
from .device import DeviceInfo
from .hlc import HybridClock
from .storage import Progress, SyncStorage

log = logging.getLogger(__name__)

SYNC_DB_NAME = "sync.sqlite3"
FOLDER_NAME = "Spectra Sync"
FORMAT = 1  # 同期フォルダの形式の版（library.sqlite3 の版とは別）

KINDS = ("added", "metadata", "translation", "removed", "collections", "file")


@dataclass(frozen=True)
class RemoteChange:
    """他の端末の変更を反映した結果（UI に知らせる）。

    kind: added / metadata / translation / removed / collections / file
    removed のときの paper_id は削除する前の id、fields は metadata のとき変わった項目。
    """

    kind: str
    sha256: str = ""
    paper_id: int | None = None
    fields: frozenset[str] = frozenset()


@dataclass
class SyncReport:
    """1 回の同期の結果（状態の表示とログ用）。"""

    exported: int = 0
    imported: int = 0
    changes: list[RemoteChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    read_only: bool = False  # 同期フォルダの形式が新しすぎる


class SyncEngine:
    def __init__(self, library: Library, storage: SyncStorage, device: DeviceInfo, *, clock: HybridClock | None = None):
        self.library = library
        self.storage = storage
        self.device = device
        self.clock = clock or HybridClock(device.id)
        self._listeners: list[Callable[[list[RemoteChange]], None]] = []
        self._lock = threading.Lock()

    # -- 通知 ------------------------------------------------------------------

    def add_remote_listener(self, fn: Callable[[list[RemoteChange]], None]) -> Callable[[], None]:
        """他の端末の変更を反映したとき、1 回の反映でまとめて 1 回だけ呼ぶ（約束は Library.add_listener と同じ）。"""
        with self._lock:
            self._listeners.append(fn)

        def remove() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)

        return remove

    def _notify(self, changes: list[RemoteChange]) -> None:
        if not changes:
            return
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(list(changes))
            except Exception:
                log.exception("remote listener failed")

    # -- 同期 ------------------------------------------------------------------

    def sync_now(self, cancel: threading.Event | None = None) -> SyncReport:
        """ローカルを走査して書き出し、他の端末の変更を反映する（ワーカーから呼ぶ）。骨組みでは何もしない。"""
        if not self.storage.available():
            raise SyncUnavailable()
        return SyncReport()

    def fetch(
        self,
        paper_id: int,
        kind: str,
        progress: Progress | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        """原文（original）か訳文（translation）を手元に取得してパスを返す（ワーカーから呼ぶ）。

        手元にあれば同期フォルダに触らずにすぐ返す。失敗は SyncUnavailable / RemoteMissing / HashMismatch、
        論文が無ければ PaperNotFound、キャンセルは CancelledError。
        """
        if kind not in ("original", "translation"):
            raise ValueError(f"unknown kind: {kind}")
        paper = self.library.get(paper_id)
        if paper is None:
            raise PaperNotFound(f"論文が見つかりません（id={paper_id}）")
        local = paper.pdf_path if kind == "original" else paper.translated_path
        if (paper.has_original if kind == "original" else paper.has_translation_file):
            return local
        if cancel is not None and cancel.is_set():
            raise CancelledError
        if not self.storage.available():
            raise SyncUnavailable()
        raise RemoteMissing()  # 骨組み: 取り込みは本体で実装する

    def evict(self, paper_id: int) -> None:
        """このパソコンのコピーを削除する（クラウドには残す）。手元の原文と訳文のファイルだけを消す。骨組みでは何もしない。"""
        if self.library.get(paper_id) is None:
            raise PaperNotFound(f"論文が見つかりません（id={paper_id}）")

    def close(self) -> None:
        pass

