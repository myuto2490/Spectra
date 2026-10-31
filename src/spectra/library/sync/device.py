"""この端末の ID と表示名。設定ごと別のパソコンにコピーされた場合は作り直す。

表示名は同期フォルダ（クラウド）に書き出され、ほかのパソコンにも表示されるので、ホスト名やユーザー名
（「Taro-no-MacBook-Pro」のように個人名を含みやすい）からは作らず、OS の種類と ID の先頭だけにする。
別のパソコンへのコピーを見分ける指紋（ホスト名と MAC アドレス）は、ハッシュにして手元の device.json にだけ置く。
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from ...config import paths

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceInfo:
    id: str  # 32 桁の 16 進（uuid4）
    name: str  # 表示用（ほかの端末から来た値は、画面でリッチテキストとして解釈しないこと）


def _raw_fingerprint() -> str:
    return f"{platform.node()}|{uuid.getnode():x}"


def _fingerprint() -> str:
    return hashlib.sha256(_raw_fingerprint().encode()).hexdigest()


def default_name(device_id: str) -> str:
    kind = {"darwin": "Mac", "win32": "Windows"}.get(sys.platform, "Linux" if sys.platform.startswith("linux") else "PC")
    return f"{kind}（{device_id[:4]}）"


def load_device(path: Path | None = None) -> DeviceInfo:
    path = path or paths.device_file()
    fingerprint = _fingerprint()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        stored = raw.get("fingerprint")
        # 以前の版は指紋をそのまま（ハッシュにせず）保存していた
        same_machine = stored == fingerprint or stored == _raw_fingerprint()
        if same_machine and _valid_id(raw.get("id")):
            name = str(raw.get("name") or "")
            if not name or name == platform.node():  # 以前の版の既定値（ホスト名）は使わない
                name = default_name(raw["id"])
            info = DeviceInfo(raw["id"], name)
            if stored != fingerprint or raw.get("name") != name:
                _save(path, info, fingerprint)
            return info
        log.info("device file belongs to another machine; creating a new device id")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError):
        log.warning("device file is unreadable; creating a new one")
    device_id = uuid.uuid4().hex
    info = DeviceInfo(device_id, default_name(device_id))
    _save(path, info, fingerprint)
    return info


def _save(path: Path, info: DeviceInfo, fingerprint: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": info.id, "name": info.name, "fingerprint": fingerprint}, ensure_ascii=False), encoding="utf-8")


def _valid_id(value) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)
