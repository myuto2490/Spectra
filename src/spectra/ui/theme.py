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

# ブランドのグラデーション（アプリアイコンと同じ紫 → 青）
BRAND_START = "#6E5BFF"
BRAND_END = "#2F80FF"

# 角丸と余白（4 の倍数）
RADIUS_CONTROL = 8
RADIUS_CARD = 14
RADIUS_DROP = 22
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
    accent="#4A6CFF",
    accent_hover="#5B7BFF",
    accent_pressed="#3B5BEB",
    on_accent="#FFFFFF",
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
    accent="#7C8CFF",
    accent_hover="#8E9BFF",
    accent_pressed="#6A7AF0",
    on_accent="#FFFFFF",
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
QLabel[role="hero"] {{ font-size: 34px; font-weight: 800; letter-spacing: -0.5px; }}
QLabel[role="chip"] {{
    color: {t.secondary}; background: {t.surface}; border: 1px solid {t.separator};
    border-radius: 12px; padding: 4px 12px; font-size: {ty['caption']}px; font-weight: 600;
}}
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
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {BRAND_START}, stop:1 {BRAND_END});
    color: #FFFFFF; border: none; font-weight: 700; padding: 6px 18px;
}}
QPushButton[primary="true"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7D6CFF, stop:1 #4A90FF);
}}
QPushButton[primary="true"]:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #5E4BEF, stop:1 #2370EF);
}}
QPushButton[primary="true"]:focus {{ border: 2px solid {t.focus}; padding: 4px 16px; }}
QPushButton[primary="true"]:disabled {{ background: {t.fill}; color: {t.tertiary}; border-color: {t.fill}; }}
QPushButton[destructive="true"] {{ color: {t.danger}; }}
QPushButton[large="true"] {{ padding: 10px 28px; font-size: {ty['body'] + 2}px; border-radius: 12px; }}
QPushButton[link="true"] {{
    background: transparent; border: none; color: {t.accent}; padding: 2px 0; text-align: left;
}}
QPushButton[link="true"]:hover {{ text-decoration: underline; }}
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
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {BRAND_START}, stop:1 {BRAND_END}); border-radius: 3px;
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
