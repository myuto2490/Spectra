"""GUI のスモークテスト（画面を表示せずに実行する）。"""

import os
import time
from pathlib import Path

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
    # テストでは書誌情報を照会しない（Crossref / arXiv に通信しない）
    settings = Settings(engine="dummy", metadata_lookup=False)
    w = MainWindow(settings=settings, secrets=SecretStore(use_keyring=False))
    w.show()
    yield app, w
    w.queue.cancel_all()
    pump(app, 30, until=lambda: not w._busy())
    w.close()
    w.library.close()
    # 閉じた窓を実際に破棄する（残すと、次のテストでスタイルを当て直すたびに作り直され、テストが遅くなる）
    from PySide6.QtCore import QCoreApplication, QEvent

    w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


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
    assert w.primary_btn.text() == "PDF出力"
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
    assert w.primary_btn.text() == "PDF出力"


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
    assert view.detail.translate_btn.text() == "翻訳する"
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
    assert w.library_view.detail.translate_btn.text() == "翻訳をキャンセル"
    w.library_view.detail.translate_btn.click()
    assert w.queue.is_cancelling()
    assert w.status.text() == "キャンセルしています…"
    pump(app, 10, until=lambda: not w._busy())
    assert "キャンセルしました" in w.status.text()


def test_adding_paper_looks_up_without_asking(window, paper_path, monkeypatch):
    """照会がオン（既定）なら、確認のダイアログを出さずに追加した論文を照会する。"""
    app, w = window
    w.settings.metadata_lookup = True
    looked_up = []
    monkeypatch.setattr("spectra.ui.jobs.lookup_paper", lambda library, pid, client=None: looked_up.append(pid) or False)
    monkeypatch.setattr("spectra.ui.main_window.QMessageBox.exec", lambda self: pytest.fail("確認のダイアログが出た"))
    assert w.open_file(paper_path)
    pump(app, 5, until=lambda: looked_up and not w.lookups.is_active())
    assert looked_up == [w.paper.id]


def test_enable_lookup_from_detail_without_asking(window, paper_path, monkeypatch):
    """照会がオフのとき、詳細パネルの「照会を有効にする」は確認なしでオンにして照会する。"""
    app, w = window
    looked_up = []
    monkeypatch.setattr("spectra.ui.jobs.lookup_paper", lambda library, pid, client=None: looked_up.append(pid) or False)
    monkeypatch.setattr("spectra.ui.main_window.QMessageBox.exec", lambda self: pytest.fail("確認のダイアログが出た"))
    paper = w.add_files([paper_path], open_single=False)[0]
    assert w.library.get(paper.id).meta_status == "local"
    w.library_view.detail.enableLookupRequested.emit()
    assert w.settings.metadata_lookup is True
    pump(app, 5, until=lambda: looked_up and not w.lookups.is_active())
    assert looked_up == [paper.id]


def test_detail_tabs_keep_selection(window, tmp_path):
    """詳細パネルは「論文の詳細」と「AI翻訳の詳細」のタブに分かれ、論文を選び直してもタブはそのまま。"""
    app, w = window
    from spectra.library import Author, Metadata
    from spectra.ui.paper_detail import PAPER_TAB, TRANSLATION_TAB

    papers = _queue_papers(w, tmp_path, 2)
    md = Metadata(
        title="A spatially resolved study",
        authors=[Author("Greathouse", "Thomas K."), Author("Richter", "Matthew")],
        year=2011, venue="Icarus", volume="214", issue="2", pages="606-621",
        doi="10.1016/j.icarus.2011.05.028", abstract="Using TEXES ...", source="crossref",
    )
    w.library.update_metadata(papers[0].id, md, status="done")
    view, detail = w.library_view, w.library_view.detail
    view.reload()

    view.select_ids([papers[0].id])
    pump(app, 0.05)
    assert detail.tabs.isVisible() and detail.current_tab() == PAPER_TAB
    assert detail.pages.currentIndex() == PAPER_TAB
    assert detail.title.text() == "A spatially resolved study"
    assert detail.info._rows["venue"][1].text() == "Icarus"
    assert "10.1016" in detail.info._rows["doi"][1].text()
    assert detail.read_btn.isVisible()
    assert not detail.info._rows["volume"][1].isHidden()

    detail.tab_buttons[TRANSLATION_TAB].click()
    assert detail.pages.currentIndex() == TRANSLATION_TAB
    assert detail.translate_btn.text() == "翻訳する"
    assert detail.file_pages.text() == str(papers[0].page_count)

    # 別の論文を選び直しても、複数選択を挟んでも、AI翻訳の詳細のまま
    view.select_ids([papers[1].id])
    pump(app, 0.05)
    assert detail.current_tab() == TRANSLATION_TAB and detail.pages.currentIndex() == TRANSLATION_TAB
    assert detail.info._rows["volume"][1].isHidden()  # 空の項目は隠す
    view.select_ids([p.id for p in papers])
    pump(app, 0.05)
    assert not detail.tabs.isVisible() and detail.multi.isVisible()
    view.select_ids([papers[0].id])
    pump(app, 0.05)
    assert detail.pages.currentIndex() == TRANSLATION_TAB


def test_escape_clears_the_search(window, tmp_path):
    """検索欄に打った直後でも、Esc で検索が消える（Esc を押せる状態が打つたびに更新される）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    app, w = window
    w.library.add(_distinct_paper(tmp_path / "a.pdf", "Alpha"))
    w.library_view.reload()
    w._update_state()
    w.search.setFocus()
    QTest.keyClicks(w.search, "zzz")
    pump(app, 0.05)
    assert w.library_view.proxy.rowCount() == 0
    assert w.act_cancel.isEnabled()
    QTest.keyClick(w.search, Qt.Key.Key_Escape)
    pump(app, 0.05)
    assert w.search.text() == "" and w.library_view.proxy.rowCount() == 1


def test_search_ignores_width_case_and_kana(window, tmp_path):
    app, w = window
    from spectra.library import Author, Metadata

    p = w.library.add(_distinct_paper(tmp_path / "a.pdf", "x"))[0]
    w.library.update_metadata(
        p.id,
        Metadata(
            title="ＦＵＬＬ－ＷＩＤＴＨ Ｔｉｔｌｅ のサーベイ",
            authors=[Author("やまだ", "たろう")],
            venue="Ｉｃａｒｕｓ",
            doi="10.1109/cvpr.2016.90",
        ),
    )
    other = w.library.add(_distinct_paper(tmp_path / "b.pdf", "y"))[0]
    w.library.update_metadata(other.id, Metadata(title="Unrelated"))
    w.library_view.reload()
    view = w.library_view
    for query in ("full-width", "ICARUS", "ｉｃａｒｕｓ", "ヤマダ", "さーべい", "https://doi.org/10.1109/CVPR.2016.90", "doi:10.1109/cvpr.2016.90"):
        w.search.setText(query)
        assert view.proxy.rowCount() == 1, query
    w.search.setText("full　icarus")  # 全角の空白でも AND で区切る
    assert view.proxy.rowCount() == 1
    w.search.setText("full unrelated")
    assert view.proxy.rowCount() == 0


def test_added_papers_are_revealed_even_when_search_hides_them(window, tmp_path):
    app, w = window
    w.library.add(_distinct_paper(tmp_path / "a.pdf", "Alpha"))
    w.library_view.reload()
    w.search.setText("no such words")
    papers = w.add_files([_distinct_paper(tmp_path / "b.pdf", "Beta"), _distinct_paper(tmp_path / "c.pdf", "Gamma")])
    assert len(papers) == 2
    assert w.search.text() == ""
    assert set(w.library_view.selected_ids()) == {p.id for p in papers}


def test_opening_settings_does_not_read_the_saved_key(window, monkeypatch):
    """設定を開いただけでは、保存済みの API キーを読まない（macOS のキーチェーンの許可を求めない）。"""
    app, w = window
    from spectra.ui.settings_dialog import KeyField, SettingsDialog

    w.secrets.set("gemini", "AIzaSaved")
    w.secrets._cache.clear()
    reads = []
    original = w.secrets.get
    monkeypatch.setattr(w.secrets, "get", lambda engine, use_env=True: reads.append(engine) or original(engine, use_env))
    # macOS では、キーの有無はキーチェーンに属性だけを問い合わせて調べる（中身を読まないので許可を求めない）。それを再現する
    monkeypatch.setattr(w.secrets, "has", lambda engine, use_env=True: engine == "gemini")
    verified = []
    monkeypatch.setattr(KeyField, "verify", lambda self: verified.append(self.engine_id))

    d = SettingsDialog(w.settings, w.secrets, w.cache, w)
    field = d.key_fields["gemini"]
    assert reads == []
    assert field.edit.text() == "" and "保存済み" in field.edit.placeholderText()
    assert "保存済み" in field.status.text()
    field._save()  # 何も入力せずに欄を離れても、キーは消えない
    assert w.secrets.get("gemini", use_env=False) == "AIzaSaved"
    reads.clear()
    field.reveal.setChecked(True)  # 表示したときに初めて読む
    assert reads == ["gemini"] and field.edit.text() == "AIzaSaved"
    d.close()


def test_error_dialogs_hide_the_home_folder(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    app, w = window
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append((self.text(), self.informativeText())) or 0)
    home = str(Path.home())
    w._alert("ファイルを読み込めませんでした", f"[Errno 2] No such file: '{home}/Documents/paper.pdf'")
    assert shown and home not in shown[-1][1] and "~/Documents/paper.pdf" in shown[-1][1]


# -- PDF の表示（ページ n/N・ズーム・検索・ページの一覧・リンク・見ていた場所の記憶） -----------------------


def test_page_box_zoom_menu_and_find_bar(window, paper_path, monkeypatch):
    app, w = window
    assert w.open_file(paper_path)
    pump(app, 0.3)
    view = w.preview.original
    assert w.page_edit.text() == "1" and w.page_total.text() == f"/ {view.page_count()}"
    assert not w.page_prev_btn.isEnabled() and w.page_next_btn.isEnabled()

    w.page_edit.setText("3")
    w.page_edit.returnPressed.emit()
    pump(app, 0.2)
    assert view.current_page() == 2 and w.page_edit.text() == "3"
    w.page_edit.setText("999")  # 範囲の外は最後のページへ
    w.page_edit.returnPressed.emit()
    pump(app, 0.2)
    assert view.current_page() == view.page_count() - 1 and not w.page_next_btn.isEnabled()

    w.preview.set_zoom(1.5)
    assert w.zoom_btn.text() == "150%"
    w.zoom_menu.actions()[1].trigger()  # ページ全体に合わせる
    pump(app, 0.2)
    assert view.fit_mode() == "page"
    w.zoom_menu.actions()[0].trigger()  # 幅に合わせる
    pump(app, 0.2)
    assert view.fit_mode() == "width"

    # ⌘F は読書画面では PDF 内の検索の帯を開く（ライブラリには戻らない）
    assert w.find_bar.isHidden()
    w.focus_search()
    assert w.screen_mode == "reader" and not w.find_bar.isHidden()
    w.find_bar.edit.setText("neural")
    w.find_bar.edit.returnPressed.emit()
    pump(app, 0.2)
    assert view.hit_count() >= 1 and "件" in w.find_bar.count.text()
    w.find_bar.close_bar()
    assert view.hit_count() == 0 and w.find_bar.isHidden()


def test_nav_pane_toggle_and_page_jump(window, paper_path):
    app, w = window
    assert w.open_file(paper_path)
    pump(app, 0.3)
    assert w.nav_pane.isHidden()
    w.nav_btn.setChecked(True)
    pump(app, 0.2)
    assert not w.nav_pane.isHidden() and w.nav_pane.thumbnails.count() == w.preview.original.page_count()
    w.nav_pane.pageActivated.emit(2)
    pump(app, 0.2)
    assert w.preview.original.current_page() == 2
    w.show_library()
    assert w.nav_pane.isHidden()  # ライブラリ画面では出さない


def test_pdf_link_asks_before_opening(window, monkeypatch):
    app, w = window
    from PySide6.QtWidgets import QMessageBox

    opened, boxes = [], []
    monkeypatch.setattr("spectra.ui.main_window.QDesktopServices.openUrl", lambda url: opened.append(url.toString()))

    def fake_exec(box):
        boxes.append(box.informativeText())
        return 0

    monkeypatch.setattr("spectra.ui.main_window.plain_box", lambda box: type("P", (), {"exec": lambda self: fake_exec(box)})())
    # 確認で「開く」を選ぶ
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: next(b for b in self.buttons() if b.text() == "開く"))
    w._open_pdf_link("https://example.com/paper")
    assert opened == ["https://example.com/paper"] and boxes == ["https://example.com/paper"]
    # Web とメール以外（file: や javascript:）は確認も出さずに開かない
    w._open_pdf_link("file:///etc/passwd")
    w._open_pdf_link("javascript:alert(1)")
    assert opened == ["https://example.com/paper"] and len(boxes) == 1


def test_reading_position_is_remembered_per_paper(window, paper_path):
    app, w = window
    assert w.open_file(paper_path)
    pump(app, 0.3)
    sha = w.paper.sha256
    w.preview.original.set_zoom(1.25)
    w.preview.original.go_to_page(1)
    pump(app, 0.2)
    w.show_library()
    st = w.settings.reading_state[sha]
    assert st["fit"] == "none" and st["page"] == 1 and st["zoom"] == pytest.approx(1.25, abs=1e-3)

    assert w.open_paper(w.library.papers()[0].id)  # 開き直すと戻る
    pump(app, 0.4)
    assert w.preview.original.current_page() == 1
    assert w.preview.original.zoom() == pytest.approx(1.25, abs=1e-3)


def test_update_notice_flow(window, monkeypatch):
    """初回に尋ね、オンなら確かめ、新しい版があればライブラリの上に帯を出す（通信はしない）。"""
    app, w = window
    from spectra import update
    from spectra.ui import main_window as mw

    asked, started = [], []
    monkeypatch.setattr(mw, "ask_enable_update_check", lambda parent: asked.append(1) or True)

    def fake_start(on_found, on_error=None, on_none=None, skipped="", **kw):
        started.append(skipped)
        on_found(update.UpdateInfo("99.0.0", "https://github.com/myuto2490/Spectra/releases/tag/v99.0.0"))

    monkeypatch.setattr(mw, "start_check", fake_start)
    w._check_updates = True
    w.settings.update_check = "ask"
    w.settings.update_last_checked = 0.0
    w._startup_update_check()
    assert asked == [1] and w.settings.update_check == "on"
    assert started == [""] and w.settings.update_last_checked > 0
    assert not w.update_wrap.isHidden() and "99.0.0" in w.update_banner._title.text()

    w.update_banner.skip_btn.click()  # 「この版を通知しない」
    assert w.settings.update_skipped_version == "99.0.0" and w.update_wrap.isHidden()

    # 24 時間以内なら確かめない・「確認しない」と答えたら通信しない
    started.clear()
    w._startup_update_check()
    assert started == []
    w.settings.update_check = "off"
    w.settings.update_last_checked = 0.0
    w._startup_update_check()
    assert started == [] and asked == [1]

    # 環境変数で止めているとき（テスト・開発）は、尋ねも確かめもしない
    w._check_updates = False
    w.settings.update_check = "ask"
    w._startup_update_check()
    assert asked == [1] and w.settings.update_check == "ask"


# ---- 右のパネルとノート -------------------------------------------------------


class _FakePaper:
    def __init__(self, id, note="", note_updated_at=None, note_merged=False):
        self.id, self.note, self.note_updated_at, self.note_merged = id, note, note_updated_at, note_merged


class _FakeNotes:
    def __init__(self):
        self.saved = []
        self.fail = False

    def set_note(self, paper_id, text):
        if self.fail:
            raise OSError("disk full")
        self.saved.append((paper_id, text))


def _note_editor(lib=None):
    from spectra.ui.note_editor import NoteEditor

    ed = NoteEditor(lib if lib is not None else _FakeNotes())
    ed.show()
    return ed


def test_note_editor_autosaves_after_a_pause(window):
    app, _w = window
    lib = _FakeNotes()
    ed = _note_editor(lib)
    changed = []
    ed.noteChanged.connect(lambda i, t: changed.append((i, t)))
    ed.set_paper(_FakePaper(7, note="既存のメモ", note_updated_at=1_700_000_000))
    assert ed.text() == "既存のメモ" and "最終更新" in ed.status.text() and not ed.dirty
    ed.edit.setPlainText("既存のメモ\n追記")
    assert ed.dirty and lib.saved == []  # 入力の直後は保存しない
    ed._timer.setInterval(10)
    ed._timer.start()
    pump(app, 2, until=lambda: lib.saved)
    assert lib.saved == [(7, "既存のメモ\n追記")] and changed == lib.saved and not ed.dirty


def test_note_editor_saves_before_switching_papers(window):
    lib = _FakeNotes()
    ed = _note_editor(lib)
    ed.set_paper(_FakePaper(1))
    ed.edit.setPlainText("1 のメモ")
    ed.set_paper(_FakePaper(2, note="2 のメモ"))  # 切り替えで、未保存の分を先に 1 に保存する
    assert lib.saved == [(1, "1 のメモ")] and ed.text() == "2 のメモ" and ed.paper_id == 2
    ed.edit.setPlainText("2 を編集")
    ed.set_paper(None)  # 何も選ばない・閉じるときも保存する
    assert lib.saved[-1] == (2, "2 を編集") and ed.text() == ""


def test_note_editor_keeps_typing_when_the_same_paper_is_shown_again(window):
    ed = _note_editor()
    ed.set_paper(_FakePaper(1, note="古い"))
    ed.edit.setPlainText("入力中")
    ed.set_paper(_FakePaper(1, note="同期で届いた内容"))  # 入力の途中は上書きしない
    assert ed.text() == "入力中" and ed.dirty
    ed.flush()
    ed.set_paper(_FakePaper(1, note="同期で届いた内容"))  # 保存したあとなら、届いた内容に更新する
    assert ed.text() == "同期で届いた内容"


def test_note_editor_merged_notice_and_save_error(window):
    lib = _FakeNotes()
    ed = _note_editor(lib)
    ed.set_paper(_FakePaper(1, note="a", note_merged=True))
    assert not ed.notice.isHidden() and "連結" in ed.notice.text()
    ed.set_paper(_FakePaper(2, note="b"))
    assert ed.notice.isHidden()
    lib.fail = True
    ed.edit.setPlainText("失敗する保存")
    ed.flush()
    assert ed.dirty and "保存できませんでした" in ed.status.text()  # 入力は残り、あとで再試行できる
    lib.fail = False
    ed.flush()
    assert not ed.dirty and lib.saved == [(2, "失敗する保存")]


def test_note_editor_limits_length(window):
    from spectra.ui import note_editor

    ed = _note_editor()
    ed.set_paper(_FakePaper(1))
    ed.edit.setPlainText("あ" * (note_editor.NOTE_MAX_CHARS + 50))
    assert len(ed.text()) == note_editor.NOTE_MAX_CHARS and "超えた分" in ed.status.text()


def test_side_panel_tabs_are_remembered(window, tmp_path):
    app, w = window
    from PySide6.QtWidgets import QLabel

    from spectra.config.settings import Settings
    from spectra.ui.side_panel import SidePanel

    papers = _queue_papers(w, tmp_path, 1)
    settings = Settings(side_panel_tab="note")
    panel = SidePanel(w.library, settings, "reader")
    panel.show()
    seen = []
    panel.tabChanged.connect(seen.append)
    assert panel.current_tab() == "note"  # 覚えていたタブで開く

    paper = w.library.get(papers[0].id)
    panel.set_paper(paper)
    assert panel.detail.pages.currentIndex() == panel.detail.pages.indexOf(panel.note_editor)
    panel.detail.tab_buttons[0].click()
    assert panel.current_tab() == "details" and settings.side_panel_tab == "details" and seen == ["details"]
    panel.set_current_tab("translation")
    assert settings.side_panel_tab == "translation" and seen[-1] == "translation"
    panel.set_current_tab("nope")  # 知らないタブは無視する
    assert panel.current_tab() == "translation"
    assert Settings.load().side_panel_tab == "translation"  # 保存されている

    # 読書画面では、翻訳の設定のパネルを「AI翻訳の詳細」に入れる。外すと元に戻る
    extra = QLabel("インスペクタ")
    panel.set_translation_widget(extra)
    assert panel.detail.pages.currentWidget() is extra and extra.isVisible()
    panel.set_current_tab("note")
    panel.set_current_tab("translation")
    assert panel.detail.pages.currentWidget() is extra
    panel.set_translation_widget(None)
    assert panel.detail.pages.currentWidget() is panel.detail._translation_page

    # ノートの入力は、論文を選び直しても失われず、閉じるときに保存される
    got = []
    panel.noteChanged.connect(lambda i, t: got.append((i, t)))
    panel.set_current_tab("note")
    panel.note_editor.edit.setPlainText("メモ")
    panel.flush_note()
    assert got == [(paper.id, "メモ")]
    with pytest.raises(ValueError):
        SidePanel(w.library, settings, "other")


def test_side_panel_forwards_detail_signals(window, tmp_path):
    app, w = window
    from spectra.config.settings import Settings
    from spectra.ui.side_panel import SidePanel

    papers = _queue_papers(w, tmp_path, 1)
    panel = SidePanel(w.library, Settings(), "library")
    panel.show()
    panel.set_paper(w.library.get(papers[0].id))
    read, translate = [], []
    panel.readRequested.connect(read.append)
    panel.translateRequested.connect(translate.append)
    panel.detail.read_btn.click()
    assert read == [papers[0].id]
    panel.detail.translateRequested.emit([papers[0].id])
    assert translate == [[papers[0].id]]
    panel.set_paper(None)  # 何も選ばないときはノートの欄も空になる
    assert panel.note_editor.paper_id is None
