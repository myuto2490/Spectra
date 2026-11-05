"""更新の通知の画面部品：初回の問い合わせ、上の帯、バックグラウンドでの確認。

main_window への組み込みは別のブランチで行う。ここでは部品だけを提供する。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import update
from . import icons, theme, worker
from .widgets import button, label, link_button, plain_box, set_prop


def ask_enable_update_check(parent: QWidget | None = None) -> bool:
    """更新の確認をオンにするか、初回に 1 回だけ尋ねる。ウィンドウを閉じたときはオフ（通信しない）として扱う。"""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setText("新しいバージョンを確認しますか？")
    box.setInformativeText(
        "Spectra の新しい版が出ていないか、1 日に 1 回、GitHub に問い合わせます。\n"
        "送るのは通常の HTTPS の要求だけで、ライブラリや端末の情報は送りません（GitHub には IP アドレスが見えます）。\n"
        "設定の「詳細」でいつでも変えられます。"
    )
    yes = box.addButton("確認する", QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton("確認しない", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(yes)
    box.setEscapeButton(no)
    plain_box(box).exec()
    return box.clickedButton() is yes


def open_release_page(url: str) -> bool:
    """リリースのページをブラウザで開く。Spectra のリリース以外の URL は開かない。"""
    if not update.is_release_url(url):
        return False
    return QDesktopServices.openUrl(QUrl(url))


class UpdateBanner(QFrame):
    """「Spectra 0.5.0 が利用できます  [ダウンロード] [あとで] [この版を通知しない]」の帯。"""

    downloadRequested = Signal(str)  # 開く URL
    dismissed = Signal()  # あとで（今回の起動では出さない）
    skipRequested = Signal(str)  # この版を通知しない（版）

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("banner", "info")
        self._info: update.UpdateInfo | None = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 8, 10)
        lay.setSpacing(10)
        self._icon = QLabel()
        self._icon.setFixedSize(20, 20)
        lay.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self._title = label("", "headline", wrap=True)
        self._body = label("", "secondary", wrap=True)
        texts.addWidget(self._title)
        texts.addWidget(self._body)
        lay.addLayout(texts, 1)
        self.download_btn = button("ダウンロード", primary=True)
        self.download_btn.clicked.connect(self._on_download)
        lay.addWidget(self.download_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.later_btn = button("あとで")
        self.later_btn.clicked.connect(self._on_later)
        lay.addWidget(self.later_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.skip_btn = link_button("この版を通知しない")
        self.skip_btn.clicked.connect(self._on_skip)
        lay.addWidget(self.skip_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.hide()

    @property
    def info(self) -> update.UpdateInfo | None:
        return self._info

    def show_update(self, info: update.UpdateInfo, current: str | None = None) -> None:
        from .. import __version__

        self._info = info
        t = theme.current()
        self._icon.setPixmap(icons.pixmap("info", t.accent, 20))
        self._title.setText(f"Spectra {info.version} が利用できます")
        self._body.setText(f"今お使いの版は {current or __version__} です。ダウンロードのページをブラウザで開きます。")
        set_prop(self, "banner", "info")
        self.show()

    def _on_download(self) -> None:
        if self._info is not None:
            self.downloadRequested.emit(self._info.url)
            open_release_page(self._info.url)

    def _on_later(self) -> None:
        self.hide()
        self.dismissed.emit()

    def _on_skip(self) -> None:
        if self._info is not None:
            self.skipRequested.emit(self._info.version)
        self.hide()


def start_check(
    on_found: Callable[[update.UpdateInfo], None],
    on_error: Callable[[BaseException], None] | None = None,
    on_none: Callable[[], None] | None = None,
    skipped: str = "",
    current: str | None = None,
    client: httpx.Client | None = None,
) -> worker.Task:
    """バックグラウンドで更新を確認する。新しい版があれば on_found、なければ on_none、失敗なら on_error。

    起動時の確認では on_error を渡さない（失敗しても何も表示しない）。「今すぐ確認」では渡して表示する。
    """
    from .. import __version__

    def job(_report, _cancel):
        return update.check_for_update(current or __version__, client=client, skipped=skipped)

    def done(result):
        if result is None:
            if on_none:
                on_none()
        else:
            on_found(result)

    return worker.start(job, on_done=done, on_error=on_error)

