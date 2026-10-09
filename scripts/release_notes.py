"""CHANGELOG.md から指定バージョンの節を取り出す（GitHub Release の本文用）。

使い方: python scripts/release_notes.py v1.2.3 > notes.md
"""

from __future__ import annotations

import re
import sys

from _version import CHANGELOG


def notes(version: str) -> str:
    version = version.removeprefix("refs/tags/").removeprefix("v")
    text = CHANGELOG.read_text(encoding="utf-8")
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)", text, re.MULTILINE | re.DOTALL)
    if not m:
        raise SystemExit(f"CHANGELOG.md に [{version}] の節がありません")
    return m.group(1).strip() + "\n"


if __name__ == "__main__":
    sys.stdout.write(notes(sys.argv[1]))
