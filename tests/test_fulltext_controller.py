"""本文の索引づくりと問い合わせ（ui/fulltext_controller.py）。オフスクリーン・作り物の PDF。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pymupdf
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from spectra.config.settings import Settings  # noqa: E402
from spectra.library import Library, Translation  # noqa: E402
from spectra.library.fulltext import ORIGINAL, TRANSLATED, FulltextIndex  # noqa: E402
from spectra.ui.fulltext_controller import FulltextController, sources_for  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _pdf(path: Path, text: str, font: str = "helv") -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((20, 40), text + "\nlorem ipsum dolor sit amet", fontname=font, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def _wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def lib(tmp_path):
    library = Library(tmp_path / "library")
    yield library
    library.close()


def _controller(lib, tmp_path, settings=None, **kw):
    kw.setdefault("startup_delay_ms", 0)
    kw.setdefault("change_delay_ms", 0)
    kw.setdefault("query_delay_ms", 0)
    kw.setdefault("pause", 0)
    return FulltextController(settings or Settings(), lib, index_path=tmp_path / "cache" / "fulltext.sqlite3", **kw)


def _results(ctl):
    got = []
    ctl.resultsReady.connect(lambda text, found: got.append((text, found)))
    return got


def test_indexes_library_after_startup_and_finds_body_text(app, lib, tmp_path):
    paper, _ = lib.add(_pdf(tmp_path / "a.pdf", "scattering phase function"))
    ctl = _controller(lib, tmp_path)
    assert _wait(lambda: ctl.index is not None and not ctl.indexing and ctl.stats().indexed == 1)
    got = _results(ctl)
    ctl.query("phase")
    assert _wait(lambda: got)
    text, found = got[-1]
    assert text == "phase" and paper.sha256 in found
    ctl.close()


def test_startup_waits_before_indexing(app, lib, tmp_path):
    lib.add(_pdf(tmp_path / "a.pdf", "alpha beta gamma"))
    ctl = _controller(lib, tmp_path, startup_delay_ms=60_000)
    _wait(lambda: False, timeout=0.3)
    assert not ctl.indexing and ctl.index is None
    ctl.close()


def test_added_paper_and_translation_are_indexed_and_purge_prunes(app, lib, tmp_path):
    ctl = _controller(lib, tmp_path)
    paper, _ = lib.add(_pdf(tmp_path / "a.pdf", "original english words"))
    assert _wait(lambda: ctl.index is not None and not ctl.indexing and (paper.sha256, ORIGINAL) in ctl.index.known())
    ja = _pdf(tmp_path / "ja.pdf", "訳文の放射伝達", font="japan").read_bytes()
    lib.save_translation(paper.id, ja, Translation(paper.id, "m", "plain", 0, None, {}, 1, 0, 10, time.time()))
    assert _wait(lambda: not ctl.indexing and (paper.sha256, TRANSLATED) in ctl.index.known())
    lib.trash(paper.id)
    lib.purge(paper.id)
    assert _wait(lambda: not ctl.indexing and ctl.index.known() == {})
    ctl.close()


def test_translations_can_be_left_out(app, lib, tmp_path):
    paper, _ = lib.add(_pdf(tmp_path / "a.pdf", "english words here"))
    ja = _pdf(tmp_path / "ja.pdf", "訳文の放射伝達", font="japan").read_bytes()
    lib.save_translation(paper.id, ja, Translation(paper.id, "m", "plain", 0, None, {}, 1, 0, 10, time.time()))
    kinds = {s.kind for s in sources_for(lib, translations=False)}
    assert kinds == {ORIGINAL}


def test_paper_whose_pdf_has_not_arrived_is_skipped_until_the_file_exists(app, lib, tmp_path):
    """同期で登録されただけ（PDF がまだ届いていない）論文は、実際のファイルができるまで飛ばす。"""
    data = _pdf(tmp_path / "remote.pdf", "arrived later text").read_bytes()
    import hashlib

    paper = lib.sync_insert(hashlib.sha256(data).hexdigest(), "remote.pdf", 1, time.time())
    assert not paper.pdf_path.exists()
    ctl = _controller(lib, tmp_path)
    assert _wait(lambda: ctl.index is not None and not ctl.indexing)
    _wait(lambda: False, timeout=0.2)
    assert ctl.index.known() == {}
    # 取得が終わって、ファイルが実際に置かれた（同期は合図を出さないので、取得の後に schedule される）
    paper.pdf_path.parent.mkdir(parents=True, exist_ok=True)
    paper.pdf_path.write_bytes(data)
    ctl.schedule()
    assert _wait(lambda: not ctl.indexing and (paper.sha256, ORIGINAL) in ctl.index.known())
    ctl.close()


def test_cancel_stops_the_pass(app, lib, tmp_path):
    for i in range(5):
        lib.add(_pdf(tmp_path / f"p{i}.pdf", f"paper number {i} words"))
    ctl = _controller(lib, tmp_path, pause=0.2)
    assert _wait(lambda: ctl.progress is not None)
    ctl.cancel()
    assert _wait(lambda: not ctl.indexing)
    assert len(ctl.index.known()) < 5
    ctl.close()


def test_stale_query_results_are_dropped(app, lib, tmp_path):
    lib.add(_pdf(tmp_path / "a.pdf", "alpha beta"))
    ctl = _controller(lib, tmp_path)
    assert _wait(lambda: ctl.index is not None and not ctl.indexing and ctl.index.known())
    got = _results(ctl)
    ctl.query("alpha")
    ctl.query("beta")
    assert _wait(lambda: any(t == "beta" for t, _ in got))
    _wait(lambda: False, timeout=0.2)
    assert [t for t, _ in got] == ["beta"]
    ctl.close()


def test_corrupt_index_falls_back_without_crashing(app, lib, tmp_path):
    lib.add(_pdf(tmp_path / "a.pdf", "alpha beta"))
    ctl = _controller(lib, tmp_path)
    assert _wait(lambda: ctl.index is not None and not ctl.indexing)

    def broken(*a, **k):
        from spectra.library.fulltext import FulltextUnavailable

        raise FulltextUnavailable("database disk image is malformed")

    ctl.index.search = broken
    got = _results(ctl)
    ctl.query("alpha")
    assert _wait(lambda: got)
    assert got[-1] == ("alpha", None)  # 書誌の検索だけに退く
    assert not ctl.available and ctl.unavailable
    ctl.query("beta")
    assert got[-1] == ("beta", None)
    ctl.close()


def test_unopenable_index_location_is_unavailable(app, lib, tmp_path):
    blocker = tmp_path / "cache"
    blocker.write_text("not a folder")
    ctl = _controller(lib, tmp_path)
    lib.add(_pdf(tmp_path / "a.pdf", "alpha beta"))
    assert _wait(lambda: not ctl.available)
    got = _results(ctl)
    ctl.query("alpha")
    assert got[-1] == ("alpha", None)
    ctl.close()


def test_disable_rebuild_and_delete(app, lib, tmp_path):
    lib.add(_pdf(tmp_path / "a.pdf", "alpha beta"))
    settings = Settings()
    ctl = _controller(lib, tmp_path, settings)
    assert _wait(lambda: ctl.index is not None and not ctl.indexing and ctl.index.known())
    ctl.rebuild()
    assert _wait(lambda: not ctl.indexing and ctl.index.known())
    settings.fulltext_search = False
    ctl.delete_index()
    ctl.apply_settings()
    assert not (tmp_path / "cache" / "fulltext.sqlite3").exists()
    got = _results(ctl)
    ctl.query("alpha")
    assert got[-1] == ("alpha", None)
    lib.add(_pdf(tmp_path / "b.pdf", "gamma delta"))
    _wait(lambda: False, timeout=0.2)
    assert not (tmp_path / "cache" / "fulltext.sqlite3").exists()  # オフの間は作らない
    settings.fulltext_search = True
    ctl.apply_settings()
    assert _wait(lambda: ctl.index is not None and not ctl.indexing and len(ctl.index.known()) == 2)
    ctl.close()


def test_main_window_creates_and_closes_the_controller(app, tmp_path):
    from spectra.ui.main_window import MainWindow

    win = MainWindow(settings=Settings(), library=Library(tmp_path / "lib"), check_updates=False)
    assert win.fulltext.enabled
    win.close()
    assert win.fulltext._closed


def test_index_is_kept_outside_the_library_folder():
    from spectra.config import paths

    assert paths.library_dir() not in paths.fulltext_index_file().parents
    FulltextIndex  # noqa: B018 - 公開の名前
