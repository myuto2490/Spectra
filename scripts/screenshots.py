"""README 用のスクリーンショットを、架空のデモ用データで撮り直す。

使い方: python scripts/screenshots.py [--out docs/images] [--only library,reader,...]

- 本物のホームを使わないよう、HOME と SPECTRA_HOME を使い捨ての一時フォルダにして、自分自身を起動し直す。
  同期フォルダの候補もその中の架空の GoogleDrive-demo@example.com だけになり、利用者のアカウントは写らない。
- 論文と書誌情報は架空のもの。デモ論文の訳文は docs/demo/demo_paper.py の書き下ろしの訳文を使う
  （画面の翻訳エンジンは Gemini と表示するが、通信はせず、API キーも架空のもの）。
- 撮るのはウィンドウの中だけ（QWidget.grab）。通知・ほかのアプリ・メニューバーは写らない。
- 撮る前に画面の文字を調べ、個人情報らしいもの（/Users/ などの実際のパス、example.com 以外のメールアドレス）があれば止める。
- 保存した PNG にはメタデータを付けない。
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
SCENES = ("library", "reader", "export", "trash", "sync")
_CHILD = "SPECTRA_SCREENSHOT_CHILD"
SIZE = (1440, 900)

# 架空の論文（タイトル, 著者 [(姓, 名)], 年, 雑誌, DOI）。DOI は例示用の 10.5555
PAPERS = [
    ("Sparse Attention for Long Scientific Documents", [("Rivera", "Elena"), ("Okafor", "Chidi")], 2025, "Journal of Example Studies", "10.5555/demo.2025.001"),
    ("A Survey of Layout Analysis for PDF Papers", [("Nakamura", "Ren")], 2024, "Example Conference on Documents", "10.5555/demo.2024.017"),
    ("深層学習による数式領域の検出", [("佐藤", "花子"), ("鈴木", "一郎")], 2023, "架空情報処理学会論文誌", "10.5555/demo.2023.042"),
    ("Measuring Readability of Machine-Translated Papers", [("Lindqvist", "Maja")], 2022, "Proceedings of the Demo Workshop", ""),
    ("Figures, Tables and Equations as Fixed Regions", [("Haddad", "Omar"), ("Chen", "Wei")], 2021, "Example Letters", "10.5555/demo.2021.008"),
]


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
        _blank_pdf(f, title)
        p, _ = lib.add(f, Metadata(title=title, authors=[Author(a, b) for a, b in authors], year=year, venue=venue, doi=doi))
        ids.append(p.id)
    lib.set_favorite(ids[1], True)
    col = lib.create_collection("修士論文")
    lib.add_to_collection(col.id, ids[:3])

    settings = Settings(engine="gemini", metadata_lookup=False)
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
        print(out / f"{name}.png")

    def run() -> None:
        try:
            win.translate_papers([demo.id])
            if not pump(60, until=lambda: lib.get(demo.id).has_translation_file and not win._busy()):
                failures.append("デモ論文の翻訳が終わりませんでした")
            for mode in ("light", "dark"):
                set_theme(mode)
                if "library" in scenes:
                    win.show_library()
                    win.library_view.select_ids([demo.id])
                    snap(win, f"screenshot-library-{mode}")
                if "reader" in scenes:
                    win.open_paper(demo.id)
                    pump(1.5)
                    win.view_mode.set_current("both")
                    snap(win, f"screenshot-reader-{mode}")
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
                if "export" in scenes and mode == "light":
                    from spectra.core.options import OutputMode
                    from spectra.ui.export_dialog import ExportDialog

                    dlg = ExportDialog(OutputMode.SIDE_BY_SIDE, 1, win)
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


def _blank_pdf(path: Path, title: str) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 523, 140), title, fontsize=16, fontname="tiro")
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
