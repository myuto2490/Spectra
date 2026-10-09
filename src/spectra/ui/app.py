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
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger(__name__).info("%s %s starting", APP_NAME, __version__)


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
    if sys.platform == "win32":
        # タスクバーで Python ではなく Spectra のアイコンを表示する
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass
    app = create_app()

    from . import theme
    from .main_window import MainWindow

    window = MainWindow()

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
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
