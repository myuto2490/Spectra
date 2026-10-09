"""メインウィンドウ。

PDF を開く → 翻訳 → 保存 の 3 ステップを中心に、
ツールバー（主操作）・プレビュー（主役）・インスペクタ（詳細設定）の 3 層で構成する。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf
from PySide6.QtCore import QByteArray, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
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
from ..translate import registry
from ..translate.cache import TranslationCache
from ..translate.errors import AuthenticationError, CancelledError, MissingKeyError
from ..translate.service import PartialTranslationError
from . import theme, worker
from .about import AboutDialog
from .drop_zone import DropZone
from .inspector import Inspector
from .pdf_view import PreviewPair
from .settings_dialog import SettingsDialog
from .widgets import Banner, SegmentedControl, button, label, refresh_icon, tool_button

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/myuto2490/Spectra"


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None, secrets: SecretStore | None = None):
        super().__init__()
        self.settings = settings or Settings.load()
        self.secrets = secrets or SecretStore()
        self.cache = TranslationCache(paths.translation_cache_file())
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(QSize(980, 640))
        self.setAcceptDrops(True)
        self.setUnifiedTitleAndToolBarOnMac(True)

        self.source_path: Path | None = None
        self.source_bytes: bytes | None = None
        self.original_doc: pymupdf.Document | None = None
        self.translated_doc: pymupdf.Document | None = None
        self.result: JobResult | None = None
        self.result_stale = False
        self._job_task: worker.Task | None = None
        self._estimate_gen = 0

        self._build_ui()
        self._build_actions()
        self._restore_window()
        self._estimate_timer = QTimer(self, singleShot=True, interval=350, timeout=self._start_estimate)
        self.refresh_engine_status()
        self._update_state()

    # ======================================================================
    # 画面構成
    # ======================================================================

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Window")
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_toolbar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.stack = QStackedWidget()
        self.drop_zone = DropZone()
        self.drop_zone.openRequested.connect(self.open_dialog)
        self.drop_zone.settingsRequested.connect(lambda: self.show_settings("engines"))
        self.stack.addWidget(self.drop_zone)

        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        banner_wrap = QWidget()
        bl = QVBoxLayout(banner_wrap)
        bl.setContentsMargins(16, 12, 16, 0)
        self.banner = Banner()
        self.banner.action.connect(self._on_banner_action)
        bl.addWidget(self.banner)
        self.banner_wrap = banner_wrap
        banner_wrap.hide()
        self.banner.closed.connect(banner_wrap.hide)
        cl.addWidget(banner_wrap)
        self.preview = PreviewPair()
        self.preview.original.pageChanged.connect(self._on_page_changed)
        self.preview.translated.pageChanged.connect(self._on_page_changed)
        self.preview.original.zoomChanged.connect(self._on_zoom_changed)
        self.preview.translated.zoomChanged.connect(self._on_zoom_changed)
        cl.addWidget(self.preview, 1)
        self.stack.addWidget(content)
        self.content = content
        body.addWidget(self.stack, 1)

        self.inspector = Inspector(self.settings)
        self.inspector.settingsChanged.connect(self._on_settings_changed)
        self.inspector.openSettings.connect(self.show_settings)
        body.addWidget(self.inspector)
        v.addLayout(body, 1)
        self.footer = self._build_footer()
        v.addWidget(self.footer)
        self.setCentralWidget(root)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Toolbar")
        bar.setFixedHeight(56)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 8, 14, 8)
        h.setSpacing(8)
        self.open_btn = tool_button("folder", "PDF を開く（⌘O）" if theme.IS_MAC else "PDF を開く（Ctrl+O）")
        self.open_btn.clicked.connect(self.open_dialog)
        h.addWidget(self.open_btn)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.doc_title = label(APP_NAME, "headline")
        self.doc_subtitle = label("論文 PDF を日本語に翻訳", "caption")
        titles.addWidget(self.doc_title)
        titles.addWidget(self.doc_subtitle)
        h.addSpacing(4)
        h.addLayout(titles)
        h.addStretch(1)

        self.view_mode = SegmentedControl([("original", "原文"), ("both", "対訳"), ("translated", "翻訳")])
        self.view_mode.setAccessibleName("表示")
        h.addWidget(self.view_mode)
        h.addSpacing(8)

        self.secondary_btn = button("")
        self.secondary_btn.clicked.connect(self._on_secondary)
        h.addWidget(self.secondary_btn)
        self.primary_btn = button("翻訳", primary=True)
        self.primary_btn.setMinimumWidth(96)
        self.primary_btn.clicked.connect(self._on_primary)
        h.addWidget(self.primary_btn)
        h.addSpacing(4)
        self.settings_btn = tool_button("gear", "設定")
        self.settings_btn.clicked.connect(lambda: self.show_settings())
        h.addWidget(self.settings_btn)
        self.inspector_btn = tool_button("sidebar", "インスペクタを表示/隠す", checkable=True)
        self.inspector_btn.setChecked(True)
        self.inspector_btn.toggled.connect(lambda on: self.inspector.setVisible(on))
        h.addWidget(self.inspector_btn)
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
        for w in (self.zoom_out_btn, self.zoom_label, self.zoom_in_btn, self.fit_btn):
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
        self.act_open = act("開く…", self.open_dialog, QKeySequence.StandardKey.Open)
        self.act_save = act("保存…", self.save_dialog, QKeySequence.StandardKey.Save)
        self.act_close = act("閉じる", self.close_document, QKeySequence.StandardKey.Close)
        file_menu.addActions([self.act_open, self.act_save])
        file_menu.addSeparator()
        file_menu.addAction(self.act_close)
        file_menu.addSeparator()
        file_menu.addAction(act("設定…", lambda: self.show_settings(), QKeySequence.StandardKey.Preferences, QAction.MenuRole.PreferencesRole))
        file_menu.addAction(act("終了", self.close, QKeySequence.StandardKey.Quit, QAction.MenuRole.QuitRole))

        tr = mb.addMenu("翻訳")
        self.act_translate = act("翻訳を開始", self.start_translation, f"{mod}+Return")
        self.act_cancel = act("キャンセル", self.cancel_translation, "Escape")
        tr.addActions([self.act_translate, self.act_cancel])

        view = mb.addMenu("表示")
        for key, text, sc in (("original", "原文", f"{mod}+1"), ("both", "対訳", f"{mod}+2"), ("translated", "翻訳", f"{mod}+3")):
            view.addAction(act(text, lambda _c=False, k=key: self.view_mode.set_current(k), sc))
        view.addSeparator()
        view.addAction(act("拡大", lambda: self._zoom_by(1.2), QKeySequence.StandardKey.ZoomIn))
        view.addAction(act("縮小", lambda: self._zoom_by(1 / 1.2), QKeySequence.StandardKey.ZoomOut))
        view.addAction(act("幅に合わせる", self.preview.fit_width, f"{mod}+0"))
        view.addSeparator()
        view.addAction(act("インスペクタ", self.inspector_btn.toggle, f"{mod}+Alt+I"))

        help_menu = mb.addMenu("ヘルプ")
        help_menu.addAction(act(f"{APP_NAME} について", self.show_about, role=QAction.MenuRole.AboutRole))
        help_menu.addAction(act("使い方（GitHub）", lambda: QDesktopServices.openUrl(QUrl(f"{REPO_URL}#readme"))))
        help_menu.addAction(act("問題を報告…", lambda: QDesktopServices.openUrl(QUrl(f"{REPO_URL}/issues/new/choose"))))

        self.view_mode.changed.connect(self.preview.set_mode)

    # ======================================================================
    # 状態の反映
    # ======================================================================

    def _busy(self) -> bool:
        return self._job_task is not None

    def _update_state(self) -> None:
        has_doc = self.original_doc is not None
        has_result = self.result is not None and not self.result_stale
        busy = self._busy()
        self.stack.setCurrentWidget(self.content if has_doc else self.drop_zone)
        self.inspector.setVisible(has_doc and self.inspector_btn.isChecked())
        self.inspector_btn.setEnabled(has_doc)
        self.footer.setVisible(has_doc)
        self.view_mode.setVisible(has_doc)
        self.view_mode.set_enabled("both", self.translated_doc is not None)
        self.view_mode.set_enabled("translated", self.translated_doc is not None)
        self.inspector.set_busy(busy)
        self.open_btn.setEnabled(not busy)
        self.act_open.setEnabled(not busy)
        self.act_close.setEnabled(has_doc and not busy)
        self.act_save.setEnabled(has_result and not busy)
        self.act_translate.setEnabled(has_doc and not busy)
        self.act_cancel.setEnabled(busy)
        for w in (self.zoom_in_btn, self.zoom_out_btn, self.fit_btn, self.zoom_label, self.page_label):
            w.setVisible(has_doc)

        self.primary_btn.setVisible(has_doc)
        if busy:
            self.primary_btn.setText("翻訳中…")
            self.primary_btn.setEnabled(False)
            self.secondary_btn.setText("キャンセル")
            self.secondary_btn.setVisible(True)
        elif has_result:
            self.primary_btn.setText("保存…")
            self.primary_btn.setEnabled(True)
            self.secondary_btn.setText("再翻訳")
            self.secondary_btn.setVisible(True)
        else:
            self.primary_btn.setText("翻訳")
            self.primary_btn.setEnabled(has_doc)
            self.secondary_btn.setVisible(False)

        if has_doc and self.source_path is not None:
            self.doc_title.setText(self.source_path.name)
            self.doc_subtitle.setText(f"{self.original_doc.page_count} ページ")
        else:
            self.doc_title.setText(APP_NAME)
            self.doc_subtitle.setText("論文 PDF を日本語に翻訳")

    def refresh_engine_status(self) -> None:
        engine = self.settings.engine
        info = registry.ENGINES.get(engine)
        has_key = (not info.needs_key) if info else False
        if info and info.needs_key:
            has_key = bool(self.secrets.get(engine))
        self.inspector.set_engine_status(has_key)
        if has_key:
            from ..translate.gemini import MODELS

            name = MODELS[self.settings.gemini_model][0] if engine == "gemini" else info.name
            self.drop_zone.set_engine_notice(f"{name} で翻訳します", warning=False)
        else:
            self.drop_zone.set_engine_notice(f"{info.name if info else engine} の API キーが設定されていません", warning=True)

    # ======================================================================
    # 文書
    # ======================================================================

    def open_dialog(self) -> None:
        if self._busy():
            return
        start = self.settings.last_open_dir or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "PDF を開く", start, "PDF (*.pdf)")
        if path:
            self.open_file(Path(path))

    def open_file(self, path: Path) -> bool:
        try:
            data = path.read_bytes()
            doc = open_document(data)
        except SpectraError as e:
            self._alert(e.title, e.user_message().split("\n", 1)[-1] if "\n" in e.user_message() else "")
            return False
        except OSError as e:
            self._alert("ファイルを読み込めませんでした", str(e))
            return False
        self.close_document()
        self.source_path, self.source_bytes, self.original_doc = path, data, doc
        self.settings.last_open_dir = str(path.parent)
        self.settings.save()
        self.inspector.set_page_count(doc.page_count)
        self.preview.original.set_document(doc)
        self.view_mode.set_current("original")
        self.preview.set_mode("original")
        self.status.setText("")
        self._update_state()
        self._start_estimate()
        return True

    def close_document(self) -> None:
        if self._busy():
            return
        self.preview.original.set_document(None)
        self.preview.translated.set_document(None)
        self.original_doc = self.translated_doc = None
        self.source_path = self.source_bytes = None
        self.result = None
        self.result_stale = False
        self.banner_wrap.hide()
        self.inspector.set_estimate(None)
        self._update_state()

    # -- 見積もり -----------------------------------------------------------

    def _make_job(self, translator=None, progress=None) -> TranslationJob:
        first, last = self.inspector.page_range()
        translator = translator or registry.create(
            self.settings.engine, api_key="", options=self.settings.translate_options(), model=self.settings.gemini_model
        )
        return TranslationJob(
            self.source_bytes,
            translator,
            cache=self.cache if self.settings.use_cache else None,
            analysis_options=self.settings.analysis_options(first, last),
            render_options=self.settings.render_options(),
            progress=progress,
            verify=self.settings.verify_layout,
        )

    def _start_estimate(self) -> None:
        if self.source_bytes is None:
            return
        self._estimate_gen += 1
        gen = self._estimate_gen
        self.inspector.set_estimating()
        job = self._make_job()

        def done(est):
            if gen == self._estimate_gen:
                self.inspector.set_estimate(est)

        def failed(err):
            if gen == self._estimate_gen:
                self.inspector.set_estimate(None)
                log.warning("estimate failed: %s", err)

        worker.start(lambda _r, _c: job.estimate(), done, failed)

    def _on_settings_changed(self, reanalyze: bool) -> None:
        if reanalyze:
            self._estimate_timer.start()
            self.refresh_engine_status()
        if self.result is not None and reanalyze:
            self.result_stale = True
            self.status.setText("設定が変更されました。もう一度翻訳すると反映されます。")
        self._update_state()

    # -- 翻訳 --------------------------------------------------------------

    def _on_primary(self) -> None:
        if self.result is not None and not self.result_stale:
            self.save_dialog()
        else:
            self.start_translation()

    def _on_secondary(self) -> None:
        if self._busy():
            self.cancel_translation()
        else:
            self.start_translation()

    def start_translation(self, allow_partial: bool = False) -> None:
        if self.source_bytes is None or self._busy():
            return
        engine = self.settings.engine
        info = registry.ENGINES.get(engine)
        key = self.secrets.get(engine) if info and info.needs_key else ""
        if info and info.needs_key and not key:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(f"{info.name} の API キーを設定してください")
            box.setInformativeText(f"{info.free_quota}で使えます。キーは設定画面から入力できます。")
            open_btn = box.addButton("設定を開く", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is open_btn:
                self.show_settings("engines")
            return

        translator = registry.create(
            engine, api_key=key, options=self.settings.translate_options(), model=self.settings.gemini_model
        )
        job = self._make_job(translator)
        self.banner_wrap.hide()
        self.progress.setValue(0)
        self.progress.show()
        self.status.setText("準備しています…")

        def run(report, cancel):
            job.cancel_event = cancel
            translator.cancel_event = cancel
            job._progress = report
            try:
                return job.run(allow_partial=allow_partial)
            finally:
                translator.close()

        self._job_task = worker.start(run, self._on_job_done, self._on_job_failed, self._on_job_progress)
        self._update_state()

    def cancel_translation(self) -> None:
        if self._job_task is not None:
            self._job_task.cancel()
            self.status.setText("キャンセルしています…")
            self.secondary_btn.setEnabled(False)

    def _on_job_progress(self, p: Progress) -> None:
        self.progress.setValue(int(p.fraction * 1000))
        detail = ""
        if p.stage.value == "translate" and p.total:
            detail = f"（{p.done * 100 // p.total}%）"
        self.status.setText(f"{p.label}…{detail}")

    def _finish_job(self) -> None:
        self._job_task = None
        self.progress.hide()
        self.secondary_btn.setEnabled(True)
        self._update_state()

    def _on_job_done(self, result: JobResult) -> None:
        self._finish_job()
        self.result = result
        self.result_stale = False
        self.translated_doc = pymupdf.open(stream=result.translated_pdf, filetype="pdf")
        self.preview.translated.set_document(self.translated_doc)
        self.view_mode.set_current("both")
        self.preview.set_mode("both")
        minutes, seconds = divmod(int(result.elapsed), 60)
        took = f"{minutes} 分 {seconds} 秒" if minutes else f"{seconds} 秒"
        self.status.setText(f"{result.segments} 段落 · {result.translated_chars:,} 文字 · {took}")
        if result.warnings:
            self._show_banner("warning", "翻訳が完了しました", "\n".join(result.warnings), "保存…")
        else:
            self._show_banner("success", "翻訳が完了しました", "図表とレイアウトの検査に合格しました。" if result.qa.pages_checked else "", "保存…")
        self._update_state()
        self._start_estimate()
        self.refresh_engine_status()

    def _on_job_failed(self, err: BaseException) -> None:
        self._finish_job()
        if isinstance(err, CancelledError):
            self.status.setText("キャンセルしました。翻訳済みの段落はキャッシュに保存されています。")
            return
        if isinstance(err, PartialTranslationError):
            self.status.setText("")
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(err.title)
            box.setInformativeText(
                f"{len(err.results)} 個の段落は翻訳できました。翻訳できた段落だけで PDF を作成しますか？\n"
                "残りの段落は原文のまま残り、次回はキャッシュから続きを翻訳できます。"
            )
            make = box.addButton("翻訳できた分で作成", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is make:
                self.start_translation(allow_partial=True)
            self.refresh_engine_status()
            return
        if isinstance(err, SpectraError):
            title, _, rest = err.user_message().partition("\n")
            self.status.setText(title)
            if isinstance(err, (MissingKeyError, AuthenticationError)):
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setText(title)
                box.setInformativeText(rest)
                open_btn = box.addButton("設定を開く", QMessageBox.ButtonRole.AcceptRole)
                box.addButton("閉じる", QMessageBox.ButtonRole.RejectRole)
                box.exec()
                if box.clickedButton() is open_btn:
                    self.show_settings("engines")
            else:
                self._alert(title, rest)
            return
        log.exception("unexpected error", exc_info=err)
        self.status.setText("予期しないエラーが発生しました")
        self._alert("予期しないエラーが発生しました", f"{type(err).__name__}: {err}\n\n詳細はログフォルダを確認してください。")

    # -- 保存 --------------------------------------------------------------

    def save_dialog(self) -> None:
        if self.result is None or self.source_path is None:
            return
        directory = Path(self.settings.last_save_dir or self.source_path.parent)
        suggested = directory / f"{self.source_path.stem}_ja.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "翻訳した PDF を保存", str(suggested), "PDF (*.pdf)")
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() != ".pdf":
            target = target.with_suffix(".pdf")
        try:
            target.write_bytes(self._output_bytes())
        except OSError as e:
            self._alert("保存できませんでした", str(e))
            return
        self.settings.last_save_dir = str(target.parent)
        self.settings.save()
        self._saved_path = target
        self._show_banner("success", "保存しました", str(target), "フォルダを開く")

    def _output_bytes(self) -> bytes:
        """現在の出力形式の PDF。翻訳後に形式だけを変えた場合は作り直す（再翻訳は不要）。"""
        assert self.result is not None and self.source_bytes is not None
        mode = OutputMode(self.settings.output_mode)
        if mode == OutputMode.TRANSLATED:
            return self.result.translated_pdf
        original = pymupdf.open(stream=self.source_bytes, filetype="pdf")
        translated = pymupdf.open(stream=self.result.translated_pdf, filetype="pdf")
        first, last = self.inspector.page_range()
        pages = range(first, (last if last is not None else original.page_count - 1) + 1)
        return finalize(build_output(original, translated, mode, pages))

    def _on_banner_action(self) -> None:
        if self.banner._action.text() == "保存…":
            self.save_dialog()
        elif getattr(self, "_saved_path", None):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._saved_path.parent)))

    # ======================================================================
    # その他
    # ======================================================================

    def show_settings(self, tab: str = "engines") -> None:
        dlg = SettingsDialog(self.settings, self.secrets, self.cache, self)
        dlg.keysChanged.connect(self.refresh_engine_status)
        dlg.settingsChanged.connect(lambda: self._on_settings_changed(True))
        dlg.appearanceChanged.connect(self.apply_appearance)
        dlg.show_tab(tab)
        dlg.exec()
        self.refresh_engine_status()

    def show_about(self) -> None:
        AboutDialog(self).exec()

    def _show_banner(self, kind: str, title: str, body: str = "", action: str = "") -> None:
        self.banner.show_message(kind, title, body, action)
        self.banner_wrap.show()

    def _alert(self, title: str, body: str = "") -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(title)
        box.setInformativeText(body)
        box.exec()

    def _zoom_by(self, factor: float) -> None:
        view = self.preview.active_view()
        view.set_zoom(view.zoom() * factor)

    def _on_zoom_changed(self, zoom: float) -> None:
        self.zoom_label.setText(f"{round(zoom * 100)}%")

    def _on_page_changed(self, index: int, total: int) -> None:
        self.page_label.setText(f"{index + 1} / {total} ページ" if total else "")

    def apply_appearance(self) -> None:
        from PySide6.QtWidgets import QApplication

        theme.set_appearance(self.settings.appearance)
        theme.apply(QApplication.instance())
        self.refresh_theme()

    def refresh_theme(self) -> None:
        for b in (self.open_btn, self.settings_btn, self.inspector_btn, self.zoom_in_btn, self.zoom_out_btn, self.fit_btn):
            refresh_icon(b)
        self.drop_zone.refresh_theme()
        self.refresh_engine_status()
        for view in (self.preview.original, self.preview.translated):
            view.viewport().update()

    # -- ドラッグ＆ドロップ ------------------------------------------------------

    @staticmethod
    def _pdf_from_event(event) -> Path | None:
        md = event.mimeData()
        if md.hasUrls():
            for url in md.urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                    return Path(url.toLocalFile())
        return None

    def dragEnterEvent(self, event) -> None:
        if not self._busy() and self._pdf_from_event(event):
            event.acceptProposedAction()
            self.drop_zone.set_drag_active(True)

    def dragLeaveEvent(self, event) -> None:
        self.drop_zone.set_drag_active(False)

    def dropEvent(self, event) -> None:
        self.drop_zone.set_drag_active(False)
        path = self._pdf_from_event(event)
        if path:
            event.acceptProposedAction()
            self.open_file(path)

    # -- ウィンドウ ---------------------------------------------------------

    def _restore_window(self) -> None:
        geo = self.settings.window.get("geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        else:
            self.resize(1320, 860)
        self.inspector_btn.setChecked(self.settings.window.get("inspector", True))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.original_doc is None:
            self.drop_zone.open_button.setFocus()

    def closeEvent(self, event) -> None:
        if self._busy():
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setText("翻訳を中止して終了しますか？")
            box.setInformativeText("翻訳済みの段落はキャッシュに保存されているため、次回は続きから翻訳できます。")
            quit_btn = box.addButton("終了", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not quit_btn:
                event.ignore()
                return
            self._job_task.cancel()
        self.settings.window = {
            "geometry": bytes(self.saveGeometry().toBase64()).decode(),
            "inspector": self.inspector_btn.isChecked(),
        }
        self.settings.save()
        super().closeEvent(event)
