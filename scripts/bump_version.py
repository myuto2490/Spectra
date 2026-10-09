"""バージョン番号を上げ、CHANGELOG の Unreleased を新バージョンの節に確定する。

使い方:
    python scripts/bump_version.py patch          # 0.1.0 -> 0.1.1
    python scripts/bump_version.py minor          # 0.1.1 -> 0.2.0
    python scripts/bump_version.py major          # 0.2.0 -> 1.0.0
    python scripts/bump_version.py 0.3.0rc1       # 番号を直接指定
    python scripts/bump_version.py 0.1.0          # 現在と同じ番号を指定すると、その番号で確定（初回リリース）
    python scripts/bump_version.py minor --dry-run

コミットとタグ付けは行わない（実行後に表示されるコマンドで行う）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import re

from _version import CHANGELOG, REPO_URL, parse, read_version, write_version


def next_version(current: str, part: str) -> str:
    major, minor, patch, pre, _ = parse(current)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        # プレリリースからの patch は同じ番号の正式版にする（1.2.0rc1 -> 1.2.0）
        return f"{major}.{minor}.{patch}" if pre else f"{major}.{minor}.{patch + 1}"
    parse(part)
    return part


def update_changelog(text: str, old: str, new: str, today: str) -> str:
    if "## [Unreleased]" not in text:
        raise SystemExit("CHANGELOG.md に '## [Unreleased]' 節がありません")
    body = text.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    if not re.search(r"^- ", body, re.MULTILINE):
        raise SystemExit("Unreleased 節に変更点が書かれていません。先に CHANGELOG.md を更新してください")
    text = text.replace("## [Unreleased]", f"## [Unreleased]\n\n## [{new}] - {today}", 1)

    # 末尾のリンク定義を更新
    text = re.sub(r"^\[Unreleased\]: .*$", f"[Unreleased]: {REPO_URL}/compare/v{new}...HEAD", text, flags=re.MULTILINE)
    has_prev_tag = re.search(rf"^\[{re.escape(old)}\]: ", text, re.MULTILINE)
    link = f"{REPO_URL}/compare/v{old}...v{new}" if has_prev_tag else f"{REPO_URL}/releases/tag/v{new}"
    return text.rstrip("\n") + f"\n[{new}]: {link}\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("part", help="major / minor / patch / 明示的なバージョン番号")
    ap.add_argument("--dry-run", action="store_true", help="変更せずに結果だけ表示する")
    args = ap.parse_args()

    current = read_version()
    new = next_version(current, args.part)
    if parse(new)[:3] < parse(current)[:3]:
        raise SystemExit(f"新しいバージョン {new} が現在の {current} より小さくなっています")
    changelog = update_changelog(CHANGELOG.read_text(encoding="utf-8"), current, new, dt.date.today().isoformat())

    print(f"{current} -> {new}")
    if args.dry_run:
        return
    write_version(new)
    CHANGELOG.write_text(changelog, encoding="utf-8")
    print(
        "\n次の手順でリリースします:\n"
        f'  git commit -am "chore(release): v{new}"\n'
        f"  git checkout main && git merge --no-ff develop\n"
        f'  git tag -a v{new} -m "Spectra v{new}"\n'
        f"  git push origin main develop v{new}"
    )


if __name__ == "__main__":
    main()
