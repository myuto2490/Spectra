"""同期の core のセキュリティのテスト。同期フォルダの中身は、ほかのパソコンやクラウドから来る信頼できない入力として扱う。"""

import json
import os
import platform
import time
from pathlib import Path

import pytest
from pdf_factory import make_library_paper

from spectra.library import Library
from spectra.library.sync import DeviceInfo, FolderStorage, SyncEngine, SyncUnavailable, load_device
from spectra.library.sync import files as F
from spectra.library.sync.device import _raw_fingerprint
from spectra.library.sync.storage import check_relative
from spectra.library.sync.testing import FakeFolderStorage

A, B, C = "a" * 32, "b" * 32, "c" * 32


@pytest.fixture()
def pair(tmp_path):
    made = []

    def make(name, device_id):
        lib = Library(tmp_path / name)
        engine = SyncEngine(lib, FakeFolderStorage(tmp_path / "cloud"), DeviceInfo(device_id, name))
        made.append((engine, lib))
        return lib, engine

    yield make
    for engine, lib in made:
        engine.close()
        lib.close()


@pytest.fixture()
def pdf(tmp_path):
    def make(name="paper.pdf", seed=0, **kw):
        path = tmp_path / "src" / name
        path.parent.mkdir(exist_ok=True)
        make_library_paper(str(path), seed=seed, **kw)
        return path

    return make


def _plant(cloud: Path, sha: str, name: str, body) -> None:
    folder = cloud / "papers" / sha
    folder.mkdir(parents=True, exist_ok=True)
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    (folder / name).write_bytes(data)


def _hlc(device=C, wall=None) -> str:
    return f"{int((wall or time.time()) * 1000):013d}.000000.{device}"


# -- 1. パスのすり抜け ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["../x", "/etc/passwd", "a/../../b", "C:/Windows/x", "C:x", "a\\b", "con.json", "papers/aux/x.json",
     "nul", "LPT1.txt", "papers/x./y", "papers/x /y", "a\x00b", "a\nb", ""],
)
def test_check_relative_rejects(bad):
    with pytest.raises(ValueError):
        check_relative(bad)


def test_remote_file_name_cannot_escape(pair, pdf, tmp_path):
    """ほかの端末から来たファイル名は、最後の要素だけを残す（表示と保存の候補にしか使わないが、念のため）。"""
    assert F.clean_file_name("../../etc/passwd") == "passwd"
    assert F.clean_file_name("C:\\Users\\taro\\secret.pdf") == "secret.pdf"
    assert F.clean_file_name("..") == ""
    lib_a, a = pair("A", A)
    lib_b, b = pair("B", B)
    p, _ = lib_a.add(pdf())
    a.sync_now()
    path = tmp_path / "cloud" / "papers" / p.sha256 / f"state.{A}.json"
    body = json.loads(path.read_text())
    body["file"]["name"] = "../../../../outside.pdf"
    path.write_text(json.dumps(body))
    b.sync_now()
    got = lib_b.find_by_sha(p.sha256)
    assert got.file_name == "outside.pdf"
    assert got.pdf_path.parent == lib_b.root / "files" / str(got.id)  # 保存場所はファイル名に左右されない


# -- 2. シンボリックリンク ---------------------------------------------------------------


def test_symlinked_directory_is_not_followed(pair, pdf, tmp_path):
    lib_a, a = pair("A", A)
    p, _ = lib_a.add(pdf())
    a.sync_now()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text('{"kind": "paper"}')
    cloud = tmp_path / "cloud"
    fake_sha = "f" * 64
    (cloud / "papers" / fake_sha).symlink_to(outside, target_is_directory=True)
    storage = FolderStorage(cloud)
    assert not any(e.path.startswith(f"papers/{fake_sha}") for e in storage.list())  # たどらない
    with pytest.raises(ValueError):
        storage.write_atomic(f"papers/{fake_sha}/state.{A}.json", b"{}")
    assert not (outside / f"state.{A}.json").exists()


def test_symlinked_file_is_not_read(tmp_path):
    cloud = tmp_path / "cloud"
    (cloud / "papers").mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (cloud / "papers" / "x.json").symlink_to(secret)
    storage = FolderStorage(cloud)
    assert storage.list() == []
    with pytest.raises(ValueError):
        storage.read_bytes("papers/x.json")
    with pytest.raises(OSError):  # 確認の後にすり替えられても、開くときにたどらない
        FolderStorage._open_read(cloud / "papers" / "x.json")


def test_symlinked_file_is_not_opened_without_nofollow(tmp_path, monkeypatch):
    """O_NOFOLLOW のない Windows でも、シンボリックリンクをたどって開かない。"""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (tmp_path / "x.json").symlink_to(secret)
    with pytest.raises(OSError):
        FolderStorage._open_read(tmp_path / "x.json")


def test_write_replaces_link_instead_of_writing_through(tmp_path):
    """書き込みは一時ファイル → rename なので、ハードリンクの先（外のファイル）を書き換えない。"""
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("original")
    os.link(outside, cloud / "format.json")
    FolderStorage(cloud).write_atomic("format.json", b"new")
    assert outside.read_text() == "original"


# -- 3. 壊れた・悪意のあるデータ ---------------------------------------------------------------


def test_deeply_nested_json_is_rejected():
    assert F.decode(b"[" * 200_000 + b"]" * 200_000) is None  # RecursionError で落ちない
    assert F.decode(b'{"a": NaN}') is None and F.decode(b'{"a": Infinity}') is None
    assert F.decode(b"\xff\xfe") is None
    assert F.decode(b"{" + b" " * (F.MAX_JSON_BYTES + 1) + b"}") is None


def test_malicious_state_does_not_crash_sync(pair, pdf, tmp_path):
    lib_b, b = pair("B", B)
    sha = "e" * 64
    cloud = tmp_path / "cloud"
    _plant(cloud, sha, f"state.{C}.json", {
        "kind": "paper", "v": 1, "device": C, "sha": sha,
        "fields": {
            "title": ["Evil\u202eftp.exe\x07 <b>title</b>", _hlc(), False],
            "year": [10**40, _hlc(), False],
            "authors": [[{"family": 1}], _hlc(), False],
        },
        "life": [True, _hlc()],
        "file": {"name": "x.pdf", "pages": 10**12, "added_at": 1e308},
        "opened": -5,
        "collections": "not-a-dict",
    })
    _plant(cloud, sha, f"translation.{C}.json", {
        "kind": "translation", "device": C, "sha": sha, "hlc": _hlc(), "alive": True, "pdf_sha": "0" * 64,
        "info": {"first_page": 5, "last_page": 2, "created_at": 1.0},
    })
    _plant(cloud, sha, "state.deep.json", b"[" * 100_000)
    _plant(cloud, sha, "state.big.json", b'{"kind": "paper", "pad": "' + b"x" * (F.MAX_JSON_BYTES + 10) + b'"}')
    report = b.sync_now()  # 落ちない
    got = lib_b.find_by_sha(sha)
    assert got is not None
    assert got.metadata.title == "Evilftp.exe <b>title</b>"  # 向きを入れ替える文字・制御文字を除く
    assert got.metadata.year is None and got.metadata.authors == []
    assert got.page_count == 100_000 and 0 < got.added_at < 4_102_444_800  # 巨大な値は丸める・捨てる
    assert got.last_opened_at is None
    assert got.translation is None  # last_page < first_page の訳文は捨てる
    assert report.errors == []


def test_translation_values_are_bounded():
    entry = {
        "kind": "translation", "device": C, "sha": "e" * 64, "hlc": _hlc(), "alive": True, "pdf_sha": "0" * 64,
        "info": {"first_page": -3, "last_page": None, "segments": -1, "chars": True, "created_at": float("inf"),
                 "options": {"x": "y" * 20_000}},
    }
    assert F.parse_translation(entry, "e" * 64) is None  # 時刻が無限大
    entry["info"]["created_at"] = 1_700_000_000
    got = F.parse_translation(entry, "e" * 64)
    assert got["info"]["first_page"] == 0 and got["info"]["segments"] == 0 and got["info"]["chars"] == 0
    assert got["info"]["options"] == {}  # 巨大な設定は捨てる


def test_collection_name_is_cleaned(pair, tmp_path):
    lib_b, b = pair("B", B)
    uid = "d" * 32
    folder = tmp_path / "cloud" / "collections" / uid
    folder.mkdir(parents=True)
    (folder / f"state.{C}.json").write_text(json.dumps({
        "kind": "collection", "device": C, "uid": uid, "name": ["\u202eNLP\x00 '); DROP TABLE papers;--", _hlc()],
        "life": [True, _hlc()],
    }))
    b.sync_now()
    assert [c.name for c in lib_b.collections()] == ["NLP '); DROP TABLE papers;--"]  # SQL は値として扱われる
    assert lib_b.papers() == []  # テーブルは無事


# -- 5. 個人情報 ---------------------------------------------------------------------


def test_errors_do_not_contain_absolute_paths(tmp_path):
    secret_root = tmp_path / "GoogleDrive-taro@example.com" / "My Drive"
    storage = FolderStorage(secret_root)
    with pytest.raises(SyncUnavailable) as info:
        storage.list()
    assert "taro@example.com" not in info.value.user_message() and str(secret_root) not in str(info.value)
    secret_root.mkdir(parents=True)
    (secret_root / "papers").write_text("not a dir")
    with pytest.raises(SyncUnavailable) as info:
        storage.write_atomic("papers/x/state.json", b"{}")
    assert "taro@example.com" not in str(info.value)


def test_device_name_is_not_the_hostname(tmp_path):
    info = load_device(tmp_path / "device.json")
    assert platform.node() not in info.name or not platform.node()
    raw = json.loads((tmp_path / "device.json").read_text())
    assert platform.node() not in raw["fingerprint"] or not platform.node()  # 指紋はハッシュで持つ


def test_old_device_file_is_migrated(tmp_path):
    path = tmp_path / "device.json"
    path.write_text(json.dumps({"id": A, "name": platform.node(), "fingerprint": _raw_fingerprint()}))
    info = load_device(path)
    assert info.id == A  # 同じ端末として扱う
    assert info.name != platform.node() or not platform.node()
    assert json.loads(path.read_text())["fingerprint"] != _raw_fingerprint()


def test_sync_folder_contains_no_local_paths_or_secrets(pair, pdf, tmp_path, monkeypatch):
    monkeypatch.setenv("SPECTRA_GEMINI_API_KEY", "AIza-secret-key-for-test")
    lib_a, a = pair("A", A)
    p, _ = lib_a.add(pdf())
    lib_a.save_translation(p.id, b"%PDF-x", __import__("spectra.library", fromlist=["Translation"]).Translation(
        paper_id=p.id, model="m", formality="default", first_page=0, last_page=None,
        options={"font": "custom"}, segments=1, untranslated=0, chars=1, created_at=time.time(),
    ))
    a.sync_now()
    needles = [str(tmp_path), str(Path.home()), "AIza-secret-key-for-test", p.source_path]
    if platform.node():
        needles.append(platform.node())
    for f in (tmp_path / "cloud").rglob("*.json"):
        text = f.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, (f.name, needle)


@pytest.mark.parametrize(
    "invisible",
    ["​", "‌", "‍", "‎", "‏", "⁠", "⁡", "⁤", "﻿", "­",
     "‮", "⁦", "\u0085", "\u009f"],
)
def test_invisible_characters_are_removed(invisible):
    from spectra.library.sync import clean_device_name

    if invisible in ("\u200c", "\u200d"):  # 接合子は文章では残し、端末名とファイル名では除く
        assert F.clean_text(f"Ma{invisible}c") == f"Ma{invisible}c"
    else:
        assert F.clean_text(f"Ma{invisible}c") == "Mac"
    assert clean_device_name(f"Ma{invisible}c") == "Mac"
    assert F.clean_file_name(f"pa{invisible}per.pdf") == "paper.pdf"
    assert F.clean_text("改行\nとタブ\tは残す") == "改行\nとタブ\tは残す"


def test_lookalike_names_from_other_devices_are_unified(pair, pdf, tmp_path):
    """ほかの端末から来たタイトル・著者・コレクション名・端末名から、ゼロ幅スペースなどを除く。"""
    lib_b, b = pair("B", B)
    sha, uid = "e" * 64, "d" * 32
    cloud = tmp_path / "cloud"
    _plant(cloud, sha, f"state.{C}.json", {
        "kind": "paper", "v": 1, "device": C, "sha": sha,
        "fields": {"title": ["At​tention", _hlc(), False],
                   "authors": [[{"family": "Vas﻿wani", "given": "A⁠"}], _hlc(), False]},
        "life": [True, _hlc()], "file": {"name": "p‍p.pdf", "pages": 1, "added_at": time.time()},
    })
    folder = cloud / "collections" / uid
    folder.mkdir(parents=True)
    (folder / f"state.{C}.json").write_text(json.dumps({
        "kind": "collection", "device": C, "uid": uid, "name": ["N​LP", _hlc()], "life": [True, _hlc()],
    }))
    (cloud / "devices").mkdir(exist_ok=True)
    (cloud / "devices" / f"{C}.json").write_text(json.dumps({
        "kind": "device", "device": C, "name": "Mac​‮", "app": "0.2​", "format": 1,
    }))
    b.sync_now()
    got = lib_b.find_by_sha(sha)
    assert got.metadata.title == "Attention" and got.metadata.authors[0].family == "Vaswani"
    assert got.metadata.authors[0].given == "A" and got.file_name == "pp.pdf"
    assert [c.name for c in lib_b.collections()] == ["NLP"]
    other = next(d for d in b.devices() if d.id == C)
    assert other.name == "Mac" and other.app == "0.2"
