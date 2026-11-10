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


def reset_time_text(now: float | None = None) -> str:
    """1 日の上限が戻る時刻の案内。このパソコンの時刻で「今日の 16:00 ごろ」のように具体的に書く。

    上限は米国太平洋時間の 0 時に戻る（日本時間では、米国の夏時間のあいだは 16 時、それ以外は 17 時）。
    """
    import time

    from ..config.quota import next_reset

    now = time.time() if now is None else now
    reset = time.localtime(next_reset(now))
    today = time.localtime(now)
    day = "今日" if (reset.tm_year, reset.tm_yday) == (today.tm_year, today.tm_yday) else "明日"
    return f"上限は{day}の {reset.tm_hour}:{reset.tm_min:02d} ごろに戻ります。"


class QuotaExceededError(TranslatorError):
    title = "無料枠の 1 日あたりの上限に達しました"

    def __init__(self, detail: str = "", *, hint: str | None = None, all_keys: bool = False):
        """hint を渡さなければ、上限が戻る時刻を入れた案内にする（all_keys: 登録したキーがすべて上限）。"""
        if hint is None:
            if all_keys:
                hint = (
                    f"登録したすべての API キーが、今日の上限に達しました。{reset_time_text()}"
                    "翻訳済みの段落はキャッシュに保存されているので、続きから翻訳できます。"
                )
            else:
                hint = (
                    f"{reset_time_text()}翻訳済みの段落はキャッシュに保存されているので、続きから翻訳できます。"
                    "設定で別のモデルを選ぶか、API キーを追加すると、別の枠で続けられる場合があります。"
                )
        super().__init__(detail, hint=hint)


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
