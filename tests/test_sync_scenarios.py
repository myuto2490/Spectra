"""クラウド同期の 2 台のシナリオのテスト（docs/SYNC.md §10）。

実装者とは別の人が、公開 API（Library・SyncEngine・FakeFolderStorage）だけを使って、仕様から書いたもの。
2 台の端末は、同じ一時フォルダを指す 2 つの FakeFolderStorage と、別々の Library で再現する。
同期フォルダの中身の形式には、仕様（§5.2）で決まっている場所（papers/<sha>/、devices/<端末ID>.json）だけを使う。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from pdf_factory import make_library_paper

from spectra.library import Library, Metadata, Translation
from spectra.library.store import DB_NAME
from spectra.library.sync import (
    DeviceInfo,
    HashMismatch,
    HybridClock,
    RemoteChange,
    RemoteMissing,
    SyncEngine,
    SyncUnavailable,
)
from spectra.library.sync.testing import FakeFolderStorage


class Clock:
    """端末ごとの壁時計（秒）。時計のずれを再現する。"""

    def __init__(self, offset: float = 0.0):
        self.offset = offset

    def __call__(self) -> float:
        return time.time() + self.offset


@dataclass
class Device:
    name: str
    lib: Library
    storage: FakeFolderStorage
    engine: SyncEngine
    changes: list[RemoteChange]

    def sync(self):
        return self.engine.sync_now()

    def paper(self, sha: str):
        return self.lib.find_by_sha(sha)


@pytest.fixture()
def sync_root(tmp_path):
    return tmp_path / "cloud" / "Spectra Sync"


@pytest.fixture()
def make_device(tmp_path, sync_root):
    made: list[Device] = []

    def make(name: str, *, offset: float = 0.0) -> Device:
        lib = Library(tmp_path / name / "library")
        storage = FakeFolderStorage(sync_root)
        info = DeviceInfo(f"{len(made) + 1:032x}", name)
        engine = SyncEngine(lib, storage, info, clock=HybridClock(info.id, now=Clock(offset)))
        changes: list[RemoteChange] = []
        engine.add_remote_listener(changes.extend)
        device = Device(name, lib, storage, engine, changes)
        made.append(device)
        return device

    yield make
    for d in made:
        d.engine.close()
        d.lib.close()


@pytest.fixture()
def pdf(tmp_path):
    def make(name: str = "paper.pdf", seed: int = 0, **kw) -> Path:
        path = tmp_path / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        make_library_paper(str(path), seed=seed, **kw)
        return path

    return make


def translation(**kw) -> Translation:
    values = dict(
        paper_id=0, model="dummy", formality="default", first_page=0, last_page=None,
        options={}, segments=10, untranslated=0, chars=100, created_at=time.time(),
    )
    return Translation(**{**values, **kw})


def roundtrip(*devices: Device) -> None:
    """全端末で書き出し → 全端末で取り込み（同期アプリが即座に届けた場合）。"""
    for d in devices:
        d.sync()
    for d in devices:
        d.sync()


def stored_paths(storage: FakeFolderStorage) -> list[str]:
    return [p.relative_to(storage.root).as_posix() for p in storage.root.rglob("*") if p.is_file()]


# -- 段階 1: 書き出しと取り込み -------------------------------------------------------


def test_paper_added_on_one_device_appears_on_the_other_without_its_pdf(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    a.lib.update_metadata(paper.id, Metadata(title="Shared Paper"), manual_fields={"title"})
    roundtrip(a, b)

    got = b.paper(paper.sha256)
    assert got is not None
    assert got.metadata.title == "Shared Paper"
    assert got.has_original is False  # 書誌情報だけが届き、PDF は開いたときに取得する（§12 Q3）
    assert any(c.kind == "added" and c.sha256 == paper.sha256 for c in b.changes)


def test_sync_does_not_read_pdfs_until_fetch(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)
    assert not [p for p in b.storage.reads if p.endswith(".pdf")]

    local = b.engine.fetch(b.paper(paper.sha256).id, "original")
    assert local.read_bytes() == paper.pdf_path.read_bytes()
    assert b.paper(paper.sha256).has_original is True
    assert any(c.kind == "file" for c in b.changes)


def test_second_sync_is_a_no_op(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    a.lib.add(pdf(seed=1))
    roundtrip(a, b)
    b.changes.clear()
    writes_before = len(a.storage.writes) + len(b.storage.writes)

    report_a, report_b = a.sync(), b.sync()
    assert report_a.imported == report_b.imported == 0
    assert b.changes == []
    assert len(a.storage.writes) + len(b.storage.writes) == writes_before  # 変わっていなければ書かない


def test_each_device_writes_only_its_own_state_files(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)
    b.lib.set_favorite(b.paper(paper.sha256).id, True)
    roundtrip(a, b)

    for d, other in ((a, b), (b, a)):
        json_writes = [p for p in d.storage.writes if p.endswith(".json") and p.startswith("papers/")]
        assert json_writes
        assert all(other.engine.device.id not in p for p in json_writes)  # 相手の名前のファイルは書かない


def test_secrets_settings_and_caches_are_never_written(make_device, pdf):
    a = make_device("A")
    paper = a.lib.add(pdf(seed=1))[0]
    a.lib.save_translation(paper.id, pdf(name="t.pdf", seed=2).read_bytes(), translation(paper_id=paper.id))
    a.sync()

    names = stored_paths(a.storage)
    assert names
    assert not [n for n in names if n.endswith((".sqlite3", "-wal", "-shm"))]  # SQLite を同期フォルダに置かない
    assert not [n for n in names if "settings" in n or "translations" in n or "cache" in n]
    for n in names:
        if n.endswith(".json"):
            text = (a.storage.root / n).read_text(encoding="utf-8")
            assert "api_key" not in text.lower()
            assert str(a.lib.root) not in text  # 端末に固有の絶対パス（取り込み元など）を書き出さない
            assert paper.source_path not in text


def test_sync_keeps_the_library_database_version(make_device, pdf):
    a = make_device("A")
    a.lib.add(pdf(seed=1))
    a.sync()
    db = sqlite3.connect(str(a.lib.root / DB_NAME))
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1  # v0.2.0 でも開ける
    finally:
        db.close()


# -- 段階 2: 双方向のマージ ------------------------------------------------------------


def test_edits_to_different_fields_on_two_devices_are_both_kept(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)
    bid = b.paper(paper.sha256).id

    a.lib.update_metadata(paper.id, Metadata(title="Title from A"), manual_fields={"title"})
    b.lib.update_metadata(bid, Metadata(venue="Venue from B"), manual_fields={"venue"})
    roundtrip(a, b)

    for d in (a, b):
        md = d.paper(paper.sha256).metadata
        assert (md.title, md.venue) == ("Title from A", "Venue from B")


def test_same_field_last_writer_wins_on_both_devices(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)

    a.lib.update_metadata(paper.id, Metadata(title="First"), manual_fields={"title"})
    time.sleep(0.01)
    b.lib.update_metadata(b.paper(paper.sha256).id, Metadata(title="Second"), manual_fields={"title"})
    roundtrip(a, b)

    assert a.paper(paper.sha256).metadata.title == b.paper(paper.sha256).metadata.title == "Second"


def test_change_made_after_seeing_a_remote_change_wins_even_with_a_slow_clock(make_device, pdf, monkeypatch):
    a, b = make_device("A"), make_device("B", offset=-3600)  # B の時計は 1 時間遅れている
    paper = a.lib.add(pdf(seed=1))[0]
    a.lib.update_metadata(paper.id, Metadata(title="From A"), manual_fields={"title"})
    roundtrip(a, b)

    import spectra.library.store as store

    real = store.time.time
    monkeypatch.setattr(store.time, "time", lambda: real() - 3600)  # B での編集の updated_at も遅れる
    b.lib.update_metadata(b.paper(paper.sha256).id, Metadata(title="From B, later"), manual_fields={"title"})
    monkeypatch.setattr(store.time, "time", real)
    roundtrip(a, b)

    assert a.paper(paper.sha256).metadata.title == "From B, later"
    assert b.paper(paper.sha256).metadata.title == "From B, later"


def test_manual_edit_beats_a_later_automatic_lookup_on_another_device(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)

    a.lib.update_metadata(paper.id, Metadata(title="Corrected by hand"), manual_fields={"title"})
    time.sleep(0.01)
    b.lib.update_metadata(b.paper(paper.sha256).id, Metadata(title="From Crossref", source="crossref"))
    roundtrip(a, b)

    for d in (a, b):
        got = d.paper(paper.sha256)
        assert got.metadata.title == "Corrected by hand"
        assert "title" in got.edited


def test_meta_status_is_not_synced(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    a.lib.set_meta_status(paper.id, "failed")
    roundtrip(a, b)
    assert b.paper(paper.sha256).meta_status != "failed"


def test_favorite_and_last_opened(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)

    b.lib.set_favorite(b.paper(paper.sha256).id, True)
    b.lib.touch_opened(b.paper(paper.sha256).id)
    roundtrip(a, b)

    got = a.paper(paper.sha256)
    assert got.favorite is True
    assert got.last_opened_at == pytest.approx(b.paper(paper.sha256).last_opened_at)  # 最大値でそろう


def test_same_pdf_added_on_both_devices_becomes_one_paper(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    path = pdf(seed=1)
    pa, pb = a.lib.add(path)[0], b.lib.add(path)[0]
    assert pa.sha256 == pb.sha256
    roundtrip(a, b)

    for d in (a, b):
        assert [p.sha256 for p in d.lib.papers()] == [pa.sha256]


def test_different_pdfs_with_the_same_doi_stay_separate(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    pa = a.lib.add(pdf(name="pre.pdf", seed=1))[0]
    pb = b.lib.add(pdf(name="pub.pdf", seed=2))[0]
    a.lib.update_metadata(pa.id, Metadata(doi="10.1000/x"), manual_fields={"doi"})
    b.lib.update_metadata(pb.id, Metadata(doi="10.1000/x"), manual_fields={"doi"})
    roundtrip(a, b)

    assert len(a.lib.papers()) == len(b.lib.papers()) == 2


# -- コレクション ---------------------------------------------------------------------


def test_concurrent_additions_to_one_collection_are_both_kept(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    p1, p2 = a.lib.add(pdf(name="1.pdf", seed=1))[0], a.lib.add(pdf(name="2.pdf", seed=2))[0]
    col = a.lib.create_collection("Reading")
    roundtrip(a, b)
    bcol = next(c for c in b.lib.collections() if c.name == "Reading")

    a.lib.add_to_collection(col.id, [p1.id])
    b.lib.add_to_collection(bcol.id, [b.paper(p2.sha256).id])  # 同時に別の論文を足す
    roundtrip(a, b)

    for d in (a, b):
        c = next(c for c in d.lib.collections() if c.name == "Reading")
        members = {p.sha256 for p in d.lib.papers() if c.id in p.collection_ids}
        assert members == {p1.sha256, p2.sha256}


def test_same_named_collections_are_merged_on_first_connection_only(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    a.lib.create_collection("Thesis")
    b.lib.create_collection("Thesis")
    roundtrip(a, b)
    for d in (a, b):
        assert [c.name for c in d.lib.collections()].count("Thesis") == 1  # §12 Q10

    a.lib.create_collection("Later")
    b.lib.create_collection("Later")  # 接続したあとに、たまたま同じ名前で作った
    roundtrip(a, b)
    for d in (a, b):
        assert [c.name for c in d.lib.collections()].count("Later") == 2


def test_collection_rename_and_delete_propagate(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    col = a.lib.create_collection("Old")
    gone = a.lib.create_collection("Gone")
    roundtrip(a, b)

    a.lib.rename_collection(col.id, "New")
    a.lib.delete_collection(gone.id)
    roundtrip(a, b)
    assert sorted(c.name for c in b.lib.collections()) == ["New"]


# -- 削除とゴミ箱 ---------------------------------------------------------------------


def test_trash_propagates_and_restore_brings_it_back(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)

    a.lib.trash(paper.id)
    roundtrip(a, b)
    assert paper.sha256 not in {p.sha256 for p in b.lib.papers()}
    assert any(c.kind == "removed" and c.sha256 == paper.sha256 for c in b.changes)

    a.lib.restore(paper.id)
    roundtrip(a, b)
    assert paper.sha256 in {p.sha256 for p in b.lib.papers()}


def test_purged_paper_does_not_come_back_from_another_device(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)

    a.lib.purge(paper.id)  # CLI の remove と同じ
    roundtrip(a, b)
    roundtrip(a, b)
    assert a.paper(paper.sha256) is None
    assert paper.sha256 not in {p.sha256 for p in b.lib.papers()}


def test_delete_wins_over_a_concurrent_edit(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)

    b.lib.update_metadata(b.paper(paper.sha256).id, Metadata(title="Edited on B"), manual_fields={"title"})
    time.sleep(0.01)
    a.lib.trash(paper.id)
    roundtrip(a, b)

    for d in (a, b):
        assert paper.sha256 not in {p.sha256 for p in d.lib.papers()}  # §12 Q6: 削除が勝つ


def test_adding_the_same_pdf_again_revives_a_deleted_paper_everywhere(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    path = pdf(seed=1)
    paper = a.lib.add(path)[0]
    roundtrip(a, b)
    a.lib.purge(paper.id)
    roundtrip(a, b)

    b.lib.add(path)
    roundtrip(a, b)
    assert paper.sha256 in {p.sha256 for p in a.lib.papers()}


def test_evict_keeps_the_paper_in_the_cloud(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)
    bid = b.paper(paper.sha256).id
    b.engine.fetch(bid, "original")

    b.engine.evict(bid)  # 「このパソコンのコピーを削除（クラウドには残す）」
    assert b.paper(paper.sha256).has_original is False
    roundtrip(a, b)
    assert a.paper(paper.sha256) is not None and a.paper(paper.sha256).has_original is True
    assert b.engine.fetch(bid, "original").exists()


# -- 訳文 -----------------------------------------------------------------------------


def test_translation_is_synced_as_a_pdf(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    data = pdf(name="t.pdf", seed=2).read_bytes()
    a.lib.save_translation(paper.id, data, translation(paper_id=paper.id, model="gemini-x"))
    roundtrip(a, b)

    got = b.paper(paper.sha256)
    assert got.translation is not None and got.translation.model == "gemini-x"
    assert got.has_translation_file is False  # 訳文は ☁（§5.5.3）
    assert b.engine.fetch(got.id, "translation").read_bytes() == data


def test_complete_translation_wins_over_a_newer_partial_one(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)

    full = pdf(name="full.pdf", seed=3).read_bytes()
    a.lib.save_translation(paper.id, full, translation(paper_id=paper.id, untranslated=0, last_page=None))
    time.sleep(0.01)
    partial = pdf(name="part.pdf", seed=4).read_bytes()
    b.lib.save_translation(
        b.paper(paper.sha256).id, partial, translation(paper_id=0, untranslated=3, last_page=0, created_at=time.time())
    )
    roundtrip(a, b)
    roundtrip(a, b)

    for d in (a, b):
        got = d.paper(paper.sha256)
        assert got.translation.untranslated == 0  # §5.4: 欠けなく訳せたものが勝つ
        assert d.engine.fetch(got.id, "translation").read_bytes() == full


# -- 同期アプリの困った動き -------------------------------------------------------------


def test_offline_raises_but_the_local_library_keeps_working(make_device, pdf):
    a = make_device("A")
    paper = a.lib.add(pdf(seed=1))[0]
    a.storage.offline = True
    with pytest.raises(SyncUnavailable):
        a.sync()
    a.lib.set_favorite(paper.id, True)
    assert a.engine.fetch(paper.id, "original").exists()  # 手元にあれば同期フォルダに触らない

    a.storage.offline = False
    a.sync()


def test_state_arrives_before_the_pdf(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    a.storage.hold()
    paper = a.lib.add(pdf(seed=1))[0]
    a.sync()
    a.storage.deliver(lambda p: p.endswith(".json"))  # JSON だけが先に届く
    b.sync()

    bid = b.paper(paper.sha256).id
    with pytest.raises(RemoteMissing):
        b.engine.fetch(bid, "original")
    assert not b.paper(paper.sha256).has_original

    a.storage.deliver()
    assert b.engine.fetch(bid, "original").read_bytes() == paper.pdf_path.read_bytes()


def test_partially_synced_pdf_is_rejected_and_leaves_nothing_behind(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    a.storage.partial_next("papers/*/original.pdf")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)

    bid = b.paper(paper.sha256).id
    with pytest.raises((HashMismatch, RemoteMissing)):
        b.engine.fetch(bid, "original")
    got = b.paper(paper.sha256)
    assert not got.has_original
    assert not got.pdf_path.exists()
    assert not list(got.folder.glob("*.tmp")) if got.folder.exists() else True


def test_unreadable_json_is_treated_as_not_yet_arrived(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    a.storage.partial_next("papers/*/*.json")
    paper = a.lib.add(pdf(seed=1))[0]
    a.sync()
    b.sync()  # 途中までの JSON を読んでも落ちない
    assert b.paper(paper.sha256) is None

    a.lib.set_favorite(paper.id, True)  # 次の書き出しで完全な JSON が届く
    roundtrip(a, b)
    assert b.paper(paper.sha256) is not None and b.paper(paper.sha256).favorite


def test_conflict_copies_from_sync_apps_are_merged_harmlessly(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    a.lib.update_metadata(paper.id, Metadata(title="Original"), manual_fields={"title"})
    a.sync()
    state = next(p for p in stored_paths(a.storage) if p.startswith(f"papers/{paper.sha256}/") and p.endswith(".json"))
    base = state.rsplit("/", 1)[1].removesuffix(".json")
    for name in (f"{base} (1).json", f"{base} (B's conflicted copy 2026-10-31).json", f"{base}-B.json", f".{base}.sync-conflict-1.json"):
        a.storage.conflict_copy(state, name)

    b.sync()
    assert b.paper(paper.sha256).metadata.title == "Original"
    assert len(b.lib.papers()) == 1


def test_icloud_placeholder_is_not_downloaded(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    roundtrip(a, b)
    a.storage.make_placeholder(f"papers/{paper.sha256}/original.pdf")

    bid = b.paper(paper.sha256).id
    b.sync()
    assert not b.paper(paper.sha256).has_original
    with pytest.raises((RemoteMissing, SyncUnavailable)):
        b.engine.fetch(bid, "original")


def test_newer_sync_format_makes_this_device_read_only(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    a.lib.add(pdf(seed=1))
    roundtrip(a, b)
    future = b.storage.root / "devices" / f"{'f' * 32}.json"
    future.parent.mkdir(parents=True, exist_ok=True)
    future.write_text(json.dumps({"id": "f" * 32, "name": "Future", "format": 999}), encoding="utf-8")

    writes = len(b.storage.writes)
    b.lib.add(pdf(name="new.pdf", seed=5))
    report = b.sync()
    assert report.read_only is True
    assert len(b.storage.writes) == writes  # 新しい形式を壊さないよう、書き込まない
    assert len(b.lib.papers()) == 2  # 手元のライブラリは使える


def test_malicious_entries_in_the_sync_folder_are_ignored(make_device, pdf, tmp_path):
    a, b = make_device("A"), make_device("B")
    a.lib.add(pdf(seed=1))
    roundtrip(a, b)
    root = b.storage.root
    (root / "papers" / "not-a-sha").mkdir(parents=True)
    (root / "papers" / "not-a-sha" / "state.x.json").write_text("{}", encoding="utf-8")
    bad = root / "papers" / ("0" * 64)
    bad.mkdir(parents=True)
    (bad / f"state.{'e' * 32}.json").write_text("[" * 100000, encoding="utf-8")  # 深い入れ子
    (bad / f"state.{'d' * 32}.json").write_bytes(b"\xff\xfe garbage")

    b.sync()  # 例外で落ちない
    assert len(b.lib.papers()) == 1
    assert not (tmp_path / "escaped").exists()


def test_concurrent_offline_work_converges(make_device, pdf):
    """2 台がそれぞれオフラインで作業し、あとでつながっても、両方が同じ状態になる。"""
    a, b = make_device("A"), make_device("B")
    shared = a.lib.add(pdf(name="s.pdf", seed=1))[0]
    roundtrip(a, b)

    a.storage.offline = b.storage.offline = True
    a.lib.add(pdf(name="a.pdf", seed=2))
    b.lib.add(pdf(name="b.pdf", seed=3))
    a.lib.set_favorite(shared.id, True)
    b.lib.update_metadata(b.paper(shared.sha256).id, Metadata(year=2017), manual_fields={"year"})
    a.storage.offline = b.storage.offline = False
    roundtrip(a, b)
    roundtrip(a, b)

    def snapshot(d: Device):
        return sorted(
            (p.sha256, p.favorite, p.metadata.title, p.metadata.year, frozenset(p.edited)) for p in d.lib.papers()
        )

    assert len(a.lib.papers()) == 3
    assert snapshot(a) == snapshot(b)


# -- ノート ------------------------------------------------------------------------


def test_note_written_on_one_device_appears_on_the_other(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    a.lib.set_note(paper.id, "第 3 節を読み直す")
    roundtrip(a, b)
    got = b.paper(paper.sha256)
    assert got.note == "第 3 節を読み直す" and not got.note_merged
    assert any(c.kind == "note" and c.sha256 == paper.sha256 for c in b.changes)

    b.changes.clear()
    b.lib.set_note(got.id, "第 3 節を読み直した")  # 見てから書いたので、連結しない
    roundtrip(a, b)
    assert a.paper(paper.sha256).note == "第 3 節を読み直した" and not a.paper(paper.sha256).note_merged
    assert [c.kind for c in a.changes if c.sha256 == paper.sha256][-1] == "note"


def test_notes_written_at_the_same_time_are_both_kept(make_device, pdf):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    a.lib.set_note(paper.id, "共通")
    roundtrip(a, b)

    a.lib.set_note(paper.id, "A で追記")
    time.sleep(0.01)
    b.lib.set_note(b.paper(paper.sha256).id, "B で追記")
    roundtrip(a, b)
    texts = {d.paper(paper.sha256).note for d in (a, b)}
    assert len(texts) == 1
    text = texts.pop()
    assert "A で追記" in text and "B で追記" in text and "同時に編集された内容" in text
    assert text.index("B で追記") < text.index("A で追記")  # 新しい方（B）が先
    assert a.paper(paper.sha256).note_merged and b.paper(paper.sha256).note_merged

    roundtrip(a, b)
    roundtrip(a, b)
    assert a.paper(paper.sha256).note == b.paper(paper.sha256).note == text  # 往復して重ならない

    a.lib.set_note(paper.id, "整理した")  # 連結を見てから書いたので、そのまま届く
    roundtrip(a, b)
    assert b.paper(paper.sha256).note == "整理した" and not b.paper(paper.sha256).note_merged


def test_notes_from_three_devices_converge(make_device, pdf):
    devices = [make_device(n) for n in ("A", "B", "C")]
    paper = devices[0].lib.add(pdf(seed=1))[0]
    roundtrip(*devices)
    for d in devices:
        d.lib.set_note(d.paper(paper.sha256).id, f"{d.name} のメモ")
        time.sleep(0.01)
    roundtrip(*devices)
    roundtrip(*devices)
    notes = {d.paper(paper.sha256).note for d in devices}
    assert len(notes) == 1
    text = devices[0].paper(paper.sha256).note
    assert all(f"{n} のメモ" in text for n in ("A", "B", "C")) and text.count("同時に編集された内容") == 2


def test_broken_note_in_the_sync_folder_is_ignored(make_device, pdf, sync_root):
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    a.lib.set_note(paper.id, "元のメモ")
    a.sync()
    path = next(sync_root.glob(f"papers/{paper.sha256}/state.*.json"))
    body = json.loads(path.read_text(encoding="utf-8"))
    body["note"] = {"text": ["not", "text"], "hlc": "x", "vv": "y"}
    path.write_text(json.dumps(body), encoding="utf-8")
    b.sync()
    got = b.paper(paper.sha256)
    assert got is not None and got.note == ""  # ノートだけを捨て、論文は取り込む
