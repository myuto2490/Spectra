"""翻訳エンジンのエラー。UI にそのまま表示できる日本語メッセージと対処方法を持つ。"""

from __future__ import annotations

from ..errors import SpectraError


class TranslatorError(SpectraError):
    title = "翻訳に失敗しました"
    retryable = False


class MissingKeyError(TranslatorError):
    title = "API キーが設定されていません"
    hint = "設定 → 翻訳エンジン から API キーを入力してください。"


class AuthenticationError(TranslatorError):
    title = "API キーが正しくありません"
    hint = "設定 → 翻訳エンジン で API キーを確認してください。"


class QuotaExceededError(TranslatorError):
    title = "今月の無料翻訳枠を使い切りました"
    hint = "翻訳済みの段落はキャッシュに保存されています。来月の枠の更新後に続きから翻訳できます。別の翻訳エンジンに切り替えることもできます。"


class RateLimitError(TranslatorError):
    title = "翻訳サーバーへのリクエストが多すぎます"
    hint = "しばらく待ってから再試行してください。"
    retryable = True


class NetworkError(TranslatorError):
    title = "翻訳サーバーに接続できません"
    hint = "インターネット接続を確認してください。"
    retryable = True


class ServiceError(TranslatorError):
    title = "翻訳サーバーでエラーが発生しました"
    hint = "時間をおいて再試行してください。"
    retryable = True


class CancelledError(TranslatorError):
    title = "キャンセルされました"
