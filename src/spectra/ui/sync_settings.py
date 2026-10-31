"""設定の「同期」タブ：同期フォルダの選択・接続のときの説明・同期の状態。

同期フォルダ方式（docs/SYNC.md §7.1・§7.2）。専門用語（同期フォルダ・競合など）は画面に出さない。
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..config.settings import Settings
from . import worker
from .sync_controller import ERROR, OFF, SYNCING, UNAVAILABLE, SyncController, sync_root
from .widgets import FormGroup, Switch, button, label, link_button

CONSENT_TEXT = (
    "同期をオンにすると、ライブラリの論文の PDF（原文・訳文）・書誌情報・コレクションを、選んだフォルダに保存します。"
    "そのフォルダを Google Drive などで同期している場合は、そのサービスにアップロードされ、各サービスの規約に従って扱われます。"
    "Spectra の開発者がこれらのデータを受け取ることはありません。\n\n"
    "未公開の原稿や、共有が禁止されている論文が含まれる場合は、所属先の規則を確認してください。"
    "API キーと設定は同期しません。"
)


def candidate_folders(home: Path | None = None) -> list[tuple[str, Path]]:
    """同期アプリのフォルダの候補（見つかったものだけ）。決まった場所を勝手に使うことはせず、候補として示すだけ。

    ディレクトリの名前を見るだけで、中のファイルは読まない（ストリーミングで読み込みが止まらないように）。
    """
    home = home or Path.home()
    found: list[tuple[str, Path]] = []

    def add(name: str, path: Path) -> None:
        try:
            if path.is_dir() and all(p != path for _, p in found):
                found.append((name, path))
        except OSError:
            pass

    cloud = home / "Library" / "CloudStorage"  # macOS（File Provider）
    try:
        entries = sorted(cloud.iterdir()) if cloud.is_dir() else []
    except OSError:
        entries = []
    for entry in entries:
        n = entry.name
        if n.startswith("GoogleDrive-"):
            account = n.removeprefix("GoogleDrive-")
            for sub in ("マイドライブ", "My Drive"):
                if (entry / sub).is_dir():
                    add(f"Google Drive（{account}）", entry / sub)
                    break
            else:
                add(f"Google Drive（{account}）", entry)
        elif n.startswith("Dropbox"):
            add("Dropbox", entry)
        elif n.startswith("OneDrive"):
            add("OneDrive", entry)
    add("iCloud Drive", home / "Library" / "Mobile Documents" / "com~apple~CloudDocs")
    add("Dropbox", home / "Dropbox")
    add("OneDrive", home / "OneDrive")
    if sys.platform == "win32":
        add("Google Drive", home / "Google Drive")
        add("iCloud Drive", home / "iCloudDrive")
        for letter in "GHIJ":
            drive = Path(f"{letter}:/")
            for sub in ("マイドライブ", "My Drive"):
                add("Google Drive", drive / sub)
    return found


def folder_name(path: str, candidates: list[tuple[str, Path]]) -> str:
    """保存先の短い名前（「Google Drive › Spectra Sync」）。パスはツールチップに出す。"""
    p = Path(path)
    for name, root in candidates:
        try:
            p.relative_to(root)
        except ValueError:
            continue
        rest = p.relative_to(root).parts
        short = name.split("（")[0]
        return " › ".join([short, *rest, "Spectra Sync"])
    return " › ".join([p.name or str(p), "Spectra Sync"])


def is_known_folder(path: Path, candidates: list[tuple[str, Path]]) -> bool:
    for _, root in candidates:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def format_ago(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "まだありません"
    now = now or time.time()
    sec = max(0, now - ts)
    if sec < 60:
        return "たった今"
    if sec < 3600:
        return f"{int(sec // 60)} 分前"
    d, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    if d.date() == today.date():
        return f"今日 {d:%H:%M}"
    return f"{d:%Y/%m/%d %H:%M}"


def folder_size(root: Path) -> int:
    """（ワーカーから）同期フォルダの使用量。ファイルの中身は読まず、大きさの情報だけを見る。"""
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.stat(os.path.join(dirpath, f)).st_size
    return total


def format_size(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} MB"
    return f"{max(1, n // 1024)} KB" if n else "0 MB"


class SyncPage(QScrollArea):
    """設定の「同期」タブ。"""

    syncChanged = Signal()  # 接続・解除・オン/オフを変えた
    restoreBackupRequested = Signal(object)  # Path（同期前の状態に戻す）

    def __init__(self, settings: Settings, sync: SyncController | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.sync = sync
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.candidates = candidate_folders()
        self._size_gen = 0
        body = QWidget()
        self._lay = QVBoxLayout(body)
        self._lay.setContentsMargins(0, 0, 4, 0)
        self._lay.setSpacing(16)
        self.setWidget(body)
        self._built: list[QWidget] = []
        if sync is not None:
            sync.stateChanged.connect(self._refresh_status)
        self.rebuild()

    # -- 構成 ------------------------------------------------------------------

    def connected(self) -> bool:
        return bool(self.settings.sync_folder)

    def rebuild(self) -> None:
        for w in self._built:
            self._lay.removeWidget(w)
            w.hide()
            w.deleteLater()
        self._built = []
        while self._lay.count():
            item = self._lay.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        if self.connected():
            self._build_connected()
        else:
            self._build_choose()
        self._lay.addStretch(1)

    def _add(self, w: QWidget) -> None:
        self._built.append(w)
        self._lay.addWidget(w)

    def _build_choose(self) -> None:
        group = FormGroup(
            "別のパソコンと同期",
            "PDF と書誌情報を保存先に置きます。API キーと設定は同期しません。",
        )
        intro = label("ライブラリ・原文・訳文を、ほかのパソコンの Spectra でも開けるようにします。", "secondary", wrap=True)
        group.add_widget(intro)
        self.choose_buttons: dict[str, Path] = {}
        for name, path in self.candidates:
            b = button("このフォルダを使う")
            b.setAccessibleName(f"{name}を使う")
            b.clicked.connect(lambda _c=False, p=path: self.connect_folder(p))
            row = group.add_row(name, b)
            row.setToolTip(str(path))
            self.choose_buttons[name] = path
        other = link_button("ほかのフォルダを選択…")
        other.clicked.connect(self._choose_other)
        group.add_widget(other)
        self._add(group)
        self._add(
            label(
                "Google Drive・Dropbox・OneDrive・iCloud Drive などのアプリで同期しているフォルダを選んでください。"
                "Linux では Dropbox・Syncthing・rclone などが使えます。",
                "caption",
                wrap=True,
            )
        )

    def _build_connected(self) -> None:
        group = FormGroup("同期")
        dest = QWidget()
        dl = QHBoxLayout(dest)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(8)
        self.dest_label = label(folder_name(self.settings.sync_folder, self.candidates), "secondary")
        self.dest_label.setToolTip(str(sync_root(self.settings)))
        change = button("変更…")
        change.clicked.connect(self._choose_other)
        dl.addWidget(self.dest_label)
        dl.addWidget(change)
        group.add_row("保存先", dest)
        self.enabled_switch = Switch(self.settings.sync_enabled)
        self.enabled_switch.toggled.connect(self._on_toggle)
        group.add_row("同期する", self.enabled_switch)
        saved = QWidget()
        sl = QHBoxLayout(saved)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(8)
        self.saved_label = label("", "secondary")
        self.sync_now_btn = button("今すぐ同期")
        self.sync_now_btn.clicked.connect(lambda: self.sync and self.sync.sync_now())
        sl.addWidget(self.saved_label)
        sl.addWidget(self.sync_now_btn)
        group.add_row("最後に保存", saved)
        self.usage_label = label("…", "secondary")
        group.add_row("使用量", self.usage_label)
        self._add(group)
        # 状態の説明（同期中・失敗の理由など）。空のときに空の行が残らないよう、カードの外に置く
        self.status_text = label("", "caption", wrap=True)
        self.status_text.setContentsMargins(4, 0, 4, 0)
        self._add(self.status_text)
        self._add(
            label(
                "オフラインでも読みたい場合は、同期アプリでこのフォルダを「オフラインで使用可能」にしてください。",
                "caption",
                wrap=True,
            )
        )
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(4, 0, 0, 0)
        disconnect = link_button("接続を解除…")
        disconnect.clicked.connect(self.disconnect)
        rl.addWidget(disconnect)
        # 初回の同期の前に自動で作ったバックアップがあれば、目立たない場所に一度だけ出す
        self.restore_btn = link_button("同期前の状態に戻す…")
        self.restore_btn.clicked.connect(self.restore_backup)
        self.restore_btn.setVisible(bool(self.sync and self.sync.backup_path()))
        rl.addSpacing(16)
        rl.addWidget(self.restore_btn)
        rl.addStretch(1)
        self._add(row)
        self._refresh_status()
        self._refresh_usage()

    # -- 表示の更新 ----------------------------------------------------------

    def _refresh_status(self) -> None:
        if not self.connected() or not hasattr(self, "saved_label"):
            return
        try:
            s = self.sync
            self.saved_label.setText(format_ago(s.last_saved) if s else "—")
            self.sync_now_btn.setEnabled(bool(s and s.enabled() and s.state != SYNCING))
            if s is None or s.state == OFF:
                text = "同期を一時停止しています。" if not self.settings.sync_enabled else ""
            elif s.state == SYNCING:
                text = "同期しています…"
            elif s.state in (ERROR, UNAVAILABLE):
                err = s.last_error
                title = getattr(err, "title", "") or "同期できませんでした"
                hint = getattr(err, "hint", "")
                text = f"{title}。{hint}" if hint else title
            else:
                text = ""
            self.status_text.setText(text)
            self.status_text.setVisible(bool(text))
        except RuntimeError:
            pass  # 画面が閉じられた後

    def _refresh_usage(self) -> None:
        self._size_gen += 1
        gen = self._size_gen
        root = sync_root(self.settings)

        def done(n):
            if gen == self._size_gen:
                with contextlib.suppress(RuntimeError):
                    self.usage_label.setText(format_size(n))

        worker.start(lambda _r, _c: folder_size(root) if root.is_dir() else 0, done, lambda _e: None)

    # -- 操作 ------------------------------------------------------------------

    def _choose_other(self) -> None:
        start = self.settings.sync_folder or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "同期に使うフォルダを選択", start)
        if path:
            self.connect_folder(Path(path))

    def confirm_consent(self) -> bool:
        """同期を始める前の説明（§7.2）。接続するときに出す。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("同期を始める前に")
        box.setInformativeText(CONSENT_TEXT)
        start = box.addButton("同期を始める", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(start)
        box.exec()
        return box.clickedButton() is start

    def connect_folder(self, folder: Path) -> bool:
        folder = Path(folder)
        if folder.name == "Spectra Sync":  # 「Spectra Sync」そのものを選んだ場合は、その親を同期フォルダとする
            folder = folder.parent
        if not is_known_folder(folder, self.candidates):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText("このフォルダは、ほかのパソコンと同期されていない可能性があります")
            box.setInformativeText("Google Drive などのアプリで同期しているフォルダを選ぶと、ほかのパソコンでも開けます。このまま使いますか？")
            use = box.addButton("このまま使う", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not use:
                return False
        if not self.connected() and not self.confirm_consent():
            return False
        try:
            (folder / "Spectra Sync").mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "同期", f"フォルダを作成できませんでした。\n{e}")
            return False
        self.settings.sync_folder = str(folder)
        self.settings.sync_enabled = True
        self.settings.save()
        self.syncChanged.emit()
        self.rebuild()
        return True

    def _on_toggle(self, on: bool) -> None:
        self.settings.sync_enabled = on
        self.settings.save()
        self.syncChanged.emit()
        self._refresh_status()

    def restore_backup(self) -> None:
        path = self.sync.backup_path() if self.sync else None
        if path is None:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("同期を始める前のライブラリに戻しますか？")
        box.setInformativeText(
            "同期はオフになります。同期を始めてから追加・編集した内容は、このパソコンでは失われます（同期先のデータは消えません）。"
            "その間に完全に削除した論文は、PDF が戻らないことがあります。"
        )
        restore = box.addButton("戻す", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is restore:
            self.restoreBackupRequested.emit(path)
            self.rebuild()

    def disconnect(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("同期をやめますか？")
        box.setInformativeText("このパソコンのライブラリはそのまま残り、同期先のデータも削除しません。")
        stop = box.addButton("同期をやめる", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not stop:
            return
        self.settings.sync_enabled = False
        self.settings.sync_folder = ""
        self.settings.save()
        self.syncChanged.emit()
        self.rebuild()
