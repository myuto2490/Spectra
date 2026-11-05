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


# 1 日の上限は米国太平洋時間の 0 時に戻る（米国の夏時間のあいだは日本時間の 16 時、それ以外は 17 時）
RESET_TIME = "上限は日本時間の 17 時（米国の夏時間のあいだは 16 時）ごろに戻ります。"


class QuotaExceededError(TranslatorError):
    title = "無料枠の 1 日あたりの上限に達しました"
    hint = (
        f"{RESET_TIME}翻訳済みの段落はキャッシュに保存されているので、続きから翻訳できます。"
        "設定で別のモデルを選ぶか、API キーを追加すると、別の枠で続けられる場合があります。"
    )
    all_keys_hint = (
        f"登録したすべての API キーが、今日の上限に達しました。{RESET_TIME}"
        "翻訳済みの段落はキャッシュに保存されているので、続きから翻訳できます。"
    )


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
