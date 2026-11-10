"""和文フォントの選択とダウンロード。

Noto Serif JP / Noto Sans JP（SIL Open Font License）は、利用者の操作でダウンロードする。
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..errors import SpectraError
from ..translate.base import USER_AGENT
from . import paths

log = logging.getLogger(__name__)

# 取得するファイルは、コミットで固定し、大きさと sha256 を確かめる（上流のブランチが書き換わっても、
# 確かめていないフォントを読み込んだり PDF に埋め込んだりしない）
_NOTO_COMMIT = "f8d157532fbfaeda587e826d4cd5b21a49186f7c"
_NOTO = f"https://github.com/notofonts/noto-cjk/raw/{_NOTO_COMMIT}"


class FontDownloadError(SpectraError):
    title = "フォントをダウンロードできませんでした"
    hint = "インターネットへの接続を確認して、もう一度お試しください。"


@dataclass(frozen=True)
class FontChoice:
    font_id: str
    name: str
    description: str
    serif: bool
    files: dict[str, str]  # 保存名 -> URL（regular, bold の順）
    size_mb: float = 0.0
    checks: dict[str, tuple[int, str]] = field(default_factory=dict)  # 保存名 -> (バイト数, sha256)


FONT_CHOICES: dict[str, FontChoice] = {
    "builtin": FontChoice("builtin", "Noto Sans JP（ゴシック体・内蔵）", "画面でも紙でも読みやすいゴシック体。追加のダウンロードは不要です。", False, {}),
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
        {
            "NotoSerifJP-Regular.otf": (6_210_492, "2c9a12dbd4f2408c4610c7ee84a108b62d7236c3775baed618c64d9cb44b2f04"),
            "NotoSerifJP-Bold.otf": (6_479_172, "1e03488a0d5e819f07fcd74f54703a7961ba466d3ae900f8a2a730541e6d4543"),
        },
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
    regular, bold = bundled_font()
    return ResolvedFont(regular, bold, False)


def bundled_font() -> tuple[str | None, str | None]:
    """アプリに同梱した Noto Sans JP（標準・太字）。見つからなければ (None, None)。"""
    from ..core.fontbook import BUNDLED_BOLD, BUNDLED_REGULAR

    return (
        str(BUNDLED_REGULAR) if BUNDLED_REGULAR.is_file() else None,
        str(BUNDLED_BOLD) if BUNDLED_BOLD.is_file() else None,
    )


def download(font_id: str, progress: Callable[[int, int], None] | None = None, client: httpx.Client | None = None) -> None:
    """フォントをダウンロードする。大きさと sha256 が合ったファイルだけを置き、途中で失敗しても不完全なファイルは残さない。"""
    choice = FONT_CHOICES[font_id]
    target = paths.fonts_dir()
    target.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True, headers={"User-Agent": USER_AGENT})
    total = sum(size for size, _ in choice.checks.values())
    done = 0
    try:
        for name, url in choice.files.items():
            size, sha = choice.checks[name]
            dest = target / name
            if dest.is_file():
                done += size
                continue
            tmp = dest.with_suffix(".part")
            digest = hashlib.sha256()
            received = 0
            try:
                with client.stream("GET", url) as r, open(tmp, "wb") as fh:
                    r.raise_for_status()
                    for chunk in r.iter_bytes(1 << 16):
                        received += len(chunk)
                        if received > size:  # 想定より大きい応答は、そこで読むのをやめる
                            raise FontDownloadError("ダウンロードしたファイルが想定と違います。", hint="時間をおいて、もう一度お試しください。")
                        fh.write(chunk)
                        digest.update(chunk)
                        if progress:
                            progress(done + received, total)
                if received != size or digest.hexdigest() != sha:
                    raise FontDownloadError("ダウンロードしたファイルが想定と違います。", hint="時間をおいて、もう一度お試しください。")
                tmp.replace(dest)
            except httpx.HTTPError as e:
                raise FontDownloadError() from e
            finally:
                tmp.unlink(missing_ok=True)  # 置き換えた後は無い。失敗したときの書きかけを残さない
            done += size
    finally:
        if own:
            client.close()


def remove(font_id: str) -> None:
    for name in FONT_CHOICES[font_id].files:
        (paths.fonts_dir() / name).unlink(missing_ok=True)
