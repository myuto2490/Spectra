"""リリースタグとアプリのバージョン・CHANGELOG が一致しているか検査する（CI 用）。

使い方: python scripts/check_version.py v1.2.3
"""

from __future__ import annotations

import sys

from _version import CHANGELOG, parse, read_version


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    tag = argv[0].removeprefix("refs/tags/")
    version = read_version()
    parse(version)
    errors = []
    if tag != f"v{version}":
        errors.append(f"タグ {tag} と __version__ ({version}) が一致しません")
    if f"## [{version}]" not in CHANGELOG.read_text(encoding="utf-8"):
        errors.append(f"CHANGELOG.md に [{version}] の節がありません")
    for e in errors:
        print(f"::error::{e}")
    if not errors:
        print(f"OK: {tag}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
