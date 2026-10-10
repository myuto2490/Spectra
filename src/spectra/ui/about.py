"""「Spectra について」ダイアログ。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import APP_NAME, __version__
from .app_icon import app_pixmap
from .widgets import button, label, link_button

REPO_URL = "https://github.com/myuto2490/Spectra"

NOTICES = (
    "PyMuPDF / MuPDF — GNU AGPL v3（Artifex Software）",
    "Qt / PySide6 — GNU LGPL v3（The Qt Company）",
    "fontTools — MIT License",
    "httpx — BSD License / keyring — MIT License / platformdirs — MIT License",
    "Noto Sans JP — SIL Open Font License 1.1（同梱）",
    "Noto Serif JP — SIL Open Font License 1.1（任意でダウンロード）",
)


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} について")
        self.setFixedWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 28, 28, 20)
        lay.setSpacing(6)
        icon = QLabel()
        icon.setPixmap(app_pixmap(96))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon)
        lay.addSpacing(6)
        for text, role in ((APP_NAME, "largeTitle"), (f"バージョン {__version__}", "secondary")):
            lb = label(text, role)
            lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(lb)
        desc = label("論文 PDF を、図表・数式・レイアウトを保ったまま日本語に翻訳します。", "secondary", wrap=True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addSpacing(8)
        lay.addWidget(desc)
        links = QHBoxLayout()
        links.addStretch(1)
        for text, url in (("GitHub", REPO_URL), ("変更履歴", f"{REPO_URL}/blob/main/CHANGELOG.md"), ("ライセンス", f"{REPO_URL}/blob/main/LICENSE")):
            b = link_button(text)
            b.clicked.connect(lambda _c=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            links.addWidget(b)
            links.addSpacing(10)
        links.addStretch(1)
        lay.addLayout(links)
        lay.addSpacing(10)
        lic = label("このソフトウェアはフリーソフトウェアです。GNU AGPL v3 以降の条件のもとで再配布・改変できます。", "caption", wrap=True)
        lic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(lic)
        lay.addSpacing(8)
        lay.addWidget(label("使用しているソフトウェア", "section"))
        lay.addWidget(label("\n".join(NOTICES), "caption", wrap=True))
        lay.addSpacing(12)
        row = QHBoxLayout()
        row.addStretch(1)
        ok = button("閉じる", primary=True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        lay.addLayout(row)
        self.resize(QSize(440, self.sizeHint().height()))
