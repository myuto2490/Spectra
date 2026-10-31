"""論文ライブラリのエラー。"""

from __future__ import annotations

from ..errors import SpectraError


class LibraryError(SpectraError):
    title = "ライブラリの操作に失敗しました"


class LookupFailed(LibraryError):
    title = "書誌情報を照会できませんでした"
    hint = "インターネット接続を確認して、あとで再試行してください。"


class PaperNotFound(LibraryError):
    """論文が見つからない（別の端末から削除された場合を含む）。翻訳のワーカーではキャンセルと同じに扱う。"""

    title = "論文が見つかりません"


class SyncError(SpectraError):
    title = "同期できませんでした"


class SyncUnavailable(SyncError):
    title = "同期フォルダを読めません"
    hint = "同期フォルダの場所と、Google Drive などの同期アプリが動いているかを確認してください。"


class RemoteMissing(SyncError):
    title = "この論文のファイルはまだ同期されていません"
    hint = "別のパソコンからの同期が終わるまで待ってから、もう一度開いてください。"


class HashMismatch(SyncError):
    title = "ファイルの同期が終わっていないか、ファイルが壊れています"
    hint = "しばらく待ってから再試行してください。"
