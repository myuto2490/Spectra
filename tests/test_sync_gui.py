"""同期の画面のテスト（オフスクリーン・ネットワーク不要）。docs/SYNC.md §7・§10。

FakeEngine は fetch だけを差し替え（取得の成功・失敗・到着待ちを思いどおりに起こすため）、sync_now は本物
（FakeFolderStorage に実際に書き出し・取り込みをする）。ほかのパソコンの変更の通知（RemoteChange）は _notify で再現する。
後半の 2 台のテストは、fetch も含めてすべて本物の SyncEngine を使う。
"""

import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from spectra.library.errors import RemoteMissing  # noqa: E402
from spectra.library.sync import DeviceInfo, RemoteChange, SyncEngine  # noqa: E402
from spectra.library.sync.testing import FakeFolderStorage  # noqa: E402


class FakeEngine(SyncEngine):
    """fetch だけを差し替える。stash にある内容を手元に書き、fail が設定されていればその例外を出す。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stash: dict[int, bytes] = {}
        self.fail: BaseException | None = None
        self.gate = threading.Event()
        self.gate.set()
        self.fetched: list[tuple[int, str]] = []

    def fetch(self, paper_id, kind, progress=None, cancel=None):
        # core の約束に合わせる: 不正な kind は ValueError、論文が無ければ PaperNotFound、手元にあればすぐ返す、
        # 同期フォルダに触る前に cancel を見る、成功したら "file" の RemoteChange を出す
        from spectra.library.errors import PaperNotFound, SyncUnavailable
        from spectra.translate.errors import CancelledError

        if kind not in ("original", "translation"):
            raise ValueError(kind)
        paper = self.library.get(paper_id)
        if paper is None:
            raise PaperNotFound()
        local = paper.pdf_path if kind == "original" else paper.translated_path
        if (paper.has_original if kind == "original" else paper.has_translation_file):
            return local
        if cancel is not None and cancel.is_set():
            raise CancelledError
        if not self.storage.available():
            raise SyncUnavailable()
        if kind == "translation" and paper.translation is None:
            raise RemoteMissing()
        while not self.gate.wait(0.05):
            if cancel is not None and cancel.is_set():
                raise CancelledError
        if self.fail is not None:
            raise self.fail
        data = self.stash[paper_id]
        if progress:
            progress(len(data) // 2, len(data))
            progress(len(data), len(data))
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        self.fetched.append((paper_id, kind))
        self._notify([RemoteChange("file", paper.sha256, paper.id)])  # ワーカーのスレッドから（core と同じ）
        return local


@pytest.fixture()
def cloud(tmp_path):
    return tmp_path / "cloud"


@pytest.fixture()
def make_window(tmp_path, monkeypatch, cloud):
    monkeypatch.setenv("SPECTRA_DEV", "1")
    from spectra.config.secrets import SecretStore
    from spectra.config.settings import Settings
    from spectra.ui.app import create_app
    from spectra.ui.main_window import MainWindow

    app = create_app([])
    made = []

    def make(enabled=True):
        engines = []

        def factory(settings, library):
            e = FakeEngine(library, FakeFolderStorage(cloud / "Spectra Sync"), DeviceInfo("a" * 32, "テスト"))
            engines.append(e)
            return e

        settings = Settings(engine="dummy", metadata_lookup=False, sync_enabled=enabled, sync_folder=str(cloud) if enabled else "")
        w = MainWindow(settings=settings, secrets=SecretStore(use_keyring=False), sync_engine_factory=factory)
        w.show()
        made.append(w)
        w.engines = engines
        return app, w

    yield make
    for w in made:
        w.queue.cancel_all()
        pump(app, 10, until=lambda w=w: not w._busy())
        w.close()
        w.library.close()
        _dispose(app, w)


def _dispose(app, w):
    """閉じた窓を実際に破棄する（残すと、次のテストの create_app でスタイルを当て直すたびに古いウィジェットも
    すべて作り直され、テストがだんだん遅くなる）。"""
    from PySide6.QtCore import QCoreApplication, QEvent

    w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def pump(app, seconds=0.2, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def dialogs(monkeypatch):
    """QMessageBox を表示せず、肯定の（削除・同期を始めるなどの）ボタンを押したことにして、文言を記録する。"""
    from PySide6.QtWidgets import QMessageBox

    shown = []
    accept_roles = (QMessageBox.ButtonRole.DestructiveRole, QMessageBox.ButtonRole.AcceptRole)

    def exec_(self):
        shown.append(self.text() + "\n" + self.informativeText())
        return 0

    def clicked(self):
        return next((b for b in self.buttons() if self.buttonRole(b) in accept_roles), None)

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    monkeypatch.setattr(QMessageBox, "clickedButton", clicked)
    return shown


def _paper(tmp_path, name, title):
    from pdf_factory import make_paper

    doc = make_paper()
    doc.set_metadata({"title": title})
    path = tmp_path / name
    doc.save(path)
    return path


def _evict(w, paper_id):
    """手元のコピーだけを消した状態（ほかのパソコンから同期された論文と同じ）を作る。"""
    paper = w.library.get(paper_id)
    data = paper.pdf_path.read_bytes()
    paper.pdf_path.unlink()
    w.library_view.refresh_paper(paper_id)
    return data


def _title_index(w, paper_id):
    from spectra.ui.library_model import COL, PaperIdRole

    m = w.library_view.model
    for row in range(m.rowCount()):
        idx = m.index(row, COL["title"])
        if idx.data(PaperIdRole) == paper_id:
            return idx
    raise AssertionError("paper not in model")


# ---------------------------------------------------------------------------------
# 状態の行
# ---------------------------------------------------------------------------------


def test_status_row_is_hidden_when_sync_is_off(make_window):
    app, w = make_window(enabled=False)
    pump(app)
    assert not w.library_view.sync_status.isVisible()
    assert not w.sync.enabled()


def test_status_row_states(make_window, cloud):
    (cloud / "Spectra Sync").mkdir(parents=True)
    app, w = make_window()
    assert pump(app, 5, until=lambda: w.library_view.sync_status.text().startswith("保存済み"))
    assert w.library_view.sync_status.isVisible()
    assert "同期の状態" in w.library_view.sync_status.accessibleName()

    # 同期フォルダが読めない
    w.engines[-1].storage.offline = True
    w.sync.sync_now()
    assert pump(app, 5, until=lambda: w.library_view.sync_status.text() == "同期できませんでした")
    assert w.library_view.sync_status.property("tone") == "warning"

    # 一時停止
    w.settings.sync_enabled = False
    w._on_sync_setting()
    assert w.library_view.sync_status.text() == "同期を一時停止中"


def test_popover_shows_facts_without_warning(make_window, cloud):
    (cloud / "Spectra Sync").mkdir(parents=True)
    app, w = make_window()
    pump(app, 2, until=lambda: w.sync.last_saved is not None)
    from spectra.ui.sync_popover import SyncPopover

    pop = SyncPopover(w.settings, w.sync, w)
    texts = [lb.text() for lb in pop.findChildren(type(pop.state_label))]
    assert any("状態: 保存済み" in t for t in texts)
    assert any("同期アプリ" in t for t in texts)
    assert pop.sync_now_btn.isEnabled()
    pop.close()


# ---------------------------------------------------------------------------------
# 手元にない論文
# ---------------------------------------------------------------------------------


def test_missing_paper_shows_cloud_and_opens_after_download(make_window, tmp_path, cloud):
    (cloud / "Spectra Sync").mkdir(parents=True)
    app, w = make_window()
    paper = w.library.add(_paper(tmp_path, "a.pdf", "Alpha"))[0]
    w.library_view.reload()
    data = _evict(w, paper.id)
    w.engines[-1].stash[paper.id] = data

    idx = _title_index(w, paper.id)
    from PySide6.QtCore import Qt

    assert idx.data(Qt.ItemDataRole.DecorationRole) is not None
    assert "このパソコンにまだありません" in idx.data(Qt.ItemDataRole.AccessibleTextRole)
    assert "このパソコンにまだありません" in idx.data(Qt.ItemDataRole.ToolTipRole)

    # どの入口から開いても、取得してから開く（ここでは一覧から開くのと同じ open_paper）
    w.engines[-1].gate.clear()
    assert w.open_paper(paper.id) is False
    assert w.sync.download_state(paper.id) is not None
    w.library_view.select_ids([paper.id])
    pump(app, 0.3)
    assert w.library_view.detail.fetch_box.isVisible()
    assert not w.library_view.detail.read_btn.isEnabled()
    assert not w.library_view.detail.translate_btn.isEnabled()
    assert w.library_view.detail.tr_blocked.text() == "取得が終わると翻訳できます"
    assert "取得中" in w.library_view.sync_status.text()
    w.engines[-1].gate.set()
    assert pump(app, 10, until=lambda: w.screen_mode == "reader")
    assert w.paper.id == paper.id and w.original_doc is not None
    assert w.library.get(paper.id).has_original
    w.show_library()
    assert _title_index(w, paper.id).data(Qt.ItemDataRole.DecorationRole) is None


def test_download_waits_when_file_has_not_arrived(make_window, tmp_path, cloud):
    (cloud / "Spectra Sync").mkdir(parents=True)
    app, w = make_window()
    paper = w.library.add(_paper(tmp_path, "a.pdf", "Alpha"))[0]
    w.library_view.reload()
    _evict(w, paper.id)
    w.engines[-1].fail = RemoteMissing()
    w.open_paper(paper.id)
    assert pump(app, 5, until=lambda: (d := w.sync.download_state(paper.id)) is not None and d.state == "waiting")
    from PySide6.QtCore import Qt

    assert "届くのを待っています" in _title_index(w, paper.id).data(Qt.ItemDataRole.AccessibleTextRole)
    w.library_view.select_ids([paper.id])
    pump(app, 0.1)
    detail = w.library_view.detail
    assert detail.fetch_retry.isVisible() and "まだ同期されていません" in detail.fetch_text.text()
    assert w.screen_mode == "library"


def test_offline_blocks_only_papers_without_a_local_copy(make_window, tmp_path, cloud):
    (cloud / "Spectra Sync").mkdir(parents=True)
    app, w = make_window()
    local = w.library.add(_paper(tmp_path, "a.pdf", "Local paper"))[0]
    remote = w.library.add(_paper(tmp_path, "b.pdf", "Remote paper"))[0]
    w.library_view.reload()
    _evict(w, remote.id)
    w.engines[-1].storage.offline = True
    w.sync.sync_now()
    assert pump(app, 5, until=lambda: not w.sync.available())

    detail = w.library_view.detail
    w.library_view.select_ids([remote.id])
    pump(app, 0.1)
    assert not detail.read_btn.isEnabled() and not detail.translate_btn.isEnabled()
    assert "同期先" in detail.read_btn.toolTip()
    w.library_view.select_ids([local.id])
    pump(app, 0.1)
    assert detail.read_btn.isEnabled() and detail.translate_btn.isEnabled()
    assert w.open_paper(local.id)  # 手元にある論文は、同期先が読めなくても開ける


def test_translating_a_missing_paper_downloads_first(make_window, tmp_path, cloud):
    (cloud / "Spectra Sync").mkdir(parents=True)
    app, w = make_window()
    paper = w.library.add(_paper(tmp_path, "a.pdf", "Alpha"))[0]
    w.library_view.reload()
    w.engines[-1].stash[paper.id] = _evict(w, paper.id)
    w.translate_papers([paper.id])
    assert pump(app, 60, until=lambda: not w._busy())
    p = w.library.get(paper.id)
    assert p.has_original and p.translation is not None
    assert (paper.id, "original") in w.engines[-1].fetched


# ---------------------------------------------------------------------------------
# ゴミ箱と削除
# ---------------------------------------------------------------------------------


def test_trash_restore_and_purge(make_window, tmp_path, dialogs):
    app, w = make_window(enabled=False)
    paper = w.library.add(_paper(tmp_path, "a.pdf", "Alpha"))[0]
    other = w.library.add(_paper(tmp_path, "b.pdf", "Beta"))[0]
    view = w.library_view
    view.reload()

    w.remove_papers([paper.id])
    assert "30 日以内ならゴミ箱から戻せます" in dialogs[-1]
    assert "すべてのパソコン" not in dialogs[-1]  # 同期がオフのとき
    assert w.library.get(paper.id).trashed_at is not None
    assert view.proxy.rowCount() == 1  # すべての論文には出ない
    assert view.sidebar.trash_item.text() == "ゴミ箱 1"

    view.sidebar.select_scope("trash")
    pump(app, 0.05)
    assert view.proxy.rowCount() == 1 and view.trash_bar.isVisible()
    from spectra.ui.library_model import COL

    assert view.model.headerData(COL["added"], __import__("PySide6.QtCore").QtCore.Qt.Orientation.Horizontal) == "削除した日"
    view.select_ids([paper.id])
    pump(app, 0.05)
    detail = view.detail
    from spectra.ui.paper_detail import TRASH_PAGE

    assert detail.pages.currentIndex() == TRASH_PAGE and not detail.tabs.isVisible()
    assert "あと 30 日" in detail.trash_left.text()
    assert w.open_paper(paper.id) is False  # ゴミ箱の論文は開かない

    detail.restore_btn.click()
    assert w.library.get(paper.id).trashed_at is None
    assert view.proxy.rowCount() == 0 and view.sidebar.trash_item.text() == "ゴミ箱"

    w.remove_papers([paper.id, other.id])
    view.select_ids([paper.id])
    w.purge_papers([paper.id])
    assert "取り消せません" in dialogs[-1]
    assert w.library.get(paper.id) is None
    w.empty_trash()
    assert "ゴミ箱の 1 件を完全に削除しますか？" in dialogs[-1]
    assert w.library.trashed_papers() == []
    pump(app, 0.05)
    assert view.center.currentWidget() is view.no_match and view.no_match_text.text() == "ゴミ箱は空です"
    view.sidebar.select_scope("all")
    pump(app, 0.05)
    assert view.center.currentWidget() is view.empty  # 論文が 1 本もなくなった


def test_delete_wording_mentions_all_computers_when_syncing(make_window, tmp_path, dialogs):
    app, w = make_window()
    paper = w.library.add(_paper(tmp_path, "a.pdf", "Alpha"))[0]
    w.library_view.reload()
    w.remove_papers([paper.id])
    assert "すべてのパソコンから削除されます" in dialogs[-1] and "30 日以内" in dialogs[-1]


# ---------------------------------------------------------------------------------
# ほかのパソコンの変更
# ---------------------------------------------------------------------------------


def _notify_from_thread(engine, changes):
    t = threading.Thread(target=lambda: engine._notify(changes))
    t.start()
    t.join(2)


def test_remote_translation_shows_banner_instead_of_replacing(make_window, tmp_path):
    app, w = make_window()
    paper = w.library.add(_paper(tmp_path, "a.pdf", "Alpha"))[0]
    w.library_view.reload()
    assert w.open_paper(paper.id)
    _notify_from_thread(w.engines[-1], [RemoteChange("translation", paper.sha256, paper.id)])
    assert pump(app, 2, until=lambda: w.banner.isVisible())
    assert "別のパソコンで新しい訳文が作られました" in w.banner._title.text()
    assert w.banner._action.text() == "開き直す"
    assert w.screen_mode == "reader"


def test_remote_delete_of_open_paper_returns_to_library(make_window, tmp_path):
    app, w = make_window()
    paper = w.library.add(_paper(tmp_path, "a.pdf", "Alpha"))[0]
    w.library_view.reload()
    assert w.open_paper(paper.id)
    w.library.trash(paper.id)  # 反映による削除（core が行う）
    _notify_from_thread(w.engines[-1], [RemoteChange("removed", paper.sha256, paper.id)])
    assert pump(app, 2, until=lambda: w.screen_mode == "library")
    assert "別のパソコンで削除されました" in w.banner._title.text()
    assert w.library_view.proxy.rowCount() == 0


def test_remote_delete_removes_queued_translation(make_window, tmp_path):
    app, w = make_window()
    a = w.library.add(_paper(tmp_path, "a.pdf", "Alpha"))[0]
    b = w.library.add(_paper(tmp_path, "b.pdf", "Beta"))[0]
    w.library_view.reload()
    gate = threading.Event()

    def runner(req, report, cancel):
        gate.wait(10)
        from spectra.translate.errors import CancelledError

        raise CancelledError

    w.queue._runner = runner
    w.translate_papers([a.id, b.id])
    assert w.queue.state(b.id) == "queued"
    w.library.trash(b.id)
    _notify_from_thread(w.engines[-1], [RemoteChange("removed", b.sha256, b.id)])
    assert pump(app, 2, until=lambda: w.queue.state(b.id) is None)
    gate.set()
    assert pump(app, 10, until=lambda: not w._busy())


def test_translation_worker_treats_paper_not_found_as_cancel(make_window, tmp_path, monkeypatch):
    """翻訳中に別のパソコンで削除された（save_translation が PaperNotFound）ときは、エラーのダイアログを出さない。"""
    app, w = make_window()
    paper = w.library.add(_paper(tmp_path, "a.pdf", "Alpha"))[0]
    w.library_view.reload()
    from spectra.library.errors import PaperNotFound

    def gone(*_a, **_k):
        raise PaperNotFound()

    monkeypatch.setattr(w.library, "save_translation", gone)
    alerts = []
    monkeypatch.setattr(w, "_alert", lambda title, body="": alerts.append(title))
    w.translate_papers([paper.id])
    assert pump(app, 60, until=lambda: not w._busy())
    assert alerts == [] and "キャンセルしました" in w.status.text()


# ---------------------------------------------------------------------------------
# 設定の「同期」タブ
# ---------------------------------------------------------------------------------


def test_sync_tab_connect_and_disconnect(make_window, tmp_path, dialogs, monkeypatch):
    app, w = make_window(enabled=False)
    from spectra.ui.settings_dialog import SettingsDialog

    d = SettingsDialog(w.settings, w.secrets, w.cache, w, sync=w.sync)
    d.syncChanged.connect(w._on_sync_setting)
    page = d.sync_page
    folder = tmp_path / "Dropbox"
    folder.mkdir()
    page.candidates = [("Dropbox", folder)]
    assert page.connect_folder(folder)
    assert any("同期を始める前に" in t and "API キーと設定は同期しません" in t for t in dialogs)
    assert (folder / "Spectra Sync").is_dir()
    assert w.settings.sync_enabled and w.settings.sync_folder == str(folder)
    assert w.sync.enabled()
    assert pump(app, 3, until=lambda: w.library_view.sync_status.isVisible())
    assert page.dest_label.text() == "Dropbox › Spectra Sync"

    page.disconnect()
    assert "同期先のデータも削除しません" in dialogs[-1]
    assert not w.settings.sync_enabled and w.settings.sync_folder == ""
    assert not w.sync.enabled() and not w.library_view.sync_status.isVisible()
    d.close()


def test_unknown_folder_asks_before_use(make_window, tmp_path, dialogs):
    app, w = make_window(enabled=False)
    from spectra.ui.sync_settings import SyncPage

    page = SyncPage(w.settings, w.sync)
    page.candidates = []
    assert page.connect_folder(tmp_path / "plain")
    assert any("同期されていない可能性があります" in t for t in dialogs)


def test_candidate_folders_and_names(tmp_path):
    from spectra.ui.sync_settings import candidate_folders, folder_name, format_ago

    gd = tmp_path / "Library" / "CloudStorage" / "GoogleDrive-me@example.com" / "マイドライブ"
    gd.mkdir(parents=True)
    (tmp_path / "Dropbox").mkdir()
    found = candidate_folders(tmp_path)
    assert ("Google Drive（me@example.com）", gd) in found
    assert any(name == "Dropbox" for name, _ in found)
    assert folder_name(str(gd / "論文"), found) == "Google Drive › 論文 › Spectra Sync"
    assert format_ago(time.time() - 120) == "2 分前"
    assert format_ago(None) == "まだありません"


def test_empty_library_offers_sync_setup(make_window, monkeypatch):
    app, w = make_window(enabled=False)
    opened = []
    monkeypatch.setattr(w, "show_settings", lambda tab="engines": opened.append(tab))
    w.library_view.syncSetupRequested.disconnect()
    w.library_view.syncSetupRequested.connect(lambda: w.show_settings("sync"))
    pump(app)
    assert w.library_view.empty_sync_btn.isVisible()
    w.library_view.empty_sync_btn.click()
    assert opened == ["sync"]


def test_first_sync_banner_counts_papers(make_window, tmp_path, cloud):
    (cloud / "Spectra Sync").mkdir(parents=True)
    app, w = make_window(enabled=False)
    w.library.add(_paper(tmp_path, "a.pdf", "Alpha"))
    w.library_view.reload()
    w._first_sync_count = 1
    w.library.add(_paper(tmp_path, "b.pdf", "Beta"))  # 同期で届いた論文の代わり
    from spectra.library.sync import SyncReport

    w._on_synced(SyncReport(imported=1))
    assert "ライブラリを同期しました（論文 2 件）" in w.banner._title.text()
    assert "1 件と合わせて 2 件" in w.banner._body.text()
    assert Path(cloud).exists()


# ---------------------------------------------------------------------------------
# 本物の同期（core の本体）で、2 台のパソコンを再現する
# ---------------------------------------------------------------------------------


@pytest.fixture()
def two_computers(tmp_path, monkeypatch, cloud):
    """パソコン A はライブラリと SyncEngine だけ、パソコン B は Spectra の画面。同じ同期フォルダを使う。"""
    monkeypatch.setenv("SPECTRA_DEV", "1")
    from spectra.config.secrets import SecretStore
    from spectra.config.settings import Settings
    from spectra.library import Library
    from spectra.ui.app import create_app
    from spectra.ui.main_window import MainWindow

    root = cloud / "Spectra Sync"
    root.mkdir(parents=True)
    lib_a = Library(tmp_path / "A" / "library")
    engine_a = SyncEngine(lib_a, FakeFolderStorage(root), DeviceInfo("1" * 32, "MacBook Pro"))

    app = create_app([])

    def factory(settings, library):
        return SyncEngine(library, FakeFolderStorage(root), DeviceInfo("2" * 32, "Windows"))

    settings = Settings(engine="dummy", metadata_lookup=False, sync_enabled=True, sync_folder=str(cloud))
    w = MainWindow(settings=settings, secrets=SecretStore(use_keyring=False), sync_engine_factory=factory)
    w.show()
    yield app, lib_a, engine_a, w
    w.queue.cancel_all()
    pump(app, 10, until=lambda: not w._busy())
    w.close()
    w.library.close()
    _dispose(app, w)
    engine_a.close()
    lib_a.close()


def _sync_b(app, w):
    done = []
    w.sync.synced.connect(lambda r: done.append(r))
    w.sync.sync_now()
    assert pump(app, 10, until=lambda: bool(done)), "同期が終わらない"
    pump(app, 0.2)
    return done[-1]


def test_real_sync_shows_paper_from_other_computer_and_opens_it(two_computers, tmp_path):
    app, lib_a, engine_a, w = two_computers
    a_paper = lib_a.add(_paper(tmp_path, "a.pdf", "Shared paper"))[0]
    engine_a.sync_now()

    _sync_b(app, w)
    papers = w.library.papers()
    assert [p.sha256 for p in papers] == [a_paper.sha256]
    b_paper = papers[0]
    assert not b_paper.has_original  # 書誌情報だけが届き、PDF は開いたときに取得する
    from PySide6.QtCore import Qt

    assert "このパソコンにまだありません" in _title_index(w, b_paper.id).data(Qt.ItemDataRole.AccessibleTextRole)

    assert w.open_paper(b_paper.id) is False
    assert pump(app, 10, until=lambda: w.screen_mode == "reader")
    assert w.library.get(b_paper.id).has_original and w.original_doc is not None


def test_real_sync_delete_on_other_computer_closes_reader(two_computers, tmp_path):
    app, lib_a, engine_a, w = two_computers
    a_paper = lib_a.add(_paper(tmp_path, "a.pdf", "Shared paper"))[0]
    engine_a.sync_now()
    _sync_b(app, w)
    b_id = w.library.papers()[0].id
    w.open_paper(b_id)
    assert pump(app, 10, until=lambda: w.screen_mode == "reader")

    lib_a.trash(a_paper.id)
    engine_a.sync_now()
    _sync_b(app, w)
    assert pump(app, 2, until=lambda: w.screen_mode == "library")
    assert "別のパソコンで削除されました" in w.banner._title.text()
    assert w.library.get(b_id).trashed_at is not None  # こちらでもゴミ箱に入る（30 日は戻せる）
    assert w.library_view.sidebar.trash_item.text() == "ゴミ箱 1"


def test_real_sync_edit_on_this_computer_reaches_other(two_computers, tmp_path):
    app, lib_a, engine_a, w = two_computers
    b_paper = w.library.add(_paper(tmp_path, "b.pdf", "From B"))[0]
    w.library_view.reload()
    w.library_view.toggle_favorite([b_paper.id])
    _sync_b(app, w)
    report = engine_a.sync_now()
    a_papers = lib_a.papers()
    assert [p.sha256 for p in a_papers] == [b_paper.sha256]
    assert a_papers[0].favorite
    assert any(c.kind == "added" for c in report.changes)


def test_restore_to_before_first_sync(tmp_path, monkeypatch, cloud, dialogs):
    """同期をオンにした時点で手元に論文があれば、core が初回の同期の前にバックアップを作り、それに戻せる。"""
    monkeypatch.setenv("SPECTRA_DEV", "1")
    from spectra.config import paths
    from spectra.config.secrets import SecretStore
    from spectra.config.settings import Settings
    from spectra.library import Library
    from spectra.ui.app import create_app
    from spectra.ui.main_window import MainWindow

    root = cloud / "Spectra Sync"
    root.mkdir(parents=True)
    mine = Library(paths.library_dir())  # 同期を始める前から、このパソコンにある論文
    mine.add(_paper(tmp_path, "mine.pdf", "Mine"))
    mine.close()
    lib_a = Library(tmp_path / "A" / "library")
    engine_a = SyncEngine(lib_a, FakeFolderStorage(root), DeviceInfo("1" * 32, "MacBook Pro"))
    lib_a.add(_paper(tmp_path, "theirs.pdf", "Theirs"))
    engine_a.sync_now()

    app = create_app([])
    settings = Settings(engine="dummy", metadata_lookup=False, sync_enabled=True, sync_folder=str(cloud))
    w = MainWindow(
        settings=settings,
        secrets=SecretStore(use_keyring=False),
        sync_engine_factory=lambda s, lib: SyncEngine(lib, FakeFolderStorage(root), DeviceInfo("2" * 32, "Windows")),
    )
    w.show()
    try:
        assert pump(app, 10, until=lambda: len(w.library.papers()) == 2)
        assert w.sync.backup_path() is not None

        from spectra.ui.sync_settings import SyncPage

        page = SyncPage(w.settings, w.sync)
        page.restoreBackupRequested.connect(w.restore_backup)
        assert page.restore_btn.isVisibleTo(page)
        page.restore_backup()
        assert "同期を始める前のライブラリに戻しますか？" in dialogs[-1] and "同期先のデータは消えません" in dialogs[-1]
        assert [p.file_name for p in w.library.papers()] == ["mine.pdf"]
        assert not w.settings.sync_enabled and not w.sync.enabled()
        assert w.library_view.library is w.library and w.sync.library is w.library
        assert "同期を始める前のライブラリに戻しました" in w.banner._title.text()
        w.library_view.toggle_favorite([w.library.papers()[0].id])  # 開き直したライブラリも使える
    finally:
        w.close()
        w.library.close()
        _dispose(app, w)
        engine_a.close()
        lib_a.close()


def test_pausing_during_a_sync_closes_the_engine_after_it_finishes(make_window, cloud):
    """同期の途中で一時停止しても、同期の記録を書いている最中にエンジンを閉じない。"""
    (cloud / "Spectra Sync").mkdir(parents=True)
    app, w = make_window()
    pump(app, 5, until=lambda: not w.sync._running)
    engine = w.engines[-1]
    closed = []
    original_close = engine.close
    engine.close = lambda: (closed.append(w.sync._sync_done.is_set()), original_close())
    gate = threading.Event()
    original_sync = engine.sync_now
    engine.sync_now = lambda cancel=None: (gate.wait(5), original_sync(cancel))[1]
    w.sync.sync_now()
    assert w.sync._running
    w.settings.sync_enabled = False
    w._on_sync_setting()  # 同期の途中で止める
    assert closed == []  # まだ閉じない
    gate.set()
    assert pump(app, 5, until=lambda: bool(closed))
    assert closed == [True]  # 同期が終わってから閉じた
    assert w.library_view.sync_status.text() == "同期を一時停止中"


def test_queued_download_of_a_paper_deleted_elsewhere_ends_quietly(make_window, tmp_path, cloud):
    """取得を待っている間に、別のパソコンで完全に削除された論文は、エラーを出さずに取得の待ち行列から消える。"""
    (cloud / "Spectra Sync").mkdir(parents=True)
    app, w = make_window()
    a = w.library.add(_paper(tmp_path, "a.pdf", "Alpha"))[0]
    b = w.library.add(_paper(tmp_path, "b.pdf", "Beta"))[0]
    w.library_view.reload()
    engine = w.engines[-1]
    engine.stash[a.id] = _evict(w, a.id)
    engine.stash[b.id] = _evict(w, b.id)
    engine.gate.clear()
    w.sync.download(a.id, "keep")
    w.sync.download(b.id, "keep")
    assert w.sync.download_state(b.id).state == "queued"
    failures = []
    w.sync.downloadFailed.connect(lambda pid, err: failures.append(err))
    w.library.purge(b.id)  # 反映による完全な削除（core が行う）
    engine.gate.set()
    assert pump(app, 10, until=lambda: w.sync.active_downloads() == 0)
    assert w.sync.download_state(b.id) is None and failures == []
    assert w.library.get(a.id).has_original
    # 取得が終わった "file" の通知で、一覧の行も更新されている
    assert pump(app, 2, until=lambda: w.library_view.model.paper(a.id).has_original)


def test_popover_shows_last_change_from_other_computer_as_plain_text(two_computers, tmp_path):
    app, lib_a, engine_a, w = two_computers
    engine_a.device = DeviceInfo("1" * 32, "<b>MacBook</b> Air")  # リッチテキストとして解釈されないこと
    lib_a.add(_paper(tmp_path, "a.pdf", "Shared"))
    engine_a.sync_now()
    _sync_b(app, w)
    from PySide6.QtCore import Qt

    from spectra.ui.sync_popover import SyncPopover

    pop = SyncPopover(w.settings, w.sync, w)
    assert "ほかのパソコンからの最後の変更" in pop.other_label.text()
    assert "<b>MacBook</b> Air" in pop.other_label.text()
    assert pop.other_label.textFormat() == Qt.TextFormat.PlainText
    pop.close()


def test_disk_full_banner(make_window):
    app, w = make_window(enabled=False)
    from spectra.library.sync import SyncReport

    w._on_synced(SyncReport(disk_full=3))
    assert "空き容量が足りないため、3 件の論文を保存できませんでした" in w.banner._title.text()


def test_waiting_paper_cannot_be_translated_and_wording_is_consistent(make_window, tmp_path, cloud):
    (cloud / "Spectra Sync").mkdir(parents=True)
    app, w = make_window()
    paper = w.library.add(_paper(tmp_path, "a.pdf", "Alpha"))[0]
    w.library_view.reload()
    _evict(w, paper.id)
    w.engines[-1].fail = RemoteMissing()
    w.open_paper(paper.id)
    assert pump(app, 5, until=lambda: (d := w.sync.download_state(paper.id)) is not None and d.state == "waiting")
    assert w.status.text() == "ほかのパソコンからファイルが届くのを待っています"
    w.library_view.select_ids([paper.id])
    pump(app, 0.1)
    detail = w.library_view.detail
    assert not detail.translate_btn.isEnabled() and not detail.read_btn.isEnabled()
    assert detail.tr_blocked.text() == "ファイルが届いてから翻訳できます"
    assert detail.local.text() == "到着待ち"  # 短い値（説明はツールチップ）
    assert "届くのを待っています" in detail.local.toolTip()
    assert detail.width() == 324  # パネルが横に広がらない


def test_popover_truncates_long_device_names(two_computers, tmp_path):
    app, lib_a, engine_a, w = two_computers
    long_name = "<i>研究室</i>" + "の共有パソコン" * 40
    engine_a.device = DeviceInfo("1" * 32, long_name)
    lib_a.add(_paper(tmp_path, "a.pdf", "Shared"))
    engine_a.sync_now()
    _sync_b(app, w)
    from spectra.ui.sync_popover import NAME_LIMIT, SyncPopover

    pop = SyncPopover(w.settings, w.sync, w)
    shown = pop.other_label.text()
    assert "…" in shown and long_name not in shown
    assert shown.count("の共有パソコン") <= NAME_LIMIT // 7
    tip = pop.other_label.toolTip()
    assert "&lt;i&gt;研究室&lt;/i&gt;" in tip  # タグはエスケープされ、文字のまま見える
    pop.close()
