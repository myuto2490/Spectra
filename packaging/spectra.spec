# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller の設定。packaging/ ディレクトリから `pyinstaller spectra.spec` で実行する。

バージョンは src/spectra/__init__.py の __version__ から取得する（唯一の情報源）。
"""

import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/spectra/__init__.py").read_text(encoding="utf-8")).group(1)
NUMERIC = re.match(r"(\d+)\.(\d+)\.(\d+)", VERSION).groups()
ICONS = Path(SPECPATH) / "build"
APP_NAME = "Spectra"
BUNDLE_ID = "io.github.myuto2490.spectra"

# 使わない Qt モジュールを除外して配布サイズを抑える
EXCLUDES = [
    "tkinter", "unittest", "pydoc_data", "test",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick", "PySide6.QtQml",
    "PySide6.QtQuick3D", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialPort",
    "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
]

a = Analysis(
    [str(Path(SPECPATH) / "launcher.py")],
    pathex=[str(ROOT / "src")],
    datas=[(str(ROOT / "src/spectra/assets"), "spectra/assets")],
    hiddenimports=["keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService"],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
    )

    nums = tuple(int(n) for n in NUMERIC) + (0,)
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[
            StringFileInfo([StringTable("041104B0", [
                StringStruct("CompanyName", APP_NAME),
                StringStruct("FileDescription", "Spectra — 論文 PDF 翻訳"),
                StringStruct("FileVersion", VERSION),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", VERSION),
                StringStruct("LegalCopyright", "GNU AGPL v3"),
                StringStruct("OriginalFilename", "Spectra.exe"),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0411, 1200])]),
        ],
    )
    icon = str(ICONS / "icon.ico")
else:
    version_info = None
    icon = str(ICONS / ("icon.icns" if sys.platform == "darwin" else "icon.png"))

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon=icon,
    version=version_info,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME, upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "LSApplicationCategoryType": "public.app-category.productivity",
            "CFBundleDevelopmentRegion": "ja",
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "PDF Document",
                    "CFBundleTypeRole": "Viewer",
                    "LSHandlerRank": "Alternate",
                    "LSItemContentTypes": ["com.adobe.pdf"],
                }
            ],
        },
    )
