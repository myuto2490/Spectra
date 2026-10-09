"""インスペクタ：翻訳エンジン・出力・翻訳対象の設定と、翻訳前の見積もり。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config.settings import Settings
from ..core.options import Formality, OutputMode
from ..core.pipeline import Estimate
from ..translate.base import Usage
from ..translate.registry import ENGINES, public_engines
from .widgets import FormGroup, Switch, label, link_button, set_prop

OUTPUT_MODES = [
    (OutputMode.TRANSLATED.value, "翻訳のみ"),
    (OutputMode.SIDE_BY_SIDE.value, "左右に対訳"),
    (OutputMode.INTERLEAVE.value, "ページごとに対訳"),
]
FORMALITIES = [
    (Formality.DEFAULT.value, "自動"),
    (Formality.PLAIN.value, "である調"),
    (Formality.POLITE.value, "です・ます調"),
]


def _combo(items: list[tuple[str, str]], current: str) -> QComboBox:
    c = QComboBox()
    for key, text in items:
        c.addItem(text, key)
    idx = c.findData(current)
    c.setCurrentIndex(max(0, idx))
    c.setMinimumWidth(150)
    c.setCursor(Qt.CursorShape.PointingHandCursor)
    return c


class Inspector(QScrollArea):
    settingsChanged = Signal(bool)  # True なら再解析（見積もりの更新）が必要
    openSettings = Signal(str)  # 設定画面のタブ

    def __init__(self, settings: Settings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.setObjectName("Inspector")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedWidth(320)
        body = QWidget()
        body.setObjectName("Inspector")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(20)
        self.setWidget(body)
        self._page_count = 0

        # 翻訳エンジン
        self.engine_group = FormGroup("翻訳エンジン")
        self.engine = _combo([(e.engine_id, e.name) for e in public_engines()], settings.engine)
        self.engine.currentIndexChanged.connect(self._on_engine)
        self.engine_group.add_row("エンジン", self.engine)
        usage = QWidget()
        ul = QVBoxLayout(usage)
        ul.setContentsMargins(0, 0, 0, 0)
        ul.setSpacing(6)
        top = QHBoxLayout()
        self.usage_title = label("今月の使用量", "secondary")
        self.usage_value = label("—", "secondary")
        top.addWidget(self.usage_title)
        top.addStretch(1)
        top.addWidget(self.usage_value)
        ul.addLayout(top)
        self.usage_bar = QProgressBar()
        self.usage_bar.setRange(0, 1000)
        self.usage_bar.setAccessibleName("今月の使用量")
        ul.addWidget(self.usage_bar)
        self.key_status = label("", "caption", wrap=True)
        ul.addWidget(self.key_status)
        self.key_button = link_button("API キーを設定…")
        self.key_button.clicked.connect(lambda: self.openSettings.emit("engines"))
        ul.addWidget(self.key_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.engine_group.add_widget(usage)
        lay.addWidget(self.engine_group)

        # 出力
        out = FormGroup("出力")
        self.output = _combo(OUTPUT_MODES, settings.output_mode)
        self.output.currentIndexChanged.connect(self._on_output)
        out.add_row("形式", self.output)
        pages = QWidget()
        pl = QHBoxLayout(pages)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        self.page_from = QSpinBox()
        self.page_to = QSpinBox()
        for sb, name in ((self.page_from, "開始ページ"), (self.page_to, "終了ページ")):
            sb.setRange(1, 1)
            sb.setFixedWidth(56)
            sb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sb.setAccessibleName(name)
            sb.valueChanged.connect(self._on_pages)
        pl.addWidget(self.page_from)
        pl.addWidget(label("〜", "secondary"))
        pl.addWidget(self.page_to)
        out.add_row("ページ", pages)
        self.formality = _combo(FORMALITIES, settings.formality)
        self.formality.currentIndexChanged.connect(self._on_formality)
        self.formality_row = out.add_row("文体", self.formality)
        lay.addWidget(out)

        # 翻訳する内容
        content = FormGroup("翻訳する内容", "図の中の文字・数式・表・著者名はそのまま残します。")
        self.captions = Switch(settings.translate_captions)
        self.footnotes = Switch(settings.translate_footnotes)
        self.references = Switch(not settings.skip_references)
        content.add_row("図表のキャプション", self.captions)
        content.add_row("脚注", self.footnotes)
        content.add_row("参考文献", self.references, "書誌情報は通常は訳さずに残します")
        for sw in (self.captions, self.footnotes, self.references):
            sw.toggled.connect(self._on_content)
        lay.addWidget(content)

        # 見積もり
        self.estimate_group = FormGroup("見積もり")
        self.est_paragraphs = QLabel("—")
        self.est_chars = QLabel("—")
        self.est_cost = QLabel("—")
        for w in (self.est_paragraphs, self.est_chars, self.est_cost):
            w.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.estimate_group.add_row("翻訳する段落", self.est_paragraphs)
        self.estimate_group.add_row("文字数", self.est_chars)
        self.estimate_group.add_row("API で消費する文字数", self.est_cost, "翻訳済みの文は消費しません")
        lay.addWidget(self.estimate_group)
        lay.addStretch(1)

        self._usage: Usage | None = None
        self._estimate: Estimate | None = None
        self._update_engine_state(has_key=False)

    # -- 外部からの更新 ------------------------------------------------------

    def set_page_count(self, n: int) -> None:
        self._page_count = n
        for sb in (self.page_from, self.page_to):
            sb.blockSignals(True)
            sb.setRange(1, max(1, n))
        self.page_from.setValue(1)
        self.page_to.setValue(max(1, n))
        for sb in (self.page_from, self.page_to):
            sb.blockSignals(False)

    def page_range(self) -> tuple[int, int | None]:
        first = self.page_from.value() - 1
        last = self.page_to.value() - 1
        if first == 0 and last >= self._page_count - 1:
            return 0, None
        return first, last

    def set_engine_status(self, has_key: bool, usage: Usage | None = None, message: str = "", tone: str | None = None) -> None:
        self._usage = usage
        self._update_engine_state(has_key, message, tone)
        self._refresh_estimate_tone()

    def _update_engine_state(self, has_key: bool, message: str = "", tone: str | None = None) -> None:
        info = ENGINES.get(self.settings.engine)
        needs_key = info.needs_key if info else True
        u = self._usage
        show_usage = u is not None and u.limit is not None
        self.usage_title.setVisible(show_usage)
        self.usage_value.setVisible(show_usage)
        self.usage_bar.setVisible(show_usage)
        if show_usage:
            ratio = u.used / u.limit if u.limit else 0
            self.usage_bar.setValue(int(ratio * 1000))
            self.usage_value.setText(f"{u.used:,} / {u.limit:,} 文字")
            set_prop(self.usage_bar, "tone", "danger" if ratio > 0.95 else "warning" if ratio > 0.8 else None)
        if not needs_key:
            text, t = "API キーは不要です。", None
        elif not has_key:
            text, t = "API キーが設定されていません。", "warning"
        else:
            text, t = message or (info.free_quota if info else ""), tone
        self.key_status.setText(text)
        set_prop(self.key_status, "tone", t)
        self.key_button.setVisible(needs_key)
        self.key_button.setText("API キーを設定…" if not has_key else "API キーを管理…")
        self.formality_row.setEnabled(self.settings.engine == "deepl")
        self.formality.setToolTip("" if self.settings.engine == "deepl" else "文体の指定は DeepL でのみ使えます")

    def set_estimate(self, est: Estimate | None) -> None:
        self._estimate = est
        if est is None:
            for w in (self.est_paragraphs, self.est_chars, self.est_cost):
                w.setText("—")
            self.estimate_group.set_footer("")
            return
        self.est_paragraphs.setText(f"{est.segments:,}")
        self.est_chars.setText(f"{est.total_chars:,}")
        self.est_cost.setText(f"{est.pending_chars:,}")
        self._refresh_estimate_tone()

    def set_estimating(self) -> None:
        for w in (self.est_paragraphs, self.est_chars, self.est_cost):
            w.setText("計算中…")

    def _refresh_estimate_tone(self) -> None:
        est, u = self._estimate, self._usage
        if est is None:
            return
        if u is not None and u.remaining is not None and est.pending_chars > u.remaining:
            self.estimate_group.set_footer(
                f"今月の残り（{u.remaining:,} 文字）を超えています。ページを絞るか、別のエンジンを使ってください。", "danger"
            )
        elif est.pending_chars == 0 and est.total_chars:
            self.estimate_group.set_footer("すべてキャッシュ済みです。API の文字数は消費しません。", "success")
        else:
            self.estimate_group.set_footer("")

    # -- 変更ハンドラ ---------------------------------------------------------

    def _save(self, reanalyze: bool) -> None:
        self.settings.save()
        self.settingsChanged.emit(reanalyze)

    def _on_engine(self) -> None:
        self.settings.engine = self.engine.currentData()
        self._usage = None
        self._save(True)

    def _on_output(self) -> None:
        self.settings.output_mode = self.output.currentData()
        self._save(False)

    def _on_formality(self) -> None:
        self.settings.formality = self.formality.currentData()
        self._save(True)

    def _on_content(self) -> None:
        self.settings.translate_captions = self.captions.isChecked()
        self.settings.translate_footnotes = self.footnotes.isChecked()
        self.settings.skip_references = not self.references.isChecked()
        self._save(True)

    def _on_pages(self) -> None:
        if self.page_to.value() < self.page_from.value():
            sender = self.sender()
            other = self.page_to if sender is self.page_from else self.page_from
            other.blockSignals(True)
            other.setValue(sender.value())
            other.blockSignals(False)
        self.settingsChanged.emit(True)

    def set_busy(self, busy: bool) -> None:
        self.widget().setEnabled(not busy)
