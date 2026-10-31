"""テスト用の同期フォルダ。一時フォルダを包み、同期アプリの困った動きを注入できる。

2 台の端末は、同じ root を指す 2 つの FakeFolderStorage で再現する。
ネットワークは使わない。製品のコードからは使わない。
"""

from __future__ import annotations

import fnmatch
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..errors import SyncUnavailable
from .storage import Entry, FolderStorage, Progress


class FakeFolderStorage(FolderStorage):
    def __init__(self, root: Path):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        super().__init__(root)
        self.offline = False  # True なら同期フォルダが読めない（SyncUnavailable）
        self.delay = 0.0  # 読み込みごとに待つ秒数（ストリーミングの疑似）
        self.stall = threading.Event()  # set されている間、読み込みが止まる（release() で再開）
        self.reads: list[str] = []  # 読んだパス（PDF を読みに行っていないことの確認用）
        self.writes: list[str] = []
        self._held: list[tuple[str, bytes]] | None = None
        self._partial: list[str] = []
        self._fail: list[tuple[str, BaseException]] = []

    # -- 注入 ------------------------------------------------------------------

    def hold(self) -> None:
        """以後の書き込みを、deliver() まで同期フォルダに届けない（同期アプリの遅延）。"""
        self._held = []

    def deliver(self, match: Callable[[str], bool] | None = None, *, reverse: bool = False) -> list[str]:
        """止めていた書き込みのうち match に合うものを届ける（reverse で順序を入れ替える）。残りは止めたまま。"""
        held = self._held or []
        sent = [(p, d) for p, d in held if match is None or match(p)]
        rest = [(p, d) for p, d in held if not (match is None or match(p))]
        for path, data in reversed(sent) if reverse else sent:
            super().write_atomic(path, data)
        self._held = rest if rest else None
        return [p for p, _ in sent]

    def partial_next(self, pattern: str) -> None:
        """次に pattern に合うパスへ書くとき、前半だけを（rename せず）書く（同期の途中のファイル）。"""
        self._partial.append(pattern)

    def fail_next(self, pattern: str, exc: BaseException) -> None:
        """次に pattern に合うパスを読み書きするとき、exc を送出する。"""
        self._fail.append((pattern, exc))

    def release(self) -> None:
        self.stall.clear()

    def make_placeholder(self, path: str) -> None:
        """iCloud の「ストレージを最適化」: path を .<name>.icloud に置き換える。"""
        full = self._resolve(path)
        full.rename(full.with_name(f".{full.name}.icloud"))

    def conflict_copy(self, path: str, new_name: str) -> str:
        """同期アプリが作る競合コピー（例: "state.X (1).json"）を、同じフォルダに作る。"""
        full = self._resolve(path)
        dest = full.with_name(new_name)
        dest.write_bytes(full.read_bytes())
        return dest.relative_to(self.root).as_posix()

    # -- 上書き ----------------------------------------------------------------

    def _before(self, path: str) -> None:
        if self.offline:
            raise SyncUnavailable("offline (fake)")
        for i, (pattern, exc) in enumerate(self._fail):
            if fnmatch.fnmatch(path, pattern):
                del self._fail[i]
                raise exc
        while self.stall.is_set():
            time.sleep(0.01)
        if self.delay:
            time.sleep(self.delay)

    def available(self) -> bool:
        return not self.offline and super().available()

    def list(self, prefix: str = "") -> list[Entry]:
        self._before(prefix or ".")
        return super().list(prefix)

    def read_bytes(self, path: str, limit: int | None = None) -> bytes:
        self._before(path)
        self.reads.append(path)
        return super().read_bytes(path, limit)

    def copy_to(self, path: str, dest: Path, progress: Progress | None = None, cancel: threading.Event | None = None) -> str:
        self._before(path)
        self.reads.append(path)
        return super().copy_to(path, dest, progress, cancel)

    def write_atomic(self, path: str, data: bytes) -> None:
        self._before(path)
        self.writes.append(path)
        for i, pattern in enumerate(self._partial):
            if fnmatch.fnmatch(path, pattern):
                del self._partial[i]
                full = self._resolve(path)
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_bytes(data[: max(1, len(data) // 2)])
                return
        if self._held is not None:
            self._held.append((path, data))
            return
        super().write_atomic(path, data)

    def put_file(self, path: str, src: Path) -> None:
        self.write_atomic(path, Path(src).read_bytes())
