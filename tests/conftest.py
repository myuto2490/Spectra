import gc
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from pdf_factory import make_paper  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_user_data(tmp_path_factory, monkeypatch):
    """テストが利用者の設定・キャッシュ・API キー（キーチェーン）に触れないようにする。"""
    monkeypatch.setenv("SPECTRA_HOME", str(tmp_path_factory.mktemp("spectra-home")))
    monkeypatch.setenv("SPECTRA_NO_UPDATE_CHECK", "1")  # 起動時の更新の確認（GitHub への通信と問い合わせの画面）を止める
    for var in ("SPECTRA_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def _qt_cleanup(final: bool = False) -> None:
    """テストが残した Qt のウィンドウを閉じて破棄し、保留中の削除を済ませる。

    残したままだと、インタプリタの終了時に Python の後始末の順番で Qt のオブジェクトが壊され、
    「QObject: shared QObject was deleted directly」が出る（Linux・Python 3.10 では abort して CI が落ちた）。
    """
    if "PySide6.QtWidgets" not in sys.modules:
        return  # Qt を使っていないテストでは何もしない
    from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    for w in QApplication.topLevelWidgets():
        w.close()
        w.deleteLater()
    QThreadPool.globalInstance().waitForDone(5000)
    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    if final:
        gc.collect()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.shutdown()  # インタプリタの終了より前に、決まった順番で QApplication を破棄する


@pytest.fixture(autouse=True)
def _close_qt_windows():
    yield
    _qt_cleanup()


def pytest_sessionfinish(session, exitstatus):
    _qt_cleanup(final=True)


@pytest.fixture()
def paper_path(tmp_path: Path) -> Path:
    path = tmp_path / "paper.pdf"
    make_paper(str(path))
    return path


@pytest.fixture()
def paper(paper_path: Path):
    import pymupdf

    doc = pymupdf.open(paper_path)
    yield doc
    doc.close()
