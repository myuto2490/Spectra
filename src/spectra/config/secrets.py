"""API キーの保管。

OS の資格情報ストア（macOS キーチェーン / Windows 資格情報マネージャー / Linux Secret Service）に保存する。
利用できない環境では、所有者だけが読める権限（600）のファイルに保存し、その旨を UI に表示する。
環境変数が設定されていればそれを優先する（CLI・CI 向け）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path

from .. import APP_NAME
from . import paths

log = logging.getLogger(__name__)

ENV_VARS = {
    "gemini": ("SPECTRA_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def _account(engine: str) -> str:
    return f"{engine}-api-key"


class SecretStore:
    def __init__(self, fallback_file: Path | None = None, use_keyring: bool = True):
        self.fallback_file = fallback_file or paths.config_dir() / "credentials.json"
        self._keyring = self._init_keyring() if use_keyring else None

    @staticmethod
    def _init_keyring():
        try:
            import keyring
            from keyring.backends import fail

            backend = keyring.get_keyring()
            if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 1) <= 0:
                return None
            return keyring
        except Exception:
            log.info("keyring is unavailable", exc_info=True)
            return None

    @property
    def secure(self) -> bool:
        """OS の資格情報ストアを使っているか。"""
        return self._keyring is not None

    @property
    def backend_name(self) -> str:
        if self._keyring is None:
            return "設定ファイル"
        import sys

        if sys.platform == "darwin":
            return "macOS キーチェーン"
        if sys.platform == "win32":
            return "Windows 資格情報マネージャー"
        return "システムのキーリング"

    # -- 取得・保存・削除 --------------------------------------------------------

    def get(self, engine: str, use_env: bool = True) -> str:
        if use_env:
            for var in ENV_VARS.get(engine, ()):
                if os.environ.get(var):
                    return os.environ[var].strip()
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(APP_NAME, _account(engine))
                if value:
                    return value
            except Exception:
                log.warning("failed to read from keyring", exc_info=True)
        return self._read_file().get(engine, "")

    def source(self, engine: str) -> str | None:
        """キーの保存場所（表示用）。未設定なら None。"""
        for var in ENV_VARS.get(engine, ()):
            if os.environ.get(var):
                return f"環境変数 {var}"
        if self.get(engine, use_env=False):
            return self.backend_name
        return None

    def set(self, engine: str, value: str) -> None:
        value = value.strip()
        if not value:
            self.delete(engine)
            return
        if self._keyring is not None:
            try:
                self._keyring.set_password(APP_NAME, _account(engine), value)
                self._remove_from_file(engine)
                return
            except Exception:
                log.warning("failed to write to keyring, falling back to file", exc_info=True)
        data = self._read_file()
        data[engine] = value
        self._write_file(data)

    def delete(self, engine: str) -> None:
        if self._keyring is not None:
            # 未登録の場合も例外になるため無視する
            with contextlib.suppress(Exception):
                self._keyring.delete_password(APP_NAME, _account(engine))
        self._remove_from_file(engine)

    # -- フォールバック用ファイル ----------------------------------------------------

    def _read_file(self) -> dict[str, str]:
        try:
            data = json.loads(self.fallback_file.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_file(self, data: dict[str, str]) -> None:
        self.fallback_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.fallback_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.fallback_file)
        with contextlib.suppress(OSError):
            os.chmod(self.fallback_file, 0o600)

    def _remove_from_file(self, engine: str) -> None:
        data = self._read_file()
        if engine in data:
            del data[engine]
            if data:
                self._write_file(data)
            else:
                self.fallback_file.unlink(missing_ok=True)
