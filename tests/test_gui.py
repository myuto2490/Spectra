"""GUI のスモークテスト（画面を表示せずに実行する）。"""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture()
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECTRA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SPECTRA_DEV", "1")
    from spectra.config.secrets import SecretStore
    from spectra.config.settings import Settings
    from spectra.ui.app import create_app
    from spectra.ui.main_window import MainWindow

    app = create_app([])
    settings = Settings(engine="dummy")
    w = MainWindow(settings=settings, secrets=SecretStore(use_keyring=False))
    w.show()
    yield app, w
    w._job_task = None
    w.close()


def pump(app, seconds=0.2, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until and until():
            return
        time.sleep(0.01)


def test_empty_state(window):
    app, w = window
    pump(app)
    assert w.stack.currentWidget() is w.drop_zone
    assert not w.primary_btn.isVisible()


def test_open_translate_and_export(window, paper_path):
    app, w = window
    assert w.open_file(paper_path)
    pump(app, 5, until=lambda: w.inspector.est_paragraphs.text().isdigit())
    assert int(w.inspector.est_paragraphs.text()) > 5
    assert w.primary_btn.text() == "翻訳"

    w.start_translation()
    assert w._busy()
    pump(app, 60, until=lambda: not w._busy())
    assert w.result is not None and w.result.qa.ok
    assert w.primary_btn.text() == "保存…"
    assert w.preview.translated.document() is not None

    # 出力形式だけを変えた場合は再翻訳せずに作り直せる
    w.settings.output_mode = "side_by_side"
    import pymupdf

    out = pymupdf.open(stream=w._output_bytes(), filetype="pdf")
    assert out[0].rect.width == pytest.approx(1190)


def test_invalid_file_is_rejected(window, tmp_path, monkeypatch):
    app, w = window
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    alerts = []
    monkeypatch.setattr(w, "_alert", lambda title, body="": alerts.append(title))
    assert not w.open_file(bad)
    assert alerts and w.original_doc is None


def test_settings_dialog_opens(window, monkeypatch):
    app, w = window
    from spectra.ui.settings_dialog import KeyField, SettingsDialog

    verified = []
    monkeypatch.setattr(KeyField, "verify", lambda self: verified.append(self.engine_id))  # ネットワークに出ない

    d = SettingsDialog(w.settings, w.secrets, w.cache, w)
    assert set(d.key_fields) == {"gemini"}
    assert d.model.count() == 6
    d.model.setCurrentIndex(d.model.findData("gemini-3.8-flash"))
    assert w.settings.gemini_model == "gemini-3.8-flash" and "混雑" in d.model_desc.text()
    d.auto_fallback.setChecked(False)
    assert w.settings.auto_fallback is False
    for tab in ("engines", "fonts", "advanced"):
        d.show_tab(tab)
        pump(app, 0.05)
    d.key_fields["gemini"].edit.setText("AIzaTestKey")
    d.key_fields["gemini"]._save()
    assert w.secrets.get("gemini", use_env=False) == "AIzaTestKey"
    assert verified == ["gemini"]
    d.close()


def test_preview_height_matches_pages_after_reopening(window, paper_path, tmp_path):
    """別の PDF を開き直しても、前の文書のページ分だけスクロールできてしまわないこと。"""
    import pymupdf

    app, w = window
    one_page = tmp_path / "one.pdf"
    doc = pymupdf.open()
    doc.insert_pdf(pymupdf.open(paper_path), from_page=0, to_page=0)
    doc.save(one_page)

    def content_height(view):
        pages = view._pages
        return sum(p.height() for p in pages) + 16 * (len(pages) - 1) + 48

    for path in (paper_path, one_page):
        assert w.open_file(path)
        pump(app, 0.5)
        view = w.preview.original
        assert len(view._pages) == pymupdf.open(path).page_count
        assert view._container.height() == content_height(view)
    view.set_zoom(0.5)
    pump(app, 0.2)
    assert view._container.height() == content_height(view)
