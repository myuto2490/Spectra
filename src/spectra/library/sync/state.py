"""この端末の同期の記録（library/sync.sqlite3）。library.sqlite3 とは別のファイルにし、版も別に持つ。

- 基準（baseline）: 最後に書き出した、または反映した時点のライブラリの値。走査でこれと比べて、ローカルの変更を見つける。
- 自分の記録（own）: この端末が書いた値と HLC。自分の state ファイルの中身になる。
- 他の端末のファイル（remote_files）: 解析済みの中身と (サイズ, mtime)。変わったファイルだけを読み直す。
- pending_apply: 反映の途中で落ちたときにエコーを防ぐための記録。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..errors import LibraryError

MIGRATIONS: list[str] = [
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE papers (
        sha TEXT PRIMARY KEY,
        baseline TEXT NOT NULL,        -- {"values": {...}, "alive": bool, "last_opened_at": float|null}（null の行は未登録）
        own TEXT NOT NULL,             -- {"fields": {name: [value, hlc, manual]}, "life": [alive, hlc], "file": {...}, "opened": float|null}
        exported INTEGER NOT NULL DEFAULT 0   -- own を同期フォルダに書き出したか
    );
    CREATE TABLE translations (
        sha TEXT PRIMARY KEY,
        baseline TEXT NOT NULL,        -- ライブラリの訳文の情報（null なら訳文なし）
        own TEXT NOT NULL,             -- {"hlc", "alive", "pdf_sha", "info"}（自分の translation ファイルの中身）
        applied TEXT NOT NULL,         -- 手元の訳文がどれか {"device", "pdf_sha"}
        exported INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE collections (
        uid TEXT PRIMARY KEY,
        local_id INTEGER UNIQUE,
        baseline TEXT NOT NULL,        -- {"name": str, "alive": bool}
        own TEXT NOT NULL,             -- {"name": [name, hlc], "life": [alive, hlc]}
        exported INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE members (
        uid TEXT NOT NULL,
        sha TEXT NOT NULL,
        baseline INTEGER NOT NULL,     -- ライブラリで所属しているか
        own TEXT NOT NULL,             -- [present, hlc]
        PRIMARY KEY (uid, sha)
    );
    CREATE TABLE remote_files (
        path TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        mtime REAL NOT NULL,
        data TEXT                      -- 解析した中身（読めなかったら null。次に変わったら読み直す）
    );
    CREATE TABLE pending_apply (
        sha TEXT PRIMARY KEY,
        target TEXT NOT NULL           -- 反映しようとしている baseline
    );
    """,
]


class SyncState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if version > len(MIGRATIONS):
                self._db.close()
                raise LibraryError(
                    "新しいバージョンの Spectra で作成された同期の記録です。", hint="Spectra を最新版に更新してください。"
                )
            for number, script in enumerate(MIGRATIONS[version:], version + 1):
                try:
                    self._db.executescript(f"BEGIN;\n{script.strip().rstrip(';')};\nPRAGMA user_version = {number};\nCOMMIT;")
                except sqlite3.Error:
                    if self._db.in_transaction:
                        self._db.execute("ROLLBACK")
                    raise

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- meta ------------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        rows = self.query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(text: str | None, default=None):
    if text is None:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default
