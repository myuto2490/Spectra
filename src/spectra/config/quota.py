"""API キーの 1 日の上限の記録。上限に達したキーとモデルの組を、上限が戻る時刻まで試さない。

設定フォルダの quota.json に {"<キーの id>|<モデル>": 戻る時刻（秒）} を置く。キーの中身は書かない。
Gemini の 1 日の上限は、米国太平洋時間の 0 時に戻る（日本時間の 16 時、米国の夏時間でないときは 17 時）。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import math
import os
import threading
import time
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

RESET_ZONE = "America/Los_Angeles"


def next_reset(now: float | None = None) -> float:
    """次に上限が戻る時刻（米国太平洋時間の次の 0 時）。時間帯の情報がなければ 24 時間後。"""
    now = time.time() if now is None else now
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(RESET_ZONE)
    except Exception:  # Windows で tzdata がない場合など
        return now + 24 * 3600
    local = dt.datetime.fromtimestamp(now, zone)
    midnight = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(), tzinfo=zone)
    return midnight.timestamp()


class QuotaBook:
    def __init__(self, path: Path | None = None, clock=time.time):
        self.path = path or paths.config_dir() / "quota.json"
        self.clock = clock
        self._lock = threading.Lock()

    def until(self, key_id: str, model: str) -> float | None:
        """そのキーとモデルが上限のあいだは、戻る時刻。上限でなければ None。"""
        with self._lock:
            value = self._read().get(f"{key_id}|{model}")
        return value if value is not None and value > self.clock() else None

    def exhausted_keys(self, model: str | None = None) -> dict[str, float]:
        """上限のキーの id と戻る時刻（model を指定しなければ、どれかのモデルで上限のもの）。"""
        out: dict[str, float] = {}
        with self._lock:
            data = self._read()
        now = self.clock()
        for name, until in data.items():
            key_id, _, m = name.partition("|")
            if until > now and (model is None or m == model):
                out[key_id] = max(out.get(key_id, 0.0), until)
        return out

    def mark(self, key_id: str, model: str) -> float:
        until = next_reset(self.clock())
        with self._lock:
            data = self._read()
            data[f"{key_id}|{model}"] = until
            self._write(data)
        return until

    def forget(self, key_id: str) -> None:
        """キーを削除・入れ替えたとき、そのキーの記録を消す。"""
        with self._lock:
            data = {k: v for k, v in self._read().items() if not k.startswith(f"{key_id}|")}
            self._write(data)

    # -- ファイル ----------------------------------------------------------------

    def _read(self) -> dict[str, float]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        now = self.clock()
        if not isinstance(raw, dict):
            return {}
        return {
            k: float(v)
            for k, v in raw.items()
            if isinstance(k, str) and "|" in k and isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and now < v < now + 3 * 24 * 3600  # 過ぎたもの・遠すぎる未来は捨てる
        }

    def _write(self, data: dict[str, float]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            log.warning("could not save the quota record", exc_info=True)
            with contextlib.suppress(OSError):
                self.path.with_name(self.path.name + ".tmp").unlink(missing_ok=True)
