"""セキュリティの点検（v0.4.0 の後）で直した点のテスト。通信はしない。"""

import hashlib
import json
import os
import time

import httpx
import pytest
from pdf_factory import make_library_paper

from spectra import update
from spectra.config import fonts as font_store
from spectra.core.extract import MAX_GRID_CELLS, _split_block, cluster_drawings
from spectra.core.models import Line, Span
from spectra.library import Library, _http, crossref
from spectra.library.bibtex import to_bibtex
from spectra.library.errors import HashMismatch, LookupFailed
from spectra.library.models import Author, Metadata
from spectra.library.sync import DeviceInfo, SyncEngine
from spectra.library.sync import files as F
from spectra.library.sync.hlc import HLC, MAX_DRIFT_MS, HybridClock
from spectra.library.sync.testing import FakeFolderStorage
from spectra.translate.errors import RateLimitError, ServiceError
from spectra.translate.gemini import GeminiTranslator

A, B, C = "a" * 32, "b" * 32, "c" * 32


def _hlc(device=C, wall=None) -> str:
    return f"{int((wall or time.time()) * 1000):013d}.000000.{device}"


@pytest.fixture()
def pdf(tmp_path):
    def make(name="paper.pdf", seed=0, **kw):
        path = tmp_path / "src" / name
        path.parent.mkdir(exist_ok=True)
        make_library_paper(str(path), seed=seed, **kw)
        return path

    return make


@pytest.fixture()
def device(tmp_path):
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


# -- A1. 同期の時刻（HLC） ------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "9.0." + C,  # 桁が足りない（文字列の比較では、どの時刻よりも新しくなる）
        "99999999999999999.000000." + C,  # 桁が多い
        "1700000000000.0000000." + C,
        "1700000000000.000000.dev",  # 端末 ID の形でない
        "1700000000000.000000." + C + "\n",
        "１７０００００００００００.000000." + C,  # 全角の数字
        1700000000000.0,
        None,
        ["1700000000000.000000." + C],
    ],
)
def test_hlc_must_have_the_exact_shape(bad):
    assert F._hlc(bad) is None


def test_hlc_written_by_spectra_is_accepted():
    stamp = str(HybridClock(A).tick())
    assert F._hlc(stamp) == stamp


def test_ids_do_not_accept_a_trailing_newline():
    assert not F.HEX32_RE.match(C + "\n") and not F.SHA_RE.match("e" * 64 + "\n")


def test_clock_ignores_a_far_future_time():
    now = [1_700_000_000.0]
    clock = HybridClock(A, now=lambda: now[0])
    clock.observe(HLC(int(now[0] * 1000) + MAX_DRIFT_MS + 1, 0, B))
    assert clock.tick().wall_ms == int(now[0] * 1000)  # 遠い未来の時刻には引きずられない
    near = HLC(int(now[0] * 1000) + 60_000, 3, B)
    clock.observe(near)  # 時計のずれの範囲なら、今までどおり相手の後になる
    assert clock.tick() > near


def test_clock_counter_does_not_outgrow_its_digits():
    clock = HybridClock(A, last=HLC(2_000_000_000_000, 999_999, A), now=lambda: 1.0)
    stamp = str(clock.tick())
    assert F._hlc(stamp) == stamp and stamp > str(HLC(2_000_000_000_000, 999_999, A))


def test_far_future_stamp_in_the_sync_folder_does_not_poison_the_clock(device, pdf, tmp_path):
    lib, engine = device("B", B)
    paper, _ = lib.add(pdf())
    engine.sync_now()
    future = time.time() + 400 * 86400
    folder = tmp_path / "cloud" / "papers" / paper.sha256
    (folder / f"state.{C}.json").write_text(json.dumps({
        "kind": "paper", "v": 1, "device": C, "sha": paper.sha256,
        "fields": {"title": ["From the future", _hlc(wall=future), False], "venue": ["Short", "9.0." + C, False]},
        "life": [True, _hlc(wall=future)], "file": {"name": "x.pdf", "pages": 1, "added_at": time.time()},
    }))
    engine.sync_now()
    assert engine.clock.last.wall_ms < (time.time() + 3600) * 1000
    assert lib.get(paper.id).metadata.venue != "Short"  # 形の違う時刻の項目は捨てる


# -- A2. このパソコンのコピーを削除 --------------------------------------------------------


def test_evict_keeps_the_local_copy_when_the_remote_copy_is_damaged(device, pdf, tmp_path):
    lib, engine = device("A", A)
    paper, _ = lib.add(pdf())
    engine.sync_now()
    remote = tmp_path / "cloud" / "papers" / paper.sha256 / "original.pdf"
    good = remote.read_bytes()
    remote.write_bytes(good[: len(good) // 2])  # 途中までしか届いていない
    with pytest.raises(HashMismatch):
        engine.evict(paper.id)
    assert lib.get(paper.id).has_original
    remote.write_bytes(good)
    engine.evict(paper.id)
    assert not lib.get(paper.id).has_original


def test_storage_sha256(tmp_path):
    storage = FakeFolderStorage(tmp_path / "cloud")
    storage.write_atomic("papers/x.bin", b"abc")
    assert storage.sha256("papers/x.bin") == hashlib.sha256(b"abc").hexdigest()


# -- A7. 一時ファイル ----------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="シンボリックリンクを作れる環境だけ")
def test_write_does_not_follow_a_symlink_planted_as_the_temp_file(tmp_path):
    storage = FakeFolderStorage(tmp_path / "cloud")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (tmp_path / "cloud" / "papers").mkdir()
    os.symlink(victim, tmp_path / "cloud" / "papers" / ".x.json.tmp")
    storage.write_atomic("papers/x.json", b"{}")
    src = tmp_path / "src.bin"
    src.write_bytes(b"data")
    os.symlink(victim, tmp_path / "cloud" / "papers" / ".y.pdf.tmp")
    super(FakeFolderStorage, storage).put_file(  # 偽物の put_file は write_atomic を通るので、本物を呼ぶ
        "papers/y.pdf", src
    )
    assert victim.read_text() == "keep me"
    assert (tmp_path / "cloud" / "papers" / "x.json").read_bytes() == b"{}"
    assert (tmp_path / "cloud" / "papers" / "y.pdf").read_bytes() == b"data"


# -- A3. 極端に大きなページ ------------------------------------------------------------


def test_cluster_drawings_bounds_its_grid_on_a_huge_page():
    huge = (0.0, 0.0, 1e7, 1e7)
    drawings = [{"rect": (10, 10, 60, 60)}, {"rect": (30, 30, 90, 90)}, {"rect": (9e6, 9e6, 9e6 + 50, 9e6 + 50)}]
    started = time.monotonic()
    clusters = cluster_drawings(drawings, huge)
    assert time.monotonic() - started < 5
    assert sorted(len(members) for _rect, members in clusters) == [1, 2]
    assert MAX_GRID_CELLS <= 4_000_000


def test_cluster_drawings_ignores_a_page_without_a_finite_size():
    assert cluster_drawings([{"rect": (0, 0, 10, 10)}], (0.0, 0.0, float("inf"), 100.0)) == []


def test_normal_page_is_clustered_as_before():
    page = (0.0, 0.0, 595.0, 842.0)
    drawings = [{"rect": (100, 100, 150, 150)}, {"rect": (151, 100, 200, 150)}, {"rect": (400, 600, 450, 650)}]
    assert sorted(len(m) for _r, m in cluster_drawings(drawings, page)) == [1, 2]


def test_page_render_scale_is_capped():
    from spectra.ui.pdf_view import MAX_PAGE_PIXELS, capped_scale

    assert capped_scale(595, 842, 8.0) == 8.0  # A4 を 400%・Retina: そのまま
    scale = capped_scale(14400, 14400, 8.0)
    assert 14400 * 14400 * scale * scale <= MAX_PAGE_PIXELS * 1.001
    assert capped_scale(float("inf"), 100, 2.0) == 2.0  # 大きさが読めないページは、描画側の失敗に任せる


def test_many_heading_lines_do_not_overflow_the_stack():
    def line(i: int) -> Line:
        box = (72.0, 20.0 * i, 200.0, 20.0 * i + 10)
        return Line(spans=[Span("Abstract", "Times", 10.0, 0, 0, box, (72.0, box[3]))], bbox=box, direction=(1.0, 0.0))

    parts = _split_block([line(i) for i in range(3000)])
    assert len(parts) == 3000


# -- A4. フォントのダウンロード ---------------------------------------------------------


@pytest.fixture()
def fake_font(monkeypatch, tmp_path):
    good = {"Fake-Regular.otf": b"regular" * 100, "Fake-Bold.otf": b"bold" * 100}
    choice = font_store.FontChoice(
        "fake", "Fake", "", True,
        {name: f"https://example.invalid/{name}" for name in good}, 0.1,
        {name: (len(data), hashlib.sha256(data).hexdigest()) for name, data in good.items()},
    )
    monkeypatch.setitem(font_store.FONT_CHOICES, "fake", choice)
    monkeypatch.setattr(font_store.paths, "fonts_dir", lambda: tmp_path / "fonts")
    return good


def _font_client(bodies: dict[str, bytes]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies[request.url.path.rsplit("/", 1)[-1]])

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_font_download_accepts_only_the_expected_files(fake_font, tmp_path):
    seen = []
    font_store.download("fake", progress=lambda d, t: seen.append((d, t)), client=_font_client(fake_font))
    assert font_store.is_installed("fake")
    assert seen[-1][0] == seen[-1][1] == sum(len(v) for v in fake_font.values())


@pytest.mark.parametrize("tamper", [lambda b: b[:-1] + b"X", lambda b: b + b"more" * 1000, lambda b: b[:10]])
def test_font_download_rejects_a_different_file(fake_font, tmp_path, tamper):
    bodies = dict(fake_font)
    bodies["Fake-Bold.otf"] = tamper(bodies["Fake-Bold.otf"])
    with pytest.raises(font_store.FontDownloadError):
        font_store.download("fake", client=_font_client(bodies))
    assert not font_store.is_installed("fake")
    assert not list((tmp_path / "fonts").glob("*.part"))  # 書きかけを残さない


def test_font_urls_are_pinned_to_a_commit():
    choice = font_store.FONT_CHOICES["noto-serif"]
    assert set(choice.checks) == set(choice.files)
    for name, url in choice.files.items():
        assert "/raw/main/" not in url and font_store._NOTO_COMMIT in url
        size, sha = choice.checks[name]
        assert size > 1_000_000 and len(sha) == 64


# -- A6. BibTeX ---------------------------------------------------------------------


def test_bibtex_fields_cannot_break_out(tmp_path, pdf):
    lib = Library(tmp_path / "lib")
    try:
        paper, _ = lib.add(pdf())
        evil = Metadata(
            title="Nice}}, note = {\\input{/etc/passwd}} 50% & ~x^2",
            authors=[Author("Doe}, x = {y", "J\\write18{rm}")],
            year=2024, venue="J. {Evil}", volume="1}", issue="\\x", pages="1-9}",
            doi="10.1/a}b\\c d", arxiv_id="2401.00001}", url="https://e.org/a_b%20c}\n@misc{x,",
        )
        paper = lib.update_metadata(paper.id, evil, manual_fields=set(Metadata.FIELDS))
        bib = to_bibtex(paper)
    finally:
        lib.close()
    # 波括弧は、エスケープしたものを除くと、項目とエントリを囲むものだけで、釣り合っている
    bare = bib.replace("\\{", "").replace("\\}", "")
    depth = 0
    for ch in bare:
        depth += (ch == "{") - (ch == "}")
        assert depth >= 0
    assert depth == 0 and bib.startswith("@article{") and "\n@" not in bib  # エントリは 1 つだけ
    assert "\\input" not in bib and "\\write18" not in bib
    assert "\\textbackslash{}input" in bib and "50\\% \\& \\textasciitilde{}x\\textasciicircum{}2" in bib
    assert "doi = {10.1/abcd}" in bib and "url = {https://e.org/a_b%20c@miscx,}" in bib


# -- A9. 応答の検証 -----------------------------------------------------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_lookup_response_is_bounded(monkeypatch):
    monkeypatch.setattr(_http, "MAX_BODY", 1000)
    big = _client(lambda r: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(LookupFailed):
        _http.get(big, "https://api.crossref.org/works/10.1/x", service="Crossref")
    ok = _client(lambda r: httpx.Response(200, json={"message": {"title": ["T"]}}, headers={"content-encoding": "identity"}))
    assert _http.get(ok, "https://api.crossref.org/works/10.1/x", service="Crossref").json()["message"]["title"] == ["T"]


@pytest.mark.parametrize(
    "message",
    [{"author": ["not-a-dict"]}, {"issued": {"date-parts": [["nineteen"]]}}, {"issued": "2020"}, ["list"], {"author": 3}],
)
def test_crossref_odd_shapes_are_a_lookup_failure(message):
    client = _client(lambda r: httpx.Response(200, json={"message": message}))
    with pytest.raises(LookupFailed):
        crossref.fetch("10.1/x", client)


def test_update_response_is_bounded_and_parsed(monkeypatch):
    body = {"tag_name": "v99.0.0", "html_url": update.RELEASES_URL + "tag/v99.0.0"}
    assert update.check_for_update("0.4.0", _client(lambda r: httpx.Response(200, json=body))).version == "99.0.0"
    monkeypatch.setattr(update, "MAX_BODY", 100)
    with pytest.raises(update.UpdateCheckError):
        update.fetch_latest(_client(lambda r: httpx.Response(200, content=b" " * 500 + b"{}")))


@pytest.mark.parametrize(
    "url",
    [
        update.RELEASES_URL + "../../../../evil/repo",
        update.RELEASES_URL + "tag/v1.0.0?x=1",
        update.RELEASES_URL + "tag/v1 .0",
        update.RELEASES_URL + "tag/v1.0.0#frag",
        update.RELEASES_URL + "tag/%2e%2e/x",
    ],
)
def test_release_url_must_stay_on_the_releases_page(url):
    assert not update.is_release_url(url)


def test_release_urls_from_github_are_accepted():
    assert update.is_release_url(update.RELEASES_URL)
    assert update.is_release_url(update.RELEASES_URL + "tag/v0.5.0")
    assert update.is_release_url(update.RELEASES_URL + "tag/v0.5.0-rc.1+build")


def _gemini(handler) -> GeminiTranslator:
    t = GeminiTranslator(api_key="k", client=_client(handler), auto_fallback=False)
    t.sleep = lambda _s: None
    return t


def test_gemini_non_json_success_is_a_service_error():
    t = _gemini(lambda r: httpx.Response(200, content=b"<html>proxy page</html>"))
    with pytest.raises(ServiceError):
        t._request([{"id": 0, "text": "x"}])
    t = _gemini(lambda r: httpx.Response(200, json=["not", "a", "dict"]))
    with pytest.raises(ServiceError):
        t._request([{"id": 0, "text": "x"}])


@pytest.mark.parametrize("delay", ["NaNs", "-5s", "infs", "0s"])
def test_gemini_ignores_a_retry_delay_that_is_not_a_positive_number(delay):
    body = {"error": {"status": "RESOURCE_EXHAUSTED", "message": "slow down", "details": [{"retryDelay": delay}]}}
    with pytest.raises(RateLimitError) as e:
        GeminiTranslator._raise_for_status(httpx.Response(429, json=body))
    assert e.value.retry_after is None


def test_gemini_keeps_a_normal_retry_delay():
    body = {"error": {"status": "RESOURCE_EXHAUSTED", "message": "slow down", "details": [{"retryDelay": "7s"}]}}
    with pytest.raises(RateLimitError) as e:
        GeminiTranslator._raise_for_status(httpx.Response(429, json=body))
    assert e.value.retry_after == 7.0


# -- A10. 書誌情報を読めない PDF ---------------------------------------------------------


def test_paper_is_added_even_if_its_metadata_cannot_be_read(tmp_path, pdf, monkeypatch):
    from spectra.library import store

    def boom(_doc, _name):
        raise RuntimeError("broken page")

    monkeypatch.setattr(store, "extract_local", boom)
    lib = Library(tmp_path / "lib")
    try:
        paper, existed = lib.add(pdf("broken.pdf"))
        assert not existed and paper.has_original and paper.display_title == "broken"
    finally:
        lib.close()


# -- A12. PDF 出力 ------------------------------------------------------------------


def test_export_write_is_atomic(tmp_path, monkeypatch):
    from spectra.ui import main_window

    target = tmp_path / "out.pdf"
    target.write_bytes(b"old")
    main_window.write_file_atomic(target, b"new")
    assert target.read_bytes() == b"new" and [p.name for p in tmp_path.iterdir()] == ["out.pdf"]

    def fail(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(main_window.os, "replace", fail)
    with pytest.raises(OSError):
        main_window.write_file_atomic(target, b"broken")
    assert target.read_bytes() == b"new" and [p.name for p in tmp_path.iterdir()] == ["out.pdf"]

