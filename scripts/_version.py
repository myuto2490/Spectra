"""バージョン関連スクリプトの共通処理。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "src" / "spectra" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
REPO_URL = "https://github.com/myuto2490/Spectra"
# 固定のバージョン番号を書かない文書（リリースのたびに古くならないように。履歴は CHANGELOG に書く）
VERSIONLESS_DOCS = ("README.md", "SECURITY.md", "CONTRIBUTING.md")

# PEP 440 の範囲で SemVer に対応する形: 1.2.3 / 1.2.3a1 / 1.2.3b2 / 1.2.3rc1
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
_ASSIGN_RE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
# URL の中（https://…/v1.0.0/ など）は除く。x.y.z / X.Y.Z のような書き方は数字でないので当たらない
_URL_RE = re.compile(r"https?://[^\s)>\]\"'`]+")
# 1.2.3・v1.2.3・1.2.3rc1。IP アドレスや 4 つ以上の数の並び（1.2.3.4）は当てない
_FIXED_VERSION_RE = re.compile(r"(?<![\w.])v?\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?!\.?\d)")


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


def fixed_versions(text: str) -> list[tuple[int, str]]:
    """文書の中の固定のバージョン番号を (行番号, 番号) で返す。URL の中は除く。"""
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = _URL_RE.sub("", line)
        found += [(lineno, m.group(0)) for m in _FIXED_VERSION_RE.finditer(line)]
    return found
