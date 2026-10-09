"""OS ごとの標準ディレクトリ。環境変数 SPECTRA_HOME で一括変更できる（テスト・ポータブル運用向け）。"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import PlatformDirs

from .. import APP_NAME

_dirs = PlatformDirs(APP_NAME, appauthor=False, roaming=True)


def _home() -> Path | None:
    value = os.environ.get("SPECTRA_HOME")
    return Path(value) if value else None


def config_dir() -> Path:
    home = _home()
    return home / "config" if home else Path(_dirs.user_config_dir)


def data_dir() -> Path:
    home = _home()
    return home / "data" if home else Path(_dirs.user_data_dir)


def cache_dir() -> Path:
    home = _home()
    return home / "cache" if home else Path(_dirs.user_cache_dir)


def log_dir() -> Path:
    home = _home()
    return home / "logs" if home else Path(_dirs.user_log_dir)


def settings_file() -> Path:
    return config_dir() / "settings.json"


def translation_cache_file() -> Path:
    return cache_dir() / "translations.sqlite3"


def fonts_dir() -> Path:
    return data_dir() / "fonts"
