"""README 用のスクリーンショットを、架空のデモ用データで撮り直す。

使い方: python scripts/screenshots.py [--out docs/images] [--only library,reader,...]

- 本物のホームを使わないよう、HOME と SPECTRA_HOME を使い捨ての一時フォルダにして、自分自身を起動し直す。
  同期フォルダの候補もその中の架空の GoogleDrive-demo@example.com だけになり、利用者のアカウントは写らない。
- 論文と書誌情報は架空のもの。デモ論文の訳文は docs/demo/demo_paper.py の書き下ろしの訳文を使う
  （画面の翻訳エンジンは Gemini と表示するが、通信はせず、API キーも架空のもの）。
- 撮るのはウィンドウの中だけ（QWidget.grab）。通知・ほかのアプリ・メニューバーは写らない。
- 撮る前に画面の文字を調べ、個人情報らしいもの（/Users/ などの実際のパス、example.com 以外のメールアドレス）があれば止める。
- 保存した PNG にはメタデータを付けない。
- README で軽く読み込めるよう、幅を MAX_WIDTH まで縮め、256 色に減らして保存する（Pillow。開発用の依存に入っている）。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCENES = ("library", "reader", "export", "trash", "sync", "settings")
_CHILD = "SPECTRA_SCREENSHOT_CHILD"
SIZE = (1440, 900)
MAX_WIDTH = 1920  # README では幅 960 で表示するので、高解像度の画面でも 2 倍あれば足りる

# 架空の論文（タイトル, 著者 [(姓, 名)], 年, 雑誌, DOI）。DOI は例示用の 10.5555
PAPERS = [
    ("Sparse Attention for Long Scientific Documents", [("Rivera", "Elena"), ("Okafor", "Chidi")], 2025, "Journal of Example Studies", "10.5555/demo.2025.001"),
    ("A Survey of Layout Analysis for PDF Papers", [("Nakamura", "Ren")], 2024, "Example Conference on Documents", "10.5555/demo.2024.017"),
    ("深層学習による数式領域の検出", [("佐藤", "花子"), ("鈴木", "一郎")], 2023, "架空情報処理学会論文誌", "10.5555/demo.2023.042"),
    ("Measuring Readability of Machine-Translated Papers", [("Lindqvist", "Maja")], 2022, "Proceedings of the Demo Workshop", ""),
    ("Figures, Tables and Equations as Fixed Regions", [("Haddad", "Omar"), ("Chen", "Wei")], 2021, "Example Letters", "10.5555/demo.2021.008"),
]
# 架空の論文の本文（本文の検索の場面で、抜粋が出るように。SEARCH の語を含む）
BODIES = {
    1: "Earlier systems ignored punctuation when they split a page into regions, so headings were often merged with the paragraphs below them.",
    2: "本研究では、数式の領域を検出したあと、punctuation と記号の並びから行内の数式を区別する。",
    3: "Readers rated each page for fluency; errors in punctuation and line breaks lowered the scores the most.",
    4: "Captions keep their original punctuation, and equation numbers stay aligned with the right margin.",
}
SEARCH = "punctuation"  # ライブラリの場面で検索欄に入れる語（タイトルにはなく、本文にだけある）
NOTE = "レイアウトを保ったまま訳す方法。図表と数式の位置を変えない点が、先行研究との違い。\n\n- 式 (1) の縮小率は下限を設けていない？\n- 3 章の比較を、自分の実験の評価にも使う"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "images")
    ap.add_argument("--only", default=",".join(SCENES), help=f"撮る場面（カンマ区切り）: {', '.join(SCENES)}")
    args = ap.parse_args()
    scenes = [s for s in args.only.split(",") if s]
    unknown = set(scenes) - set(SCENES)
    if unknown:
        ap.error(f"不明な場面: {', '.join(sorted(unknown))}")
    if os.environ.get(_CHILD):
        return _shoot(args.out.resolve(), scenes)
    with tempfile.TemporaryDirectory(prefix="spectra-shots-") as tmp:
        env = dict(os.environ)
        env.update(
            HOME=tmp,
            SPECTRA_HOME=str(Path(tmp) / "spectra"),
            PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT / "docs" / "demo"), env.get("PYTHONPATH", "")]),
            **{_CHILD: "1"},
        )
        env.pop("QT_QPA_PLATFORM", None)  # 本物の描画で撮る（offscreen では文字の描画が違う）
        cmd = [sys.executable, str(Path(__file__).resolve()), "--out", str(args.out.resolve()), "--only", ",".join(scenes)]
        return subprocess.call(cmd, env=env, cwd=tmp)


def _shoot(out: Path, scenes: list[str]) -> int:
    import demo_paper
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from spectra.config.secrets import SecretStore
    from spectra.config.settings import Settings
    from spectra.library import Library
    from spectra.library import lookup as lookup_module
    from spectra.library.models import Author, Metadata
    from spectra.translate import registry
    from spectra.ui import theme
    from spectra.ui.app import create_app
    from spectra.ui.main_window import MainWindow

    home = Path.home()
    # 架空の同期フォルダの候補（同期タブ用）
    drive = home / "Library" / "CloudStorage" / "GoogleDrive-demo@example.com" / "マイドライブ"
    drive.mkdir(parents=True)
    # 擬似翻訳の代わりに、デモ論文の書き下ろしの訳文を使う
    registry.create = lambda *_a, **_k: demo_paper.DemoTranslator()
    # 書誌情報の照会は既定でオンなので、画面もオンのときの見た目にする。ただし通信はしない
    lookup_module.lookup = lambda *_a, **_k: None

    app = create_app([])
    work = home / "papers"
    work.mkdir()
    lib = Library()
    demo_pdf = work / "demo.pdf"
    demo_pdf.write_bytes((ROOT / "docs" / "demo" / "demo.pdf").read_bytes())
    demo, _ = lib.add(demo_pdf, Metadata(title="Layout-Preserving Translation of Scientific Documents",
                                          authors=[Author("Tanaka", "Aiko"), Author("Carter", "Ben")], year=2026,
                                          venue="Example Institute of Technology Reports", doi="10.5555/demo.2026.001"))
    ids = [demo.id]
    for i, (title, authors, year, venue, doi) in enumerate(PAPERS):
        f = work / f"paper{i}.pdf"
        _blank_pdf(f, title, BODIES.get(i, ""))
        p, _ = lib.add(f, Metadata(title=title, authors=[Author(a, b) for a, b in authors], year=year, venue=venue, doi=doi))
        ids.append(p.id)
    for pid in ids:
        lib.set_meta_status(pid, "done")  # 照会済みの扱いにして「照会を有効にする」の案内を出さない
    for pid in (ids[0], ids[2]):  # 一覧に、塗りつぶしたスターと枠だけのスターが並ぶように
        lib.set_favorite(pid, True)
    _demo_annotations(lib, demo.id, demo_pdf)
    lib.set_note(demo.id, NOTE)
    col = lib.create_collection("修士論文")
    lib.add_to_collection(col.id, ids[:3])

    settings = Settings(engine="gemini", metadata_lookup=True)
    secrets = SecretStore(use_keyring=False)
    secrets.set("gemini", "demo-key-not-used")  # 一時フォルダの設定ファイルに入るだけで、どこにも送らない
    win = MainWindow(settings=settings, secrets=secrets, library=lib)
    win.resize(*SIZE)
    win.show()
    failures: list[str] = []

    def pump(seconds: float = 0.3, until=None) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            if until and until():
                return True
            time.sleep(0.01)
        return until is None

    def set_theme(mode: str) -> None:
        theme.set_appearance(mode)
        theme.apply(app)
        win.refresh_theme()
        pump()

    def snap(widget, name: str) -> None:
        if win.banner.isVisible():  # 翻訳の完了などの一時的な知らせは写さない
            win.banner._on_close()
        pump(0.4)
        leaks = _personal_text(widget, home)
        if leaks:
            failures.append(f"{name}: {leaks}")
            return
        image = widget.grab().toImage()
        out.mkdir(parents=True, exist_ok=True)
        for key in image.textKeys():  # 念のため、テキストのメタデータを付けない
            image.setText(key, "")
        image.save(str(out / f"{name}.png"))
        _shrink(out / f"{name}.png")
        print(out / f"{name}.png")

    def run() -> None:
        try:
            win.translate_papers([demo.id])
            if not pump(60, until=lambda: lib.get(demo.id).has_translation_file and not win._busy()):
                failures.append("デモ論文の翻訳が終わりませんでした")
            # 本文の索引を作る（ふだんは起動の数秒後に裏で作る）
            win.fulltext.schedule(0)
            pump(1.0)
            if not pump(60, until=lambda: not win.fulltext.indexing and (win.fulltext.stats() or None) is not None):
                failures.append("本文の索引ができませんでした")
            for mode in ("light", "dark"):
                set_theme(mode)
                if "library" in scenes:
                    # お気に入りのスター（塗り・枠）と、本文の検索の抜粋（「本文に一致」）
                    win.show_library()
                    win.library_view.side_panel.set_current_tab("details")
                    win.search.setText(SEARCH)
                    view = win.library_view
                    if not pump(10, until=lambda: any(win.library_view.proxy.matches(i) for i in ids)):
                        failures.append("本文の検索の結果が出ませんでした")
                    view.select_ids([demo.id])
                    snap(win, f"screenshot-library-{mode}")
                    win.search.clear()
                    pump()
                if "reader" in scenes:
                    win.open_paper(demo.id)
                    pump(1.5)
                    win.view_mode.set_current("both")
                    win.nav_btn.setChecked(True)  # 左のページの一覧
                    win.side_panel.set_current_tab("note")
                    pump(1.0)
                    snap(win, f"screenshot-reader-{mode}")
                    win.nav_btn.setChecked(False)
                    win.show_library()
                if "trash" in scenes:
                    lib.trash(ids[-1])
                    win.library_view.reload()
                    win.library_view.sidebar.select_scope("trash")
                    win.library_view.select_ids([ids[-1]])
                    snap(win, f"screenshot-trash-{mode}")
                    lib.restore(ids[-1])
                    win.library_view.reload()
                    win.library_view.sidebar.select_scope("all")
                if "sync" in scenes:
                    _sync_shots(win, mode, snap)
                if "settings" in scenes:
                    _settings_shots(win, mode, snap)
                if "export" in scenes and mode == "light":
                    from spectra.core.options import OutputMode
                    from spectra.ui.export_dialog import ExportDialog

                    count = len(lib.annotations(demo.id))  # 注釈があるときだけ「注釈を含める」が出る
                    dlg = ExportDialog(OutputMode.SIDE_BY_SIDE, 1, win, annotation_count=count)
                    dlg.show()
                    snap(dlg, "screenshot-export")
                    dlg.close()
        finally:
            win.close()
            QApplication.instance().quit()

    QTimer.singleShot(500, run)
    app.exec()
    lib.close()
    for f in failures:
        print(f"撮影を中止しました: {f}", file=sys.stderr)
    return 1 if failures else 0


def _sync_shots(win, mode: str, snap) -> None:
    """設定の「同期」タブ（未接続で、架空の候補が見えている状態）。"""
    from spectra.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(win.settings, win.secrets, win.cache, win, sync=win.sync)
    dlg.show_tab("sync")
    dlg.show()
    snap(dlg, f"screenshot-sync-{mode}")
    dlg.close()


def _settings_shots(win, mode: str, snap) -> None:
    """設定の「詳細」タブの本文検索の欄（索引の件数と大きさが出ている状態）。"""
    from spectra.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(win.settings, win.secrets, win.cache, win, sync=win.sync, fulltext=win.fulltext)
    dlg.show_tab("advanced")
    dlg.show()
    area = dlg.stack.currentWidget()
    group = dlg.fulltext_search
    while group.parentWidget() is not area.widget():  # 本文検索の欄（見出しから）を上端に
        group = group.parentWidget()
    area.verticalScrollBar().setValue(max(0, group.y() - 8))
    snap(dlg, f"screenshot-settings-{mode}")
    dlg.close()


def _demo_annotations(lib, paper_id: int, pdf: Path) -> None:
    """デモ論文に、マーカー 2 つと付箋 1 つを付ける（位置は原文の文字から求める）。"""
    import pymupdf

    with pymupdf.open(pdf) as doc:
        page = doc[0]
        for phrase, color, comment in (
            ("replaces only the running text", "yellow", ""),
            ("Blocks that belong to", "green", ""),
            ("punctuation never starts a line.", "blue", "禁則処理。日本語の組版の規則に合わせている"),
        ):
            quads = [tuple(r) for r in page.search_for(phrase)]
            lib.add_annotation(paper_id, "highlight", 0, quads, quote=phrase, color=color, comment=comment)
    lib.add_annotation(paper_id, "note", 0, [(512, 250, 512, 250)], color="pink", comment="この図を発表のスライドに使う")


def _shrink(path: Path) -> None:
    """幅を MAX_WIDTH までに縮め、256 色にして最大の圧縮で保存し直す（メタデータは付けない）。

    減色は MAXCOVERAGE で、画面の小さな色の部分（マーカーの色など）も元の色のまま残す（元との差は 1 チャンネル 12 以内）。
    """
    from PIL import Image

    with Image.open(path) as src:
        image = src.convert("RGB")
    if image.width > MAX_WIDTH:
        image = image.resize((MAX_WIDTH, round(image.height * MAX_WIDTH / image.width)), Image.Resampling.LANCZOS)
    image.quantize(256, method=Image.Quantize.MAXCOVERAGE, dither=Image.Dither.NONE).save(path, optimize=True)


def _blank_pdf(path: Path, title: str, body: str = "") -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 523, 140), title, fontsize=16, fontname="tiro")
    if body:
        page.insert_textbox(pymupdf.Rect(72, 160, 523, 400), body, fontsize=11, fontname="japan")
    doc.save(path)


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _personal_text(root, home: Path) -> list[str]:
    """画面に出ている文字から、個人情報らしいものを探す（一時フォルダのパスと example.com は除く）。"""
    from PySide6.QtWidgets import QAbstractButton, QLabel, QLineEdit, QWidget

    real_home = os.path.expanduser("~" + os.environ.get("USER", ""))
    found = []
    for w in [root, *root.findChildren(QWidget)]:
        if not w.isVisible():
            continue
        texts = [w.toolTip()]
        if isinstance(w, (QLabel, QAbstractButton, QLineEdit)):
            texts.append(w.text())
        for t in filter(None, texts):
            t = t.replace(str(home), "~")
            if "/Users/" in t or "\\Users\\" in t or (real_home and real_home in t):
                found.append(t)
            found += [m for m in _EMAIL.findall(t) if not m.endswith("example.com")]
    return found


if __name__ == "__main__":
    sys.exit(main())
