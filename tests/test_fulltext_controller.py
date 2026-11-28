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


# -- 設定の画面 --------------------------------------------------------------


def _dialog(settings, ctl):
    from spectra.config.secrets import SecretStore
    from spectra.ui.settings_dialog import SettingsDialog

    return SettingsDialog(settings, SecretStore(use_keyring=False), None, fulltext=ctl)


def test_settings_show_index_counts_and_rebuild(app, lib, tmp_path):
    lib.add(_pdf(tmp_path / "a.pdf", "alpha beta"))
    (tmp_path / "scan.pdf").write_bytes(b"%PDF-1.4 broken")
    settings = Settings()
    ctl = _controller(lib, tmp_path, settings)
    assert _wait(lambda: ctl.index is not None and not ctl.indexing and ctl.index.known())
    d = _dialog(settings, ctl)
    assert d.index_info.text().startswith("1 本・1 ページ")
    ctl.rebuild()
    assert _wait(lambda: not ctl.indexing and ctl.index.known())
    d._refresh_index_info()
    assert d.index_info.text().startswith("1 本")
    d.close()
    ctl.close()


def test_settings_warn_about_skipped_and_large_index(app, lib, tmp_path, monkeypatch):
    from spectra.library.fulltext import Stats
    from spectra.ui import settings_dialog

    settings = Settings()
    ctl = _controller(lib, tmp_path, settings, startup_delay_ms=60_000)
    big = settings_dialog.WARN_INDEX_BYTES + 1
    monkeypatch.setattr(ctl, "stats", lambda: Stats(5, 3, 30, big, {"ok": 3, "no_text": 1, "encrypted": 1}))
    d = _dialog(settings, ctl)
    note = d.index_note.text()
    assert "2 本" in note and "上限" in note
    assert d.index_note.property("tone") == "warning"
    d.close()
    ctl.close()


def test_settings_toggle_off_and_delete(app, lib, tmp_path, monkeypatch):
    from spectra.ui import settings_dialog

    lib.add(_pdf(tmp_path / "a.pdf", "alpha beta"))
    settings = Settings()
    ctl = _controller(lib, tmp_path, settings)
    assert _wait(lambda: ctl.index is not None and not ctl.indexing and ctl.index.known())
    d = _dialog(settings, ctl)
    d.fulltext_search.setChecked(False)
    assert settings.fulltext_search is False and d.index_info.text() == "オフ"
    assert not d.fulltext_translations.isEnabled()
    d.fulltext_search.setChecked(True)
    assert settings.fulltext_search
    assert _wait(lambda: ctl.index is not None and not ctl.indexing)

    def confirm(box):
        delete = next(b for b in box.buttons() if b.text() == "削除")
        return type("P", (), {"exec": lambda self: box.clickedButton() or delete.click()})()

    monkeypatch.setattr(settings_dialog, "plain_box", confirm)
    d._delete_index()
    assert settings.fulltext_search is False
    assert not (tmp_path / "cache" / "fulltext.sqlite3").exists()
    assert Settings.load().fulltext_search is False  # 保存された
    d.close()
    ctl.close()


def test_settings_without_controller_has_no_fulltext_group(app):
    d = _dialog(Settings(), None)
    assert not hasattr(d, "fulltext_search")
    d.close()


# -- レビューの指摘 ----------------------------------------------------------


def test_busy_query_falls_back_once_without_disabling(app, lib, tmp_path):
    from spectra.library.fulltext import FulltextBusy

    lib.add(_pdf(tmp_path / "a.pdf", "alpha beta"))
    ctl = _controller(lib, tmp_path)
    assert _wait(lambda: ctl.index is not None and not ctl.indexing and ctl.index.known())
    real = ctl.index.search
    ctl.index.search = lambda *a, **k: (_ for _ in ()).throw(FulltextBusy("database is locked"))
    got = _results(ctl)
    ctl.query("alpha")
    assert _wait(lambda: got)
    assert got[-1] == ("alpha", None) and ctl.available and not ctl.unavailable
    ctl.index.search = real
    ctl.query("alpha beta")
    assert _wait(lambda: got[-1][0] == "alpha beta" and got[-1][1])
    ctl.close()


def test_busy_pass_retries_later_without_disabling(app, lib, tmp_path, monkeypatch):
    from spectra.library.fulltext import FulltextBusy
    from spectra.ui import fulltext_controller

    lib.add(_pdf(tmp_path / "a.pdf", "alpha beta"))
    calls = []

    def busy(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise FulltextBusy("database is locked")
        return 0

    monkeypatch.setattr(fulltext_controller, "run_pass", busy)
    monkeypatch.setattr(fulltext_controller, "BUSY_RETRY_MS", 50)
    ctl = _controller(lib, tmp_path)
    assert _wait(lambda: len(calls) >= 2)
    assert ctl.available
    ctl.close()


def test_closed_or_disabled_controller_never_creates_the_index(app, lib, tmp_path):
    settings = Settings()
    settings.fulltext_search = False
    ctl = _controller(lib, tmp_path, settings)
    assert ctl.stats() is None
    ctl.query("alpha")
    _wait(lambda: False, timeout=0.2)
    assert not (tmp_path / "cache").exists()
    settings.fulltext_search = True
    ctl.close()
    assert ctl.stats() is None
    assert not (tmp_path / "cache").exists()


def test_one_bad_pdf_does_not_stop_the_pass(app, lib, tmp_path, monkeypatch):
    from spectra.library import fulltext

    a, _ = lib.add(_pdf(tmp_path / "a.pdf", "first paper text"))
    b, _ = lib.add(_pdf(tmp_path / "b.pdf", "second paper text"))
    real = fulltext.extract
    monkeypatch.setattr(
        fulltext, "extract", lambda p, c=None: ("ok", ["bad \ud800"]) if p == a.pdf_path else real(p, c)
    )
    ctl = _controller(lib, tmp_path)
    assert _wait(lambda: ctl.index is not None and not ctl.indexing and len(ctl.index.known()) == 2)
    assert ctl.index.known()[(a.sha256, ORIGINAL)][2] == "broken"
    assert ctl.index.known()[(b.sha256, ORIGINAL)][2] == "ok"
    ctl.close()
