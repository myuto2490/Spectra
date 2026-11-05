"""端末名（利用者が設定で変えられる）と、端末の指紋のテスト。"""

import hashlib
import json
import platform

import pytest
from pdf_factory import make_library_paper

from spectra.config.settings import Settings
from spectra.library import Library
from spectra.library.sync import (
    MAX_NAME_LENGTH,
    DeviceInfo,
    SyncEngine,
    clean_device_name,
    default_name,
    load_device,
)
from spectra.library.sync.device import _fingerprint, _raw_fingerprint
from spectra.library.sync.testing import FakeFolderStorage

A = "a" * 32


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  研究室の Mac  ", "研究室の Mac"),
        ("‮evil\x00\x07name\n", "evilname"),
        ("a" * 40, "a" * MAX_NAME_LENGTH),
        ("   ", ""),
        ("", ""),
        (None, ""),
        ("two   spaces", "two spaces"),
    ],
)
def test_clean_device_name(raw, expected):
    assert clean_device_name(raw) == expected


def test_default_name_does_not_use_hostname(tmp_path):
    info = load_device(tmp_path / "device.json")
    assert info.name == default_name(info.id)
    host = platform.node()
    if host:
        assert host not in info.name
        assert host.split(".")[0] not in info.name


def test_custom_name_is_not_written_to_device_file(tmp_path):
    path = tmp_path / "device.json"
    info = load_device(path, name="  研究室の Mac ")
    assert info.name == "研究室の Mac"
    assert "研究室" not in path.read_text(encoding="utf-8")  # 利用者の名前は設定にだけ置く
    assert load_device(path, name="").name == default_name(info.id)  # 空なら既定に戻る
    assert load_device(path, name="‮\x00 ").name == default_name(info.id)


def test_fingerprint_is_salted_with_device_id(tmp_path):
    assert _fingerprint("a" * 32) != _fingerprint("b" * 32)
    unsalted = hashlib.sha256(_raw_fingerprint().encode()).hexdigest()
    path = tmp_path / "device.json"
    path.write_text(json.dumps({"id": A, "name": default_name(A), "fingerprint": unsalted}))
    assert load_device(path).id == A  # 以前の形でも同じ端末とみなす
    assert json.loads(path.read_text())["fingerprint"] == _fingerprint(A)  # 新しい形で書き直す
    raw = json.loads(path.read_text())
    raw["fingerprint"] = hashlib.sha256(f"{'b' * 32}|{_raw_fingerprint()}".encode()).hexdigest()
    path.write_text(json.dumps(raw))
    assert load_device(path).id != A  # 別の ID の指紋（＝別のパソコンからのコピー）は作り直す


@pytest.fixture()
def engine(tmp_path):
    lib = Library(tmp_path / "lib")
    make_library_paper(str(tmp_path / "p.pdf"))
    lib.add(tmp_path / "p.pdf")
    e = SyncEngine(lib, FakeFolderStorage(tmp_path / "cloud"), DeviceInfo(A, default_name(A)))
    yield e
    e.close()
    lib.close()


def _device_file(engine) -> dict:
    return json.loads((engine.storage.root / "devices" / f"{A}.json").read_text(encoding="utf-8"))


def test_set_device_name_rewrites_and_leaves_no_old_name(engine, tmp_path):
    engine.set_device_name("Old Lab Machine")
    engine.sync_now()
    assert _device_file(engine)["name"] == "Old Lab Machine"
    # 同期アプリが作った、自分の情報の競合コピー（古い名前入り）
    copy = engine.storage.root / "devices" / f"{A} (1).json"
    copy.write_text((engine.storage.root / "devices" / f"{A}.json").read_text(encoding="utf-8"))
    assert engine.set_device_name("  研究室の Mac‮ ") == "研究室の Mac"
    assert engine.devices()[0].name == "研究室の Mac"  # 同期を待たずに、すぐに反映
    engine.sync_now()
    assert _device_file(engine)["name"] == "研究室の Mac"
    assert not copy.exists()
    for f in engine.storage.root.rglob("*"):
        if f.is_file():
            assert b"Old Lab Machine" not in f.read_bytes(), f
    rows = engine.state.query("SELECT value FROM meta") + engine.state.query("SELECT data AS value FROM remote_files WHERE data IS NOT NULL")
    assert not any("Old Lab Machine" in (r["value"] or "") for r in rows)


def test_empty_name_returns_to_default(engine):
    engine.set_device_name("Custom")
    engine.sync_now()
    assert engine.set_device_name("   ") == default_name(A)
    engine.sync_now()
    assert _device_file(engine)["name"] == default_name(A)


def test_unchanged_name_does_not_rewrite(engine):
    engine.sync_now()
    writes = len(engine.storage.writes)
    engine.sync_now()
    assert len(engine.storage.writes) == writes


def test_settings_device_name():
    s = Settings()
    assert s.device_name == "" and s.schema >= 5
    assert Settings.from_dict({"device_name": 12, "schema": 4}).device_name == ""
    assert Settings.from_dict({"device_name": "Lab", "schema": 5}).device_name == "Lab"
