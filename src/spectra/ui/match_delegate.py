"""論文の一覧のタイトルの列：検索で本文・メモに一致した論文は、タイトルの下に一致した箇所の抜粋を出す。"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from . import theme
from .library_model import MatchRole

ROW_HEIGHT = 30
MATCH_ROW_HEIGHT = 48  # 抜粋を出す行があるときの行の高さ


def _segments(text: str, spans: tuple[tuple[int, int], ...]) -> list[tuple[str, bool]]:
    """抜粋を (文字列, 一致した部分か) の列に分ける。"""
    out: list[tuple[str, bool]] = []
    pos = 0
    for a, b in sorted(spans):
        a, b = max(a, pos), min(b, len(text))
        if a >= b:
            continue
        if a > pos:
            out.append((text[pos:a], False))
        out.append((text[a:b], True))
        pos = b
    if pos < len(text):
        out.append((text[pos:], False))
    return out


LEAD = 8  # 一覧では、最初の一致の何文字前から出すか（列が狭くても一致した語が見えるように）


def _near_first_hit(text: str, spans: tuple[tuple[int, int], ...]) -> tuple[str, tuple[tuple[int, int], ...]]:
    """抜粋を、最初の一致の少し前から始める。"""
    if not spans:
        return text, spans
    first = min(a for a, _ in spans)
    if first <= LEAD + 1:
        return text, spans
    cut = first - LEAD
    shift = cut - 1  # 先頭に「…」を付ける
    return "…" + text[cut:], tuple((a - shift, b - shift) for a, b in spans if a >= cut)


class MatchDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        match = index.data(MatchRole)
        if not match or option.rect.height() < MATCH_ROW_HEIGHT - 4:
            super().paint(painter, option, index)
            return
        heading, text, spans = match
        heading = heading.split("・")[0]  # 一覧では「本文に一致」「メモに一致」だけ（場所は詳細パネルに出す）
        text, spans = _near_first_hit(text, spans)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget is not None else None
        if style is not None:
            style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, widget)
        # 1 行目：タイトル（記号・省略は既定の描き方のまま）
        full = QRect(option.rect)
        top = QStyleOptionViewItem(option)
        top.rect = QRect(full.x(), full.y() + 3, full.width(), full.height() // 2)
        super().paint(painter, top, index)
        # 2 行目：見出しと抜粋
        top_opt = QStyleOptionViewItem(top)
        self.initStyleOption(top_opt, index)
        text_left = full.x() + 6
        if style is not None:
            text_left = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, top_opt, widget).left() + 3
        line = QRect(text_left, full.y() + full.height() // 2 + 1, full.right() - text_left - 6, full.height() // 2 - 4)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        t = theme.current()
        if selected:
            head_color = body_color = hit_color = opt.palette.color(QPalette.ColorRole.HighlightedText)
        else:
            head_color, body_color, hit_color = QColor(t.accent), QColor(t.secondary), QColor(t.label)
        small = QFont(opt.font)
        small.setPointSizeF(max(8.0, opt.font.pointSizeF() - 1.5))
        bold = QFont(small)
        bold.setBold(True)
        painter.save()
        painter.setClipRect(line)
        x, right = line.left(), line.right()
        baseline = line.top() + (line.height() + QFontMetrics(small).ascent() - QFontMetrics(small).descent()) // 2
        pieces = [(heading, small, head_color)]
        if text:
            pieces.append(("　", small, body_color))
            pieces += [(seg, bold if hit else small, hit_color if hit else body_color) for seg, hit in _segments(text, spans)]
        for piece, font, color in pieces:
            fm = QFontMetrics(font)
            width = fm.horizontalAdvance(piece)
            if x + width > right:
                piece = fm.elidedText(piece, Qt.TextElideMode.ElideRight, max(0, right - x))
                width = fm.horizontalAdvance(piece)
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(x, baseline, piece)
            x += width
            if x >= right:
                break
        painter.restore()
