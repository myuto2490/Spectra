"""GUI の起動。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .. import APP_ID, APP_NAME, __version__


def _setup_logging() -> None:
    from ..config import paths

    try:
        log_dir = paths.log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_dir / "spectra.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        handler = logging.StreamHandler()
    # ログはバグ報告に貼られることがあるので、ユーザー名を含むパスやメールアドレスを伏せて書く
    from .privacy import RedactingFormatter

    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # 通信とフォントのサブセット化は記録が多いので、警告以上だけを残す
    for noisy in ("httpx", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger(__name__).info("%s %s starting", APP_NAME, __version__)


def _install_excepthook() -> None:
    """どこでも捕まえられなかった例外を、ログに残す。

    配布版には標準エラー出力の行き先がなく、ボタンの処理やワーカーのスレッドで起きた想定外の例外は、
    何も残さずに消えてしまう（「何も起きない」という報告の原因を調べられない）。ログは個人の情報を伏せて書かれる。
    """
    import threading

    log = logging.getLogger("spectra.unhandled")

    def hook(exc_type, exc, tb) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            log.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    def thread_hook(args) -> None:
        if args.exc_type is not SystemExit:
            log.critical("unhandled exception in thread %s", getattr(args.thread, "name", "?"), exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook


def create_app(argv: list[str] | None = None):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    app.setStyle("Fusion")

    from ..config.settings import Settings
    from . import theme
    from .app_icon import app_icon

    theme.set_appearance(Settings.load().appearance)
    app.setFont(theme.ui_font())
    app.setWindowIcon(app_icon())
    theme.apply(app)
    return app


def main() -> int:
    _setup_logging()
    _install_excepthook()
    if sys.platform == "win32":
        # タスクバーで Python ではなく Spectra のアイコンを表示する
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass
    app = create_app()

    from ..library import Library, LibraryError
    from . import theme
    from .main_window import MainWindow

    try:
        library = Library()
    except LibraryError as e:
        # 新しいバージョンで作られたライブラリなど。壊さないよう、開かずに知らせて終了する
        from PySide6.QtWidgets import QMessageBox

        title, _, rest = e.user_message().partition("\n")
        from .widgets import plain_box

        box = QMessageBox(QMessageBox.Icon.Critical, APP_NAME, title)
        box.setInformativeText(rest)
        plain_box(box).exec()
        return 1
    window = MainWindow(library=library)

    def on_scheme_changed(*_args) -> None:
        theme.apply(app)
        window.refresh_theme()

    app.styleHints().colorSchemeChanged.connect(on_scheme_changed)
    window.show()
    # コマンドラインや「このアプリで開く」で渡された PDF を開く
    for arg in sys.argv[1:]:
        if arg.lower().endswith(".pdf") and Path(arg).is_file():
            window.open_file(Path(arg))
            break
    code = app.exec()
    _shutdown(app, window)
    return code


def _shutdown(app, window) -> None:
    """終わりの片付け。バックグラウンドの処理を待ち、ウィンドウと QApplication を決まった順番で破棄する。

    Python の終了の後始末に任せると、破棄の順番によっては「QObject: shared QObject was deleted directly」が出て、
    環境によっては落ちる。
    """
    from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool

    QThreadPool.globalInstance().waitForDone(3000)
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    if hasattr(app, "shutdown"):  # 古い PySide6 にはない
        app.shutdown()


if __name__ == "__main__":
    sys.exit(main())
