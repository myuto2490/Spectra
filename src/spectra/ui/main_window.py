"""メインウィンドウ。

論文を集めて整理する「ライブラリ」と、1 本の論文を原文・訳文で読む「読書画面」の 2 画面で構成する。
読書画面は ツールバー（主操作）・プレビュー（主役）・インスペクタ（翻訳の設定）の 3 層。
翻訳はライブラリのどちらの画面からでもキューに入れられ、1 本ずつ順にバックグラウンドで進む。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

import pymupdf
from PySide6.QtCore import QByteArray, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME
from ..config import paths
from ..config.secrets import SecretStore
from ..config.settings import Settings
from ..core.export import build_output, finalize
from ..core.options import OutputMode
from ..core.pipeline import JobResult, Progress, TranslationJob, open_document
from ..errors import SpectraError
from ..library import Library, Paper, Translation
from ..library.errors import PaperNotFound, RemoteMissing
from ..translate import registry
from ..translate.cache import TranslationCache
from ..translate.errors import (
    AuthenticationError,
    CancelledError,
    MissingKeyError,
    QuotaExceededError,
)
from ..translate.service import PartialTranslationError
from . import icons, theme, worker
from .about import AboutDialog
from .export_dialog import ExportDialog, suffix_for
from .inspector import Inspector
from .jobs import LookupQueue, TranslationQueue, TranslationRequest
from .library_model import TRASH_DAYS
from .library_view import LibraryView
from .paper_detail import MetadataDialog
from .pdf_view import PreviewPair
from .settings_dialog import SettingsDialog
from .sync_controller import ERROR, SYNCING, UNAVAILABLE, SyncController
from .widgets import (
    Banner,
    SegmentedControl,
    button,
    label,
    plain_box,
    plain_tooltip,
    refresh_icon,
    tool_button,
)

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/myuto2490/Spectra"
LIBRARY, READER = "library", "reader"
DOWNLOAD_STAGE = "download"  # 翻訳の前に原文を取得している（翻訳キューの進捗として送る）


def _mod(key: str) -> str:
    return f"⌘{key}" if theme.IS_MAC else f"Ctrl+{key}"


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: Settings | None = None,
        secrets: SecretStore | None = None,
        library: Library | None = None,
        sync_engine_factory=None,
    ):
        super().__init__()
        self.settings = settings or Settings.load()
        self.secrets = secrets or SecretStore()
        self.cache = TranslationCache(paths.translation_cache_file())
        self.library = library or Library()
        self.queue = TranslationQueue(self._run_translation, self)
        self.lookups = LookupQueue(self.library, self)
        self.sync = SyncController(self.settings, self.library, sync_engine_factory, self)
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(QSize(980, 640))
        self.setAcceptDrops(True)
        self.setUnifiedTitleAndToolBarOnMac(True)

        self.screen_mode = LIBRARY
        # 読書画面で開いている論文
        self.paper: Paper | None = None
        self.source_bytes: bytes | None = None
        self.original_doc: pymupdf.Document | None = None
        self.translated_doc: pymupdf.Document | None = None
        self.translated_pdf: bytes | None = None  # 翻訳のみの版（ページ構成は原文と同じ）
        self.translated_range: tuple[int, int | None] = (0, None)  # 翻訳したページの範囲
        self.result: JobResult | None = None  # この画面で翻訳した直後の結果
        self.result_stale = False
        self._estimate_gen = 0
        self._status_message = ""
        self._banner_callback: Callable[[], None] | None = None
        self._job_ids: set[int] = set()

        self._build_ui()
        self._build_actions()
        self._restore_window()
        self._estimate_timer = QTimer(self, singleShot=True, interval=350, timeout=self._start_estimate)
        self.queue.changed.connect(self._on_queue_changed)
        self.queue.progress.connect(self._on_job_progress)
        self.queue.finished.connect(self._on_job_done)
        self.queue.failed.connect(self._on_job_failed)
        self.lookups.updated.connect(self._on_lookup_updated)
        self.lookups.changed.connect(self._on_lookups_changed)
        self.sync.stateChanged.connect(self._refresh_sync_status)
        self.sync.remoteChanged.connect(self._on_remote_changes)
        self.sync.downloadChanged.connect(self._on_download_changed)
        self.sync.downloadFinished.connect(self._on_download_finished)
        self.sync.downloadFailed.connect(self._on_download_failed)
        self.sync.synced.connect(self._on_synced)
        self._sync_clock = QTimer(self, interval=30_000, timeout=self._refresh_sync_status)  # 「2 分前」を更新する
        self._sync_clock.start()
        # アプリが前面に来たときにも同期する（ほかのパソコンで変えた内容をすぐ反映するため）
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(
                lambda st: st == Qt.ApplicationState.ApplicationActive and self.sync.sync_now()
            )
        self._first_sync_count: int | None = None  # 接続した直後の論文の数（初回の同期の知らせに使う）
        self.library_view.reload()
        self._refresh_sync_status()
        # 30 日を過ぎたゴミ箱の論文を完全に削除する（同期の有無に関係なく）
        worker.start(lambda _r, _c: self.library.purge_expired(), lambda n: n and self.library_view.reload(), lambda e: log.warning("purge failed: %s", e))
        self.refresh_engine_status()
        self._update_state()
        # 前回、照会が終わる前に閉じた論文の続き
        if self.settings.metadata_lookup:
            pending = [p.id for p in self.library.papers() if p.meta_status == "pending"]
            if pending:
                self.lookups.enqueue(pending)

    # ======================================================================
    # 画面構成
    # ======================================================================

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Window")
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.library_view = LibraryView(self.library)
        v.addWidget(self._build_toolbar())

        banner_wrap = QWidget()
        bl = QVBoxLayout(banner_wrap)
        bl.setContentsMargins(16, 12, 16, 0)
        self.banner = Banner()
        self.banner.action.connect(self._on_banner_action)
        bl.addWidget(self.banner)
        self.banner_wrap = banner_wrap
        banner_wrap.hide()
        self.banner.closed.connect(banner_wrap.hide)
        v.addWidget(banner_wrap)

        self.stack = QStackedWidget()
        self.library_view.openRequested.connect(self.open_paper)
        self.library_view.addRequested.connect(self.open_dialog)
        self.library_view.settingsRequested.connect(lambda: self.show_settings("engines"))
        self.library_view.selectionChanged.connect(self._refresh_detail)
        self.library_view.searchReset.connect(lambda: self.search.clear())
        self.library_view.table.customContextMenuRequested.connect(self._table_menu)
        d = self.library_view.detail
        d.readRequested.connect(self.open_paper)
        d.translateRequested.connect(self.translate_papers)
        d.cancelRequested.connect(self.queue.remove)
        d.editRequested.connect(self.edit_metadata)
        d.retryLookupRequested.connect(lambda pid: self._lookup([pid]))
        d.enableLookupRequested.connect(self.enable_lookup)
        d.removeRequested.connect(self.remove_papers)
        d.copyBibtexRequested.connect(self.copy_bibtex)
        d.restoreRequested.connect(self.restore_papers)
        d.purgeRequested.connect(self.purge_papers)
        d.cancelDownloadRequested.connect(self.sync.cancel_download)
        d.retryDownloadRequested.connect(lambda pid: self.sync.download(pid, "open"))
        self.library_view.syncSetupRequested.connect(lambda: self.show_settings("sync"))
        self.library_view.syncStatusClicked.connect(self.show_sync_popover)
        self.library_view.emptyTrashRequested.connect(self.empty_trash)
        self.stack.addWidget(self.library_view)

        reader = QWidget()
        rl = QHBoxLayout(reader)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        self.preview = PreviewPair()
        self.preview.original.pageChanged.connect(self._on_page_changed)
        self.preview.translated.pageChanged.connect(self._on_page_changed)
        self.preview.original.zoomChanged.connect(self._on_zoom_changed)
        self.preview.translated.zoomChanged.connect(self._on_zoom_changed)
        rl.addWidget(self.preview, 1)
        self.inspector = Inspector(self.settings)
        self.inspector.settingsChanged.connect(self._on_settings_changed)
        self.inspector.openSettings.connect(self.show_settings)
        rl.addWidget(self.inspector)
        self.content = reader
        self.stack.addWidget(reader)
        v.addWidget(self.stack, 1)

        self.footer = self._build_footer()
        v.addWidget(self.footer)
        self.setCentralWidget(root)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Toolbar")
        bar.setFixedHeight(60)
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 10, 16, 10)
        h.setSpacing(8)

        # ライブラリ
        self.sidebar_btn = tool_button("sidebar_left", f"サイドバーを表示/隠す（⌥{_mod('S')}）" if theme.IS_MAC else "サイドバーを表示/隠す（Ctrl+Alt+S）", checkable=True)
        self.sidebar_btn.setChecked(True)
        self.sidebar_btn.toggled.connect(lambda on: self.library_view.side.setVisible(on))
        h.addWidget(self.sidebar_btn)
        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("タイトル・著者・雑誌・DOI で検索")
        self.search.setAccessibleName("ライブラリを検索")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(440)
        self.search.setMinimumWidth(240)
        self._search_icon = self.search.addAction(
            icons.icon("search", theme.current().secondary, 16), QLineEdit.ActionPosition.LeadingPosition
        )
        self.search.textChanged.connect(self.library_view.set_search)
        h.addWidget(self.search, 1)

        # 読書画面
        self.back_btn = tool_button("back", f"ライブラリに戻る（{_mod('[')}）")
        self.back_btn.clicked.connect(self.show_library)
        h.addWidget(self.back_btn)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.doc_title = label("", "headline")
        self.doc_subtitle = label("", "caption")
        titles.addWidget(self.doc_title)
        titles.addWidget(self.doc_subtitle)
        h.addSpacing(4)
        h.addLayout(titles, 1)
        h.addStretch(0)

        self.view_mode = SegmentedControl([("original", "原文"), ("both", "対訳"), ("translated", "翻訳")])
        self.view_mode.setAccessibleName("表示")
        h.addWidget(self.view_mode)
        h.addSpacing(8)

        self.secondary_btn = button("")
        self.secondary_btn.clicked.connect(self._on_secondary)
        h.addWidget(self.secondary_btn)
        self.primary_btn = button("翻訳", primary=True)
        self.primary_btn.setMinimumWidth(112)
        self.primary_btn.clicked.connect(self._on_primary)
        h.addWidget(self.primary_btn)
        self.add_btn = button("＋ 論文を追加", primary=True)
        self.add_btn.setToolTip(f"PDF をライブラリに追加（{_mod('O')}）")
        self.add_btn.clicked.connect(self.open_dialog)
        h.addWidget(self.add_btn)
        h.addSpacing(4)
        self.settings_btn = tool_button("gear", "設定")
        self.settings_btn.clicked.connect(lambda: self.show_settings())
        h.addWidget(self.settings_btn)
        self.inspector_btn = tool_button("sidebar", "インスペクタを表示/隠す", checkable=True)
        self.inspector_btn.setChecked(True)
        self.inspector_btn.toggled.connect(lambda on: self.inspector.setVisible(on))
        h.addWidget(self.inspector_btn)
        self._library_widgets = [self.sidebar_btn, self.search, self.add_btn]
        self._reader_widgets = [self.back_btn, self.doc_title, self.doc_subtitle, self.view_mode, self.inspector_btn]
        return bar

    def _build_footer(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Footer")
        bar.setFixedHeight(36)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 4, 10, 4)
        h.setSpacing(8)
        self.status = label("", "caption")
        h.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFixedWidth(220)
        self.progress.setAccessibleName("翻訳の進捗")
        self.progress.hide()
        h.addWidget(self.progress)
        self.queue_cancel_btn = button("すべて中止")
        self.queue_cancel_btn.clicked.connect(self.cancel_all_translations)
        self.queue_cancel_btn.hide()
        h.addWidget(self.queue_cancel_btn)
        h.addStretch(1)
        self.page_label = label("", "caption")
        h.addWidget(self.page_label)
        h.addSpacing(8)
        self.zoom_out_btn = tool_button("zoom_out", "縮小")
        self.zoom_out_btn.clicked.connect(lambda: self._zoom_by(1 / 1.2))
        self.zoom_label = QLabel("100%")
        self.zoom_label.setProperty("role", "caption")
        self.zoom_label.setFixedWidth(44)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_in_btn = tool_button("zoom_in", "拡大")
        self.zoom_in_btn.clicked.connect(lambda: self._zoom_by(1.2))
        self.fit_btn = tool_button("fit", "幅に合わせる")
        self.fit_btn.clicked.connect(self.preview.fit_width)
        self._zoom_widgets = [self.page_label, self.zoom_out_btn, self.zoom_label, self.zoom_in_btn, self.fit_btn]
        for w in self._zoom_widgets[1:]:
            h.addWidget(w)
        return bar

    def _build_actions(self) -> None:
        mod = "Ctrl"
        mb = self.menuBar()

        def act(text, slot, shortcut=None, role=None) -> QAction:
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if role is not None:
                a.setMenuRole(role)
            self.addAction(a)
            return a

        file_menu = mb.addMenu("ファイル")
        self.act_open = act("論文を追加…", self.open_dialog, QKeySequence.StandardKey.Open)
        self.act_save = act("PDF出力…", self.save_dialog, QKeySequence.StandardKey.Save)
        self.act_close = act("ライブラリに戻る", self.close_document, QKeySequence.StandardKey.Close)
        file_menu.addActions([self.act_open, self.act_save])
        file_menu.addSeparator()
        file_menu.addAction(self.act_close)
        file_menu.addSeparator()
        file_menu.addAction(act("設定…", lambda: self.show_settings(), QKeySequence.StandardKey.Preferences, QAction.MenuRole.PreferencesRole))
        file_menu.addAction(act("終了", self.close, QKeySequence.StandardKey.Quit, QAction.MenuRole.QuitRole))

        lib = mb.addMenu("ライブラリ")
        self.act_find = act("検索", self.focus_search, QKeySequence.StandardKey.Find)
        self.act_open_paper = act("開く", self._open_selected)
        self.act_favorite = act("お気に入り", lambda: self.library_view.toggle_favorite(), f"{mod}+D")
        self.act_edit = act("書誌情報を編集…", self._edit_selected, f"{mod}+I")
        self.act_lookup = act("書誌情報を取得し直す", lambda: self._lookup(self.library_view.selected_ids(), ask=True))
        self.act_bibtex = act("BibTeX をコピー", lambda: self.copy_bibtex(self.library_view.selected_ids()), f"{mod}+Shift+C")
        self.act_new_collection = act("新しいコレクション…", self.library_view.create_collection, f"{mod}+Shift+N")
        # 削除のショートカットは検索欄の文字の削除と重ならないよう、一覧にフォーカスがあるときだけ効かせる
        self.act_remove = QAction("ライブラリから削除…", self)
        self.act_remove.setShortcuts([QKeySequence(f"{mod}+Backspace"), QKeySequence(QKeySequence.StandardKey.Delete)])
        self.act_remove.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.act_remove.triggered.connect(lambda: self.remove_papers(self.library_view.selected_ids()))
        self.library_view.table.addAction(self.act_remove)
        lib.addActions([self.act_find, self.act_open_paper])
        lib.addSeparator()
        lib.addActions([self.act_favorite, self.act_edit, self.act_lookup, self.act_bibtex])
        lib.addSeparator()
        lib.addAction(self.act_new_collection)
        lib.addSeparator()
        lib.addAction(self.act_remove)

        tr = mb.addMenu("翻訳")
        self.act_translate = act("翻訳を開始", self.start_translation, f"{mod}+Return")
        self.act_cancel = act("キャンセル", self.cancel_translation, "Escape")
        self.act_cancel_all = act("すべての翻訳を中止", self.cancel_all_translations)
        tr.addActions([self.act_translate, self.act_cancel, self.act_cancel_all])

        view = mb.addMenu("表示")
        self.act_library = act("ライブラリ", self.show_library, f"{mod}+[")
        view.addAction(self.act_library)
        view.addAction(act("サイドバー", self.sidebar_btn.toggle, f"{mod}+Alt+S"))
        view.addSeparator()
        self._view_actions = []
        for key, text, sc in (("original", "原文", f"{mod}+1"), ("both", "対訳", f"{mod}+2"), ("translated", "翻訳", f"{mod}+3")):
            a = act(text, lambda _c=False, k=key: self.view_mode.set_current(k), sc)
            self._view_actions.append(a)
            view.addAction(a)
        view.addSeparator()
        self._zoom_actions = [
            act("拡大", lambda: self._zoom_by(1.2), QKeySequence.StandardKey.ZoomIn),
            act("縮小", lambda: self._zoom_by(1 / 1.2), QKeySequence.StandardKey.ZoomOut),
            act("幅に合わせる", self.preview.fit_width, f"{mod}+0"),
        ]
        view.addActions(self._zoom_actions)
        view.addSeparator()
        self.act_inspector = act("インスペクタ", self.inspector_btn.toggle, f"{mod}+Alt+I")
        view.addAction(self.act_inspector)

        help_menu = mb.addMenu("ヘルプ")
        help_menu.addAction(act(f"{APP_NAME} について", self.show_about, role=QAction.MenuRole.AboutRole))
        help_menu.addAction(act("使い方（GitHub）", lambda: QDesktopServices.openUrl(QUrl(f"{REPO_URL}#readme"))))
        help_menu.addAction(act("問題を報告…", lambda: QDesktopServices.openUrl(QUrl(f"{REPO_URL}/issues/new/choose"))))

        self.view_mode.changed.connect(self.preview.set_mode)

    # ======================================================================
    # 状態の反映
    # ======================================================================

    def _busy(self) -> bool:
        """翻訳キューが動いているか（待っている論文を含む）。"""
        return self.queue.is_active()

    def _paper_job(self) -> str | None:
        return self.queue.state(self.paper.id) if self.paper is not None else None

    def _update_state(self) -> None:
        reader = self.screen_mode == READER and self.original_doc is not None
        # 論文が 1 本もなくてもライブラリ画面を出す（空の状態は一覧の中で案内する）
        self.stack.setCurrentWidget(self.content if reader else self.library_view)
        for w in self._library_widgets:
            w.setVisible(not reader)
        for w in self._reader_widgets:
            w.setVisible(reader)
        self.primary_btn.setVisible(reader)
        self.inspector.setVisible(reader and self.inspector_btn.isChecked())

        job = self._paper_job() if reader else None
        has_result = reader and self.translated_pdf is not None and not self.result_stale
        self.view_mode.set_enabled("both", self.translated_doc is not None)
        self.view_mode.set_enabled("translated", self.translated_doc is not None)
        self.inspector.set_busy(job is not None)
        selected = self.library_view.selected_ids() if not reader else []
        self.act_close.setEnabled(reader)
        self.act_library.setEnabled(reader)
        self.act_save.setEnabled(has_result)
        self.act_translate.setEnabled((reader and job is None) or bool(selected))
        self.act_cancel.setEnabled(job is not None or (not reader and bool(self.search.text())))
        self.act_cancel_all.setEnabled(self._busy())
        self.act_find.setEnabled(not reader)
        for a in (self.act_open_paper, self.act_favorite, self.act_edit, self.act_lookup, self.act_bibtex, self.act_remove):
            a.setEnabled(bool(selected))
        self.act_open_paper.setEnabled(len(selected) == 1)
        self.act_edit.setEnabled(len(selected) == 1)
        self.act_new_collection.setEnabled(not reader)
        for a in (*self._view_actions, *self._zoom_actions, self.act_inspector):
            a.setEnabled(reader)
        for w in self._zoom_widgets:
            w.setVisible(reader)

        if job == "running":
            self.primary_btn.setText("翻訳中…")
            self.primary_btn.setEnabled(False)
            self.secondary_btn.setText("キャンセル")
            self.secondary_btn.setEnabled(not self.queue.is_cancelling())
            self.secondary_btn.setVisible(True)
        elif job == "queued":
            self.primary_btn.setText("待機中…")
            self.primary_btn.setEnabled(False)
            self.secondary_btn.setText("キャンセル")
            self.secondary_btn.setEnabled(True)
            self.secondary_btn.setVisible(True)
        elif has_result:
            self.primary_btn.setText("PDF出力")
            self.primary_btn.setEnabled(True)
            self.secondary_btn.setText("再翻訳")
            self.secondary_btn.setEnabled(True)
            self.secondary_btn.setVisible(True)
        else:
            self.primary_btn.setText("翻訳")
            self.primary_btn.setEnabled(reader)
            self.secondary_btn.setVisible(False)

        if reader and self.paper is not None:
            self.doc_title.setText(_elide(self.paper.display_title, 80))
            self.doc_title.setToolTip(plain_tooltip(self.paper.display_title))
            sub = [s for s in (self.paper.authors_short, f"{self.original_doc.page_count} ページ") if s]
            self.doc_subtitle.setText(" · ".join(sub))
            self.setWindowTitle(f"{self.paper.display_title} — {APP_NAME}")
        else:
            self.doc_title.setText("")
            self.doc_subtitle.setText("")
            self.setWindowTitle(APP_NAME)
        self._refresh_footer()

    def _refresh_footer(self) -> None:
        reader = self.screen_mode == READER and self.original_doc is not None
        current = self.queue.current()
        p = self.queue.last_progress
        waiting = len(self.queue.pending())
        text = ""
        show_progress = False
        if current is not None:
            show_progress = True
            pct = f"（{p.done * 100 // p.total}%）" if isinstance(p, Progress) and p.stage.value == "translate" and p.total else ""
            if self.queue.is_cancelling():
                text = "キャンセルしています…"
            elif p == DOWNLOAD_STAGE:
                text = "ダウンロードしています…"
            elif reader and self.paper is not None and current.paper_id == self.paper.id:
                text = f"{p.label}…{pct}" if isinstance(p, Progress) else "準備しています…"
            else:
                text = f"「{_elide(current.title, 36)}」を翻訳しています…{pct}"
            if waiting:
                text += f" · 残り {waiting} 件"
            if isinstance(p, Progress):
                self.progress.setValue(int(p.fraction * 1000))
            else:
                self.progress.setValue(0)
        elif self.lookups.is_active() and not reader:
            text = "書誌情報を取得しています…"
        downloads = self.sync.active_downloads()
        if downloads:
            # 翻訳と取得が同時に動くときは 1 行にまとめる
            text = f"{text}（ほかに {downloads} 件をダウンロード中）" if text else f"{downloads} 件のファイルを取得しています…"
        if not text:
            text = self._status_message
        self.status.setText(text)
        self.progress.setVisible(show_progress)
        self.queue_cancel_btn.setVisible(not reader and current is not None)
        self.footer.setVisible(reader or bool(text))

    def refresh_engine_status(self) -> None:
        engine = self.settings.engine
        info = registry.ENGINES.get(engine)
        has_key = (not info.needs_key) if info else False
        if info and info.needs_key:
            has_key = self.secrets.has(engine)
        self.inspector.set_engine_status(has_key)
        self.library_view.set_engine_notice("" if has_key else f"{info.name if info else engine} の API キーが設定されていません")

    def _set_status(self, text: str) -> None:
        self._status_message = text
        self._refresh_footer()

    # ======================================================================
    # ライブラリへの追加
    # ======================================================================

    def open_dialog(self) -> None:
        start = self.settings.last_open_dir or str(Path.home())
        files, _ = QFileDialog.getOpenFileNames(self, "論文を追加", start, "PDF (*.pdf)")
        if files:
            self.add_files([Path(f) for f in files])

    def open_file(self, path: Path) -> bool:
        """PDF をライブラリに追加して（既にあればそれを）読書画面で開く。"""
        papers = self.add_files([path], open_single=True)
        return bool(papers)

    def add_files(self, files: list[Path], open_single: bool = True) -> list[Paper]:
        """PDF（フォルダなら中の PDF すべて）をライブラリに追加する。1 本だけなら読書画面で開く。"""
        pdfs = _collect_pdfs(files)
        if not pdfs:
            self._alert("PDF が見つかりませんでした", "PDF ファイル、または PDF の入ったフォルダを選んでください。")
            return []
        added: list[Paper] = []
        existing: list[Paper] = []
        errors: list[str] = []
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for i, path in enumerate(pdfs):
                if len(pdfs) > 1:
                    self._set_status(f"ライブラリに追加しています…（{i + 1}/{len(pdfs)}）")
                    QApplication.processEvents()
                try:
                    paper, existed = self.library.add(path)
                except SpectraError as e:
                    if len(pdfs) == 1:
                        QGuiApplication.restoreOverrideCursor()
                        title, _, rest = e.user_message().partition("\n")
                        self._alert(title, rest)
                        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                    errors.append(path.name)
                    continue
                except OSError as e:
                    if len(pdfs) == 1:
                        QGuiApplication.restoreOverrideCursor()
                        self._alert("ファイルを読み込めませんでした", str(e))
                        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                    errors.append(path.name)
                    continue
                (existing if existed else added).append(paper)
        finally:
            QGuiApplication.restoreOverrideCursor()
            self._set_status("")
        self.settings.last_open_dir = str(pdfs[0].parent)
        self.settings.save()
        if len(pdfs) == 1 and errors:
            return []  # 理由はダイアログで知らせた

        for p in added:
            if not self.settings.metadata_lookup:
                self.library.set_meta_status(p.id, "local")
        self.library_view.reload()
        if self.settings.metadata_lookup and added:
            self.lookups.enqueue([p.id for p in added])

        papers = added + existing
        if open_single and len(papers) == 1 and not errors:
            self.open_paper(papers[0].id)
            if existing:
                self._show_banner("info", "この論文は既にライブラリにあります", "ライブラリにある論文を開きました。")
            else:
                self._notify_same_identifier(papers[0])
            return papers

        self._show_screen(LIBRARY)
        self.library_view.select_ids([p.id for p in papers])
        notes = []
        if existing:
            notes.append(f"{len(existing)} 件は既にライブラリにありました。")
        if errors:
            notes.append(f"{len(errors)} 件は開けませんでした（{', '.join(errors[:3])}{' など' if len(errors) > 3 else ''}）。")
        if added:
            self._show_banner("success" if not errors else "warning", f"{len(added)} 件の論文を追加しました", " ".join(notes))
        elif notes:
            self._show_banner("warning" if errors else "info", "新しく追加した論文はありません", " ".join(notes))
        return papers

    def _notify_same_identifier(self, paper: Paper) -> None:
        same = self.library.same_identifier(paper)
        if same:
            other = same[0]
            self._show_banner(
                "info",
                "同じ論文がライブラリに既にあるかもしれません",
                f"「{_elide(other.display_title, 60)}」と DOI（または arXiv ID）が同じです。",
            )

    # -- 書誌情報の照会 ------------------------------------------------------

    def enable_lookup(self) -> None:
        self.settings.metadata_lookup = True
        self.settings.save()
        self._lookup([p.id for p in self.library.papers() if p.meta_status in ("local", "pending")])

    def _lookup(self, ids: list[int], ask: bool = False) -> None:
        """ids の書誌情報を照会する。ask=True（利用者が取得し直しを選んだ）なら、照会がオフでもオンにして取得する。"""
        if not ids:
            return
        if not self.settings.metadata_lookup:
            if ask:
                self.enable_lookup()
            return
        for pid in ids:
            self.library.set_meta_status(pid, "pending")
        self.lookups.enqueue(ids)
        self._on_lookups_changed()

    def _on_lookup_updated(self, paper_id: int) -> None:
        self.library_view.refresh_paper(paper_id)
        if self.paper is not None and self.paper.id == paper_id:
            self.paper = self.library.get(paper_id) or self.paper
            self._update_state()

    def _on_lookups_changed(self) -> None:
        waiting = {p.id for p in self.library_view.model.papers() if self.lookups.is_waiting(p.id)}
        self.library_view.model.set_looking_up(waiting)
        self._refresh_detail()
        self._refresh_footer()

    # ======================================================================
    # ライブラリの操作
    # ======================================================================

    def _show_screen(self, mode: str) -> None:
        if mode != self.screen_mode:
            self.screen_mode = mode
            self._status_message = ""
            self.banner_wrap.hide()
        self._update_state()

    def show_library(self) -> None:
        current = self.paper.id if self.paper is not None else None
        self._release_document()
        self._show_screen(LIBRARY)
        if current is not None:
            self.library_view.reveal(current)
        self.library_view.table.setFocus()

    def focus_search(self) -> None:
        if self.screen_mode == READER:
            self.show_library()
        self.search.setFocus()
        self.search.selectAll()

    def _refresh_detail(self) -> None:
        papers = self.library_view.selected_papers()
        job = None
        if len(papers) == 1:
            job = self.queue.state(papers[0].id) or self.library_view.model.job(papers[0].id)
        self.library_view.detail.show_papers(
            papers,
            job=job,
            looking_up=len(papers) == 1 and self.lookups.is_waiting(papers[0].id),
            lookup_enabled=self.settings.metadata_lookup,
            download=self.sync.download_state(papers[0].id) if len(papers) == 1 else None,
            sync_on=self.sync.enabled(),
            sync_available=self.sync.available() or not self.sync.enabled(),
        )
        if self.screen_mode == LIBRARY:
            self._update_state()

    def _open_selected(self) -> None:
        ids = self.library_view.selected_ids()
        if len(ids) == 1:
            self.open_paper(ids[0])

    def _edit_selected(self) -> None:
        ids = self.library_view.selected_ids()
        if len(ids) == 1:
            self.edit_metadata(ids[0])

    def edit_metadata(self, paper_id: int) -> None:
        paper = self.library.get(paper_id)
        if paper is None:
            return
        dlg = MetadataDialog(paper, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        md, changed = dlg.result_metadata()
        if not changed:
            return
        self.library.update_metadata(paper_id, md, manual_fields=changed)
        self.library_view.refresh_paper(paper_id)
        if changed & {"doi", "arxiv_id"} and (md.doi or md.arxiv_id):
            self._lookup([paper_id])

    def copy_bibtex(self, ids: list[int]) -> None:
        from ..library.bibtex import to_bibtex

        papers = [p for p in (self.library.get(i) for i in ids) if p is not None]
        if not papers:
            return
        QApplication.clipboard().setText("\n\n".join(to_bibtex(p) for p in papers) + "\n")
        self._set_status(f"{len(papers)} 件の BibTeX をコピーしました" if len(papers) > 1 else "BibTeX をコピーしました")

    def remove_papers(self, ids: list[int]) -> None:
        """「ライブラリから削除」：ゴミ箱に入れる（30 日以内なら戻せる。同期が有効なら、すべてのパソコンから消える）。"""
        papers = [p for p in (self.library.get(i) for i in ids) if p is not None and p.trashed_at is None]
        if not papers:
            return
        running = [p for p in papers if self.queue.state(p.id) == "running"]
        if running:
            self._alert("翻訳中の論文は削除できません", "翻訳をキャンセルしてから、もう一度削除してください。")
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        if len(papers) == 1:
            box.setText(f"「{_elide(papers[0].display_title, 60)}」をライブラリから削除しますか？")
        else:
            box.setText(f"{len(papers)} 件の論文をライブラリから削除しますか？")
        everywhere = "すべてのパソコンから削除されます。" if self.sync.enabled() else ""
        box.setInformativeText(f"{everywhere}{TRASH_DAYS} 日以内ならゴミ箱から戻せます。元のファイルは残ります。")
        delete = box.addButton("削除", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        plain_box(box).exec()
        if box.clickedButton() is not delete:
            return
        for p in papers:
            self.queue.remove(p.id)
            self.sync.cancel_download(p.id)
            if self.paper is not None and self.paper.id == p.id:
                self._release_document()
            self.library.trash(p.id)
            self.library_view.refresh_paper(p.id)
        self._set_status(f"{len(papers)} 件の論文をゴミ箱に入れました")
        self._update_state()

    def restore_papers(self, ids: list[int]) -> None:
        for pid in ids:
            p = self.library.get(pid)
            if p is not None and p.trashed_at is not None:
                self.library.restore(pid)
                self.library_view.refresh_paper(pid)
        self._set_status(f"{len(ids)} 件の論文をゴミ箱から戻しました")
        self._update_state()

    def purge_papers(self, ids: list[int]) -> None:
        papers = [p for p in (self.library.get(i) for i in ids) if p is not None and p.trashed_at is not None]
        if not papers:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("完全に削除しますか？" if len(papers) == 1 else f"{len(papers)} 件の論文を完全に削除しますか？")
        everywhere = "すべてのパソコンから削除されます。" if self.sync.enabled() else ""
        box.setInformativeText(f"{everywhere}この操作は取り消せません。")
        delete = box.addButton("完全に削除", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        plain_box(box).exec()
        if box.clickedButton() is not delete:
            return
        for p in papers:
            self.library.purge(p.id)
            self.library_view.refresh_paper(p.id)
        self._update_state()

    def empty_trash(self) -> None:
        n = len(self.library.trashed_papers())
        if not n:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"ゴミ箱の {n} 件を完全に削除しますか？")
        everywhere = "すべてのパソコンから削除されます。" if self.sync.enabled() else ""
        box.setInformativeText(f"{everywhere}この操作は取り消せません。")
        delete = box.addButton("ゴミ箱を空にする", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        plain_box(box).exec()
        if box.clickedButton() is not delete:
            return
        self.library.empty_trash()
        self.library_view.reload()
        self._update_state()

    def _table_menu(self, pos) -> None:
        view = self.library_view
        index = view.table.indexAt(pos)
        if not index.isValid():
            return
        ids = view.selected_ids()
        if not ids:
            return
        papers = view.selected_papers()
        menu = QMenu(self)
        if view.in_trash():
            menu.addAction("ゴミ箱から戻す", lambda: self.restore_papers(ids))
            menu.addAction("完全に削除…", lambda: self.purge_papers(ids))
            menu.exec(view.table.viewport().mapToGlobal(pos))
            return
        if len(ids) == 1:
            menu.addAction("開く", lambda: self.open_paper(ids[0]))
        if any(self.queue.state(i) for i in ids):
            menu.addAction("翻訳をキャンセル", lambda: [self.queue.remove(i) for i in ids])
        else:
            menu.addAction("翻訳キューに追加" if len(ids) > 1 else "翻訳する", lambda: self.translate_papers(ids))
        menu.addSeparator()
        fav = all(p.favorite for p in papers)
        menu.addAction("お気に入りから外す" if fav else "お気に入りに追加", lambda: view.toggle_favorite(ids))
        menu.addMenu(view.collection_menu(ids))
        if view.proxy.scope()[0] == "collection":
            menu.addAction("このコレクションから外す", lambda: view.remove_from_current_collection(ids))
        menu.addSeparator()
        if len(ids) == 1:
            menu.addAction("書誌情報を編集…", lambda: self.edit_metadata(ids[0]))
        menu.addAction("書誌情報を取得し直す", lambda: self._lookup(ids, ask=True))
        menu.addAction("BibTeX をコピー", lambda: self.copy_bibtex(ids))
        if len(ids) == 1 and papers[0].has_original:  # 手元にない論文では出さない
            reveal = "Finder で表示" if theme.IS_MAC else "フォルダを開く"
            menu.addAction(reveal, lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(papers[0].pdf_path.parent))))
        if self.sync.enabled():
            menu.addSeparator()
            missing = [p.id for p in papers if self.sync.needs_download(p)]
            if missing:
                menu.addAction("このパソコンに保存", lambda: [self.sync.download(i, "keep") for i in missing])
            local = [p.id for p in papers if p.has_original and self.queue.state(p.id) is None]
            if local:
                menu.addAction("このパソコンのコピーを削除（クラウドには残す）", lambda: self.evict_papers(local))
        menu.addSeparator()
        menu.addAction("ライブラリから削除…", lambda: self.remove_papers(ids))
        menu.exec(view.table.viewport().mapToGlobal(pos))

    # ======================================================================
    # 読書画面
    # ======================================================================

    def open_paper(self, paper_id: int) -> bool:
        """読書画面で開く。原文が手元になければ、取得のキューに入れ、取得できたら開く（どの入口からでも同じ動き）。"""
        paper = self.library.get(paper_id)
        if paper is None or paper.trashed_at is not None:
            return False
        if not paper.has_original:
            if not self.sync.enabled():
                self._alert("この論文の PDF がこのパソコンにありません", "同期を有効にすると、ほかのパソコンから取得できます。")
                return False
            self.sync.download(paper_id, "open")
            if self.screen_mode == LIBRARY:
                self.library_view.reveal(paper_id)
            return False
        if paper.translation is not None and not paper.has_translation_file and self.sync.enabled():
            self.sync.download(paper_id, "keep")  # 原文は先に開き、訳文は届いたら表示する
        try:
            data = paper.pdf_path.read_bytes()
            doc = open_document(data)
        except SpectraError as e:
            title, _, rest = e.user_message().partition("\n")
            self._alert(title, rest)
            return False
        except OSError as e:
            self._alert("ライブラリの PDF を読み込めませんでした", str(e))
            return False
        self._release_document()
        self.paper, self.source_bytes, self.original_doc = paper, data, doc
        self.inspector.set_page_count(doc.page_count)
        self.preview.original.set_document(doc)
        mode = "original"
        if self._load_saved_translation(paper):
            mode = "both"
        self.view_mode.set_current(mode)
        self.preview.set_mode(mode)
        self.library.touch_opened(paper.id)
        self._show_screen(READER)
        self._start_estimate()
        return True

    def _load_saved_translation(self, paper: Paper) -> bool:
        t = paper.translation
        if t is None or not paper.translated_path.exists():
            return False
        try:
            pdf = paper.translated_path.read_bytes()
            doc = pymupdf.open(stream=pdf, filetype="pdf")
        except Exception:
            log.warning("saved translation is unreadable: %s", paper.translated_path, exc_info=True)
            return False
        self._set_translation(pdf, doc, (t.first_page, t.last_page))
        self.inspector.set_page_range(t.first_page, t.last_page)
        return True

    def _set_translation(self, pdf: bytes, doc: pymupdf.Document, page_range: tuple[int, int | None]) -> None:
        self.translated_pdf = pdf
        self.translated_doc = doc
        self.translated_range = page_range
        self.result_stale = False
        self.preview.translated.set_document(doc)

    def _release_document(self) -> None:
        self.preview.original.set_document(None)
        self.preview.translated.set_document(None)
        self.original_doc = self.translated_doc = None
        self.paper = None
        self.source_bytes = None
        self.translated_pdf = None
        self.result = None
        self.result_stale = False
        self.inspector.set_estimate(None)

    def close_document(self) -> None:
        if self.screen_mode == READER:
            self.show_library()

    # -- 見積もり -----------------------------------------------------------

    def _start_estimate(self) -> None:
        if self.source_bytes is None:
            return
        self._estimate_gen += 1
        gen = self._estimate_gen
        self.inspector.set_estimating()
        first, last = self.inspector.page_range()
        translator = registry.create(
            self.settings.engine, api_key="", options=self.settings.translate_options(), model=self.settings.gemini_model
        )
        job = TranslationJob(
            self.source_bytes,
            translator,
            cache=self.cache if self.settings.use_cache else None,
            analysis_options=self.settings.analysis_options(first, last),
            render_options=self.settings.render_options(),
        )

        def done(est):
            if gen == self._estimate_gen:
                self.inspector.set_estimate(est)

        def failed(err):
            if gen == self._estimate_gen:
                self.inspector.set_estimate(None)
                log.warning("estimate failed: %s", err)

        from . import worker

        worker.start(lambda _r, _c: job.estimate(), done, failed)

    def _on_settings_changed(self, reanalyze: bool) -> None:
        if reanalyze:
            self._estimate_timer.start()
            self.refresh_engine_status()
        if self.translated_pdf is not None and reanalyze:
            self.result_stale = True
            self._set_status("設定が変更されました。もう一度翻訳すると反映されます。")
        self._update_state()

    # ======================================================================
    # 翻訳
    # ======================================================================

    def _on_primary(self) -> None:
        if self.translated_pdf is not None and not self.result_stale:
            self.save_dialog()
        else:
            self.start_translation()

    def _on_secondary(self) -> None:
        if self._paper_job() is not None:
            self.cancel_translation()
        else:
            self.start_translation()

    def _api_key(self) -> str | None:
        """翻訳に使う API キー。未設定なら案内を出して None を返す。"""
        engine = self.settings.engine
        info = registry.ENGINES.get(engine)
        if not (info and info.needs_key):
            return ""
        key = self.secrets.get(engine)
        if key:
            return key
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"{info.name} の API キーを設定してください")
        box.setInformativeText(f"{info.free_quota}で使えます。キーは設定画面から入力できます。")
        open_btn = box.addButton("設定を開く", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        plain_box(box).exec()
        if box.clickedButton() is open_btn:
            self.show_settings("engines")
        return None

    def _request(self, paper: Paper, key: str, first: int = 0, last: int | None = None, allow_partial: bool = False) -> TranslationRequest:
        s = self.settings
        return TranslationRequest(
            paper_id=paper.id,
            title=paper.display_title,
            engine=s.engine,
            model=s.gemini_model,
            api_key=key,
            auto_fallback=s.auto_fallback,
            translate_options=s.translate_options(),
            analysis_options=s.analysis_options(first, last),
            render_options=s.render_options(),
            verify=s.verify_layout,
            cache=self.cache if s.use_cache else None,
            allow_partial=allow_partial,
            options={
                "formality": s.formality,
                "captions": s.translate_captions,
                "footnotes": s.translate_footnotes,
                "references": not s.skip_references,
                "font": s.font,
            },
        )

    def start_translation(self, allow_partial: bool = False) -> None:
        """読書画面では開いている論文を、ライブラリでは選んだ論文を翻訳する。"""
        if self.screen_mode != READER or self.paper is None:
            self.translate_papers(self.library_view.selected_ids())
            return
        if self._paper_job() is not None:
            return
        key = self._api_key()
        if key is None:
            return
        first, last = self.inspector.page_range()
        self.banner_wrap.hide()
        self._status_message = ""
        self.queue.enqueue(self._request(self.paper, key, first, last, allow_partial))
        self._update_state()

    def translate_papers(self, ids: list[int]) -> None:
        papers = [p for p in (self.library.get(i) for i in ids) if p is not None and self.queue.state(p.id) is None]
        if not papers:
            return
        key = self._api_key()
        if key is None:
            return
        for p in papers:
            self.queue.enqueue(self._request(p, key))
        if len(papers) > 1:
            self._show_banner("info", f"{len(papers)} 件の論文を翻訳キューに追加しました", "1 本ずつ順に翻訳します。翻訳中もライブラリや論文を読めます。")

    def _run_translation(self, req: TranslationRequest, report, cancel) -> JobResult:
        """（バックグラウンドのスレッドで）1 本の論文を翻訳し、訳文をライブラリに保存する。"""
        paper = self.library.get(req.paper_id)
        if paper is None or paper.trashed_at is not None:
            raise CancelledError
        if not paper.has_original:
            # 手元にない論文は、翻訳の前に同期先から取得する
            report(DOWNLOAD_STAGE)
            try:
                self.sync.fetch_blocking(req.paper_id, cancel)
            except PaperNotFound:
                raise CancelledError from None
            paper = self.library.get(req.paper_id)
            if paper is None:
                raise CancelledError
        translator = registry.create(
            req.engine, api_key=req.api_key, options=req.translate_options, model=req.model, auto_fallback=req.auto_fallback
        )
        translator.cancel_event = cancel
        job = TranslationJob(
            paper.pdf_path,
            translator,
            cache=req.cache,
            analysis_options=req.analysis_options,
            render_options=req.render_options,
            progress=report,
            cancel_event=cancel,
            verify=req.verify,
        )
        try:
            result = job.run(allow_partial=req.allow_partial)
        finally:
            translator.close()
        if cancel.is_set():
            raise CancelledError
        a = req.analysis_options
        info = Translation(
            paper_id=req.paper_id,
            model=req.model if req.engine == "gemini" else req.engine,
            formality=req.options.get("formality", ""),
            first_page=a.first_page,
            last_page=a.last_page,
            options=req.options,
            segments=result.segments,
            untranslated=result.untranslated,
            chars=result.translated_chars,
            created_at=time.time(),
        )
        try:
            self.library.save_translation(req.paper_id, result.translated_pdf, info)
        except PaperNotFound:
            raise CancelledError from None  # 翻訳中に別のパソコンで削除された
        return result

    def cancel_translation(self) -> None:
        if self.screen_mode == READER and self.paper is not None and self._paper_job() is not None:
            self.queue.remove(self.paper.id)
            self._update_state()
        elif self.screen_mode == LIBRARY and self.search.text():
            self.search.clear()

    def cancel_all_translations(self) -> None:
        dropped = self.queue.clear_pending()
        self.queue.cancel_all()
        for r in dropped:
            self.library_view.model.set_job(r.paper_id, None)
        self._update_state()

    def _on_queue_changed(self) -> None:
        model = self.library_view.model
        now = {r.paper_id for r in self.queue.pending()}
        current = self.queue.current()
        if current is not None:
            now.add(current.paper_id)
        for pid in self._job_ids - now:
            if model.job(pid) in ("running", "queued"):
                model.set_job(pid, None)
        for r in self.queue.pending():
            model.set_job(r.paper_id, "queued")
        if current is not None and model.job(current.paper_id) != "running":
            model.set_job(current.paper_id, "running")
        self._job_ids = now
        self._refresh_detail()
        self._update_state()

    def _on_job_progress(self, paper_id: int, p: Progress) -> None:
        percent = p.done * 100 // p.total if p.stage.value == "translate" and p.total else None
        self.library_view.model.set_job(paper_id, "running", percent)
        self._refresh_footer()

    def _on_job_done(self, req: TranslationRequest, result: JobResult) -> None:
        self.library_view.model.set_job(req.paper_id, None)
        self.library_view.refresh_paper(req.paper_id)
        minutes, seconds = divmod(int(result.elapsed), 60)
        took = f"{minutes} 分 {seconds} 秒" if minutes else f"{seconds} 秒"
        summary = f"{result.segments} 段落 · {result.translated_chars:,} 文字 · {took}"
        if self.screen_mode == READER and self.paper is not None and self.paper.id == req.paper_id:
            self.paper = self.library.get(req.paper_id) or self.paper
            self.result = result
            doc = pymupdf.open(stream=result.translated_pdf, filetype="pdf")
            a = req.analysis_options
            self._set_translation(result.translated_pdf, doc, (a.first_page, a.last_page))
            self.view_mode.set_current("both")
            self.preview.set_mode("both")
            self._status_message = summary
            if result.warnings:
                self._show_banner("warning", "翻訳が完了しました", "\n".join(result.warnings), "PDF出力", self.save_dialog)
            else:
                self._show_banner(
                    "success",
                    "翻訳が完了しました",
                    "図表とレイアウトが元のままであることを確認しました。" if result.qa.pages_checked else "",
                    "PDF出力",
                    self.save_dialog,
                )
            self._start_estimate()
        else:
            body = "\n".join(result.warnings) if result.warnings else summary
            self._show_banner(
                "warning" if result.warnings else "success",
                f"「{_elide(req.title, 48)}」の翻訳が完了しました",
                body,
                "読む",
                lambda pid=req.paper_id: self.open_paper(pid),
            )
        self._update_state()
        self.refresh_engine_status()

    def _on_job_failed(self, req: TranslationRequest, err: BaseException) -> None:
        model = self.library_view.model
        here = self.screen_mode == READER and self.paper is not None and self.paper.id == req.paper_id
        if isinstance(err, CancelledError):
            model.set_job(req.paper_id, None)
            self._set_status("キャンセルしました。翻訳済みの段落はキャッシュに保存されています。")
            self._update_state()
            return
        # 訳せた段落があると、原因によらず PartialTranslationError に包まれるので、元の原因で判断する
        cause = err.cause if isinstance(err, PartialTranslationError) else err
        quota = isinstance(cause, QuotaExceededError)
        # 1 日の上限・API キーの問題・接続や混雑は、待っている論文でも同じように失敗するので取りやめる
        # （失敗のダイアログが論文の数だけ続かないように）
        stop_all = isinstance(cause, (QuotaExceededError, MissingKeyError, AuthenticationError)) or getattr(cause, "retryable", False)
        dropped = self.queue.clear_pending() if stop_all else []
        for r in dropped:
            model.set_job(r.paper_id, None)
        dropped_note = f"\n待機中だった {len(dropped)} 件の翻訳は取りやめました。" if dropped else ""
        if isinstance(err, PartialTranslationError):
            model.set_job(req.paper_id, None)
            self._set_status("")
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(err.title)
            box.setInformativeText(
                f"「{_elide(req.title, 48)}」は {len(err.results)} 個の段落を翻訳できました。翻訳できた段落だけで PDF を作成しますか？\n"
                + (
                    "残りの段落は原文のまま残り、上限が戻った後（明日以降）にキャッシュから続きを翻訳できます。"
                    if quota
                    else "残りの段落は原文のまま残り、あとで翻訳し直すとキャッシュから続きを翻訳できます。"
                )
                + dropped_note
            )
            make = box.addButton("翻訳できた分で作成", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
            plain_box(box).exec()
            if box.clickedButton() is make:
                req.allow_partial = True
                self.queue.enqueue(req, front=True)
            self.refresh_engine_status()
            self._update_state()
            return
        if isinstance(err, SpectraError):
            model.set_job(req.paper_id, None if quota else "failed")
            title, _, rest = err.user_message().partition("\n")
            self._set_status(title)
            if isinstance(err, (MissingKeyError, AuthenticationError)):
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setText(title)
                box.setInformativeText(rest + dropped_note)
                open_btn = box.addButton("設定を開く", QMessageBox.ButtonRole.AcceptRole)
                box.addButton("閉じる", QMessageBox.ButtonRole.RejectRole)
                plain_box(box).exec()
                if box.clickedButton() is open_btn:
                    self.show_settings("engines")
            else:
                prefix = "" if here else f"「{_elide(req.title, 48)}」を翻訳できませんでした。\n"
                self._alert(title, prefix + rest + dropped_note)
            self._update_state()
            return
        model.set_job(req.paper_id, "failed")
        log.exception("unexpected error", exc_info=err)
        self._set_status("予期しないエラーが発生しました")
        self._alert("予期しないエラーが発生しました", f"{type(err).__name__}: {err}\n\n詳細はログフォルダを確認してください。")
        self._update_state()

    # -- 保存 --------------------------------------------------------------

    def save_dialog(self) -> None:
        """PDF出力：形式を選んでから保存先を決める。"""
        if self.translated_pdf is None or self.paper is None or self.original_doc is None:
            return
        pages = len(self._output_pages())
        dlg = ExportDialog(OutputMode(self.settings.output_mode), pages, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        mode = dlg.selected()
        self.settings.output_mode = mode.value
        self.settings.save()
        source = Path(self.paper.source_path) if self.paper.source_path else Path.home()
        directory = Path(self.settings.last_save_dir) if self.settings.last_save_dir else source.parent
        suggested = directory / f"{Path(self.paper.file_name).stem}{suffix_for(mode)}.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "PDF出力", str(suggested), "PDF (*.pdf)")
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() != ".pdf":
            target = target.with_suffix(".pdf")
        try:
            target.write_bytes(self._output_bytes(mode))
        except OSError as e:
            self._alert("PDF を出力できませんでした", str(e))
            return
        self.settings.last_save_dir = str(target.parent)
        self.settings.save()
        folder = "Finder で表示" if theme.IS_MAC else "フォルダを開く"
        self._show_banner(
            "success", "PDF を出力しました", str(target), folder, lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent)))
        )

    def _output_pages(self) -> range:
        first, last = self.translated_range
        count = self.original_doc.page_count if self.original_doc is not None else 0
        return range(first, (last if last is not None else count - 1) + 1)

    def _output_bytes(self, mode: OutputMode | None = None) -> bytes:
        """指定した形式の PDF。翻訳結果から作り直すだけなので再翻訳は不要。"""
        assert self.translated_pdf is not None and self.source_bytes is not None
        mode = mode or OutputMode(self.settings.output_mode)
        if mode == OutputMode.TRANSLATED:
            return self.translated_pdf
        original = pymupdf.open(stream=self.source_bytes, filetype="pdf")
        translated = pymupdf.open(stream=self.translated_pdf, filetype="pdf")
        return finalize(build_output(original, translated, mode, self._output_pages()))

    # ======================================================================
    # その他
    # ======================================================================

    def show_settings(self, tab: str = "engines") -> None:
        dlg = SettingsDialog(self.settings, self.secrets, self.cache, self, sync=self.sync)
        dlg.syncChanged.connect(self._on_sync_setting)
        dlg.restoreBackupRequested.connect(self.restore_backup)
        dlg.keysChanged.connect(self.refresh_engine_status)
        dlg.settingsChanged.connect(lambda: self._on_settings_changed(True))
        dlg.appearanceChanged.connect(self.apply_appearance)
        dlg.lookupChanged.connect(self._on_lookup_setting)
        dlg.show_tab(tab)
        dlg.exec()
        self.refresh_engine_status()

    # ======================================================================
    # 同期
    # ======================================================================

    def _on_sync_setting(self) -> None:
        was = self.sync.enabled()
        self.sync.configure()
        if self.sync.enabled() and not was:
            self._first_sync_count = self.library_view.active_count()
        self._refresh_sync_status()
        self._refresh_detail()

    def restore_backup(self, path) -> bool:
        """同期を始める前のライブラリに戻す（同期はオフになる）。"""
        from ..library.store import restore_backup

        if self._busy():
            self._alert("翻訳中は戻せません", "翻訳が終わるか、キャンセルしてから、もう一度お試しください。")
            return False
        for pid in list(self.sync.downloads):
            self.sync.cancel_download(pid)
        self._release_document()
        self._show_screen(LIBRARY)
        root = self.library.root
        self.settings.sync_enabled = False
        self.settings.save()
        self.sync.close()
        self.library.close()
        try:
            restore_backup(path, root)
        except SpectraError as e:
            title, _, rest = e.user_message().partition("\n")
            self._alert(title, rest)
        self.library = Library(root)
        self.library_view.library = self.library
        self.lookups.library = self.library
        self.sync.rebind(self.library)
        self.library_view.reload()
        self._refresh_sync_status()
        self._update_state()
        self._show_banner("info", "同期を始める前のライブラリに戻しました", "同期はオフになっています。")
        return True

    def _refresh_sync_status(self) -> None:
        """サイドバーの下の 1 行（同期がオフで、保存先もなければ出さない）。"""
        s = self.sync
        downloads = s.active_downloads()
        if not self.settings.sync_folder:
            icon, text, warn = "", "", False
        elif not s.enabled():
            icon, text, warn = "pause", "同期を一時停止中", False
        elif downloads:
            icon, text, warn = "cloud_download", f"取得中… {downloads} 件", False
        elif s.state == SYNCING:
            icon, text, warn = "sync", "同期中…", False
        elif s.state in (ERROR, UNAVAILABLE):
            icon, text, warn = "warning", "同期できませんでした", True
        else:
            # Spectra に分かるのは「同期フォルダに書いた」ところまでなので、「同期済み」ではなく「保存済み」
            from .sync_settings import format_ago

            icon, text, warn = "check", f"保存済み · {format_ago(s.last_saved)}", False
        self.library_view.set_sync_status(icon, text, warn)
        self._refresh_footer()

    def show_sync_popover(self) -> None:
        from .sync_popover import SyncPopover

        pop = SyncPopover(self.settings, self.sync, self)
        pop.settingsRequested.connect(lambda: self.show_settings("sync"))
        pop.downloadAllRequested.connect(self._download_all)
        btn = self.library_view.sync_status
        pop.show_below(btn)

    def _download_all(self) -> None:
        n = self.sync.download_all()
        self._set_status(f"{n} 件の論文をこのパソコンに取得します" if n else "すべての論文がこのパソコンにあります")

    def evict_papers(self, ids: list[int]) -> None:
        def done():
            for pid in ids:
                self.library_view.refresh_paper(pid)
            self._set_status(f"{len(ids)} 件の論文のコピーを、このパソコンから削除しました（クラウドには残っています）")

        def failed(err):
            for pid in ids:
                self.library_view.refresh_paper(pid)
            if isinstance(err, RemoteMissing):
                self._alert(
                    "まだ同期先に保存されていないため、このパソコンのコピーを削除できません",
                    "同期先への保存が終わってから、もう一度お試しください。",
                )
            else:
                title, _, rest = err.user_message().partition("\n") if isinstance(err, SpectraError) else (str(err), "", "")
                self._alert(title, rest)

        self.sync.evict(ids, done, failed)

    def _on_synced(self, report) -> None:
        if self._first_sync_count is not None and not getattr(report, "errors", None):
            before, self._first_sync_count = self._first_sync_count, None
            self.library_view.reload()
            total = self.library_view.active_count()
            if total > before:
                body = "論文を開くと、ファイルをダウンロードします。"
                if before:
                    body = f"このパソコンの {before} 件と合わせて {total} 件になりました。" + body
                self._show_banner("info", f"ライブラリを同期しました（論文 {total} 件）", body)
        full = getattr(report, "disk_full", 0)
        if full:
            self._show_banner(
                "warning",
                f"このパソコンの空き容量が足りないため、{full} 件の論文を保存できませんでした",
                "空き容量を増やすと、次の同期で自動でやり直します。",
            )
        if getattr(report, "read_only", False):
            self._show_banner(
                "warning",
                "別のパソコンで新しい Spectra が使われています",
                "このパソコンの Spectra を更新してください。それまでは、ほかのパソコンの変更を読むだけにします。",
            )

    def _on_remote_changes(self, changes) -> None:
        """ほかのパソコンの変更を反映した（同期のワーカーから、Qt のシグナルで届く）。"""
        if any(c.kind == "collections" for c in changes) or len(changes) > 50:
            self.library_view.reload()
        for c in changes:
            pid = c.paper_id
            if pid is None and c.sha256:
                found = self.library.find_by_sha(c.sha256)
                pid = found.id if found else None
            if pid is None:
                continue
            here = self.screen_mode == READER and self.paper is not None and self.paper.id == pid
            if c.kind == "removed":
                self.queue.remove(pid)  # 翻訳中なら、ワーカーが PaperNotFound で静かに終わる
                self.sync.cancel_download(pid)
                if here:
                    self.show_library()
                    self._show_banner("info", "この論文は別のパソコンで削除されました")
            elif c.kind == "translation" and here:
                self._show_banner(
                    "info", "別のパソコンで新しい訳文が作られました", "", "開き直す", lambda p=pid: self.open_paper(p)
                )
            elif c.kind == "metadata" and here:
                self.paper = self.library.get(pid) or self.paper
                self._update_state()
            if c.kind != "collections":
                self.library_view.refresh_paper(pid)
        self._refresh_detail()

    def _on_download_changed(self, paper_id: int) -> None:
        self.library_view.model.set_download(paper_id, self.sync.download_state(paper_id))
        ids = self.library_view.selected_ids()
        if ids == [paper_id]:
            self._refresh_detail()
        self._refresh_sync_status()

    def _on_download_finished(self, paper_id: int, purpose: str) -> None:
        self.library_view.refresh_paper(paper_id)
        here = self.screen_mode == READER and self.paper is not None and self.paper.id == paper_id
        if purpose == "open" and not here:
            self.open_paper(paper_id)
        elif here and self.translated_pdf is None:
            # 開いている論文の訳文が届いた
            paper = self.library.get(paper_id)
            if paper is not None and self._load_saved_translation(paper):
                self.paper = paper
                self.view_mode.set_current("both")
                self.preview.set_mode("both")
                self._update_state()

    def _on_download_failed(self, paper_id: int, err: BaseException) -> None:
        # 理由は行の記号と詳細パネルに出る（再試行もそこから）。開こうとしていた論文なら、その論文を選んで見せる
        if self.screen_mode == LIBRARY:
            self.library_view.reveal(paper_id)
        if isinstance(err, RemoteMissing):
            title = "ほかのパソコンからファイルが届くのを待っています"
        else:
            title = getattr(err, "title", "") or "ファイルを取得できませんでした"
        self._set_status(title)

    def _on_lookup_setting(self, on: bool) -> None:
        if on:
            self._lookup([p.id for p in self.library.papers() if p.meta_status in ("local", "pending")])
        self._refresh_detail()

    def show_about(self) -> None:
        AboutDialog(self).exec()

    def _show_banner(self, kind: str, title: str, body: str = "", action: str = "", callback: Callable[[], None] | None = None) -> None:
        self._banner_callback = callback
        self.banner.show_message(kind, title, body, action if callback else "")
        self.banner_wrap.show()

    def _on_banner_action(self) -> None:
        cb = self._banner_callback
        if cb is not None:
            self.banner_wrap.hide()
            cb()

    def _alert(self, title: str, body: str = "") -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(title)
        box.setInformativeText(body)
        plain_box(box).exec()

    def _zoom_by(self, factor: float) -> None:
        view = self.preview.active_view()
        view.set_zoom(view.zoom() * factor)

    def _on_zoom_changed(self, zoom: float) -> None:
        self.zoom_label.setText(f"{round(zoom * 100)}%")

    def _on_page_changed(self, index: int, total: int) -> None:
        self.page_label.setText(f"{index + 1} / {total} ページ" if total else "")

    def apply_appearance(self) -> None:
        theme.set_appearance(self.settings.appearance)
        theme.apply(QApplication.instance())
        self.refresh_theme()

    def refresh_theme(self) -> None:
        for b in (
            self.sidebar_btn,
            self.back_btn,
            self.settings_btn,
            self.inspector_btn,
            self.zoom_in_btn,
            self.zoom_out_btn,
            self.fit_btn,
            self.banner._close,
        ):
            refresh_icon(b)
        self._search_icon.setIcon(icons.icon("search", theme.current().secondary, 16))
        self.library_view.refresh_theme()
        self.refresh_engine_status()
        for view in (self.preview.original, self.preview.translated):
            view.viewport().update()

    # -- ドラッグ＆ドロップ ------------------------------------------------------

    @staticmethod
    def _paths_from_event(event) -> list[Path]:
        md = event.mimeData()
        out = []
        if md.hasUrls():
            for url in md.urls():
                if not url.isLocalFile():
                    continue
                p = Path(url.toLocalFile())
                if p.is_dir() or p.suffix.lower() == ".pdf":
                    out.append(p)
        return out

    def dragEnterEvent(self, event) -> None:
        if self._paths_from_event(event):
            event.acceptProposedAction()
            self.library_view.set_drag_active(True)

    def dragLeaveEvent(self, event) -> None:
        self.library_view.set_drag_active(False)

    def dropEvent(self, event) -> None:
        self.library_view.set_drag_active(False)
        paths = self._paths_from_event(event)
        if paths:
            event.acceptProposedAction()
            # 読書画面へのドロップも、ライブラリ画面へのドロップも同じく「追加」。1 本なら開く
            self.add_files(paths)

    # -- ウィンドウ ---------------------------------------------------------

    def _restore_window(self) -> None:
        geo = self.settings.window.get("geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        else:
            self.resize(1320, 860)
        self.inspector_btn.setChecked(self.settings.window.get("inspector", True))
        self.sidebar_btn.setChecked(self.settings.window.get("sidebar", True))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.library_view.paper_count() == 0:
            self.library_view.empty_add_btn.setFocus()
        else:
            self.library_view.table.setFocus()

    def closeEvent(self, event) -> None:
        if self._busy():
            waiting = len(self.queue.pending())
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setText("翻訳を中止して終了しますか？")
            note = f"待機中の {waiting} 件の翻訳は破棄されます。" if waiting else ""
            box.setInformativeText(note + "翻訳済みの段落はキャッシュに保存されているため、次回は続きから翻訳できます。")
            quit_btn = box.addButton("終了", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
            plain_box(box).exec()
            if box.clickedButton() is not quit_btn:
                event.ignore()
                return
            self.queue.cancel_all()
        self.sync.close()
        self.settings.window = {
            "geometry": bytes(self.saveGeometry().toBase64()).decode(),
            "inspector": self.inspector_btn.isChecked(),
            "sidebar": self.sidebar_btn.isChecked(),
        }
        self.settings.save()
        super().closeEvent(event)


def _elide(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _collect_pdfs(paths: list[Path]) -> list[Path]:
    """ファイルはそのまま、フォルダは中の PDF をすべて（下のフォルダも含めて）集める。"""
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        if p.is_dir():
            found = sorted(q for q in p.rglob("*") if q.suffix.lower() == ".pdf" and q.is_file() and not q.name.startswith("."))
        elif p.suffix.lower() == ".pdf":
            found = [p]
        else:
            found = []
        for q in found:
            r = q.resolve()
            if r not in seen:
                seen.add(r)
                out.append(q)
    return out
