"""論文ライブラリのエラー。"""

from __future__ import annotations

from ..errors import SpectraError


class LibraryError(SpectraError):
    title = "ライブラリの操作に失敗しました"


class LookupFailed(LibraryError):
    title = "書誌情報を照会できませんでした"
    hint = "インターネット接続を確認して、あとで再試行してください。"
