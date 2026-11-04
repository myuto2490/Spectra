"""pytest の junit.xml から、失敗したテストを GitHub Actions のアノテーションに出す。

ログ本体は認証がないと読めないが、アノテーションは公開 API（check-runs）から読める。
使い方: python scripts/ci_failure_annotations.py junit.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

LIMIT = 20  # アノテーションは 1 ステップあたり 10 件、全体で 50 件までなので、絞る


def failures(path: str):
    for case in ET.parse(path).getroot().iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is not None:
                message = (node.get("message") or "").splitlines()
                yield f"{case.get('classname')}::{case.get('name')}", (message[0] if message else kind)[:300]


def main(argv: list[str]) -> int:
    try:
        found = list(failures(argv[1]))
    except (OSError, ET.ParseError, IndexError) as e:
        print(f"::warning title=junit::could not read the result: {e}")
        return 0
    for name, message in found[:LIMIT]:
        message = message.replace("%", "%25").replace("\r", "").replace("\n", " ")
        print(f"::error title=pytest failure::{name} - {message}")
    if len(found) > LIMIT:
        print(f"::warning title=pytest::{len(found) - LIMIT} more failures not shown")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
