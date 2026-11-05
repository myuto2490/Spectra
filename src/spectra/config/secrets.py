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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import APP_NAME
from . import paths

log = logging.getLogger(__name__)

ENV_VARS = {
    "gemini": ("SPECTRA_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def _account(engine: str) -> str:
    return f"{engine}-api-key"


def slot(provider: str, key_id: str = "default") -> str:
    """キー 1 つの保存場所の名前。以前からのキー（default）は今までどおり provider だけ（アカウント名 gemini-api-key）で、
    書き直さない（macOS で、アドホック署名のアプリがキーチェーンの許可を求める回数を増やさないため）。"""
    return provider if key_id == "default" else f"{provider}-{key_id}"


@dataclass(frozen=True)
class KeyRef:
    """翻訳に使うキー 1 つ。中身は read() で、使う直前に初めて読む。"""

    id: str  # 設定の目録の id（環境変数なら "env"）
    label: str
    read: Callable[[], str]


def key_chain(settings, secrets: SecretStore, provider: str = "gemini") -> list[KeyRef]:
    """翻訳に使うキーを、使う順に返す。環境変数があればそれだけを使う（切り替えない）。

    目録が空で、以前からのキー（default）が保存されていれば、それを 1 本目として扱う。
    """
    for var in ENV_VARS.get(provider, ()):
        if os.environ.get(var):
            return [KeyRef("env", f"環境変数 {var}", lambda v=var: os.environ.get(v, "").strip())]
    entries = settings.keys_for(provider) if settings is not None else []
    if not entries and secrets.has(provider, use_env=False):
        entries = [{"id": "default", "provider": provider, "label": ""}]
    return [KeyRef(e["id"], e["label"] or default_label(provider, i), _reader(secrets, slot(provider, e["id"]))) for i, e in enumerate(entries)]


def _reader(secrets: SecretStore, name: str) -> Callable[[], str]:
    return lambda: secrets.get(name, use_env=False)


def default_label(provider: str, index: int) -> str:
    name = {"gemini": "Gemini"}.get(provider, provider)
    return name if index == 0 else f"{name} {index + 1}"


class SecretStore:
    def __init__(self, fallback_file: Path | None = None, use_keyring: bool | None = None):
        self.fallback_file = fallback_file or paths.config_dir() / "credentials.json"
        if use_keyring is None:
            # SPECTRA_HOME で保存先を切り替えているとき（テスト・ポータブル運用）は、
            # OS の資格情報ストアにある普段のキーを読み書きしない
            use_keyring = not os.environ.get("SPECTRA_HOME")
        self._keyring = self._init_keyring() if use_keyring else None
        # 読み出したキーはアプリを閉じるまで覚えておく（キーチェーンへの問い合わせを起動ごとに 1 回にする）
        self._cache: dict[str, str] = {}

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
        if engine in self._cache:
            return self._cache[engine]
        if self._keyring is not None:
            # macOS では、項目を作ったアプリ以外が読むと許可を求められる。アドホック署名のアプリはビルドごとに
            # 別のアプリとして扱われるので、保存し直して持ち主を移すことはしない（ビルドを切り替えるたびに
            # 読み出しと削除で 2 回求められるため）。確認で「常に許可」を選べば、そのアプリでは次から求められない
            try:
                value = self._keyring.get_password(APP_NAME, _account(engine))
                if value:
                    self._cache[engine] = value
                    return value
            except Exception:
                log.warning("failed to read from keyring", exc_info=True)
        value = self._read_file().get(engine, "")
        if value:
            self._cache[engine] = value
        return value

    def has(self, engine: str, use_env: bool = True) -> bool:
        """キーが設定されているか。

        macOS のキーチェーンは、キーの中身を読まずに項目の有無だけを確かめる（起動時に許可を求めない）。
        """
        import sys

        if use_env and any(os.environ.get(var) for var in ENV_VARS.get(engine, ())):
            return True
        if engine in self._cache:
            return True
        if self._keyring is not None and sys.platform == "darwin":
            found = self._keychain_has(engine)
            if found is not None:
                return found or bool(self._read_file().get(engine))
        return bool(self.get(engine, use_env=use_env))

    def _keychain_has(self, engine: str) -> bool | None:
        """キーチェーンに項目があるか（属性だけを問い合わせる）。調べられなければ None。"""
        try:
            import ctypes

            from keyring.backends import macOS
            from keyring.backends.macOS import api

            if not isinstance(self._keyring.get_keyring(), macOS.Keyring):
                return None
            query = api.create_query(
                kSecClass=api.k_("kSecClassGenericPassword"),
                kSecMatchLimit=api.k_("kSecMatchLimitOne"),
                kSecAttrService=APP_NAME,
                kSecAttrAccount=_account(engine),
                kSecReturnAttributes=True,
            )
            status = api.SecItemCopyMatching(query, ctypes.byref(ctypes.c_void_p()))
        except Exception:
            log.info("could not look up the keychain item", exc_info=True)
            return None
        if status == 0:
            return True
        if status == api.error.item_not_found:
            return False
        return None

    def source(self, engine: str) -> str | None:
        """キーの保存場所（表示用）。未設定なら None。"""
        for var in ENV_VARS.get(engine, ()):
            if os.environ.get(var):
                return f"環境変数 {var}"
        if self.has(engine, use_env=False):
            return self.backend_name
        return None

    def set(self, engine: str, value: str) -> None:
        value = value.strip()
        if not value:
            self.delete(engine)
            return
        self._cache[engine] = value
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
        self._cache.pop(engine, None)
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
