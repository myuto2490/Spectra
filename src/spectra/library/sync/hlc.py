"""ハイブリッド論理時計（HLC）。

壁時計のミリ秒・カウンター・端末 ID の組で、端末をまたいで変更の前後を決める。
相手の時刻を見たら自分の時計をその後に進めるので、時計の遅れた端末でも、相手の変更を見たあとの操作は必ず後になる。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

_WALL_DIGITS = 13
_COUNTER_DIGITS = 6
MAX_DRIFT_MS = 24 * 60 * 60 * 1000  # ほかの端末の時計のずれとして受け入れる上限（1 日）


@dataclass(frozen=True, order=True)
class HLC:
    wall_ms: int
    counter: int
    device: str

    def __str__(self) -> str:
        """文字列のままで大小を比べられる形（JSON に書く）。"""
        return f"{self.wall_ms:0{_WALL_DIGITS}d}.{self.counter:0{_COUNTER_DIGITS}d}.{self.device}"

    @classmethod
    def parse(cls, text: str) -> HLC:
        wall, counter, device = str(text).split(".", 2)
        if not (wall.isdigit() and counter.isdigit() and device):
            raise ValueError(f"invalid HLC: {text!r}")
        return cls(int(wall), int(counter), device)


ZERO = HLC(0, 0, "")


class HybridClock:
    """スレッドから安全に使える HLC の時計。now は秒を返す関数（テストで差し替える）。"""

    def __init__(self, device: str, last: HLC = ZERO, now: Callable[[], float] = time.time):
        self.device = device
        self._last = last
        self._now = now
        self._lock = threading.Lock()

    @property
    def last(self) -> HLC:
        return self._last

    def tick(self, at: float | None = None) -> HLC:
        """自分の変更に付ける時刻。at（変更した時刻、秒）を渡すと、壁時計の代わりにそれを使う。"""
        physical = int((at if at is not None else self._now()) * 1000)
        with self._lock:
            last = self._last
            if physical > last.wall_ms:
                new = HLC(physical, 0, self.device)
            elif last.counter + 1 >= 10**_COUNTER_DIGITS:
                new = HLC(last.wall_ms + 1, 0, self.device)  # カウンターの桁があふれたら、時刻を 1 ミリ秒進める
            else:
                new = HLC(last.wall_ms, last.counter + 1, self.device)
            self._last = new
            return new

    def observe(self, remote: HLC) -> None:
        """相手の時刻を見た。以後の自分の時刻は、これより後になる。

        今の時刻より MAX_DRIFT_MS を超えて先の時刻は取り込まない（壊れた・細工されたファイル 1 つで、
        この端末の時計が遠い未来に進んだまま戻らなくなるのを防ぐ）。
        """
        if remote.wall_ms > int(self._now() * 1000) + MAX_DRIFT_MS:
            return
        with self._lock:
            if (remote.wall_ms, remote.counter) > (self._last.wall_ms, self._last.counter):
                self._last = HLC(remote.wall_ms, remote.counter, self.device)
