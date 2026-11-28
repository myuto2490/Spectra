"""リリースタグとアプリのバージョン・CHANGELOG が一致しているか検査する（CI 用）。

あわせて、README.md・SECURITY.md・CONTRIBUTING.md に固定のバージョン番号が無いことを確かめる
（ダウンロード名は x.y.z、最新版は releases/latest で示し、文書が過去の版のまま残らないようにする）。

使い方: python scripts/check_version.py v1.2.3
"""

from __future__ import annotations

import sys

from _version import CHANGELOG, ROOT, VERSIONLESS_DOCS, fixed_versions, parse, read_version


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
    errors += versionless_doc_errors()
    for e in errors:
        print(f"::error::{e}")
    if not errors:
        print(f"OK: {tag}")
    return 1 if errors else 0


def versionless_doc_errors(root=ROOT) -> list[str]:
    """README.md などに書かれた固定のバージョン番号を、エラーの文にして返す。"""
    errors = []
    for name in VERSIONLESS_DOCS:
        path = root / name
        if not path.is_file():
            continue
        for lineno, v in fixed_versions(path.read_text(encoding="utf-8")):
            errors.append(f"{name}:{lineno} に固定のバージョン番号 {v} があります（x.y.z や releases/latest を使ってください）")
    return errors


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
