"""サイドバーの同期の状態の行を押したときに出す小さな画面（docs/SYNC.md §7.3）。事実だけを出し、警告にはしない。"""

from __future__ import annotations

import html

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from ..config.settings import Settings
from .privacy import display_path
from .sync_controller import ERROR, SYNCING, UNAVAILABLE, SyncController
from .sync_settings import candidate_folders, folder_name, format_ago
from .widgets import button, label, link_button, plain_tooltip

NAME_LIMIT = 40  # ほかのパソコンの名前を表示する最大の文字数


class SyncPopover(QFrame):
    settingsRequested = Signal()
    downloadAllRequested = Signal()

    def __init__(self, settings: Settings, sync: SyncController, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("SyncPopover")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(300)
        self.sync = sync
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)
        lay.addWidget(label("同期", "headline"))

        if not sync.enabled():
            state = "一時停止中"
        elif sync.state == SYNCING:
            state = "同期中…"
        elif sync.state in (ERROR, UNAVAILABLE):
            state = "同期できませんでした"
        else:
            state = "保存済み"
        self.state_label = label(f"状態: {state}", "secondary", wrap=True)
        lay.addWidget(self.state_label)
        if sync.state in (ERROR, UNAVAILABLE) and sync.last_error is not None:
            err = sync.last_error
            title = getattr(err, "title", "") or str(err)
            hint = getattr(err, "hint", "")
            detail = label(f"{title}。{hint}" if hint else title, "caption", wrap=True)
            detail.setProperty("tone", "warning")
            lay.addWidget(detail)
        lay.addWidget(label(f"最後に保存: {format_ago(sync.last_saved)}", "caption"))
        dest = label(f"保存先: {folder_name(settings.sync_folder, candidate_folders())}", "caption", wrap=True)
        dest.setToolTip(plain_tooltip(display_path(settings.sync_folder)))
        lay.addWidget(dest)
        # このパソコンがほかのパソコンでどう表示されるか（名前は［設定を開く］で変えられる）
        self.self_label = label(f"このパソコン: {sync.device().name}", "caption", wrap=True)
        self.self_label.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.self_label)
        other = self._last_other_device()
        if other is not None:
            # 端末の名前はほかのパソコンから来た文字列なので、必ずプレーンテキストとして表示する（§8.5）
            full = other.name.strip() or "名前のないパソコン"
            # 名前の長さは制限できないので、表示は省略し、全体はツールチップに出す
            name = full if len(full) <= NAME_LIMIT else full[: NAME_LIMIT - 1] + "…"
            self.other_label = label(f"ほかのパソコンからの最後の変更: {format_ago(other.last_seen)}（{name}）", "caption", wrap=True)
            self.other_label.setTextFormat(Qt.TextFormat.PlainText)
            if name != full:
                # ツールチップは HTML らしい文字列を自動でリッチテキストとして描くので、エスケープして包む
                self.other_label.setToolTip(f"<p style='white-space:pre-wrap'>{html.escape(full)}</p>")
            lay.addWidget(self.other_label)
        if sync.active_downloads():
            lay.addWidget(label(f"取得中: {sync.active_downloads()} 件", "caption"))
        lay.addWidget(label("クラウドへの反映は、同期アプリ（Google Drive など）が行います。", "caption", wrap=True))

        row = QHBoxLayout()
        row.setSpacing(8)
        self.sync_now_btn = button("今すぐ同期")
        self.sync_now_btn.setEnabled(sync.enabled() and sync.state != SYNCING)
        self.sync_now_btn.clicked.connect(self._sync_now)
        row.addWidget(self.sync_now_btn)
        row.addStretch(1)
        lay.addSpacing(4)
        lay.addLayout(row)
        links = QHBoxLayout()
        links.setSpacing(14)
        self.download_all_btn = link_button("このパソコンにすべて取得")
        self.download_all_btn.setEnabled(sync.enabled())
        self.download_all_btn.clicked.connect(self._download_all)
        settings_btn = link_button("設定を開く")
        settings_btn.clicked.connect(self._open_settings)
        links.addWidget(self.download_all_btn)
        links.addWidget(settings_btn)
        links.addStretch(1)
        lay.addLayout(links)

    def _last_other_device(self):
        """いちばん最近同期したほかのパソコン（core が最後の同期のときに求めた値。同期フォルダには触らない）。"""
        engine = self.sync.engine
        if engine is None:
            return None
        try:
            devices = engine.devices()
        except Exception:
            return None
        return next((d for d in devices if not d.is_self), None)

    def show_below(self, anchor: QWidget) -> None:
        self.adjustSize()
        pos = anchor.mapToGlobal(QPoint(0, 0))
        # サイドバーの下端にあるので、行の上に出す
        self.move(pos.x(), pos.y() - self.height() - 4)
        self.show()

    def _sync_now(self) -> None:
        self.sync.sync_now()
        self.close()

    def _download_all(self) -> None:
        self.downloadAllRequested.emit()
        self.close()

    def _open_settings(self) -> None:
        self.close()
        self.settingsRequested.emit()
