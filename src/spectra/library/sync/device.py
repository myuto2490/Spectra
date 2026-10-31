"""この端末の ID。設定ごと別のパソコンにコピーされた場合は作り直す。"""

from __future__ import annotations

import json
import logging
import platform
import uuid
from dataclasses import dataclass
from pathlib import Path

from ...config import paths

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceInfo:
    id: str  # 32 桁の 16 進（uuid4）
    name: str  # 表示用（ほかの端末から来た値は、画面でリッチテキストとして解釈しないこと）


def _fingerprint() -> str:
    return f"{platform.node()}|{uuid.getnode():x}"


def load_device(path: Path | None = None) -> DeviceInfo:
    path = path or paths.device_file()
    fingerprint = _fingerprint()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("fingerprint") == fingerprint and _valid_id(raw.get("id")):
            return DeviceInfo(raw["id"], str(raw.get("name") or platform.node() or "このパソコン"))
        log.info("device file belongs to another machine; creating a new device id")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError):
        log.warning("device file is unreadable, creating a new one: %s", path, exc_info=True)
    info = DeviceInfo(uuid.uuid4().hex, platform.node() or "このパソコン")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": info.id, "name": info.name, "fingerprint": fingerprint}, ensure_ascii=False), encoding="utf-8")
    return info


def _valid_id(value) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)
