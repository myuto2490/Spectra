"""翻訳結果の SQLite キャッシュ。

同じ論文を出力形式だけ変えて再翻訳したり、無料枠超過で中断した翻訳を再開したりしても
API の文字数を二重に消費しないようにする。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path


def cache_key(namespace: str, text: str) -> str:
    return hashlib.sha256(f"{namespace}\0{text}".encode()).hexdigest()


class TranslationCache:
    def __init__(self, path: str | Path | None):
        """path が None ならメモリ上に作る（テスト・キャッシュ無効時）。"""
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path) if self.path else ":memory:", check_same_thread=False)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS translations ("
                " key TEXT PRIMARY KEY, translated TEXT NOT NULL, created INTEGER NOT NULL)"
            )
            self._db.commit()

    def get_many(self, keys: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        with self._lock:
            for i in range(0, len(keys), 500):
                chunk = keys[i : i + 500]
                q = f"SELECT key, translated FROM translations WHERE key IN ({','.join('?' * len(chunk))})"
                found.update(self._db.execute(q, chunk).fetchall())
        return found

    def put_many(self, items: dict[str, str]) -> None:
        if not items:
            return
        now = int(time.time())
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO translations (key, translated, created) VALUES (?, ?, ?)",
                [(k, v, now) for k, v in items.items()],
            )
            self._db.commit()

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM translations").fetchone()[0])

    def size_bytes(self) -> int:
        if not self.path or not self.path.exists():
            return 0
        return sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*"))

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM translations")
            self._db.commit()
            self._db.execute("VACUUM")

    def close(self) -> None:
        with self._lock:
            self._db.close()
