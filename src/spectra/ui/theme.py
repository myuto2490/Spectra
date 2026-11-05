"""デザイントークンとスタイルシート。

Apple Human Interface Guidelines の考え方（控えめなニュートラル色、単一のアクセントカラー、
システムのライト/ダークモードへの追従）に沿った配色を、全 OS 共通で適用する。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

# 角丸と余白（4 の倍数）
RADIUS_CONTROL = 6
RADIUS_CARD = 10
SPACE = (4, 8, 12, 16, 20, 24)

# 文字サイズ（px）。macOS の 13pt を基準に、他 OS は表示密度に合わせて 1px 大きくする
BASE = 13 if IS_MAC else 14
TYPE = {
    "large_title": BASE + 9,
    "title": BASE + 4,
    "headline": BASE,
    "body": BASE,
    "callout": BASE - 1,
    "caption": BASE - 2,
}


@dataclass(frozen=True)
class Tokens:
    dark: bool
    accent: str
    accent_hover: str
    accent_pressed: str
    on_accent: str
    primary: str  # 主ボタンの塗り（白い文字が読める濃さ）
    primary_hover: str
    primary_pressed: str
    window: str
    surface: str
    surface_alt: str
    control: str
    control_hover: str
    control_pressed: str
    canvas: str
    label: str
    secondary: str
    tertiary: str
    separator: str
    border: str
    fill: str
    success: str
    warning: str
    danger: str
    focus: str
    selection: str


LIGHT = Tokens(
    dark=False,
    accent="#2563EB",
    accent_hover="#1D4ED8",
    accent_pressed="#1E40AF",
    on_accent="#FFFFFF",
    primary="#2563EB",
    primary_hover="#1D4ED8",
    primary_pressed="#1E40AF",
    window="#F4F5F9",
    surface="#FFFFFF",
    surface_alt="#FAFBFD",
    control="#FFFFFF",
    control_hover="#F3F4F8",
    control_pressed="#E7E9F0",
    canvas="#E9EBF1",
    label="#14151A",
    secondary="#6B6F7B",
    tertiary="#A9ADB8",
    separator="#E6E8EE",
    border="#D6D9E1",
    fill="#EEF0F5",
    success="#28A745",
    warning="#C77700",
    danger="#E5352B",
    focus="#7FB5FF",
    selection="#CCE4FF",
)

DARK = Tokens(
    dark=True,
    accent="#6B9BFF",
    accent_hover="#82ABFF",
    accent_pressed="#5A8AF0",
    on_accent="#FFFFFF",
    primary="#2F6FE0",
    primary_hover="#3B7BEB",
    primary_pressed="#2860C8",
    window="#121317",
    surface="#1B1D23",
    surface_alt="#171920",
    control="#252831",
    control_hover="#2D313B",
    control_pressed="#363A46",
    canvas="#0C0D10",
    label="#F2F3F7",
    secondary="#9CA0AD",
    tertiary="#5F6370",
    separator="#262932",
    border="#333744",
    fill="#21242C",
    success="#30D158",
    warning="#FFD60A",
    danger="#FF453A",
    focus="#3B82F6",
    selection="#1F4F8A",
)


_appearance = "auto"  # auto / light / dark


def set_appearance(value: str) -> None:
    global _appearance
    _appearance = value if value in ("auto", "light", "dark") else "auto"
    # タイトルバーなどネイティブの部分も合わせる。自動のときは指定を外してシステムに従う
    if QGuiApplication.instance() is not None:
        schemes = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}
        QGuiApplication.styleHints().setColorScheme(schemes.get(_appearance, Qt.ColorScheme.Unknown))


def is_dark() -> bool:
    if _appearance != "auto":
        return _appearance == "dark"
    hints = QGuiApplication.styleHints()
    return hints.colorScheme() == Qt.ColorScheme.Dark


def current() -> Tokens:
    return DARK if is_dark() else LIGHT


def ui_font() -> QFont:
    f = QFont()
    if IS_WIN:
        f.setFamilies(["Segoe UI", "Yu Gothic UI", "Meiryo UI"])
    elif not IS_MAC:
        f.setFamilies(["Inter", "Noto Sans CJK JP", "Noto Sans JP", "Ubuntu", "Cantarell", "DejaVu Sans"])
    f.setPixelSize(TYPE["body"])
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def palette(t: Tokens) -> QPalette:
    p = QPalette()
    c = QColor
    p.setColor(QPalette.ColorRole.Window, c(t.window))
    p.setColor(QPalette.ColorRole.WindowText, c(t.label))
    p.setColor(QPalette.ColorRole.Base, c(t.surface))
    p.setColor(QPalette.ColorRole.AlternateBase, c(t.surface_alt))
    p.setColor(QPalette.ColorRole.Text, c(t.label))
    p.setColor(QPalette.ColorRole.PlaceholderText, c(t.tertiary))
    p.setColor(QPalette.ColorRole.Button, c(t.control))
    p.setColor(QPalette.ColorRole.ButtonText, c(t.label))
    p.setColor(QPalette.ColorRole.Highlight, c(t.accent))
    p.setColor(QPalette.ColorRole.HighlightedText, c(t.on_accent))
    p.setColor(QPalette.ColorRole.ToolTipBase, c(t.surface))
    p.setColor(QPalette.ColorRole.ToolTipText, c(t.label))
    p.setColor(QPalette.ColorRole.Link, c(t.accent))
    p.setColor(QPalette.ColorRole.Mid, c(t.border))
    p.setColor(QPalette.ColorRole.Midlight, c(t.separator))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, c(t.tertiary))
    return p


def stylesheet(t: Tokens, chevron: str = "") -> str:
    ty = TYPE
    return f"""
* {{ outline: 0; }}
QWidget {{ color: {t.label}; }}
QMainWindow, QDialog, #Window {{ background: {t.window}; }}
QToolTip {{
    background: {t.surface}; color: {t.label}; border: 1px solid {t.border};
    border-radius: 6px; padding: 4px 8px;
}}

/* ---- 文字の役割 ---- */
QLabel[role="largeTitle"] {{ font-size: {ty['large_title']}px; font-weight: 700; }}
QLabel[role="title"] {{ font-size: {ty['title']}px; font-weight: 600; }}
QLabel[role="headline"] {{ font-size: {ty['headline']}px; font-weight: 600; }}
QLabel[role="secondary"] {{ color: {t.secondary}; }}
QLabel[role="caption"] {{ color: {t.secondary}; font-size: {ty['caption']}px; }}
QLabel[role="section"] {{
    color: {t.secondary}; font-size: {ty['caption']}px; font-weight: 700; padding-left: 4px; letter-spacing: 0.6px;
}}
QLabel[role="metric"] {{ font-weight: 600; font-size: {ty['body'] + 1}px; }}
QLabel[tone="danger"] {{ color: {t.danger}; }}
QLabel[tone="warning"] {{ color: {t.warning}; }}
QLabel[tone="success"] {{ color: {t.success}; }}
QLabel a {{ color: {t.accent}; }}

/* ---- ツールバー・ステータスバー ---- */
#Toolbar {{ background: {t.window}; border-bottom: 1px solid {t.separator}; }}
#Footer {{ background: {t.window}; border-top: 1px solid {t.separator}; }}
#Inspector {{ background: {t.window}; border-left: 1px solid {t.separator}; }}
#Canvas, #Canvas > QWidget > QWidget {{ background: {t.canvas}; }}
#PaneHeader {{ background: {t.surface_alt}; border-bottom: 1px solid {t.separator}; }}
#NavPane {{ background: {t.surface_alt}; border-right: 1px solid {t.separator}; }}
#SelectionBar {{ background: {t.surface}; border: 1px solid {t.separator}; border-radius: 10px; }}
#Thumbnails, #Outline {{ background: transparent; border: none; padding: 6px; outline: 0; color: {t.secondary}; }}
#Thumbnails::item {{ padding: 6px; border-radius: 6px; }}
#Thumbnails::item:hover, #Outline::item:hover {{ background: {t.fill}; }}
#Thumbnails::item:selected, #Outline::item:selected {{ background: {t.selection}; color: {t.label}; }}
#Outline::item {{ padding: 4px 6px; border-radius: 6px; color: {t.label}; }}

/* ---- ライブラリ ---- */
#SidebarPane {{ background: {t.surface_alt}; border-right: 1px solid {t.separator}; }}
#Sidebar {{ background: transparent; border: none; padding: 8px 8px; outline: 0; }}
#Sidebar::item {{ padding: 5px 8px; border-radius: 6px; color: {t.label}; }}
#Sidebar::item:disabled {{ color: {t.secondary}; padding-top: 10px; }}
#Sidebar::item:hover {{ background: {t.fill}; }}
#Sidebar::item:selected {{ background: {t.selection}; color: {t.label}; }}
/* 同期の状態の行（サイドバーの一番下）とポップオーバー（docs/SYNC.md §7.3） */
#SyncStatus {{
    background: transparent; border: none; border-radius: {RADIUS_CONTROL}px; padding: 6px 8px;
    text-align: left; color: {t.secondary}; font-size: {ty['caption']}px;
}}
#SyncStatus:hover {{ background: {t.fill}; }}
#SyncStatus:pressed {{ background: {t.control_pressed}; }}
#SyncStatus:focus {{ border: 2px solid {t.focus}; padding: 4px 6px; }}
#SyncStatus[tone="warning"] {{ color: {t.warning}; }}
#SyncPopover {{ background: {t.surface}; border: 1px solid {t.separator}; border-radius: {RADIUS_CARD}px; }}
#PaperTable {{
    background: {t.surface}; alternate-background-color: {t.surface_alt}; border: none;
    selection-background-color: {t.selection}; selection-color: {t.label}; outline: 0;
}}
#PaperTable::item {{ padding: 0 6px; border: none; }}
QHeaderView {{ background: {t.surface}; border: none; }}
QHeaderView::section {{
    background: {t.surface}; color: {t.secondary}; border: none; border-bottom: 1px solid {t.separator};
    border-right: 1px solid {t.separator}; padding: 5px 8px; font-size: {ty['caption']}px; font-weight: 600;
}}
QHeaderView::section:last {{ border-right: none; }}
#LibraryCenter[dragActive="true"] {{ border: 2px dashed {t.accent}; border-radius: {RADIUS_CARD}px; }}
QLineEdit#Search {{ border-radius: 9px; padding-left: 4px; }}
#DetailTabs {{ background: {t.window}; border-bottom: 1px solid {t.separator}; }}
#DetailTabs QPushButton {{
    background: transparent; border: none; border-bottom: 2px solid transparent; border-radius: 0;
    padding: 10px 2px 8px 2px; color: {t.secondary}; font-weight: 600; min-height: 18px;
}}
#DetailTabs QPushButton:hover {{ color: {t.label}; background: transparent; }}
#DetailTabs QPushButton:checked {{ color: {t.label}; border-bottom: 2px solid {t.accent}; background: transparent; }}
#DetailTabs QPushButton:focus {{ color: {t.accent}; border: none; border-bottom: 2px solid {t.focus}; padding: 10px 2px 8px 2px; }}
QLabel[role="tertiary"] {{ color: {t.tertiary}; }}
QLineEdit#Search:focus {{ padding-left: 3px; }}

/* ---- カード・グループ ---- */
QFrame[card="true"] {{
    background: {t.surface}; border: 1px solid {t.separator}; border-radius: {RADIUS_CARD}px;
}}
QFrame[rowSeparator="true"] {{ background: {t.separator}; border: none; max-height: 1px; min-height: 1px; }}
QFrame[banner="info"] {{ background: {t.surface}; border: 1px solid {t.separator}; border-radius: {RADIUS_CARD}px; }}
QFrame[banner="warning"] {{ background: {t.surface}; border: 1px solid {t.warning}; border-radius: {RADIUS_CARD}px; }}
QFrame[banner="danger"] {{ background: {t.surface}; border: 1px solid {t.danger}; border-radius: {RADIUS_CARD}px; }}

/* ---- ボタン ---- */
QPushButton {{
    background: {t.control}; color: {t.label}; border: 1px solid {t.border};
    border-radius: {RADIUS_CONTROL}px; padding: 5px 14px; min-height: 18px;
}}
QPushButton:hover {{ background: {t.control_hover}; }}
QPushButton:pressed {{ background: {t.control_pressed}; }}
QPushButton:focus {{ border: 2px solid {t.focus}; padding: 4px 13px; }}
QPushButton:disabled {{ color: {t.tertiary}; background: {t.fill}; border-color: {t.separator}; }}
QPushButton[primary="true"] {{
    background: {t.primary}; color: {t.on_accent}; border: 1px solid {t.primary}; font-weight: 600;
    padding: 5px 16px;
}}
QPushButton[primary="true"]:hover {{ background: {t.primary_hover}; border-color: {t.primary_hover}; }}
QPushButton[primary="true"]:pressed {{ background: {t.primary_pressed}; border-color: {t.primary_pressed}; }}
QPushButton[primary="true"]:focus {{ border: 2px solid {t.focus}; padding: 4px 15px; }}
QPushButton[primary="true"]:disabled {{ background: {t.fill}; color: {t.tertiary}; border-color: {t.separator}; }}
QPushButton[destructive="true"] {{ color: {t.danger}; }}
QPushButton[large="true"] {{ padding: 8px 24px; font-size: {ty['body'] + 1}px; }}
QPushButton[link="true"] {{
    background: transparent; border: none; color: {t.accent}; padding: 2px 0; text-align: left;
}}
QPushButton[link="true"]:hover {{ text-decoration: underline; }}
QPushButton[link="true"]:pressed {{ color: {t.accent_pressed}; }}
QPushButton[link="true"]:disabled {{ color: {t.tertiary}; }}
QToolButton {{
    background: transparent; border: none; border-radius: {RADIUS_CONTROL}px; padding: 5px;
}}
QToolButton:hover {{ background: {t.fill}; }}
QToolButton:pressed, QToolButton:checked {{ background: {t.control_pressed}; }}
QToolButton:focus {{ border: 2px solid {t.focus}; padding: 3px; }}
QToolButton:disabled {{ background: transparent; }}

/* ---- セグメントコントロール ---- */
#Segmented {{ background: {t.fill}; border-radius: 10px; }}
#Segmented QPushButton {{
    background: transparent; border: none; border-radius: 8px; padding: 4px 14px;
    color: {t.secondary}; min-height: 18px; font-weight: 600;
}}
#Segmented QPushButton:hover {{ background: {t.control_hover if not t.dark else t.control}; }}
#Segmented QPushButton:checked {{
    background: {t.surface if not t.dark else t.control_pressed}; color: {t.label};
    border: 1px solid {t.separator};
}}
#Segmented QPushButton:disabled {{ color: {t.tertiary}; background: transparent; }}

/* ---- 入力 ---- */
QLineEdit, QSpinBox, QComboBox {{
    background: {t.control}; border: 1px solid {t.border}; border-radius: {RADIUS_CONTROL}px;
    padding: 4px 8px; min-height: 20px; selection-background-color: {t.selection};
    selection-color: {t.label};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 2px solid {t.focus}; padding: 3px 7px; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {t.tertiary}; background: {t.fill}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url("{chevron}"); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px; padding: 4px;
    selection-background-color: {t.accent}; selection-color: {t.on_accent}; outline: 0;
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
QSlider::groove:horizontal {{ height: 4px; background: {t.fill}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {t.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: #FFFFFF; border: 1px solid {t.border}; width: 18px; height: 18px;
    margin: -8px 0; border-radius: 9px;
}}

/* ---- 進捗 ---- */
QProgressBar {{
    background: {t.fill}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px;
    text-align: center; color: transparent;
}}
QProgressBar::chunk {{
    background: {t.primary}; border-radius: 3px;
}}
QProgressBar[tone="warning"]::chunk {{ background: {t.warning}; }}
QProgressBar[tone="danger"]::chunk {{ background: {t.danger}; }}

/* ---- スクロールバー（細身） ---- */
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {t.tertiary}; border-radius: 3px; min-height: 32px; min-width: 32px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{ background: {t.secondary}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMenu {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px; padding: 4px;
}}
QMenu::item {{ padding: 5px 20px 5px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {t.accent}; color: {t.on_accent}; }}
QMenu::separator {{ height: 1px; background: {t.separator}; margin: 4px 8px; }}
"""


def apply(app: QApplication) -> Tokens:
    from ..config import paths
    from . import icons

    t = current()
    assets = icons.write_stylesheet_assets(paths.cache_dir() / "ui", t.secondary)
    app.setPalette(palette(t))
    app.setStyleSheet(stylesheet(t, assets["chevron"]))
    return t
