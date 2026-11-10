"""設定ウィンドウ：翻訳エンジン（API キー）・フォント・詳細。変更は即時に保存する。"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractButton,
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
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..config import fonts as font_store
from ..config import paths
from ..config.quota import QuotaBook
from ..config.secrets import SecretStore, default_label, slot
from ..config.settings import MAX_KEYS, Settings
from ..translate import registry
from ..translate.cache import TranslationCache
from ..translate.errors import TranslatorError
from ..translate.gemini import MODELS
from . import icons, theme, worker
from .privacy import home_to_tilde
from .widgets import (
    FormGroup,
    SegmentedControl,
    Switch,
    button,
    label,
    link_button,
    plain_tooltip,
    refresh_icon,
    set_prop,
    tool_button,
)


class KeyField(QWidget):
    """API キー 1 つ分の入力欄（名前・伏字・表示切替・確認・並べ替え・削除）。"""

    changed = Signal(str)  # engine_id
    renamed = Signal(str, str)  # (キーの id, 名前)
    moveRequested = Signal(str, int)  # (キーの id, -1 / +1)
    removeRequested = Signal(str)  # キーの id（一覧から外す）

    def __init__(
        self,
        engine_id: str,
        secrets: SecretStore,
        settings: Settings,
        parent: QWidget | None = None,
        key_id: str = "default",
        name: str = "",
        index: int = 0,
        count: int = 1,
    ):
        super().__init__(parent)
        self.engine_id = engine_id
        self.key_id = key_id
        self.slot = slot(engine_id, key_id)  # 資格情報ストアでの名前
        self.secrets = secrets
        self.settings = settings
        self.info = registry.ENGINES[engine_id]
        self.multiple = count > 1
        self._task: worker.Task | None = None
        # 保存済みのキーは、設定を開いただけでは読まない（macOS ではキーチェーンの許可を求められるため）。
        # 目のアイコンで表示したとき・接続を確認したときに初めて読む
        self._saved = secrets.has(self.slot, use_env=False)
        self._loaded: str | None = None  # 読み出したキー（読んでいなければ None）

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(label(f"{index + 1}.", "secondary"))
        self.name = QLineEdit(name)
        self.name.setPlaceholderText(default_label(engine_id, index))
        self.name.setMaxLength(40)
        self.name.setAccessibleName(f"{index + 1} 番目のキーの名前")
        self.name.editingFinished.connect(lambda: self.renamed.emit(self.key_id, self.name.text().strip()))
        head.addWidget(self.name, 1)
        self.up_btn = _arrow_button("↑", "上へ（先に使う）", index > 0)
        self.up_btn.clicked.connect(lambda: self.moveRequested.emit(self.key_id, -1))
        self.down_btn = _arrow_button("↓", "下へ", index < count - 1)
        self.down_btn.clicked.connect(lambda: self.moveRequested.emit(self.key_id, 1))
        for b in (self.up_btn, self.down_btn):
            b.setVisible(self.multiple)
            head.addWidget(b)
        lay.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText(
            "保存済み（目のアイコンで表示）" if self._saved else f"{self.info.key_label}を貼り付け"
        )
        self.edit.setAccessibleName(f"{name or default_label(engine_id, index)} の {self.info.key_label}")
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

        src = secrets.source(self.slot)
        if src and src.startswith("環境変数"):
            self._set_status(f"{src} の値が使われます。", "info")
        elif src:
            self._set_status(f"保存済み（{src}）", "success")

    def refresh_theme(self) -> None:
        pass

    def _load_saved(self) -> str:
        """保存済みのキーを読み出して欄に入れる（ここで初めてキーチェーンに問い合わせる）。"""
        if self._loaded is None and self._saved and not self.edit.text():
            self._loaded = self.secrets.get(self.slot, use_env=False)
            self.edit.setText(self._loaded)
        return self.edit.text().strip()

    def _toggle_reveal(self, on: bool) -> None:
        if on:
            self._load_saved()
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
        if not value and self._saved and self._loaded is None:
            return  # 保存済みのキーを読んでいない（欄が空なのは、まだ表示していないだけ）
        if value == (self._loaded or ""):
            return
        self.secrets.set(self.slot, value)
        QuotaBook().forget(self.key_id)  # 別のキーに入れ替えたら、上限の記録は当てはまらない
        self._loaded, self._saved = value, bool(value)
        self._set_status(f"保存しました（{self.secrets.backend_name}）" if value else "削除しました", "success" if value else None)
        self.changed.emit(self.engine_id)
        if value:
            self.verify()

    def _delete(self) -> None:
        if self.multiple:
            self.removeRequested.emit(self.key_id)  # 一覧から外す（キーの削除は一覧が行う）
            return
        self.edit.clear()
        self.secrets.delete(self.slot)
        QuotaBook().forget(self.key_id)
        self._loaded, self._saved = None, False
        self.edit.setPlaceholderText(f"{self.info.key_label}を貼り付け")
        self._set_status("削除しました", None)
        self.changed.emit(self.engine_id)

    def verify(self) -> None:
        key = self._load_saved()  # 確認を押したときに初めて、保存済みのキーを読む
        if not key:
            self._set_status("キーを入力してください。", "danger")
            return
        if key != (self._loaded or ""):
            self.secrets.set(self.slot, key)
            self._loaded, self._saved = key, True
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


def _arrow_button(text: str, tooltip: str, enabled: bool) -> QToolButton:
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tooltip)
    b.setAccessibleName(tooltip)
    b.setEnabled(enabled)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


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


def _scrollable(page: QWidget) -> QScrollArea:
    """ページを縦スクロールで包む。高さが足りないときに行が潰れないようにする。"""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(page)
    return scroll


class SettingsDialog(QDialog):
    keysChanged = Signal()
    settingsChanged = Signal()
    appearanceChanged = Signal()
    lookupChanged = Signal(bool)  # 書誌情報の照会のオン・オフ
    syncChanged = Signal()  # 同期の接続・解除・オン/オフ
    restoreBackupRequested = Signal(object)  # 同期前の状態に戻す（バックアップのパス）

    def __init__(
        self,
        settings: Settings,
        secrets: SecretStore,
        cache: TranslationCache | None,
        parent: QWidget | None = None,
        sync=None,
    ):
        super().__init__(parent)
        self.settings = settings
        self.sync = sync
        self.secrets = secrets
        self.cache = cache
        self.setWindowTitle("設定")
        self.setMinimumSize(QSize(600, 560))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 20)
        lay.setSpacing(16)

        self.tabs = SegmentedControl([("engines", "翻訳エンジン"), ("fonts", "フォント"), ("sync", "同期"), ("advanced", "詳細")])
        lay.addWidget(self.tabs, 0, Qt.AlignmentFlag.AlignHCenter)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        from .sync_settings import SyncPage

        self.sync_page = SyncPage(settings, sync)
        self.sync_page.syncChanged.connect(self.syncChanged)
        self.sync_page.restoreBackupRequested.connect(self.restoreBackupRequested)
        self._pages = {
            "engines": self._engines_page(),
            "fonts": self._fonts_page(),
            "sync": self.sync_page,
            "advanced": self._advanced_page(),
        }
        for w in self._pages.values():
            self.stack.addWidget(w)
        self.tabs.changed.connect(lambda k: self.stack.setCurrentWidget(self._pages[k]))
        self.tabs.set_current("engines")
        # 開いたままテーマが変わったとき（この画面での切り替え・システムの外観の変更）にアイコンの色を合わせる
        QGuiApplication.styleHints().colorSchemeChanged.connect(self.refresh_theme)

        footer = QHBoxLayout()
        footer.addStretch(1)
        done = button("完了", primary=True)
        done.clicked.connect(self.accept)
        footer.addWidget(done)
        lay.addLayout(footer)

    def refresh_theme(self) -> None:
        for b in self.findChildren(QAbstractButton):
            refresh_icon(b)
        self.get_key.setIcon(icons.icon("external", theme.current().accent, 14))

    def show_tab(self, key: str) -> None:
        self.tabs.set_current(key)

    # -- 翻訳エンジン ----------------------------------------------------------

    def _engines_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        group = FormGroup("API キー")
        info = registry.ENGINES["gemini"]
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label(info.name, "headline"))
        titles.addWidget(label(info.description, "caption", wrap=True))
        head.addLayout(titles, 1)
        self.get_key = link_button("キーを取得")
        self.get_key.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.get_key.setToolTip(info.key_url)
        self.get_key.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(info.key_url)))
        head.addWidget(self.get_key, 0, Qt.AlignmentFlag.AlignTop)
        head_box = QWidget()
        head_box.setLayout(head)
        group.add_widget(head_box)
        self.key_steps = label(info.key_steps, "caption", wrap=True)
        self.key_steps.setAccessibleName("API キーの取り方")
        group.add_widget(self.key_steps)
        self.key_list = QWidget()
        self.key_rows = QVBoxLayout(self.key_list)
        self.key_rows.setContentsMargins(0, 0, 0, 0)
        self.key_rows.setSpacing(14)
        group.add_widget(self.key_list)
        self.key_fields: dict[str, KeyField] = {}
        self.add_key_btn = button("キーを追加…")
        self.add_key_btn.clicked.connect(self._add_key)
        add_row = QHBoxLayout()
        add_row.addWidget(self.add_key_btn)
        add_row.addStretch(1)
        add_box = QWidget()
        add_box.setLayout(add_row)
        group.add_widget(add_box)
        self.key_auto_switch = Switch(self.settings.key_auto_switch)
        self.key_auto_switch.toggled.connect(self._on_key_auto_switch)
        group.add_row(
            "上限に達したら次のキーに切り替える",
            self.key_auto_switch,
            "1 日の無料枠の上限に達したら、一覧の次のキーで翻訳を続けます",
        )
        self._build_key_rows()
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
            "無料枠では、送信した文章が Google の製品改善に使われる場合があります。"
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
        return _scrollable(page)

    # -- API キーの一覧 ----------------------------------------------------------------

    def _key_entries(self) -> list[dict]:
        """一覧に出すキー。目録が空なら、以前からのキー（default）の欄を 1 つ出す。"""
        return self.settings.keys_for("gemini") or [{"id": "default", "provider": "gemini", "label": ""}]

    def _build_key_rows(self) -> None:
        while self.key_rows.count():
            w = self.key_rows.takeAt(0).widget()
            if w is not None:
                # deleteLater だけでは消えるまで古い欄が描かれ続け、新しい欄と重なる。先に外して隠す
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self.key_fields = {}
        entries = self._key_entries()
        exhausted = QuotaBook().exhausted_keys() if len(entries) > 1 else {}
        for i, e in enumerate(entries):
            field = KeyField("gemini", self.secrets, self.settings, key_id=e["id"], name=e["label"], index=i, count=len(entries))
            field.changed.connect(lambda _e: self.keysChanged.emit())
            field.changed.connect(lambda _e: self._refresh_key_steps())
            field.renamed.connect(self._rename_key)
            field.moveRequested.connect(self._move_key)
            field.removeRequested.connect(self._remove_key)
            if e["id"] in exhausted and field._saved:
                when = time.strftime("%H:%M", time.localtime(exhausted[e["id"]]))
                field._set_status(f"今日の上限に達しています（{when} ごろに戻ります）", "info")
            self.key_rows.addWidget(field)
            self.key_fields[field.slot] = field
        self.add_key_btn.setEnabled(len(entries) < MAX_KEYS)
        self._refresh_key_steps()

    def _refresh_key_steps(self) -> None:
        """キーの取り方の手順は、キーがまだ 1 本もないときだけ出す（設定済みの人には要らない）。"""
        self.key_steps.setVisible(bool(self.key_steps.text()) and not self.secrets.has("gemini"))

    def _save_key_entries(self, entries: list[dict]) -> None:
        self.settings.api_keys = entries
        self.settings.save()
        self._build_key_rows()
        self.keysChanged.emit()

    def _add_key(self) -> None:
        entries = self._key_entries()
        self._save_key_entries([*entries, {"id": uuid.uuid4().hex, "provider": "gemini", "label": ""}])
        list(self.key_fields.values())[-1].edit.setFocus()

    def _rename_key(self, key_id: str, name: str) -> None:
        entries = [dict(e, label=name) if e["id"] == key_id else e for e in self._key_entries()]
        if entries != self.settings.keys_for("gemini"):
            self.settings.api_keys = entries
            self.settings.save()

    def _move_key(self, key_id: str, delta: int) -> None:
        entries = self._key_entries()
        i = next(n for n, e in enumerate(entries) if e["id"] == key_id)
        j = i + delta
        if 0 <= j < len(entries):
            entries[i], entries[j] = entries[j], entries[i]
            self._save_key_entries(entries)

    def _remove_key(self, key_id: str) -> None:
        self.secrets.delete(slot("gemini", key_id))
        QuotaBook().forget(key_id)
        self._save_key_entries([e for e in self._key_entries() if e["id"] != key_id])

    def _on_key_auto_switch(self, on: bool) -> None:
        self.settings.key_auto_switch = on
        self.settings.save()

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

        lay.addStretch(1)
        return page

    def _select_font(self, fid: str) -> None:
        self.settings.font = fid
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

        lookup = FormGroup(
            "書誌情報",
            "オンにすると、PDF に書かれた DOI や arXiv ID だけを Crossref・arXiv に送り、"
            "タイトル・著者・雑誌名・概要を取得します。本文やファイルは送りません。",
        )
        self.metadata_lookup = Switch(self.settings.metadata_lookup)
        self.metadata_lookup.toggled.connect(self._on_lookup_toggle)
        lookup.add_row("インターネットで書誌情報を取得する", self.metadata_lookup)
        lay.addWidget(lookup)

        updates = FormGroup(
            "アップデート",
            "オンにすると、起動時に 1 日 1 回まで、新しいバージョンがあるかを GitHub に問い合わせます。"
            "送るのは Spectra のバージョンだけで、ライブラリや論文の情報は送りません。",
        )
        self.update_check = Switch(self.settings.update_check == "on")
        self.update_check.toggled.connect(self._on_update_check)
        updates.add_row("新しいバージョンを確認する", self.update_check)
        self.check_now_btn = button("今すぐ確認")
        self.check_now_btn.clicked.connect(self.check_for_update_now)
        self.download_btn = link_button("ダウンロードのページを開く")
        self.download_btn.hide()
        self._update_url = ""
        self.download_btn.clicked.connect(self._open_update_page)
        buttons = QWidget()
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.download_btn)
        row.addWidget(self.check_now_btn)
        updates.add_row("新しいバージョン", buttons)
        self.update_status = label("", "caption", wrap=True)
        updates.add_widget(self.update_status)
        self.update_status.parentWidget().setVisible(False)
        lay.addWidget(updates)

        qa = FormGroup("品質", "翻訳後に、図表・数式・翻訳対象外の文字が変化していないかを自動で検査します。")
        self.verify = Switch(self.settings.verify_layout)
        self.verify.toggled.connect(self._on_verify)
        qa.add_row("レイアウトを検査する", self.verify)
        lay.addWidget(qa)

        folders = FormGroup("フォルダ")
        for title, path in (("ライブラリ", paths.library_dir()), ("設定", paths.config_dir()), ("ログ", paths.log_dir())):
            b = button("開く")
            b.clicked.connect(lambda _c=False, p=path: self._open_folder(p))
            # 表示ではホームを「~」に（スクリーンショットにユーザー名が写らないように）。元のパスはツールチップに
            row = folders.add_row(title, b, home_to_tilde(str(path)))
            row.setToolTip(plain_tooltip(str(path)))
        lay.addWidget(folders)
        lay.addStretch(1)
        # 項目が多くダイアログの高さに収まらないので、行を潰さずにスクロールさせる
        return _scrollable(page)

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
        self.refresh_theme()

    def _on_update_check(self, on: bool) -> None:
        self.settings.update_check = "on" if on else "off"
        self.settings.save()

    def check_for_update_now(self) -> None:
        """「今すぐ確認」: 失敗も表示する。新しい版があればリリースのページを開くボタンを出す。"""
        from .. import __version__
        from .update_prompt import start_check

        self.check_now_btn.setEnabled(False)
        self.download_btn.hide()
        self._show_update_status("確認しています…")

        def found(info) -> None:
            self.check_now_btn.setEnabled(True)
            self._show_update_status(f"Spectra {info.version} が利用できます（今は {__version__}）。")
            self._update_url = info.url
            self.download_btn.show()

        def none() -> None:
            self.check_now_btn.setEnabled(True)
            self._show_update_status(f"最新のバージョンです（{__version__}）。")

        def error(err) -> None:
            self.check_now_btn.setEnabled(True)
            msg = err.user_message().replace("\n", " ") if hasattr(err, "user_message") else str(err)
            self._show_update_status(msg, "danger")

        self._update_task = start_check(found, on_error=error, on_none=none)

    def _open_update_page(self) -> None:
        from .update_prompt import open_release_page

        open_release_page(self._update_url)

    def _show_update_status(self, text: str, tone: str | None = None) -> None:
        self.update_status.setText(text)
        set_prop(self.update_status, "tone", tone)
        self.update_status.parentWidget().setVisible(bool(text))

    def _on_lookup_toggle(self, on: bool) -> None:
        self.settings.metadata_lookup = on
        self.settings.save()
        self.lookupChanged.emit(on)

    def _on_verify(self, on: bool) -> None:
        self.settings.verify_layout = on
        self.settings.save()

    @staticmethod
    def _open_folder(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(path)))

