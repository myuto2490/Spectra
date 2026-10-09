"""アプリ全体で使う、利用者向けメッセージを持つ例外の基底。"""

from __future__ import annotations


class SpectraError(Exception):
    title = "エラーが発生しました"
    hint = ""

    def __init__(self, detail: str = "", *, hint: str | None = None):
        super().__init__(detail or self.title)
        self.detail = detail
        if hint is not None:
            self.hint = hint

    def user_message(self) -> str:
        parts = [self.title]
        if self.detail and self.detail != self.title:
            parts.append(self.detail)
        if self.hint:
            parts.append(self.hint)
        return "\n".join(parts)


class DocumentError(SpectraError):
    title = "PDF を開けませんでした"


class EncryptedDocumentError(DocumentError):
    title = "パスワードで保護された PDF です"
    hint = "パスワードを解除した PDF を用意してから開いてください。"


class NoTextError(DocumentError):
    title = "翻訳できるテキストが見つかりません"
    hint = "スキャンした画像だけの PDF には対応していません。文字を選択・コピーできる PDF を使ってください。"
