"""画面とログに出す文字列から、個人を特定できる情報（ユーザー名を含むパス・メールアドレス）を伏せる。

- ログはバグ報告に貼られることがあるので、ホームフォルダを「~」に、メールアドレスを「…@…」にする。
- 画面（とくにスクリーンショットに写りやすい設定画面）でも、ホームフォルダは「~」で表示する。
- Google Drive のフォルダ名（GoogleDrive-<メールアドレス>）から読み取ったアカウントは、表示を作るためだけに使い、
  完全なアドレスは画面にもツールチップにも出さない。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
GOOGLE_DRIVE_RE = re.compile(r"GoogleDrive-[^/\\\s]+")
MASK = "•••"


def _homes(home: str | Path | None = None) -> list[str]:
    h = str(home if home is not None else Path.home()).rstrip("/\\")
    if not h:
        return []
    # Windows では区切りが \ と / のどちらでも出てくる
    variants = {h, h.replace("\\", "/"), h.replace("/", "\\")}
    return sorted(variants, key=len, reverse=True)


def home_to_tilde(text: str, home: str | Path | None = None) -> str:
    """ホームフォルダ（/Users/<名前>・C:\\Users\\<名前> など）を「~」に置き換える。"""
    for h in _homes(home):
        # Windows のパスは大文字小文字を区別しないので、区別せずに置き換える
        text = re.sub(re.escape(h) + r"(?=[/\\]|$|[\s'\"),:;])", "~", text, flags=re.IGNORECASE)
    return text


def display_path(path: str | Path, home: str | Path | None = None) -> str:
    """画面に出すパス：ホームは「~」、Google Drive のアカウントのフォルダ名は伏せる。"""
    text = home_to_tilde(str(path), home)
    return GOOGLE_DRIVE_RE.sub("GoogleDrive-…", text)


def redact(text: str, home: str | Path | None = None) -> str:
    """ログ用：ホームフォルダ・Google Drive のアカウント・メールアドレスを伏せる。"""
    text = display_path(text, home)
    return EMAIL_RE.sub("…@…", text)


class RedactingFormatter(logging.Formatter):
    """書式を整えた後の文字列（メッセージと例外のトレースバックの両方）から、個人の情報を伏せる。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def mask_email(email: str, others: list[str] | tuple[str, ...] = ()) -> str:
    """メールアドレスを「my•••@gmail.com」の形にする。others と区別できるまで、先頭の文字を増やす。

    完全なアドレスは返さない（区別できなくなるほど似ている場合も、最後の 1 文字は伏せる）。
    """
    local, _, domain = email.partition("@")
    rivals = [o for o in others if o != email]
    n = min(2, max(1, len(local) - 1))
    while True:
        shown = f"{local[:n]}{MASK}@{domain}"
        clash = any(f"{o.partition('@')[0][:n]}{MASK}@{o.partition('@')[2]}" == shown for o in rivals)
        if not clash or n >= len(local) - 1:
            return shown
        n += 1
