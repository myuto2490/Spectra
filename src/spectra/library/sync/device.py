"""この端末の ID と表示名。設定ごと別のパソコンにコピーされた場合は作り直す。

表示名は同期フォルダ（クラウド）に書き出され、ほかのパソコンにも表示されるので、既定の名前はホスト名やユーザー名
（「Taro-no-MacBook-Pro」のように個人名を含みやすい）からは作らず、OS の種類と ID の先頭だけにする。
利用者が設定で付けた名前（Settings.device_name）は、それで上書きする（device.json には書かない）。
別のパソコンへのコピーを見分ける指紋（ホスト名と MAC アドレス）は、端末 ID を混ぜたハッシュにして手元の device.json にだけ置く
（ほかのパソコンでは同じ値を計算できず、ホスト名と MAC の組を推測で確かめることもできない）。
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
from .files import clean_text

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceInfo:
    id: str  # 32 桁の 16 進（uuid4）
    name: str  # 表示用（ほかの端末から来た値は、画面でリッチテキストとして解釈しないこと）


MAX_NAME_LENGTH = 32


def _raw_fingerprint() -> str:
    return f"{platform.node()}|{uuid.getnode():x}"


def _fingerprint(device_id: str) -> str:
    return hashlib.sha256(f"{device_id}|{_raw_fingerprint()}".encode()).hexdigest()


def _legacy_fingerprints() -> set[str]:
    """以前の版の指紋（ID を混ぜないハッシュ、さらに前はそのままの値）。同じ端末とみなしてから書き直す。"""
    raw = _raw_fingerprint()
    return {raw, hashlib.sha256(raw.encode()).hexdigest()}


def clean_device_name(name) -> str:
    """利用者が付けた名前を正規化する: 制御文字と向きを入れ替える文字を除き、前後の空白を取り、32 文字までにする。"""
    if not isinstance(name, str):
        return ""
    return " ".join(clean_text(name).split())[:MAX_NAME_LENGTH].strip()


def default_name(device_id: str) -> str:
    kind = {"darwin": "Mac", "win32": "Windows"}.get(sys.platform, "Linux" if sys.platform.startswith("linux") else "PC")
    return f"{kind}（{device_id[:4]}）"


def load_device(path: Path | None = None, name: str = "") -> DeviceInfo:
    """この端末の ID と表示名。name（設定の device_name）が空でなければ、既定の名前の代わりに使う。"""
    path = path or paths.device_file()
    custom = clean_device_name(name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        device_id, stored = raw.get("id"), raw.get("fingerprint")
        if _valid_id(device_id) and stored in ({_fingerprint(device_id)} | _legacy_fingerprints()):
            if stored != _fingerprint(device_id) or raw.get("name") != default_name(device_id):
                _save(path, device_id)  # 以前の形の指紋と、以前の既定の名前（ホスト名）を書き直す
            return DeviceInfo(device_id, custom or default_name(device_id))
        log.info("device file belongs to another machine; creating a new device id")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError):
        log.warning("device file is unreadable; creating a new one")
    device_id = uuid.uuid4().hex
    _save(path, device_id)
    return DeviceInfo(device_id, custom or default_name(device_id))


def _save(path: Path, device_id: str) -> None:
    """device.json には既定の名前だけを書く（利用者が付けた名前は設定にだけ置き、古い名前を手元に残さない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"id": device_id, "name": default_name(device_id), "fingerprint": _fingerprint(device_id)}
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


def _valid_id(value) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)
