"""画面とログで、ユーザー名を含むパスやメールアドレスを出さないことのテスト。"""

import logging
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from spectra.ui.privacy import (  # noqa: E402
    RedactingFormatter,
    display_path,
    home_to_tilde,
    mask_email,
    redact,
)


def test_home_is_replaced_with_tilde():
    assert home_to_tilde("/Users/alice/Library/Logs/Spectra", "/Users/alice") == "~/Library/Logs/Spectra"
    assert home_to_tilde("/Users/alice", "/Users/alice") == "~"
    assert home_to_tilde("/Users/alicex/file", "/Users/alice") == "/Users/alicex/file"  # 別のユーザーは置き換えない
    win = r"C:\Users\Alice"
    assert home_to_tilde(r"c:\users\alice\AppData\Roaming\Spectra", win) == r"~\AppData\Roaming\Spectra"
    assert home_to_tilde("C:/Users/Alice/x.pdf", win) == "~/x.pdf"
    assert home_to_tilde("'/Users/alice' not found", "/Users/alice") == "'~' not found"


def test_redact_hides_google_drive_account_and_emails():
    path = "/Users/alice/Library/CloudStorage/GoogleDrive-alice.k@example.com/マイドライブ/Spectra Sync"
    assert display_path(path, "/Users/alice") == "~/Library/CloudStorage/GoogleDrive-…/マイドライブ/Spectra Sync"
    text = redact(f"failed: {path} (contact bob@example.org)", "/Users/alice")
    assert "alice" not in text and "@example.com" not in text and "bob@" not in text
    assert "GoogleDrive-…" in text and "…@…" in text


def test_log_formatter_redacts_messages_and_tracebacks(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/alice")))
    fmt = RedactingFormatter("%(levelname)s %(message)s")
    try:
        raise OSError("cannot open /Users/alice/Library/CloudStorage/GoogleDrive-alice@example.com/x.pdf")
    except OSError:
        import sys

        record = logging.LogRecord("t", logging.WARNING, __file__, 1, "sync failed: %s", ("/Users/alice/a.pdf",), sys.exc_info())
    out = fmt.format(record)
    assert "sync failed: ~/a.pdf" in out
    assert "Traceback" in out and "GoogleDrive-…" in out
    assert "alice@" not in out and "/Users/alice" not in out


def test_app_log_file_uses_redacting_formatter(monkeypatch, tmp_path):
    from spectra.ui import app

    root = logging.getLogger()
    before = list(root.handlers)
    try:
        app._setup_logging()
        added = [h for h in root.handlers if h not in before]
        assert added and isinstance(added[0].formatter, RedactingFormatter)
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_mask_email_shows_only_what_is_needed():
    assert mask_email("mykiyo@example.com") == "my•••@example.com"
    a, b = "mykiyo@example.com", "mylab@example.com"
    ma, mb = mask_email(a, [a, b]), mask_email(b, [a, b])
    assert ma != mb and ma.startswith("myk") and mb.startswith("myl")
    # どれほど似ていても、完全なアドレスは出さない
    c, d = "ab1@x.com", "ab2@x.com"
    assert mask_email(c, [c, d]) != c and "•••" in mask_email(c, [c, d])
    assert mask_email("a@x.com") == "a•••@x.com"


def test_google_drive_candidates_never_show_full_email(tmp_path):
    from spectra.ui.sync_settings import candidate_folders

    cloud = tmp_path / "Library" / "CloudStorage"
    (cloud / "GoogleDrive-mykiyo@example.com" / "マイドライブ").mkdir(parents=True)
    names = [n for n, _ in candidate_folders(tmp_path)]
    assert names == ["Google Drive"]
    (cloud / "GoogleDrive-mylab@example.com" / "My Drive").mkdir(parents=True)
    names = [n for n, _ in candidate_folders(tmp_path)]
    assert len(names) == 2 and len(set(names)) == 2
    assert all("•••@example.com" in n for n in names)
    assert not any("mykiyo@" in n or "mylab@" in n for n in names)


@pytest.fixture()
def app():
    pytest.importorskip("PySide6.QtWidgets")
    from spectra.ui.app import create_app

    return create_app([])


def test_settings_folders_show_tilde_and_sync_tooltips_hide_account(app, monkeypatch, tmp_path):
    from spectra.config import paths
    from spectra.config.secrets import SecretStore
    from spectra.config.settings import Settings
    from spectra.ui.settings_dialog import SettingsDialog
    from spectra.ui.widgets import FormGroup

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: paths.config_dir().parent))
    gd = tmp_path / "Library" / "CloudStorage" / "GoogleDrive-alice@example.com" / "マイドライブ"
    gd.mkdir(parents=True)
    settings = Settings(sync_enabled=False, sync_folder=str(gd))
    d = SettingsDialog(settings, SecretStore(use_keyring=False), None)
    texts = [lb.text() for g in d.findChildren(FormGroup) for lb in g.findChildren(type(d.sync_page.dest_label))]
    assert any(t.startswith("~") for t in texts)  # ライブラリ・設定・ログのパスは「~」から
    assert not any(str(paths.config_dir().parent) in t for t in texts)
    tip = d.sync_page.dest_label.toolTip()
    assert "alice@" not in tip and "GoogleDrive-…" in tip
    for w in d.findChildren(type(d)):
        assert "alice@" not in w.toolTip()
    d.close()


HTML_TITLE = "<b>Bold</b> <img src='file:///etc/passwd'> & <a href='https://evil.example'>click</a>"


def test_html_in_paper_metadata_is_shown_as_plain_text(app, tmp_path, monkeypatch):
    """ほかの端末や PDF のメタデータから来たタイトル・著者・概要を、HTML として解釈しない。"""
    monkeypatch.setenv("SPECTRA_DEV", "1")
    from pdf_factory import make_paper
    from PySide6.QtCore import Qt

    from spectra.config.secrets import SecretStore
    from spectra.config.settings import Settings
    from spectra.library import Author, Metadata
    from spectra.ui.library_model import COL, PaperIdRole
    from spectra.ui.main_window import MainWindow
    from spectra.ui.widgets import Banner

    w = MainWindow(settings=Settings(engine="dummy", metadata_lookup=False), secrets=SecretStore(use_keyring=False))
    try:
        path = tmp_path / "a.pdf"
        make_paper(str(path))
        paper = w.library.add(path)[0]
        w.library.update_metadata(
            paper.id, Metadata(title=HTML_TITLE, authors=[Author("<i>Evil</i>", "X")], abstract="<h1>Big</h1>")
        )
        w.library_view.reload()
        w.library_view.select_ids([paper.id])
        d = w.library_view.detail
        for lb in (d.title, d.abstract):
            assert lb.textFormat() == Qt.TextFormat.PlainText
        assert d.title.text() == HTML_TITLE
        # 一覧のツールチップ（ツールチップは HTML らしい文字列をリッチテキストとして描く）
        m = w.library_view.model
        idx = next(m.index(r, COL["title"]) for r in range(m.rowCount()) if m.index(r, 0).data(PaperIdRole) == paper.id)
        tip = idx.data(Qt.ItemDataRole.ToolTipRole)
        assert "&lt;b&gt;Bold&lt;/b&gt;" in tip and "<b>Bold</b>" not in tip
        # ゴミ箱の詳細
        w.library.trash(paper.id)
        w.library_view.refresh_paper(paper.id)
        w.library_view.sidebar.select_scope("trash")
        w.library_view.select_ids([paper.id])
        assert d.trash_title.textFormat() == Qt.TextFormat.PlainText and d.trash_title.text() == HTML_TITLE
        assert d.trash_authors.textFormat() == Qt.TextFormat.PlainText
        # バナー（論文のタイトルを含む知らせ）
        banner = w.findChild(Banner)
        w._show_banner("success", f"「{HTML_TITLE}」の翻訳が完了しました", HTML_TITLE)
        assert banner._title.textFormat() == Qt.TextFormat.PlainText
        assert banner._body.textFormat() == Qt.TextFormat.PlainText
    finally:
        w.close()
        w.library.close()


def test_message_boxes_are_plain_text(app):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel, QMessageBox

    from spectra.ui.widgets import label, plain_box, plain_tooltip

    box = QMessageBox()
    box.setText(f"「{HTML_TITLE}」をライブラリから削除しますか？")
    box.setInformativeText(HTML_TITLE)
    plain_box(box)
    for name in ("qt_msgbox_label", "qt_msgbox_informativelabel"):
        assert box.findChild(QLabel, name).textFormat() == Qt.TextFormat.PlainText
    assert label(HTML_TITLE).textFormat() == Qt.TextFormat.PlainText
    assert plain_tooltip("<b>x</b>") == "<p style='white-space:pre-wrap'>&lt;b&gt;x&lt;/b&gt;</p>"
    assert plain_tooltip("") == ""
