"""同期先の読み書き。同期フォルダ方式（FolderStorage）を置き、将来ほかの方式を差し込めるようにする。

同期フォルダの中身は、ほかの端末やアプリが書き換えうる外部入力として扱う。
パスは同期フォルダの中の相対パス（"/" 区切り）だけを受け付け、".."・絶対パス・シンボリックリンクを拒否する。
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from ...translate.errors import CancelledError
from ..errors import LocalDiskFull, RemoteMissing, SyncError, SyncUnavailable

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
TMP_SUFFIX = ".tmp"

Progress = Callable[[int, int], None]


@dataclass(frozen=True)
class Entry:
    path: str  # 同期フォルダの中の相対パス（"/" 区切り）
    size: int
    mtime: float


class SyncStorage(Protocol):
    def available(self) -> bool: ...

    def list(self, prefix: str = "") -> list[Entry]: ...

    def read_bytes(self, path: str, limit: int | None = None) -> bytes: ...

    def copy_to(
        self, path: str, dest: Path, progress: Progress | None = None, cancel: threading.Event | None = None
    ) -> str: ...

    def write_atomic(self, path: str, data: bytes) -> None: ...

    def put_file(self, path: str, src: Path) -> None: ...

    def exists(self, path: str) -> bool: ...

    def delete(self, path: str) -> None: ...

    def request_download(self, path: str) -> bool: ...


def os_error(e: OSError) -> SyncError:
    """OSError を同期の例外に変える。空き容量の不足だけは、利用者が対処できるよう区別する。"""
    if e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
        return LocalDiskFull()
    return SyncUnavailable(_describe(e))


def _describe(e: OSError) -> str:
    """例外の説明。同期フォルダの絶対パス（ユーザー名やメールアドレスを含みうる）は入れない。"""
    return e.strerror or type(e).__name__


_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def check_relative(path: str) -> str:
    """同期フォルダの中の相対パスだけを受け付ける。

    ".."・絶対パス・バックスラッシュ・ドライブ（"C:"）・NUL・制御文字・Windows の予約名（CON など）・
    末尾が点か空白の名前を拒否する（どの OS でも、同期フォルダの外や別のファイルを指さないように）。
    """
    p = PurePosixPath(path)
    if not path or p.is_absolute() or "\\" in path or ":" in path or any(ord(c) < 32 for c in path):
        raise ValueError(f"invalid sync path: {path!r}")
    for part in path.split("/"):
        if part in ("", ".", "..") or part[-1] in ". " or part.split(".")[0].lower() in _RESERVED:
            raise ValueError(f"invalid sync path: {path!r}")
    return p.as_posix()


class FolderStorage:
    """利用者が選んだフォルダ（Google Drive・Dropbox などのアプリが同期する）に読み書きする。"""

    def __init__(self, root: Path):
        self.root = Path(root)

    # -- 共通 --------------------------------------------------------------

    def available(self) -> bool:
        try:
            return self.root.is_dir()
        except OSError:
            return False

    def _require_root(self) -> None:
        if not self.available():
            raise SyncUnavailable()

    def _resolve(self, path: str) -> Path:
        full = self.root / check_relative(path)
        # 途中のディレクトリを含め、シンボリックリンクをたどって外に出ないこと
        cur = self.root
        for part in PurePosixPath(path).parts:
            cur = cur / part
            if cur.is_symlink():
                raise ValueError(f"symlink in sync path: {path!r}")
        try:
            root = self.root.resolve()
            parent = full.parent.resolve()
        except OSError:
            return full
        if parent != root and root not in parent.parents:
            raise ValueError(f"sync path escapes the sync folder: {path!r}")
        return full

    @staticmethod
    def _open_read(full: Path):
        """最後の要素がシンボリックリンクなら開かない（確認と開く間にすり替えられても外を読まない）。"""
        fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        return os.fdopen(fd, "rb")

    # -- 読み込み ------------------------------------------------------------

    def list(self, prefix: str = "") -> list[Entry]:
        self._require_root()
        base = self._resolve(prefix) if prefix else self.root
        out: list[Entry] = []
        if not base.is_dir():
            return out
        try:
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
                for name in filenames:
                    full = Path(dirpath) / name
                    if full.is_symlink():
                        continue
                    try:
                        st = full.stat()
                    except OSError:
                        continue
                    out.append(Entry(full.relative_to(self.root).as_posix(), st.st_size, st.st_mtime))
        except OSError as e:
            raise SyncUnavailable(_describe(e)) from e
        return sorted(out, key=lambda e: e.path)

    def exists(self, path: str) -> bool:
        self._require_root()
        return self._resolve(path).is_file()

    def read_bytes(self, path: str, limit: int | None = None) -> bytes:
        """limit を超えるファイルは ValueError（巨大なファイルでメモリを使い切らない）。"""
        self._require_root()
        full = self._resolve(path)
        try:
            with self._open_read(full) as fh:
                data = fh.read(limit + 1 if limit is not None else -1)
        except FileNotFoundError as e:
            raise RemoteMissing(path) from e
        except OSError as e:
            raise SyncUnavailable(_describe(e)) from e
        if limit is not None and len(data) > limit:
            raise ValueError(f"{path} is larger than {limit} bytes")
        return data

    def copy_to(
        self, path: str, dest: Path, progress: Progress | None = None, cancel: threading.Event | None = None
    ) -> str:
        """path を dest にコピーし、sha256 を返す。dest は呼び出し側が検証してから使う（途中なら .tmp を消す）。"""
        self._require_root()
        full = self._resolve(path)
        digest = hashlib.sha256()
        done = 0
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            total = full.stat().st_size
            with self._open_read(full) as src, open(dest, "wb") as out:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise CancelledError
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        except FileNotFoundError as e:
            dest.unlink(missing_ok=True)
            raise RemoteMissing(path) from e
        except OSError as e:
            dest.unlink(missing_ok=True)
            raise os_error(e) from e
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return digest.hexdigest()

    # -- 書き込み ------------------------------------------------------------

    def write_atomic(self, path: str, data: bytes) -> None:
        self._require_root()
        full = self._resolve(path)
        tmp = full.with_name(f".{full.name}{TMP_SUFFIX}")
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, full)
        except OSError as e:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise os_error(e) from e

    def put_file(self, path: str, src: Path) -> None:
        self._require_root()
        full = self._resolve(path)
        tmp = full.with_name(f".{full.name}{TMP_SUFFIX}")
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            with open(src, "rb") as fin, open(tmp, "wb") as fout:
                while chunk := fin.read(CHUNK):
                    fout.write(chunk)
            os.replace(tmp, full)
        except OSError as e:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise os_error(e) from e

    def delete(self, path: str) -> None:
        self._require_root()
        try:
            self._resolve(path).unlink(missing_ok=True)
        except OSError as e:
            raise SyncUnavailable(_describe(e)) from e

    # -- 手元にないファイル ----------------------------------------------------------

    def request_download(self, path: str) -> bool:
        """iCloud の「ストレージを最適化」で .<name>.icloud に置き換わっていれば、取り寄せを頼んで True を返す。

        取り寄せは非同期なので、呼び出し側はしばらくしてから取得し直す。
        """
        full = self._resolve(path)
        placeholder = full.with_name(f".{full.name}.icloud")
        if full.exists() or not placeholder.exists():
            return False
        if sys.platform == "darwin":
            try:
                subprocess.run(["brctl", "download", str(full)], check=False, timeout=10, capture_output=True)
            except (OSError, subprocess.SubprocessError):
                log.debug("brctl download failed", exc_info=True)
        return True

