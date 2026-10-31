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
    # 書誌情報の照会は尋ね済み（オフ）にして、確認のダイアログで止まらないようにする
    settings = Settings(engine="dummy", metadata_lookup_asked=True)
    w = MainWindow(settings=settings, secrets=SecretStore(use_keyring=False))
    w.show()
    yield app, w
    w.queue.cancel_all()
    pump(app, 30, until=lambda: not w._busy())
    w.close()
    w.library.close()


def pump(app, seconds=0.2, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until and until():
            return
        time.sleep(0.01)


def test_empty_state(window, paper_path, monkeypatch):
    """論文が 1 本もなくてもライブラリ画面を出し、一覧の中で追加の方法を案内する。"""
    app, w = window
    pump(app)
    view = w.library_view
    assert w.stack.currentWidget() is view
    assert view.center.currentWidget() is view.empty
    assert w.search.isVisible() and w.add_btn.isVisible() and view.sidebar.isVisible()
    assert not w.primary_btn.isVisible()
    assert view.detail.current_id() is None  # 詳細パネルは何も選んでいない状態
    opened = []
    monkeypatch.setattr(w, "open_dialog", lambda: opened.append(1))
    view.addRequested.disconnect()
    view.addRequested.connect(w.open_dialog)
    view.empty_add_btn.click()
    assert opened == [1]

    # 論文を追加すると一覧に切り替わり、すべて削除すると案内に戻る
    paper = w.library.add(paper_path)[0]
    view.reload()
    assert view.center.currentWidget() is view.table
    w.library.remove(paper.id)
    view.model.remove(paper.id)
    assert view.center.currentWidget() is view.empty


def test_open_translate_and_export(window, paper_path):
    app, w = window
    assert w.open_file(paper_path)
    assert w.screen_mode == "reader" and w.stack.currentWidget() is w.content
    pump(app, 5, until=lambda: w.inspector.est_paragraphs.text().isdigit())
    assert int(w.inspector.est_paragraphs.text()) > 5
    assert w.primary_btn.text() == "翻訳"

    w.start_translation()
    assert w._busy()
    pump(app, 60, until=lambda: not w._busy())
    assert w.result is not None and w.result.qa.ok
    assert w.primary_btn.text() == "書き出す…"
    assert w.preview.translated.document() is not None

    # 出力形式だけを変えた場合は再翻訳せずに作り直せる
    w.settings.output_mode = "side_by_side"
    import pymupdf

    out = pymupdf.open(stream=w._output_bytes(), filetype="pdf")
    assert out[0].rect.width == pytest.approx(1190)

    # 訳文はライブラリに保存され、開き直すと翻訳せずにすぐ表示できる
    paper = w.library.papers()[0]
    assert paper.translation is not None and paper.translation.complete
    assert paper.translated_path.exists()
    w.show_library()
    assert w.stack.currentWidget() is w.library_view and w.original_doc is None
    assert "翻訳済み" in w.library_view.model.data(w.library_view.model.index(0, 5))
    assert w.open_paper(paper.id)
    assert w.preview.translated.document() is not None
    assert w.primary_btn.text() == "書き出す…"


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


def test_export_dialog_formats():
    from spectra.core.options import OutputMode
    from spectra.ui.app import create_app
    from spectra.ui.export_dialog import ExportDialog, output_pages, suffix_for

    create_app([])
    d = ExportDialog(OutputMode.SIDE_BY_SIDE, 12)
    assert not d.grab().isNull()  # カードの描画で例外が出ないこと
    assert d.selected() == OutputMode.SIDE_BY_SIDE
    d.cards[OutputMode.INTERLEAVE].click()
    assert d.selected() == OutputMode.INTERLEAVE and "24 ページ" in d.summary.text()
    assert output_pages(OutputMode.TRANSLATED, 12) == 12
    assert {suffix_for(m) for m in OutputMode} == {"_ja", "_ja_side", "_ja_alt"}


def _distinct_paper(path, title):
    """内容（＝ハッシュ）の異なる合成論文。"""
    from pdf_factory import make_paper

    doc = make_paper()
    doc.set_metadata({"title": title})
    doc.save(path)
    return path


def test_library_add_filter_and_collections(window, tmp_path):
    app, w = window
    folder = tmp_path / "papers"
    folder.mkdir()
    a = _distinct_paper(folder / "a.pdf", "Alpha Networks for Sparse Data")
    b = _distinct_paper(folder / "b.pdf", "Beta Methods in Planetary Science")
    (folder / "notes.txt").write_text("not a pdf")

    papers = w.add_files([folder])
    assert len(papers) == 2
    assert w.screen_mode == "library" and w.stack.currentWidget() is w.library_view
    assert w.search.isVisible() and w.add_btn.isVisible()
    view = w.library_view
    assert view.proxy.rowCount() == 2
    assert all(p.meta_status == "local" for p in w.library.papers())  # 照会しない設定

    # 同じファイルをもう一度追加しても増えない
    assert len(w.add_files([a], open_single=False)) == 1
    assert len(w.library.papers()) == 2

    # 検索
    w.search.setText("planetary")
    assert view.proxy.rowCount() == 1
    w.search.setText("no such paper")
    assert view.proxy.rowCount() == 0 and view.center.currentWidget() is view.no_match
    w.search.clear()
    assert view.proxy.rowCount() == 2

    # お気に入りで絞り込む
    alpha = next(p for p in w.library.papers() if p.file_name == "a.pdf")
    view.toggle_favorite([alpha.id])
    assert w.library.get(alpha.id).favorite
    view.sidebar.select_scope("favorites")
    assert view.proxy.rowCount() == 1

    # コレクション
    c = w.library.create_collection("卒論")
    view.reload()
    view._add_to_collection(c.id, [alpha.id])
    assert view.sidebar.select_scope("collection", c.id)
    assert view.proxy.rowCount() == 1
    view.sidebar.select_scope("all")

    # 詳細パネル
    view.select_ids([alpha.id])
    pump(app, 0.05)
    assert view.detail.current_id() == alpha.id
    assert view.detail.primary.text() == "翻訳する"
    assert b.exists()


def test_translation_queue_runs_papers_in_order(window, tmp_path):
    app, w = window
    papers = [w.library.add(_distinct_paper(tmp_path / f"p{i}.pdf", f"Paper number {i}"))[0] for i in range(2)]
    w.library_view.reload()
    w.translate_papers([p.id for p in papers])
    assert w.queue.state(papers[0].id) == "running"
    assert w.queue.state(papers[1].id) == "queued"
    pump(app, 120, until=lambda: not w._busy())
    for p in papers:
        t = w.library.get(p.id).translation
        assert t is not None and t.complete and t.model == "dummy"
    assert w.banner.isVisible()  # 完了の知らせ（読書画面を開いていないとき）


def test_lookup_consent_is_asked_once(window, paper_path, monkeypatch):
    app, w = window
    w.settings.metadata_lookup_asked = False
    asked = []
    monkeypatch.setattr(w, "_ask_lookup", lambda: asked.append(1) or False)
    assert w.open_file(paper_path)
    w.show_library()
    assert w.open_file(paper_path)
    assert asked == [1]
    assert w.settings.metadata_lookup is False and w.settings.metadata_lookup_asked


def test_edit_metadata_marks_fields_as_edited(window, paper_path):
    app, w = window
    from spectra.ui.paper_detail import MetadataDialog, parse_authors

    paper = w.library.add(paper_path)[0]
    d = MetadataDialog(paper, w)
    d.edits["title"].setText("A Hand-Edited Title")
    d.edits["authors"].setText("Vaswani, Ashish; Noam Shazeer")
    d.edits["year"].setText("2017")
    md, changed = d.result_metadata()
    assert {"title", "authors", "year"} <= changed
    w.library.update_metadata(paper.id, md, manual_fields=changed)
    p = w.library.get(paper.id)
    assert p.metadata.title == "A Hand-Edited Title" and p.metadata.year == 2017
    assert [a.family for a in p.metadata.authors] == ["Vaswani", "Shazeer"]
    assert "title" in p.edited
    assert parse_authors("Ada Lovelace")[0].given == "Ada"


def test_edit_dialog_leaves_untouched_fields_alone(window, paper_path):
    """文字列との往復で値が変わる項目（団体著者など）は、触らなければ変更扱いにしない。"""
    app, w = window
    from spectra.library import Author, Metadata
    from spectra.ui.paper_detail import MetadataDialog

    paper = w.library.add(paper_path)[0]
    md = Metadata(title="T", authors=[Author(family="The Spectra Consortium")], source="crossref")
    paper = w.library.update_metadata(paper.id, md, status="done")
    d = MetadataDialog(paper, w)
    d.edits["title"].setText("Edited title")
    d.edits["arxiv_id"].setText("https://arxiv.org/abs/1706.03762v5")
    new, changed = d.result_metadata()
    assert changed == {"title", "arxiv_id"}
    assert new.authors == [Author(family="The Spectra Consortium")]
    assert new.arxiv_id == "1706.03762"
    d.edits["arxiv_id"].setText("arXiv:hep-th/9901001")
    assert d.result_metadata()[0].arxiv_id == "hep-th/9901001"


def _queue_papers(w, tmp_path, n):
    papers = [w.library.add(_distinct_paper(tmp_path / f"q{i}.pdf", f"Queued paper {i}"))[0] for i in range(n)]
    w.library_view.reload()
    return papers


@pytest.fixture()
def no_dialogs(monkeypatch):
    """QMessageBox を表示せずに、出された文言を記録する。"""
    from PySide6.QtWidgets import QMessageBox

    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(self.text() + "\n" + self.informativeText()) or 0)
    return shown


def test_network_failure_stops_queue_once(window, tmp_path, monkeypatch, no_dialogs):
    """接続できないときは、失敗のダイアログを論文の数だけ出さず、待っている翻訳を取りやめる。"""
    app, w = window
    from spectra.translate.errors import NetworkError

    def runner(req, report, cancel):
        raise NetworkError()

    w.queue._runner = runner
    alerts = []
    monkeypatch.setattr(w, "_alert", lambda title, body="": alerts.append(body))
    papers = _queue_papers(w, tmp_path, 3)
    w.translate_papers([p.id for p in papers])
    pump(app, 10, until=lambda: not w._busy())
    assert len(alerts) == 1 and "2 件の翻訳は取りやめました" in alerts[0]
    assert all(w.queue.state(p.id) is None for p in papers)
    assert w.library_view.model.job(papers[0].id) == "failed"
    assert w.library_view.model.job(papers[1].id) is None  # 待機中だった論文は「未翻訳」に戻る


def test_partial_failure_message_depends_on_cause(window, tmp_path, no_dialogs):
    """訳せた段落があるときの失敗は、原因が 1 日の上限のときだけ「明日以降」と案内する。"""
    app, w = window
    from spectra.translate.errors import NetworkError, QuotaExceededError
    from spectra.translate.service import PartialTranslationError

    for cause, tomorrow in ((NetworkError(), False), (QuotaExceededError(), True)):
        no_dialogs.clear()

        def runner(req, report, cancel, cause=cause):
            raise PartialTranslationError(cause, {"a": "b"})

        w.queue._runner = runner
        sub = tmp_path / type(cause).__name__
        sub.mkdir()
        papers = _queue_papers(w, sub, 2)
        w.translate_papers([p.id for p in papers])
        pump(app, 10, until=lambda: not w._busy())
        assert len(no_dialogs) == 1
        assert ("明日以降" in no_dialogs[0]) is tomorrow
        assert "1 件の翻訳は取りやめました" in no_dialogs[0]


def test_cancel_running_translation_updates_at_once(window, tmp_path):
    app, w = window

    def runner(req, report, cancel):
        cancel.wait(10)
        from spectra.translate.errors import CancelledError

        raise CancelledError

    w.queue._runner = runner
    paper = _queue_papers(w, tmp_path, 1)[0]
    w.translate_papers([paper.id])
    pump(app, 0.2)
    w.library_view.select_ids([paper.id])
    pump(app, 0.1)
    assert w.library_view.detail.secondary.text() == "翻訳をキャンセル"
    w.library_view.detail.secondary.click()
    assert w.queue.is_cancelling()
    assert w.status.text() == "キャンセルしています…"
    pump(app, 10, until=lambda: not w._busy())
    assert "キャンセルしました" in w.status.text()
