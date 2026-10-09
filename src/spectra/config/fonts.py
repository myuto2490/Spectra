"""和文フォントの選択とダウンロード。

Noto Serif JP / Noto Sans JP（SIL Open Font License）は、利用者の操作でダウンロードする。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..translate.base import USER_AGENT
from . import paths

log = logging.getLogger(__name__)

_NOTO = "https://github.com/notofonts/noto-cjk/raw/main"


@dataclass(frozen=True)
class FontChoice:
    font_id: str
    name: str
    description: str
    serif: bool
    files: dict[str, str]  # 保存名 -> URL（regular, bold の順）
    size_mb: float = 0.0


FONT_CHOICES: dict[str, FontChoice] = {
    "builtin": FontChoice("builtin", "標準ゴシック（内蔵）", "追加のダウンロードなしで使えます。", False, {}),
    "noto-serif": FontChoice(
        "noto-serif",
        "Noto Serif JP（明朝体）",
        "論文らしい落ち着いた明朝体。太字の見出しにも対応します。",
        True,
        {
            "NotoSerifJP-Regular.otf": f"{_NOTO}/Serif/SubsetOTF/JP/NotoSerifJP-Regular.otf",
            "NotoSerifJP-Bold.otf": f"{_NOTO}/Serif/SubsetOTF/JP/NotoSerifJP-Bold.otf",
        },
        12.7,
    ),
    "noto-sans": FontChoice(
        "noto-sans",
        "Noto Sans JP（ゴシック体）",
        "画面で読みやすいゴシック体。太字の見出しにも対応します。",
        False,
        {
            "NotoSansJP-Regular.otf": f"{_NOTO}/Sans/SubsetOTF/JP/NotoSansJP-Regular.otf",
            "NotoSansJP-Bold.otf": f"{_NOTO}/Sans/SubsetOTF/JP/NotoSansJP-Bold.otf",
        },
        9.2,
    ),
    "custom": FontChoice("custom", "フォントファイルを指定…", "TrueType / OpenType フォント（.ttf .otf .ttc）", False, {}),
}


@dataclass(frozen=True)
class ResolvedFont:
    regular: str | None
    bold: str | None
    serif: bool


def is_installed(font_id: str) -> bool:
    choice = FONT_CHOICES.get(font_id)
    if choice is None or not choice.files:
        return font_id in ("builtin", "custom")
    return all((paths.fonts_dir() / name).is_file() for name in choice.files)


def resolve_font(font_id: str, custom_path: str = "") -> ResolvedFont:
    """設定値から実際に使うフォントファイルを決める。使えなければ内蔵フォントにする。"""
    if font_id == "custom" and custom_path and Path(custom_path).is_file():
        serif = bool(re.search(r"(serif|mincho|明朝|min)", Path(custom_path).stem, re.IGNORECASE))
        return ResolvedFont(custom_path, None, serif)
    choice = FONT_CHOICES.get(font_id)
    if choice and choice.files and is_installed(font_id):
        names = list(choice.files)
        return ResolvedFont(str(paths.fonts_dir() / names[0]), str(paths.fonts_dir() / names[1]), choice.serif)
    return ResolvedFont(None, None, False)


def download(font_id: str, progress: Callable[[int, int], None] | None = None, client: httpx.Client | None = None) -> None:
    """フォントをダウンロードする。途中で失敗しても不完全なファイルは残さない。"""
    import pymupdf

    choice = FONT_CHOICES[font_id]
    target = paths.fonts_dir()
    target.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        total = 0
        sizes: dict[str, int] = {}
        for name, url in choice.files.items():
            head = client.head(url)
            sizes[name] = int(head.headers.get("content-length", 0))
            total += sizes[name]
        done = 0
        for name, url in choice.files.items():
            dest = target / name
            if dest.is_file():
                done += sizes[name]
                continue
            tmp = dest.with_suffix(".part")
            with client.stream("GET", url) as r:
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 16):
                        fh.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            font = pymupdf.Font(fontfile=str(tmp))
            if not font.has_glyph(ord("あ")):
                tmp.unlink(missing_ok=True)
                raise ValueError("ダウンロードしたフォントが正しくありません")
            tmp.replace(dest)
    finally:
        if own:
            client.close()


def remove(font_id: str) -> None:
    for name in FONT_CHOICES[font_id].files:
        (paths.fonts_dir() / name).unlink(missing_ok=True)
