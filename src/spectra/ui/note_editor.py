"""論文ごとのノートの編集欄（プレーンテキスト）。入力をやめて 1 秒後に自動で保存する。"""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget

from ..library import Paper
from .widgets import label, set_prop

log = logging.getLogger(__name__)

try:  # ノートの保存（別のブランチ）が入る前は、同じ値を仮に使う
    from ..library.models import NOTE_MAX_CHARS
except ImportError:  # pragma: no cover
    NOTE_MAX_CHARS = 100_000

SAVE_DELAY_MS = 1000
MERGED_NOTICE = "ほかのパソコンでの編集と連結しました。内容を確かめて整理してください。"


def _updated_text(ts: float | None) -> str:
    if not ts:
        return ""
    return "最終更新 " + datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M")


class NoteEditor(QWidget):
    """選んだ論文のノート。論文を切り替えるとき・欄を離れたとき・閉じるときに、未保存の分をすぐ保存する。

    保存は ``library.set_note(paper_id, text)``。保存できたら ``noteChanged(paper_id, text)`` を通知する
    （library に set_note が無い間は通知だけを行う）。
    """

    noteChanged = Signal(int, str)  # 論文 ID・保存した本文

    def __init__(self, library=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Inspector")
        self.library = library
        self._paper_id: int | None = None
        self._dirty = False
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(8)
        self.placeholder = label("論文を選ぶと、ここにノートを書けます。", "secondary", wrap=True)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.placeholder)
        self.notice = label(MERGED_NOTICE, "caption", tone="warning", wrap=True)
        lay.addWidget(self.notice)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("この論文のメモ（読んでわかったこと・あとで確かめたいことなど）")
        self.edit.setAccessibleName("ノート")
        self.edit.textChanged.connect(self._on_text_changed)
        self.edit.installEventFilter(self)
        lay.addWidget(self.edit, 1)
        self.status = label("", "caption", wrap=True)
        lay.addWidget(self.status)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY_MS)
        self._timer.timeout.connect(self.flush)
        self.set_paper(None)

    # -- 表示 ------------------------------------------------------------

    def text(self) -> str:
        return self.edit.toPlainText()

    @property
    def paper_id(self) -> int | None:
        return self._paper_id

    @property
    def dirty(self) -> bool:
        return self._dirty

    def set_paper(self, paper: Paper | None) -> None:
        """論文を切り替える。前の論文の未保存の分は先に保存する。同じ論文の再表示では、入力中の内容を消さない。"""
        new_id = paper.id if paper is not None else None
        if new_id != self._paper_id:
            self.flush()
        self._paper_id = new_id
        self.placeholder.setVisible(paper is None)
        self.edit.setVisible(paper is not None)
        if paper is None:
            self.notice.hide()
            self._set_status("")
            self._load("")
            return
        saved = getattr(paper, "note", "") or ""
        # 入力の途中は、同期などで届いた内容で上書きしない（入力が保存されたあとの再表示で更新される）
        if not self._dirty and self.text() != saved:
            self._load(saved)
        self.notice.setVisible(bool(getattr(paper, "note_merged", False)))
        if not self._dirty:
            self._set_status(_updated_text(getattr(paper, "note_updated_at", None)))

    def _load(self, text: str) -> None:
        self._loading = True
        try:
            cursor = self.edit.textCursor().position()
            self.edit.setPlainText(text)
            c = self.edit.textCursor()
            c.setPosition(min(cursor, len(text)))
            self.edit.setTextCursor(c)
        finally:
            self._loading = False
        self._dirty = False
        self._timer.stop()

    def _set_status(self, text: str, tone: str | None = None) -> None:
        self.status.setText(text)
        set_prop(self.status, "tone", tone)

    # -- 編集と保存 ------------------------------------------------------------

    def _on_text_changed(self) -> None:
        if self._loading or self._paper_id is None:
            return
        text = self.text()
        if len(text) > NOTE_MAX_CHARS:
            # 上限を超えた分は入れない（貼り付けで超えたときも、超えた分だけ除く）
            self._loading = True
            try:
                pos = min(self.edit.textCursor().position(), NOTE_MAX_CHARS)
                self.edit.setPlainText(text[:NOTE_MAX_CHARS])
                c = self.edit.textCursor()
                c.setPosition(pos)
                self.edit.setTextCursor(c)
            finally:
                self._loading = False
            self._set_status(f"ノートは {NOTE_MAX_CHARS:,} 文字までです。超えた分は入りません。", "warning")
        else:
            self._set_status("保存していません…")
        self._dirty = True
        self._timer.start()

    def flush(self) -> None:
        """未保存の分を今すぐ保存する。"""
        self._timer.stop()
        if not self._dirty or self._paper_id is None:
            return
        paper_id, text = self._paper_id, self.text()
        save = getattr(self.library, "set_note", None)
        if save is not None:
            try:
                save(paper_id, text)
            except Exception as e:  # noqa: BLE001 - 保存に失敗しても入力は残し、再試行できるようにする
                log.warning("could not save the note", exc_info=True)
                self._set_status(f"ノートを保存できませんでした（{e}）", "danger")
                return
        self._dirty = False
        self.notice.hide()
        self._set_status(_updated_text(datetime.now().timestamp()))
        self.noteChanged.emit(paper_id, text)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt の名前
        if obj is self.edit and event.type() == QEvent.Type.FocusOut:
            self.flush()
        return super().eventFilter(obj, event)

    def hideEvent(self, event):  # noqa: N802
        self.flush()
        super().hideEvent(event)
