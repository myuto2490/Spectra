"""右のパネル：「論文の詳細」「AI 翻訳の詳細」「ノート」の 3 つのタブ。

ライブラリ画面（mode="library"）でも読書画面（mode="reader"）でも同じ部品を使う。選んだタブは
``settings.side_panel_tab`` に覚える。論文の詳細・翻訳の操作のシグナルは ``PaperDetail`` のものをそのまま出す。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..config.settings import Settings
from ..library import Paper
from .paper_detail import NOTE_TAB, PAPER_TAB, TRANSLATION_TAB, PaperDetail

TAB_KEYS = {PAPER_TAB: "details", TRANSLATION_TAB: "translation", NOTE_TAB: "note"}
TAB_INDEX = {key: i for i, key in TAB_KEYS.items()}

_FORWARDED = (
    "readRequested",
    "translateRequested",
    "cancelRequested",
    "editRequested",
    "retryLookupRequested",
    "enableLookupRequested",
    "removeRequested",
    "copyBibtexRequested",
    "restoreRequested",
    "purgeRequested",
    "cancelDownloadRequested",
    "retryDownloadRequested",
)


class SidePanel(QWidget):
    readRequested = Signal(int)
    translateRequested = Signal(list)
    cancelRequested = Signal(int)
    editRequested = Signal(int)
    retryLookupRequested = Signal(int)
    enableLookupRequested = Signal()
    removeRequested = Signal(list)
    copyBibtexRequested = Signal(list)
    restoreRequested = Signal(list)
    purgeRequested = Signal(list)
    cancelDownloadRequested = Signal(int)
    retryDownloadRequested = Signal(int)
    tabChanged = Signal(str)  # "details" / "translation" / "note"
    noteChanged = Signal(int, str)  # 保存したノート（論文 ID・本文）

    def __init__(self, library, settings: Settings, mode: str = "library", parent: QWidget | None = None):
        super().__init__(parent)
        if mode not in ("library", "reader"):
            raise ValueError(f"unknown mode: {mode}")
        self.library = library
        self.settings = settings
        self.mode = mode
        self.setObjectName("Inspector")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(324)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.detail = PaperDetail(library=library)
        lay.addWidget(self.detail)
        for name in _FORWARDED:
            getattr(self.detail, name).connect(getattr(self, name))
        self.detail.note_editor.noteChanged.connect(self.noteChanged)
        self.detail.tabChanged.connect(self._on_tab_changed)
        self.detail.set_tab(TAB_INDEX.get(settings.side_panel_tab, PAPER_TAB))

    @property
    def note_editor(self):
        return self.detail.note_editor

    # -- 表示 ------------------------------------------------------------

    def set_paper(self, paper: Paper | None) -> None:
        """1 つの論文を表示する（何も選んでいなければ None）。状態つきの表示は ``detail.show_papers`` を使う。"""
        self.detail.show_papers([paper] if paper is not None else [])

    def set_translation_widget(self, widget: QWidget | None) -> None:
        """「AI 翻訳の詳細」のタブに、翻訳の設定のパネル（読書画面のインスペクタ）を入れる。"""
        self.detail.set_translation_widget(widget)

    def flush_note(self) -> None:
        """入力中のノートを今すぐ保存する（論文を閉じる・アプリを終了するとき）。"""
        self.detail.note_editor.flush()

    # -- タブ ------------------------------------------------------------

    def current_tab(self) -> str:
        return TAB_KEYS.get(self.detail.current_tab(), "details")

    def set_current_tab(self, tab: str) -> None:
        if tab in TAB_INDEX:
            self.detail.set_tab(TAB_INDEX[tab])

    def _on_tab_changed(self, index: int) -> None:
        key = TAB_KEYS.get(index, "details")
        if self.settings.side_panel_tab != key:
            self.settings.side_panel_tab = key
            self.settings.save()
        self.tabChanged.emit(key)
