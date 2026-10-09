"""インスペクタ：翻訳の設定（モデル・文体・ページ）と翻訳対象、翻訳前の見積もり。

出力形式は翻訳後の「書き出す」で選ぶ（翻訳し直さずに形式だけを変えられるため）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config.settings import Settings
from ..core.options import Formality
from ..core.pipeline import Estimate
from ..translate.base import Usage
from ..translate.gemini import MODELS
from ..translate.registry import ENGINES, public_engines
from .widgets import FormGroup, Switch, label, link_button, set_prop

FORMALITIES = [
    (Formality.DEFAULT.value, "である調"),
    (Formality.POLITE.value, "です・ます調"),
]


def _combo(items: list[tuple[str, str]], current: str) -> QComboBox:
    c = QComboBox()
    for key, text in items:
        c.addItem(text, key)
    c.setCurrentIndex(max(0, c.findData(current)))
    c.setMinimumWidth(156)
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
        self.setFixedWidth(324)
        body = QWidget()
        body.setObjectName("Inspector")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 20, 18, 20)
        lay.setSpacing(22)
        self.setWidget(body)
        self._page_count = 0
        self._usage: Usage | None = None
        self._estimate: Estimate | None = None

        # 翻訳
        group = FormGroup("翻訳")
        self.engine = _combo([(e.engine_id, e.name) for e in public_engines()], settings.engine)
        self.engine.currentIndexChanged.connect(self._on_engine)
        engine_row = group.add_row("エンジン", self.engine)
        engine_row.setVisible(len(public_engines()) > 1)  # 通常は Gemini のみ（開発時は擬似翻訳も選べる）
        self.model = _combo(
            [(mid, info.name + ("（非推奨）" if info.caution else "")) for mid, info in MODELS.items()],
            settings.gemini_model,
        )
        self.model.currentIndexChanged.connect(self._on_model)
        self.model_row = group.add_row("モデル", self.model)
        self.formality = _combo(FORMALITIES, settings.formality)
        self.formality.currentIndexChanged.connect(self._on_formality)
        group.add_row("文体", self.formality)
        pages = QWidget()
        pl = QHBoxLayout(pages)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        self.page_from = QSpinBox()
        self.page_to = QSpinBox()
        for sb, name in ((self.page_from, "開始ページ"), (self.page_to, "終了ページ")):
            sb.setRange(1, 1)
            sb.setFixedWidth(60)
            sb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sb.setAccessibleName(name)
            sb.valueChanged.connect(self._on_pages)
        pl.addWidget(self.page_from)
        pl.addWidget(label("–", "secondary"))
        pl.addWidget(self.page_to)
        group.add_row("ページ", pages)

        status = QWidget()
        sl = QVBoxLayout(status)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        self.key_status = label("", "caption", wrap=True)
        self.key_button = link_button("API キーを設定…")
        self.key_button.clicked.connect(lambda: self.openSettings.emit("engines"))
        sl.addWidget(self.key_status)
        sl.addWidget(self.key_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.status_row = status
        group.add_widget(status)
        lay.addWidget(group)

        # 翻訳する内容
        content = FormGroup("翻訳する内容", "図の中の文字・数式・表・著者名はそのまま残します。")
        self.captions = Switch(settings.translate_captions)
        self.footnotes = Switch(settings.translate_footnotes)
        self.references = Switch(not settings.skip_references)
        content.add_row("図表のキャプション", self.captions)
        content.add_row("脚注", self.footnotes)
        content.add_row("参考文献", self.references)
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
            w.setProperty("role", "metric")
        self.estimate_group.add_row("翻訳する段落", self.est_paragraphs)
        self.estimate_group.add_row("文字数", self.est_chars)
        self.estimate_group.add_row("新たに翻訳する文字数", self.est_cost)
        lay.addWidget(self.estimate_group)
        lay.addStretch(1)

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
        if needs_key and not has_key:
            text, t = "API キーが設定されていません。", "warning"
        elif self.settings.engine == "gemini" and MODELS[self.settings.gemini_model].caution:
            text, t = MODELS[self.settings.gemini_model].caution, "warning"
        else:
            text, t = message, tone
        self.key_status.setText(text)
        self.key_status.setVisible(bool(text))
        set_prop(self.key_status, "tone", t)
        self.key_button.setVisible(needs_key and not has_key)
        self.status_row.setVisible(bool(text) or self.key_button.isVisibleTo(self.status_row))
        self.model_row.setEnabled(self.settings.engine == "gemini")

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
            w.setText("…")

    def _refresh_estimate_tone(self) -> None:
        est = self._estimate
        if est is None:
            return
        if est.pending_chars == 0 and est.total_chars:
            self.estimate_group.set_footer("すべて翻訳済みです。すぐに表示できます。", "success")
        else:
            self.estimate_group.set_footer("")

    # -- 変更ハンドラ ---------------------------------------------------------

    def _save(self, reanalyze: bool) -> None:
        self.settings.save()
        self.settingsChanged.emit(reanalyze)

    def _on_engine(self) -> None:
        self.settings.engine = self.engine.currentData()
        self._save(True)

    def _on_model(self) -> None:
        self.settings.gemini_model = self.model.currentData()
        self._save(True)

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
