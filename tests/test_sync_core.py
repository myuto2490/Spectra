"""同期の core の骨組み（公開 API の形）と、Library に足した変更の合図・ゴミ箱のテスト。"""

import json
import sqlite3
import threading
import time

import pytest
from pdf_factory import make_library_paper

from spectra.config.settings import Settings
from spectra.library import Library, LibraryError, Metadata, PaperNotFound, Translation
from spectra.library.store import DB_NAME, STATE_DB_NAME
from spectra.library.sync import (
    HLC,
    DeviceInfo,
    FolderStorage,
    HashMismatch,
    HybridClock,
    RemoteChange,
    RemoteMissing,
    SyncEngine,
    SyncError,
    SyncUnavailable,
    load_device,
)
from spectra.library.sync.testing import FakeFolderStorage
from spectra.translate.errors import CancelledError


@pytest.fixture()
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


@pytest.fixture()
def pdf(tmp_path):
    def make(name="paper.pdf", seed=0, **kw):
        path = tmp_path / name
        make_library_paper(str(path), seed=seed, **kw)
        return path

    return make


def translation(**kw) -> Translation:
    values = dict(
        paper_id=0, model="dummy", formality="default", first_page=0, last_page=None,
        options={}, segments=1, untranslated=0, chars=10, created_at=time.time(),
    )
    return Translation(**{**values, **kw})


# -- Library: 変更の合図 ---------------------------------------------------------


def test_listener_receives_events_after_commit(library, pdf):
    events = []

    def listener(event):
        # ロックを離した後に呼ばれるので、別スレッドからでも Library を使える
        t = threading.Thread(target=lambda: events.append((event, len(library.papers()))))
        t.start()
        t.join(2)
        assert not t.is_alive()

    library.add_listener(listener)
    paper, _ = library.add(pdf())
    library.set_favorite(paper.id, True)
    library.touch_opened(paper.id)
    library.save_translation(paper.id, b"%PDF-x", translation())
    c = library.create_collection("A")
    library.add_to_collection(c.id, [paper.id])
    library.trash(paper.id)
    library.restore(paper.id)
    library.purge(paper.id)
    kinds = [e.kind for e, _ in events]
    assert kinds == ["added", "favorite", "opened", "translation", "collections", "collections", "removed", "added", "removed"]
    assert events[0][0].sha256 == paper.sha256 and events[0][0].paper_id == paper.id
    assert events[-1][0].sha256 == paper.sha256  # 削除の後でも sha が分かる


def test_listener_errors_are_swallowed_and_removable(library, pdf):
    calls = []
    library.add_listener(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    unsubscribe = library.add_listener(lambda e: calls.append(e.kind))
    paper, _ = library.add(pdf())  # リスナーの例外で変更は失敗しない
    assert calls == ["added"]
    unsubscribe()
    library.set_favorite(paper.id, True)
    assert calls == ["added"]


def test_meta_status_does_not_touch_updated_at(library, pdf):
    events = []
    paper, _ = library.add(pdf())
    library.add_listener(events.append)
    library.set_meta_status(paper.id, "done")
    again = library.get(paper.id)
    assert again.meta_status == "done"
    assert again.updated_at == paper.updated_at
    assert events == []


def test_paper_not_found(library):
    with pytest.raises(PaperNotFound) as info:
        library.update_metadata(999, Metadata(title="x"))
    assert isinstance(info.value, LibraryError)  # 既存の except LibraryError でも捕まえられる
    with pytest.raises(PaperNotFound):
        library.trash(999)


def test_local_file_flags(library, pdf):
    paper, _ = library.add(pdf())
    assert paper.has_original and not paper.has_translation_file  # 未翻訳は False
    paper = library.save_translation(paper.id, b"%PDF-x", translation())
    assert paper.has_translation_file
    paper.translated_path.unlink()
    paper.pdf_path.unlink()
    paper = library.get(paper.id)
    assert not paper.has_original and not paper.has_translation_file
    assert paper.translation is not None  # 訳文の情報は残る（UI は ☁ と表示する）


# -- Library: ゴミ箱 ---------------------------------------------------------------


def test_trash_restore_and_lists(library, pdf):
    a, _ = library.add(pdf("a.pdf", seed=1))
    b, _ = library.add(pdf("b.pdf", seed=2))
    library.trash(a.id)
    assert [p.id for p in library.papers()] == [b.id]
    trashed = library.trashed_papers()
    assert [p.id for p in trashed] == [a.id] and trashed[0].trashed_at
    assert library.get(a.id).trashed_at is not None  # get はゴミ箱の論文も返す
    assert a.pdf_path.exists()  # ファイルは残す
    library.restore(a.id)
    assert library.get(a.id).trashed_at is None
    assert {p.id for p in library.papers()} == {a.id, b.id}


def test_add_detailed_statuses(library, pdf):
    path = pdf()
    first = library.add_detailed(path)
    assert first.status == "new"
    assert library.add_detailed(path).status == "existing"
    library.trash(first.paper.id)
    again = library.add_detailed(path)
    assert again.status == "restored" and again.paper.id == first.paper.id and again.paper.trashed_at is None
    library.trash(first.paper.id)
    assert library.add(path) == (library.get(first.paper.id), True)  # 従来の add の形も保つ


def test_purge_empty_and_expire(library, pdf):
    a, _ = library.add(pdf("a.pdf", seed=1))
    b, _ = library.add(pdf("b.pdf", seed=2))
    c, _ = library.add(pdf("c.pdf", seed=3))
    library.trash(a.id)
    library.trash(b.id)
    now = time.time()
    assert library.purge_expired(now=now + 29 * 86400) == 0
    assert library.purge_expired(now=now + 31 * 86400) == 2
    assert library.get(a.id) is None and not a.pdf_path.parent.exists()
    library.trash(c.id)
    assert library.empty_trash() == 1
    assert library.papers() == [] and library.trashed_papers() == []
    assert Library.remove is Library.purge  # v0.2.0 からの名前


def test_trash_state_keeps_library_version(tmp_path, pdf):
    """ゴミ箱の状態は state.sqlite3 に置き、library.sqlite3 の user_version は 1 のまま（v0.2.0 でも開ける）。"""
    root = tmp_path / "lib"
    lib = Library(root)
    paper, _ = lib.add(pdf())
    lib.trash(paper.id)
    lib.close()
    db = sqlite3.connect(root / DB_NAME)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "trash" not in {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    db.close()
    state = sqlite3.connect(root / STATE_DB_NAME)
    assert state.execute("SELECT paper_id FROM trash").fetchall() == [(paper.id,)]
    state.close()
    lib = Library(root)  # 開き直してもゴミ箱の状態が残る
    try:
        assert lib.get(paper.id).trashed_at is not None
    finally:
        lib.close()


def test_orphan_trash_rows_are_cleaned(tmp_path, pdf):
    root = tmp_path / "lib"
    lib = Library(root)
    paper, _ = lib.add(pdf())
    lib.trash(paper.id)
    lib.close()
    # 完全な削除の途中で落ちた状態（papers の行だけ消えた）を作る
    db = sqlite3.connect(root / DB_NAME)
    db.execute("DELETE FROM papers")
    db.commit()
    db.close()
    lib = Library(root)
    try:
        assert lib.trashed_papers() == []
        assert lib._db.execute("SELECT COUNT(*) FROM state.trash").fetchone()[0] == 0
    finally:
        lib.close()


# -- HLC ------------------------------------------------------------------------


def test_hlc_tick_and_observe():
    now = [1000.0]
    clock = HybridClock("a" * 32, now=lambda: now[0])
    t1 = clock.tick()
    t2 = clock.tick()  # 同じミリ秒ならカウンターで進む
    assert t1 < t2 and t2.wall_ms == t1.wall_ms and t2.counter == 1
    # 編集した時刻（updated_at）を使っても、最新より後になる
    assert clock.tick(at=1.0) > t2
    # 相手の時計が 1 時間進んでいても、見た後の自分の変更はその後になる
    remote = HLC(int((now[0] + 3600) * 1000), 5, "b" * 32)
    clock.observe(remote)
    assert clock.tick() > remote


def test_hlc_string_roundtrip_and_order():
    a = HLC(1, 2, "dev")
    b = HLC(10, 0, "dev")
    assert HLC.parse(str(a)) == a
    assert str(a) < str(b)  # 文字列のままで大小を比べられる
    with pytest.raises(ValueError):
        HLC.parse("x.y.z")


# -- 同期フォルダ ----------------------------------------------------------------


def test_folder_storage_rejects_bad_paths(tmp_path):
    s = FolderStorage(tmp_path)
    for bad in ["../x", "/etc/passwd", "a/../../b", "", "a\\b"]:
        with pytest.raises(ValueError):
            s.write_atomic(bad, b"x")
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        s.write_atomic("link/x.json", b"x")


def test_folder_storage_roundtrip(tmp_path):
    s = FolderStorage(tmp_path)
    s.write_atomic("papers/ab/state.dev.json", b'{"v": 1}')
    assert s.exists("papers/ab/state.dev.json")
    assert [e.path for e in s.list("papers")] == ["papers/ab/state.dev.json"]
    assert not list((tmp_path / "papers" / "ab").glob("*.tmp"))
    with pytest.raises(ValueError):
        s.read_bytes("papers/ab/state.dev.json", limit=3)
    with pytest.raises(RemoteMissing):
        s.read_bytes("papers/ab/none.json")
    dest = tmp_path / "out" / "copy.bin"
    seen = []
    sha = s.copy_to("papers/ab/state.dev.json", dest, progress=lambda d, t: seen.append((d, t)))
    assert dest.read_bytes() == b'{"v": 1}' and len(sha) == 64 and seen[-1] == (8, 8)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        s.copy_to("papers/ab/state.dev.json", tmp_path / "out" / "c2.bin", cancel=cancel)
    assert not (tmp_path / "out" / "c2.bin").exists()  # 途中のファイルは消す
    assert not FolderStorage(tmp_path / "missing").available()
    with pytest.raises(SyncUnavailable):
        FolderStorage(tmp_path / "missing").list()


def test_fake_storage_injections(tmp_path):
    a = FakeFolderStorage(tmp_path / "cloud")
    b = FakeFolderStorage(tmp_path / "cloud")  # 同じフォルダを見る 2 台目
    a.hold()
    a.write_atomic("papers/x/state.a.json", b"1")
    a.write_atomic("papers/x/original.pdf", b"2")
    assert not b.exists("papers/x/state.a.json")  # まだ届いていない
    assert a.deliver(lambda p: p.endswith(".json")) == ["papers/x/state.a.json"]
    assert b.exists("papers/x/state.a.json") and not b.exists("papers/x/original.pdf")
    a.deliver()
    assert b.exists("papers/x/original.pdf")

    a.partial_next("*.json")
    a.write_atomic("papers/y/state.a.json", b"0123456789")
    assert b.read_bytes("papers/y/state.a.json") == b"01234"  # 途中まで

    copy = a.conflict_copy("papers/x/state.a.json", "state.a (1).json")
    assert b.exists(copy)
    a.make_placeholder("papers/x/original.pdf")
    assert b.exists("papers/x/.original.pdf.icloud") and not b.exists("papers/x/original.pdf")

    a.fail_next("papers/x/*", SyncUnavailable("boom"))
    with pytest.raises(SyncUnavailable):
        a.read_bytes("papers/x/state.a.json")
    a.offline = True
    assert not a.available()
    with pytest.raises(SyncUnavailable):
        a.list()
    assert b.reads == ["papers/y/state.a.json"]


# -- 端末 ID ---------------------------------------------------------------------


def test_device_id_is_stable_and_regenerated_on_copy(tmp_path):
    path = tmp_path / "device.json"
    first = load_device(path)
    assert load_device(path) == first and len(first.id) == 32
    raw = json.loads(path.read_text())
    raw["fingerprint"] = "another-machine"  # 設定ごと別のパソコンにコピーされた
    path.write_text(json.dumps(raw))
    assert load_device(path).id != first.id


# -- SyncEngine（骨組み） ------------------------------------------------------------


@pytest.fixture()
def engine(library, tmp_path):
    storage = FakeFolderStorage(tmp_path / "cloud")
    e = SyncEngine(library, storage, DeviceInfo("a" * 32, "テスト"))
    yield e
    e.close()


def test_fetch_returns_local_file_without_touching_sync_folder(engine, library, pdf):
    paper, _ = library.add(pdf())
    engine.storage.offline = True
    assert engine.fetch(paper.id, "original") == paper.pdf_path
    assert engine.storage.reads == []


def test_fetch_errors(engine, library, pdf):
    paper, _ = library.add(pdf())
    with pytest.raises(PaperNotFound):
        engine.fetch(999, "original")
    with pytest.raises(ValueError):
        engine.fetch(paper.id, "other")
    with pytest.raises(RemoteMissing):
        engine.fetch(paper.id, "translation")  # 訳文がない
    paper.pdf_path.unlink()
    with pytest.raises(RemoteMissing):
        engine.fetch(paper.id, "original")  # 手元にも同期先にもない
    engine.storage.offline = True
    with pytest.raises(SyncUnavailable):
        engine.fetch(paper.id, "original")
    for cls in (SyncUnavailable, RemoteMissing, HashMismatch):
        err = cls()
        assert isinstance(err, SyncError) and err.title and err.hint  # UI がそのまま表示する


def test_remote_listener_contract(engine):
    got = []
    engine.add_remote_listener(lambda changes: (_ for _ in ()).throw(RuntimeError("boom")))
    unsubscribe = engine.add_remote_listener(got.append)
    change = RemoteChange("removed", "ab" * 32, 3)
    engine._notify([change])
    engine._notify([])  # 変更がなければ呼ばない
    assert got == [[change]]
    unsubscribe()
    engine._notify([change])
    assert len(got) == 1


def test_sync_now_on_empty_library(engine):
    assert engine.sync_now().changes == []
    engine.storage.offline = True
    with pytest.raises(SyncUnavailable):
        engine.sync_now()


def test_settings_schema_4():
    s = Settings()
    from spectra.config.settings import SCHEMA_VERSION

    assert s.sync_enabled is False and s.sync_folder == "" and s.schema == SCHEMA_VERSION
    assert Settings.from_dict({"sync_enabled": True, "sync_folder": "/x", "schema": 3}).sync_enabled is True


# -- e7 のレビューで追加 ------------------------------------------------------------


def test_failed_operations_do_not_emit(library):
    events = []
    library.add_listener(events.append)
    for op in (lambda: library.update_metadata(999, Metadata(title="x")), lambda: library.trash(999), lambda: library.restore(999)):
        with pytest.raises(PaperNotFound):
            op()
    assert events == []


def test_remaining_event_kinds_and_idempotence(library, pdf):
    path = pdf()
    paper = library.add_detailed(path).paper
    events = []
    library.add_listener(lambda e: events.append(e.kind))
    library.update_metadata(paper.id, Metadata(title="New"), manual_fields={"title"})
    assert library.add_detailed(path).status == "existing"  # 合図なし
    library.save_translation(paper.id, b"%PDF-x", translation())
    library.remove_translation(paper.id)
    library.trash(paper.id)
    library.trash(paper.id)  # 二重の trash は合図なし
    assert library.add_detailed(path).status == "restored"
    library.restore(paper.id)  # ゴミ箱にない論文の restore は合図なし
    assert events == ["metadata", "translation", "translation", "removed", "added"]


def test_trash_keeps_collection_membership(library, pdf):
    a, _ = library.add(pdf("a.pdf", seed=1, body=["doi:10.1000/same"]))
    b, _ = library.add(pdf("b.pdf", seed=2, body=["doi:10.1000/same"]))
    c = library.create_collection("C")
    library.add_to_collection(c.id, [a.id])
    library.trash(a.id)
    assert all(c.id not in p.collection_ids for p in library.papers())  # 絞り込みにも出ない
    assert library.same_identifier(b) == []  # ゴミ箱の論文は除く
    library.restore(a.id)
    assert library.get(a.id).collection_ids == {c.id}
    assert [p.id for p in library.same_identifier(b)] == [a.id]


def test_fetch_cancel_before_reading(engine, library, pdf):
    paper, _ = library.add(pdf())
    paper.pdf_path.unlink()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        engine.fetch(paper.id, "original", cancel=cancel)
    assert engine.storage.reads == []


def test_settings_sync_folder_type_is_checked():
    s = Settings.from_dict({"sync_folder": 123, "sync_enabled": "yes", "schema": 4})
    assert s.sync_folder == "" and s.sync_enabled is False
