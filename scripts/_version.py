"""バージョン関連スクリプトの共通処理。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "src" / "spectra" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
REPO_URL = "https://github.com/myuto2490/Spectra"

# PEP 440 の範囲で SemVer に対応する形: 1.2.3 / 1.2.3a1 / 1.2.3b2 / 1.2.3rc1
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
_ASSIGN_RE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)


def read_version() -> str:
    m = _ASSIGN_RE.search(INIT.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"__version__ が見つかりません: {INIT}")
    return m.group(1)


def write_version(version: str) -> None:
    text = INIT.read_text(encoding="utf-8")
    INIT.write_text(_ASSIGN_RE.sub(f'__version__ = "{version}"', text), encoding="utf-8")


def parse(version: str) -> tuple[int, int, int, str | None, int | None]:
    m = VERSION_RE.match(version)
    if not m:
        raise SystemExit(f"不正なバージョン番号です: {version!r}（例: 1.2.3, 1.2.3rc1）")
    major, minor, patch, pre, num = m.groups()
    return int(major), int(minor), int(patch), pre, int(num) if num else None
