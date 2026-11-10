"""同期の本体（SyncEngine）の単体テスト。2 台のシナリオは test_sync_scenarios.py（97）が受け持ち、
ここでは内部の約束（エコーの防止、手での編集の優先、tombstone、初回の接続、外部入力の検証など）を確かめる。
"""

import hashlib
import json
import os
import time

import pytest
from pdf_factory import make_library_paper

from spectra.library import Library, Metadata, Translation
from spectra.library.sync import DeviceInfo, HashMismatch, HybridClock, RemoteMissing, SyncEngine
from spectra.library.sync import files as F
from spectra.library.sync.testing import FakeFolderStorage

A, B = "a" * 32, "b" * 32


class Dev:
    def __init__(self, tmp_path, name, device_id, offset=0.0):
        self.lib = Library(tmp_path / name)
        self.storage = FakeFolderStorage(tmp_path / "cloud")
        self.engine = SyncEngine(
            self.lib, self.storage, DeviceInfo(device_id, name), clock=HybridClock(device_id, now=lambda: time.time() + offset)
        )
        self.changes = []
        self.engine.add_remote_listener(self.changes.extend)

    def sync(self):
        return self.engine.sync_now()

    def close(self):
        self.engine.close()
        self.lib.close()


@pytest.fixture()
def devs(tmp_path):
    made = []

    def make(name, device_id, offset=0.0):
        d = Dev(tmp_path, name, device_id, offset)
        made.append(d)
        return d

    yield make
    for d in made:
        d.close()


@pytest.fixture()
def pdf(tmp_path):
    def make(name="paper.pdf", seed=0, **kw):
        path = tmp_path / "src" / name
        path.parent.mkdir(exist_ok=True)
        make_library_paper(str(path), seed=seed, **kw)
        return path

    return make


def translation(**kw) -> Translation:
    values = dict(
        paper_id=0, model="m", formality="default", first_page=0, last_page=None,
        options={}, segments=1, untranslated=0, chars=1, created_at=time.time(),
    )
    return Translation(**{**values, **kw})


def roundtrip(*devices):
    for d in devices:
        d.sync()
    for d in devices:
        d.sync()


def test_no_echo_after_applying_remote_change(devs, pdf):
    a, b = devs("A", A), devs("B", B)
    p, _ = a.lib.add(pdf())
    roundtrip(a, b)
    a.lib.update_metadata(p.id, Metadata(title="From A"), manual_fields={"title"})
    a.sync()
    b.sync()
    writes = list(b.storage.writes)
    b.sync()  # 反映した値を「ローカルの変更」として送り返さない
    assert b.storage.writes == writes
    assert b.lib.find_by_sha(p.sha256).metadata.title == "From A"


def test_pending_apply_recovery_prevents_echo(devs, pdf):
    a, b = devs("A", A), devs("B", B)
    p, _ = a.lib.add(pdf())
    roundtrip(a, b)
    a.lib.update_metadata(p.id, Metadata(title="New"), manual_fields={"title"})
    a.sync()
    # B: 反映の途中（ライブラリは更新したが、基準を記録する前）で落ちた状態を作る
    finish = b.engine._finish_pending
    b.engine._finish_pending = lambda sha, target: None
    b.sync()
    b.engine._finish_pending = finish
    assert b.engine.state.query("SELECT COUNT(*) AS n FROM pending_apply")[0]["n"] == 1
    writes = len(b.storage.writes)
    b.sync()  # 起動し直した後の同期: 後始末をして、エコーしない
    assert b.engine.state.query("SELECT COUNT(*) AS n FROM pending_apply")[0]["n"] == 0
    assert not any(w.startswith("papers/") for w in b.storage.writes[writes:])


def test_manual_edit_beats_newer_automatic_value(devs, pdf):
    a, b = devs("A", A), devs("B", B)
    p, _ = a.lib.add(pdf())
    roundtrip(a, b)
    a.lib.update_metadata(p.id, Metadata(venue="Hand Edited"), manual_fields={"venue"})
    a.sync()
    pb = b.lib.find_by_sha(p.sha256)
    time.sleep(0.01)
    b.lib.update_metadata(pb.id, Metadata(venue="Lookup Result", source="crossref"))  # 自動（後から）
    roundtrip(b, a)
    for d in (a, b):
        got = d.lib.find_by_sha(p.sha256)
        assert got.metadata.venue == "Hand Edited" and "venue" in got.edited


def test_purge_writes_tombstone(devs, pdf):
    a, b = devs("A", A), devs("B", B)
    p, _ = a.lib.add(pdf())
    roundtrip(a, b)
    a.lib.purge(p.id)  # ゴミ箱を経ずに完全に削除（CLI の remove）
    roundtrip(a, b)
    assert b.lib.papers() == []
    assert any(c.kind == "removed" for c in b.changes)


def test_tombstone_after_sync_was_paused(devs, pdf):
    """同期を止めている間に削除しても、再開したときに tombstone を書く（sync.sqlite3 の記録が残るため）。"""
    a, b = devs("A", A), devs("B", B)
    p, _ = a.lib.add(pdf())
    roundtrip(a, b)
    a.lib.purge(p.id)  # この間 sync_now は呼ばれない
    a.sync()
    b.sync()
    assert b.lib.papers() == []


def test_first_join_merges_same_name_collections(devs, pdf):
    a, b = devs("A", A), devs("B", B)
    pa, _ = a.lib.add(pdf("a.pdf", seed=1))
    ca = a.lib.create_collection("NLP")
    a.lib.add_to_collection(ca.id, [pa.id])
    a.sync()
    pb, _ = b.lib.add(pdf("b.pdf", seed=2))
    cb = b.lib.create_collection("nlp")  # 大文字小文字だけ違う同じ名前
    b.lib.add_to_collection(cb.id, [pb.id])
    roundtrip(b, a)
    for d in (a, b):
        names = [c.name for c in d.lib.collections()]
        assert len(names) == 1
        cid = d.lib.collections()[0].id
        assert all(cid in p.collection_ids for p in d.lib.papers())
    # 接続した後に同じ名前で作ったものは、別のコレクションのまま
    a.lib.create_collection("NLP")
    roundtrip(a, b)
    assert len(b.lib.collections()) == 2


def test_complete_translation_beats_newer_partial(devs, pdf):
    a, b = devs("A", A), devs("B", B)
    p, _ = a.lib.add(pdf())
    roundtrip(a, b)
    pb = b.lib.find_by_sha(p.sha256)
    a.lib.save_translation(p.id, b"%PDF-complete", translation(created_at=time.time() - 100))
    b.lib.save_translation(pb.id, b"%PDF-partial", translation(first_page=0, last_page=1, created_at=time.time()))
    roundtrip(a, b)
    for d in (a, b):
        got = d.lib.find_by_sha(p.sha256)
        assert got.translation.complete
    got = b.lib.find_by_sha(p.sha256)
    assert not got.has_translation_file  # 手元の訳文は別物になったので、開くときに取得する
    assert b.engine.fetch(got.id, "translation").read_bytes() == b"%PDF-complete"
    # 負けた B は、自分の訳文のファイルを同期フォルダから消す
    folder = a.storage.root / "papers" / p.sha256
    assert not (folder / f"translated.{B}.pdf").exists() and not (folder / f"translation.{B}.json").exists()
    assert (folder / f"translated.{A}.pdf").exists()
    roundtrip(a, b)  # その後も A の訳文のまま
    assert a.lib.find_by_sha(p.sha256).translation.complete and b.lib.find_by_sha(p.sha256).translation.complete


def test_evict_keeps_only_copy(devs, pdf):
    a = devs("A", A)
    p, _ = a.lib.add(pdf())
    # まだ書き出していない: 同期先に原文がないので、手元のコピーは消さない
    with pytest.raises(RemoteMissing):
        a.engine.evict(p.id)
    assert a.lib.get(p.id).has_original
    a.sync()
    a.engine.evict(p.id)
    got = a.lib.get(p.id)
    assert not got.has_original
    assert hashlib.sha256(a.engine.fetch(p.id, "original").read_bytes()).hexdigest() == p.sha256
    assert a.lib.get(p.id).has_original


def test_fetch_hash_mismatch_leaves_no_tmp(devs, pdf):
    a, b = devs("A", A), devs("B", B)
    p, _ = a.lib.add(pdf())
    roundtrip(a, b)
    (a.storage.root / F.original_path(p.sha256)).write_bytes(b"broken")
    pb = b.lib.find_by_sha(p.sha256)
    with pytest.raises(HashMismatch):
        b.engine.fetch(pb.id, "original")
    assert not pb.pdf_path.parent.exists() or not list(pb.pdf_path.parent.glob("*.tmp"))
    assert not b.lib.find_by_sha(p.sha256).has_original


def test_sync_reads_only_json(devs, pdf):
    a, b = devs("A", A), devs("B", B)
    a.lib.add(pdf())
    roundtrip(a, b)
    assert not any(r.endswith(".pdf") for r in b.storage.reads)  # PDF は開くまで読まない


def test_invalid_remote_files_are_ignored(devs, pdf):
    a, b = devs("A", A), devs("B", B)
    p, _ = a.lib.add(pdf())
    roundtrip(a, b)
    root = a.storage.root
    (root / "papers" / "not-a-sha").mkdir(parents=True)
    (root / "papers" / "not-a-sha" / "state.x.json").write_text("{}")
    (root / "papers" / p.sha256 / "state.evil.json").write_bytes(b"{" + b" " * (F.MAX_JSON_BYTES + 10) + b"}")
    bad = {"kind": "paper", "v": 1, "device": "c" * 32, "sha": p.sha256,
           "fields": {"title": ["x" * 10_000, "bad-hlc", False], "year": ["1999", "0000000000001.000000." + "c" * 32, False]},
           "life": [False, "nope"]}
    (root / "papers" / p.sha256 / "state.cccc.json").write_text(json.dumps(bad))
    b.sync()  # 落ちない
    got = b.lib.find_by_sha(p.sha256)
    assert got is not None and got.trashed_at is None and got.metadata.title != "x" * 10_000


def test_clock_is_persisted(tmp_path, pdf):
    lib = Library(tmp_path / "lib")
    storage = FakeFolderStorage(tmp_path / "cloud")
    e = SyncEngine(lib, storage, DeviceInfo(A, "A"))
    lib.add(pdf())
    e.sync_now()
    last = e.clock.last
    e.close()
    e2 = SyncEngine(lib, storage, DeviceInfo(A, "A"))
    try:
        assert e2.clock.last >= last
    finally:
        e2.close()
        lib.close()


def test_icloud_placeholder_requests_download(devs, pdf, monkeypatch):
    a, b = devs("A", A), devs("B", B)
    p, _ = a.lib.add(pdf())
    roundtrip(a, b)
    a.storage.make_placeholder(F.original_path(p.sha256))
    calls = []
    monkeypatch.setattr("spectra.library.sync.storage.sys.platform", "darwin")
    monkeypatch.setattr("spectra.library.sync.storage.subprocess.run", lambda args, **kw: calls.append(args))
    with pytest.raises(RemoteMissing) as info:
        b.engine.fetch(b.lib.find_by_sha(p.sha256).id, "original")
    assert "iCloud" in info.value.hint
    assert calls and calls[0][:2] == ["brctl", "download"]


def test_same_size_rewrite_within_one_mtime_tick_is_noticed(devs):
    """更新時刻の刻みが粗い（Windows・FAT）と、同じ大きさで書き直しても mtime が変わらないことがある。"""
    a, b = devs("A", A), devs("B", B)
    col = a.lib.create_collection("Old")
    roundtrip(a, b)
    path = next((a.storage.root / "collections").glob(f"*/state.{A}.json"))
    before = path.stat().st_mtime_ns
    a.lib.rename_collection(col.id, "New")
    a.sync()
    os.utime(path, ns=(before, before))  # 同じ刻みの中で書き直された
    b.sync()
    assert [c.name for c in b.lib.collections()] == ["New"]



def test_backup_before_first_sync_and_restore(devs, pdf):
    from spectra.library.store import DB_NAME, restore_backup

    a, b = devs("A", A), devs("B", B)
    a.lib.add(pdf("a.pdf", seed=1))
    a.sync()
    pb, _ = b.lib.add(pdf("b.pdf", seed=2))
    b.sync()
    backup = b.engine.backup_path
    assert backup is not None and (backup / DB_NAME).exists()
    assert not list(backup.glob("*.pdf"))  # PDF はコピーしない
    assert len(b.lib.papers()) == 2  # A の論文が統合された
    assert b.engine.backup_before_first_sync() is None  # 2 回目以降は作らない
    root = b.lib.root
    b.engine.close()
    b.lib.close()
    restore_backup(backup, root)
    assert not (root / "sync.sqlite3").exists()
    lib = Library(root)
    try:
        assert [p.sha256 for p in lib.papers()] == [pb.sha256]
    finally:
        lib.close()


def test_no_backup_for_empty_library(devs):
    a = devs("A", A)
    a.sync()
    assert a.engine.backup_path is None



def _remote_copy(devs, pdf):
    a, b = devs("A", A), devs("B", B)
    p, _ = a.lib.add(pdf())
    roundtrip(a, b)
    return a, b, b.lib.find_by_sha(p.sha256)


def test_fetch_cancel_during_copy(devs, pdf, monkeypatch):
    import threading

    from spectra.translate.errors import CancelledError

    a, b, pb = _remote_copy(devs, pdf)
    monkeypatch.setattr("spectra.library.sync.storage.CHUNK", 100)
    cancel = threading.Event()
    with pytest.raises(CancelledError):
        b.engine.fetch(pb.id, "original", progress=lambda done, total: cancel.set(), cancel=cancel)
    assert not pb.pdf_path.parent.exists() or not list(pb.pdf_path.parent.glob("*.tmp"))
    assert not b.lib.get(pb.id).has_original


def test_fetch_progress_and_file_notice(devs, pdf, monkeypatch):
    a, b, pb = _remote_copy(devs, pdf)
    monkeypatch.setattr("spectra.library.sync.storage.CHUNK", 1000)
    seen = []
    b.changes.clear()
    b.engine.fetch(pb.id, "original", progress=lambda done, total: seen.append((done, total)))
    size = pb.pdf_path.stat().st_size
    assert len(seen) > 1 and all(t == size for _, t in seen)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen) and seen[-1][0] == size
    assert [(c.kind, c.paper_id, c.sha256) for c in b.changes] == [("file", pb.id, pb.sha256)]
    b.changes.clear()
    b.engine.evict(pb.id)
    assert [(c.kind, c.paper_id, c.sha256) for c in b.changes] == [("file", pb.id, pb.sha256)]


def test_sync_cancel_then_resume(devs, pdf):
    import threading

    from spectra.translate.errors import CancelledError

    a, b = devs("A", A), devs("B", B)
    p, _ = a.lib.add(pdf())
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        a.engine.sync_now(cancel)
    assert not (a.storage.root / "papers").exists()  # 中途半端な書き出しは残らない
    roundtrip(a, b)
    assert b.lib.find_by_sha(p.sha256) is not None


def _enospc_on_write(monkeypatch):
    import builtins
    import errno

    real_open = builtins.open

    def fake_open(file, mode="r", *args, **kw):
        if "w" in mode or "x" in mode:  # 同期フォルダへの書き込みは、一時ファイルを新しく作る（"xb"）
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(file, mode, *args, **kw)

    monkeypatch.setattr("spectra.library.sync.storage.open", fake_open, raising=False)


def test_fetch_reports_local_disk_full(devs, pdf, monkeypatch):
    from spectra.library.sync import LocalDiskFull

    a, b, pb = _remote_copy(devs, pdf)
    _enospc_on_write(monkeypatch)
    with pytest.raises(LocalDiskFull) as info:
        b.engine.fetch(pb.id, "original")
    assert info.value.title and info.value.hint
    assert not b.lib.get(pb.id).has_original


def test_export_counts_disk_full_and_retries(devs, pdf, monkeypatch):
    a, b = devs("A", A), devs("B", B)
    p, _ = a.lib.add(pdf())
    with monkeypatch.context() as m:
        _enospc_on_write(m)
        report = a.sync()
    assert report.disk_full >= 1  # 論文の書き出し（と、形式の印・端末の情報）
    roundtrip(a, b)  # 空き容量が戻れば、次の同期で書き出す
    assert b.lib.find_by_sha(p.sha256) is not None


def test_devices_summary(devs, pdf):
    a, b = devs("A", A), devs("B", B)
    a.lib.add(pdf())
    roundtrip(a, b)
    assert a.engine.devices()[0].is_self and a.engine.devices()[0].name == "A"
    others = [d for d in b.engine.devices() if not d.is_self]
    assert [(d.id, d.name) for d in others] == [(A, "A")]
    assert others[0].last_seen and abs(others[0].last_seen - time.time()) < 60
    assert others[0].app
