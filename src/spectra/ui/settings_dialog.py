"""設定ウィンドウ：翻訳エンジン（API キー）・フォント・詳細。変更は即時に保存する。"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QRadioButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import fonts as font_store
from ..config import paths
from ..config.secrets import SecretStore
from ..config.settings import Settings
from ..translate import registry
from ..translate.cache import TranslationCache
from ..translate.errors import TranslatorError
from ..translate.gemini import MODELS
from . import icons, theme, worker
from .widgets import (
    FormGroup,
    SegmentedControl,
    Switch,
    button,
    label,
    link_button,
    set_prop,
    tool_button,
)


class KeyField(QWidget):
    """API キー 1 つ分の入力欄（伏字・表示切替・確認・削除）。"""

    changed = Signal(str)  # engine_id

    def __init__(self, engine_id: str, secrets: SecretStore, settings: Settings, parent: QWidget | None = None):
        super().__init__(parent)
        self.engine_id = engine_id
        self.secrets = secrets
        self.settings = settings
        self.info = registry.ENGINES[engine_id]
        self._task: worker.Task | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label(self.info.name, "headline"))
        titles.addWidget(label(f"{self.info.description}　{self.info.free_quota}。", "caption", wrap=True))
        head.addLayout(titles, 1)
        get_key = link_button("キーを取得")
        get_key.setIcon(icons.icon("external", theme.current().accent, 14))
        get_key.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        get_key.setToolTip(self.info.key_url)
        get_key.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.info.key_url)))
        head.addWidget(get_key, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText(f"{self.info.key_label}を貼り付け")
        self.edit.setAccessibleName(f"{self.info.name} の {self.info.key_label}")
        self.edit.setText(secrets.get(engine_id, use_env=False))
        self.edit.editingFinished.connect(self._save)
        self.edit.textEdited.connect(lambda _t: self._set_status("", None))
        row.addWidget(self.edit, 1)
        self.reveal = tool_button("eye", "キーを表示", checkable=True)
        self.reveal.toggled.connect(self._toggle_reveal)
        row.addWidget(self.reveal)
        self.verify_btn = button("接続を確認")
        self.verify_btn.clicked.connect(self.verify)
        row.addWidget(self.verify_btn)
        self.delete_btn = tool_button("trash", "キーを削除")
        self.delete_btn.clicked.connect(self._delete)
        row.addWidget(self.delete_btn)
        lay.addLayout(row)

        status = QHBoxLayout()
        status.setSpacing(6)
        self.status_icon = QLabel()
        self.status_icon.setFixedSize(16, 16)
        self.status = label("", "caption", wrap=True)
        status.addWidget(self.status_icon, 0, Qt.AlignmentFlag.AlignTop)
        status.addWidget(self.status, 1)
        lay.addLayout(status)

        src = secrets.source(engine_id)
        if src and src.startswith("環境変数"):
            self._set_status(f"{src} の値が使われます。", "info")
        elif src:
            self._set_status(f"保存済み（{src}）", "success")

    def _toggle_reveal(self, on: bool) -> None:
        self.edit.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        self.reveal.setProperty("iconName", "eye_off" if on else "eye")
        self.reveal.setToolTip("キーを隠す" if on else "キーを表示")
        from .widgets import refresh_icon

        refresh_icon(self.reveal)

    def _set_status(self, text: str, kind: str | None) -> None:
        t = theme.current()
        mapping = {"success": ("check", t.success), "danger": ("error", t.danger), "info": ("info", t.secondary), "busy": ("refresh", t.secondary)}
        if kind in mapping:
            name, color = mapping[kind]
            self.status_icon.setPixmap(icons.pixmap(name, color, 16))
        else:
            self.status_icon.clear()
        self.status.setText(text)
        set_prop(self.status, "tone", "danger" if kind == "danger" else "success" if kind == "success" else None)

    def _save(self) -> None:
        value = self.edit.text().strip()
        if value == self.secrets.get(self.engine_id, use_env=False):
            return
        self.secrets.set(self.engine_id, value)
        self._set_status(f"保存しました（{self.secrets.backend_name}）" if value else "削除しました", "success" if value else None)
        self.changed.emit(self.engine_id)
        if value:
            self.verify()

    def _delete(self) -> None:
        self.edit.clear()
        self.secrets.delete(self.engine_id)
        self._set_status("削除しました", None)
        self.changed.emit(self.engine_id)

    def verify(self) -> None:
        key = self.edit.text().strip()
        if not key:
            self._set_status("キーを入力してください。", "danger")
            return
        if key != self.secrets.get(self.engine_id, use_env=False):
            self.secrets.set(self.engine_id, key)
            self.changed.emit(self.engine_id)
        self.verify_btn.setEnabled(False)
        self._set_status("確認しています…", "busy")

        model = self.settings.gemini_model

        def job(_report, _cancel):
            with registry.create(self.engine_id, api_key=key, model=model) as t:
                return t.verify()

        def done(usage):
            self.verify_btn.setEnabled(True)
            if usage is not None and usage.limit:
                self._set_status(f"接続できました。今月の使用量 {usage.used:,} / {usage.limit:,} 文字", "success")
            else:
                self._set_status("接続できました。", "success")
            self.changed.emit(self.engine_id)

        def failed(err):
            self.verify_btn.setEnabled(True)
            msg = err.user_message().replace("\n", " ") if isinstance(err, TranslatorError) else str(err)
            self._set_status(msg, "danger")

        self._task = worker.start(job, done, failed)


class FontOption(QFrame):
    """フォントの選択肢 1 つ（見本・ダウンロード状態付き）。"""

    def __init__(self, choice: font_store.FontChoice, group: QButtonGroup, parent: QWidget | None = None):
        super().__init__(parent)
        self.choice = choice
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 6)
        self.radio = QRadioButton()
        self.radio.setAccessibleName(choice.name)
        group.addButton(self.radio)
        lay.addWidget(self.radio, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(label(choice.name, "headline"))
        texts.addWidget(label(choice.description, "caption", wrap=True))
        self.sample = QLabel("深層学習による論文翻訳の評価")
        self.sample.setProperty("role", "secondary")
        texts.addWidget(self.sample)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.hide()
        texts.addWidget(self.progress)
        lay.addLayout(texts, 1)
        self.action = button("")
        lay.addWidget(self.action, 0, Qt.AlignmentFlag.AlignVCenter)
        self.refresh()

    def refresh(self, custom_path: str = "") -> None:
        c = self.choice
        installed = font_store.is_installed(c.font_id)
        self.sample.setVisible(c.font_id == "builtin" or (bool(c.files) and installed) or bool(custom_path))
        if c.files:
            self.action.setVisible(True)
            self.action.setText("削除" if installed else f"ダウンロード（{c.size_mb:.0f} MB）")
            self.radio.setEnabled(installed)
            if installed:
                self._load_sample(paths.fonts_dir() / next(iter(c.files)))
        elif c.font_id == "custom":
            self.action.setVisible(True)
            self.action.setText("選択…")
            self.radio.setEnabled(bool(custom_path) and Path(custom_path).is_file())
            if custom_path:
                self.sample.setText(Path(custom_path).name)
        else:
            self.action.setVisible(False)

    def _load_sample(self, path: Path) -> None:
        fid = QFontDatabase.addApplicationFont(str(path))
        families = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        if families:
            f = QFont(families[0])
            f.setPixelSize(16)
            self.sample.setFont(f)


class SettingsDialog(QDialog):
    keysChanged = Signal()
    settingsChanged = Signal()
    appearanceChanged = Signal()

    def __init__(self, settings: Settings, secrets: SecretStore, cache: TranslationCache | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.secrets = secrets
        self.cache = cache
        self.setWindowTitle("設定")
        self.setMinimumSize(QSize(600, 560))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 20)
        lay.setSpacing(16)

        self.tabs = SegmentedControl([("engines", "翻訳エンジン"), ("fonts", "フォント"), ("advanced", "詳細")])
        lay.addWidget(self.tabs, 0, Qt.AlignmentFlag.AlignHCenter)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self._pages = {"engines": self._engines_page(), "fonts": self._fonts_page(), "advanced": self._advanced_page()}
        for w in self._pages.values():
            self.stack.addWidget(w)
        self.tabs.changed.connect(lambda k: self.stack.setCurrentWidget(self._pages[k]))
        self.tabs.set_current("engines")

        footer = QHBoxLayout()
        footer.addStretch(1)
        done = button("完了", primary=True)
        done.clicked.connect(self.accept)
        footer.addWidget(done)
        lay.addLayout(footer)

    def show_tab(self, key: str) -> None:
        self.tabs.set_current(key)

    # -- 翻訳エンジン ----------------------------------------------------------

    def _engines_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        group = FormGroup("API キー")
        self.key_fields: dict[str, KeyField] = {}
        for eng in ("gemini",):
            field = KeyField(eng, self.secrets, self.settings)
            field.changed.connect(lambda _e: self.keysChanged.emit())
            group.add_widget(field)
            self.key_fields[eng] = field
        if self.secrets.secure:
            group.set_footer(f"キーは {self.secrets.backend_name} に安全に保存され、選択した翻訳サービス以外には送信されません。")
        else:
            group.set_footer(
                "この環境では OS の資格情報ストアが使えないため、キーは自分だけが読める設定ファイルに保存されます。",
                "warning",
            )
        lay.addWidget(group)

        model_group = FormGroup(
            "モデル",
            "無料枠（クレジットカード不要）では、送信した文章が Google の製品改善に使われる場合があります。"
            "未公開の原稿など機密性の高い文書の翻訳には注意してください。",
        )
        self.model = QComboBox()
        for model_id, info in MODELS.items():
            self.model.addItem(info.name + ("（非推奨）" if info.caution else ""), model_id)
        self.model.setCurrentIndex(max(0, self.model.findData(self.settings.gemini_model)))
        self.model.currentIndexChanged.connect(self._on_model)
        self.model_desc = label("", "caption", wrap=True)
        model_group.add_row("翻訳に使うモデル", self.model)
        model_group.add_widget(self.model_desc)
        self.auto_fallback = Switch(self.settings.auto_fallback)
        self.auto_fallback.toggled.connect(self._on_auto_fallback)
        model_group.add_row(
            "混雑時に他のモデルへ切り替える",
            self.auto_fallback,
            "混雑や無料枠の上限で止まったときに、他の軽量なモデルで続きを翻訳します（3.8 Flash には切り替えません）",
        )
        self._refresh_model_desc()
        lay.addWidget(model_group)
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _refresh_model_desc(self) -> None:
        info = MODELS[self.settings.gemini_model]
        self.model_desc.setText(info.caution or info.description)
        set_prop(self.model_desc, "tone", "warning" if info.caution else None)

    def _on_model(self) -> None:
        self.settings.gemini_model = self.model.currentData()
        self._refresh_model_desc()
        self.settings.save()
        self.settingsChanged.emit()

    def _on_auto_fallback(self, on: bool) -> None:
        self.settings.auto_fallback = on
        self.settings.save()

    # -- フォント --------------------------------------------------------------

    def _fonts_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        group = FormGroup("訳文のフォント", "Noto フォントは SIL Open Font License で配布されています。")
        self.font_group = QButtonGroup(self)
        self.font_options: dict[str, FontOption] = {}
        for fid, choice in font_store.FONT_CHOICES.items():
            opt = FontOption(choice, self.font_group)
            opt.refresh(self.settings.custom_font_path)
            opt.radio.setChecked(self.settings.font == fid)
            opt.radio.toggled.connect(lambda on, f=fid: on and self._select_font(f))
            opt.action.clicked.connect(lambda _c=False, f=fid: self._font_action(f))
            group.add_widget(opt)
            self.font_options[fid] = opt
        lay.addWidget(group)

        size = FormGroup("文字の大きさ")
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        self.scale = QSlider(Qt.Orientation.Horizontal)
        self.scale.setRange(80, 105)
        self.scale.setValue(round(self.settings.text_scale * 100))
        self.scale.setAccessibleName("原文に対する文字の大きさ")
        self.scale_label = label(f"{self.scale.value()}%", "secondary")
        self.scale_label.setFixedWidth(44)
        self.scale.valueChanged.connect(self._on_scale)
        rl.addWidget(label("原文比", "secondary"))
        rl.addWidget(self.scale, 1)
        rl.addWidget(self.scale_label)
        size.add_widget(row)
        size.set_footer("枠に収まらない段落は自動で小さくします。")
        lay.addWidget(size)
        lay.addStretch(1)
        return page

    def _select_font(self, fid: str) -> None:
        self.settings.font = fid
        self.settings.save()
        self.settingsChanged.emit()

    def _on_scale(self, v: int) -> None:
        self.scale_label.setText(f"{v}%")
        self.settings.text_scale = v / 100
        self.settings.save()
        self.settingsChanged.emit()

    def _font_action(self, fid: str) -> None:
        opt = self.font_options[fid]
        if fid == "custom":
            path, _ = QFileDialog.getOpenFileName(self, "フォントファイルを選択", "", "フォント (*.ttf *.otf *.ttc)")
            if path:
                self.settings.custom_font_path = path
                opt.refresh(path)
                opt.radio.setChecked(True)
                self._select_font("custom")
            return
        if font_store.is_installed(fid):
            if self.settings.font == fid:
                self.font_options["builtin"].radio.setChecked(True)
            font_store.remove(fid)
            opt.refresh()
            return
        opt.action.setEnabled(False)
        opt.progress.setValue(0)
        opt.progress.show()

        def job(report, _cancel):
            font_store.download(fid, progress=lambda d, t: report((d, t)))

        def progress(v):
            d, t = v
            opt.progress.setValue(int(d / t * 1000) if t else 0)

        def done(_r):
            opt.progress.hide()
            opt.action.setEnabled(True)
            opt.refresh()
            opt.radio.setChecked(True)

        def failed(err):
            opt.progress.hide()
            opt.action.setEnabled(True)
            opt.sample.setText(f"ダウンロードに失敗しました: {err}")

        worker.start(job, done, failed, progress)

    # -- 詳細 -----------------------------------------------------------------

    def _advanced_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        look = FormGroup("外観")
        self.appearance = SegmentedControl([("auto", "自動"), ("light", "ライト"), ("dark", "ダーク")])
        self.appearance.set_current(self.settings.appearance)
        self.appearance.changed.connect(self._on_appearance)
        look.add_row("テーマ", self.appearance, "自動ではシステムの設定に合わせます")
        lay.addWidget(look)

        cache = FormGroup("翻訳キャッシュ", "同じ文を再び翻訳するときは、API を使わずに保存済みの訳文を使います。")
        self.use_cache = Switch(self.settings.use_cache)
        self.use_cache.toggled.connect(self._on_cache_toggle)
        cache.add_row("キャッシュを使う", self.use_cache)
        self.cache_info = label("", "secondary")
        clear = button("削除", destructive=True)
        clear.clicked.connect(self._clear_cache)
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.cache_info)
        rl.addWidget(clear)
        cache.add_row("保存済みの訳文", row)
        lay.addWidget(cache)
        self._refresh_cache_info()

        qa = FormGroup("品質", "翻訳後に、図表・数式・翻訳対象外の文字が変化していないかを自動で検査します。")
        self.verify = Switch(self.settings.verify_layout)
        self.verify.toggled.connect(self._on_verify)
        qa.add_row("レイアウトを検査する", self.verify)
        lay.addWidget(qa)

        folders = FormGroup("フォルダ")
        for title, path in (("設定", paths.config_dir()), ("ログ", paths.log_dir())):
            b = button("開く")
            b.clicked.connect(lambda _c=False, p=path: self._open_folder(p))
            folders.add_row(title, b, str(path))
        lay.addWidget(folders)
        lay.addStretch(1)
        return page

    def _refresh_cache_info(self) -> None:
        if self.cache is None:
            self.cache_info.setText("—")
            return
        size = self.cache.size_bytes() / 1024 / 1024
        self.cache_info.setText(f"{self.cache.count():,} 件（{size:.1f} MB）")

    def _clear_cache(self) -> None:
        if self.cache is not None:
            self.cache.clear()
        self._refresh_cache_info()

    def _on_cache_toggle(self, on: bool) -> None:
        self.settings.use_cache = on
        self.settings.save()
        self.settingsChanged.emit()

    def _on_appearance(self, value: str) -> None:
        self.settings.appearance = value
        self.settings.save()
        self.appearanceChanged.emit()

    def _on_verify(self, on: bool) -> None:
        self.settings.verify_layout = on
        self.settings.save()

    @staticmethod
    def _open_folder(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(path)))

