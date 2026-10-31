"""ライブラリの論文一覧のモデル（表の列・並べ替え・絞り込み）。"""

from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, QSortFilterProxyModel, Qt

from ..library import Paper
from . import icons, theme

PAPER_IDS_MIME = "application/x-spectra-paper-ids"

COLUMNS = [
    ("favorite", ""),
    ("title", "タイトル"),
    ("authors", "著者"),
    ("year", "年"),
    ("venue", "雑誌・会議"),
    ("status", "翻訳"),
    ("added", "追加日"),
]
COL = {key: i for i, (key, _) in enumerate(COLUMNS)}

_ROOT = QModelIndex()

PaperIdRole = Qt.ItemDataRole.UserRole + 1
SortRole = Qt.ItemDataRole.UserRole + 2

# 絞り込み（サイドバー）
ALL, RECENT, FAVORITES, TRANSLATED, UNTRANSLATED = "all", "recent", "favorites", "translated", "untranslated"
TRASH = "trash"
TRASH_DAYS = 30  # ゴミ箱に残す日数（core の purge_expired と同じ）
RECENT_DAYS = 7


def translation_status(paper: Paper, job: str | None = None, percent: int | None = None) -> tuple[str, str]:
    """(記号つきの表示文字列, 状態の種類)。状態は色だけでなく記号と文字でも示す。"""
    if job == "running":
        return (f"⟳ 翻訳中 {percent}%" if percent is not None else "⟳ 翻訳中"), "running"
    if job == "queued":
        return "… 待機中", "queued"
    if job == "failed":
        return "⚠ 失敗", "failed"
    t = paper.translation
    if t is None:
        return "未翻訳", "none"
    if t.complete:
        return "✓ 翻訳済み", "done"
    return "◐ 一部", "partial"


def is_translated(paper: Paper) -> bool:
    return paper.translation is not None


def local_state(paper: Paper, download=None) -> tuple[str, str] | None:
    """手元にない論文の (アイコン名, 説明)。手元にあれば None。download は SyncController.Download。"""
    if download is None and paper.has_original and (paper.translation is None or paper.has_translation_file):
        return None
    if download is not None:
        if download.state == "running":
            pct = download.percent
            return "cloud_download", f"取得中 {pct}%" if pct is not None else "取得中"
        if download.state == "queued":
            return "cloud_download", "取得の順番を待っています"
        if download.state == "waiting":
            return "hourglass", "ほかのパソコンからファイルが届くのを待っています"
        if download.state == "failed":
            return "cloud", "ファイルを取得できませんでした"
    if not paper.has_original:
        return "cloud", "このパソコンにまだありません"
    return "cloud", "訳文がこのパソコンにまだありません"


def local_short(paper: Paper, download=None) -> str:
    """詳細パネルの「このパソコン」の行に出す短い値（説明はツールチップに回す）。"""
    state = local_state(paper, download)
    if state is None:
        return "保存済み"
    if download is not None:
        if download.state == "running":
            pct = download.percent
            return f"取得中 {pct}%" if pct is not None else "取得中"
        return {"queued": "取得待ち", "waiting": "到着待ち", "failed": "取得できませんでした"}[download.state]
    return "未取得"


def days_left(paper: Paper, now: float | None = None) -> int:
    """ゴミ箱の論文が完全に削除されるまでの日数。"""
    if paper.trashed_at is None:
        return TRASH_DAYS
    left = TRASH_DAYS - (int(now or time.time()) - int(paper.trashed_at)) / 86400
    return max(0, int(left + 0.999))


_BLANK = None


def _blank_icon():
    global _BLANK
    if _BLANK is None:
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QPixmap

        _BLANK = QPixmap(28, 28)
        _BLANK.fill(_Qt.GlobalColor.transparent)
        _BLANK.setDevicePixelRatio(2.0)
    return _BLANK


def format_date(ts: float | None) -> str:
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts)
    return d.strftime("%Y/%m/%d")


class PaperTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._papers: list[Paper] = []
        self._row: dict[int, int] = {}
        self._jobs: dict[int, str] = {}  # paper_id → running / queued / failed
        self._percent: dict[int, int] = {}
        self._looking_up: set[int] = set()
        self._downloads: dict[int, object] = {}  # paper_id → SyncController.Download
        self.trash_mode = False  # ゴミ箱を表示中（「追加日」の列を「削除した日」にする）
        self._has_missing = False  # 手元にない論文が 1 本でもあるか（あれば、記号の幅をすべての行で空ける）

    # -- データの更新 ------------------------------------------------------

    def set_papers(self, papers: list[Paper]) -> None:
        self.beginResetModel()
        self._papers = list(papers)
        self._reindex()
        self.endResetModel()

    def _reindex(self) -> None:
        self._row = {p.id: i for i, p in enumerate(self._papers)}
        self._has_missing = any(local_state(p) is not None for p in self._papers)

    def upsert(self, paper: Paper) -> None:
        row = self._row.get(paper.id)
        if row is None:
            self.beginInsertRows(QModelIndex(), 0, 0)
            self._papers.insert(0, paper)
            self._reindex()
            self.endInsertRows()
        else:
            self._papers[row] = paper
            missing = any(local_state(p) is not None for p in self._papers)
            if missing != self._has_missing:
                self._has_missing = missing
                self.refresh_icons()
            self._row_changed(row)

    def remove(self, paper_id: int) -> None:
        row = self._row.get(paper_id)
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._papers[row]
        self._reindex()
        self.endRemoveRows()

    def paper(self, paper_id: int) -> Paper | None:
        row = self._row.get(paper_id)
        return self._papers[row] if row is not None else None

    def papers(self) -> list[Paper]:
        return list(self._papers)

    def set_job(self, paper_id: int, state: str | None, percent: int | None = None) -> None:
        if state is None:
            self._jobs.pop(paper_id, None)
        else:
            self._jobs[paper_id] = state
        if percent is None:
            self._percent.pop(paper_id, None)
        else:
            self._percent[paper_id] = percent
        row = self._row.get(paper_id)
        if row is not None:
            self._row_changed(row)

    def set_download(self, paper_id: int, download) -> None:
        if download is None:
            self._downloads.pop(paper_id, None)
        else:
            self._downloads[paper_id] = download
        row = self._row.get(paper_id)
        if row is not None:
            self._row_changed(row)

    def set_trash_mode(self, on: bool) -> None:
        if on != self.trash_mode:
            self.trash_mode = on
            self.headerDataChanged.emit(Qt.Orientation.Horizontal, COL["added"], COL["added"])
            if self._papers:
                self.dataChanged.emit(self.index(0, COL["added"]), self.index(len(self._papers) - 1, COL["added"]))

    def refresh_icons(self) -> None:
        """テーマが変わったら、記号の色を描き直す。"""
        if self._papers:
            self.dataChanged.emit(self.index(0, COL["title"]), self.index(len(self._papers) - 1, COL["title"]))

    def job(self, paper_id: int) -> str | None:
        return self._jobs.get(paper_id)

    def set_looking_up(self, ids: set[int]) -> None:
        changed = ids ^ self._looking_up
        self._looking_up = set(ids)
        for pid in changed:
            row = self._row.get(pid)
            if row is not None:
                self._row_changed(row)

    def _row_changed(self, row: int) -> None:
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    # -- Qt モデル -------------------------------------------------------

    def rowCount(self, parent=_ROOT) -> int:
        return 0 if parent.isValid() else len(self._papers)

    def columnCount(self, parent=_ROOT) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                if COLUMNS[section][0] == "added" and self.trash_mode:
                    return "削除した日"
                return COLUMNS[section][1]
            if role == Qt.ItemDataRole.ToolTipRole and COLUMNS[section][0] == "favorite":
                return "お気に入り"
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        p = self._papers[index.row()]
        key = COLUMNS[index.column()][0]
        md = p.metadata
        if role == PaperIdRole:
            return p.id
        if role == Qt.ItemDataRole.DisplayRole:
            if key == "favorite":
                return "★" if p.favorite else ""
            if key == "title":
                return p.display_title
            if key == "authors":
                if p.id in self._looking_up and not md.authors:
                    return "情報を取得中…"
                return p.authors_short
            if key == "year":
                return str(md.year) if md.year else ""
            if key == "venue":
                return md.venue
            if key == "status":
                return translation_status(p, self._jobs.get(p.id), self._percent.get(p.id))[0]
            if key == "added":
                return format_date(p.trashed_at if p.trashed_at is not None else p.added_at)
        if role == Qt.ItemDataRole.DecorationRole and key == "title":
            state = local_state(p, self._downloads.get(p.id))
            if state is None:
                # 記号のない行も同じ幅を空けて、タイトルの書き出しをそろえる
                return _blank_icon() if self._downloads or self._has_missing else None
            return icons.pixmap(state[0], theme.current().secondary, 14)
        if role == SortRole:
            if key == "favorite":
                return int(p.favorite)
            if key == "title":
                return p.display_title.casefold()
            if key == "authors":
                return p.authors_short.casefold()
            if key == "year":
                return md.year or 0
            if key == "venue":
                return md.venue.casefold()
            if key == "status":
                order = {"running": 0, "queued": 1, "failed": 2, "done": 3, "partial": 4, "none": 5}
                return order[translation_status(p, self._jobs.get(p.id))[1]]
            if key == "added":
                return p.trashed_at if p.trashed_at is not None else p.added_at
        if role == Qt.ItemDataRole.ToolTipRole:
            if key == "title":
                state = local_state(p, self._downloads.get(p.id))
                return f"{p.display_title}\n{state[1]}" if state else p.display_title
            if key == "authors" and md.authors:
                return ", ".join(a.full() for a in md.authors)
            if key == "favorite":
                return "お気に入り" if p.favorite else ""
        if role == Qt.ItemDataRole.AccessibleTextRole:
            if key == "title":
                state = local_state(p, self._downloads.get(p.id))
                return f"{p.display_title}（{state[1]}）" if state else p.display_title
            if key == "favorite":
                return "お気に入り" if p.favorite else "お気に入りではない"
            if key == "status":
                return translation_status(p, self._jobs.get(p.id), self._percent.get(p.id))[0].lstrip("✓◐⟳…⚠ ")
        if role == Qt.ItemDataRole.TextAlignmentRole and key in ("favorite", "year"):
            return int(Qt.AlignmentFlag.AlignCenter)
        return None

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        if index.isValid():
            base |= Qt.ItemFlag.ItemIsDragEnabled
        return base

    # -- コレクションへのドラッグ ----------------------------------------------

    def mimeTypes(self) -> list[str]:
        return [PAPER_IDS_MIME]

    def mimeData(self, indexes) -> QMimeData:
        ids = sorted({self._papers[i.row()].id for i in indexes if i.isValid()})
        md = QMimeData()
        md.setData(PAPER_IDS_MIME, ",".join(map(str, ids)).encode())
        return md

    def supportedDragActions(self):
        return Qt.DropAction.CopyAction


class PaperFilterProxy(QSortFilterProxyModel):
    """サイドバーの絞り込みと検索語で行を選び、列ごとに並べ替える。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SortRole)
        self.setDynamicSortFilter(True)
        self._scope: str = ALL
        self._collection: int | None = None
        self._terms: list[str] = []

    def _refilter(self, change) -> None:
        # Qt 6.10 で invalidateFilter() が非推奨になり、変更の前後を begin/endFilterChange で囲む形になった
        if hasattr(self, "beginFilterChange"):
            self.beginFilterChange()
            change()
            self.endFilterChange()
        else:
            change()
            self.invalidateFilter()

    def set_scope(self, scope: str, collection: int | None = None) -> None:
        def change():
            self._scope, self._collection = scope, collection

        self._refilter(change)

    def scope(self) -> tuple[str, int | None]:
        return self._scope, self._collection

    def set_search(self, text: str) -> None:
        terms = [t.casefold() for t in text.split() if t]

        def change():
            self._terms = terms

        self._refilter(change)

    def has_search(self) -> bool:
        return bool(self._terms)

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        model: PaperTableModel = self.sourceModel()
        p = model._papers[row]
        if not self._in_scope(p, model):
            return False
        if self._terms:
            md = p.metadata
            hay = " ".join(
                [
                    p.display_title,
                    p.file_name,
                    " ".join(a.full() for a in md.authors),
                    md.venue,
                    str(md.year or ""),
                    md.doi,
                    md.arxiv_id,
                ]
            ).casefold()
            return all(t in hay for t in self._terms)
        return True

    def _in_scope(self, p: Paper, model: PaperTableModel) -> bool:
        s = self._scope
        # ゴミ箱の論文は、ゴミ箱でだけ表示する
        if s == TRASH:
            return p.trashed_at is not None
        if p.trashed_at is not None:
            return False
        if s == RECENT:
            return p.added_at >= time.time() - RECENT_DAYS * 86400
        if s == FAVORITES:
            return p.favorite
        if s == TRANSLATED:
            return is_translated(p)
        if s == UNTRANSLATED:
            return not is_translated(p)
        if s == "collection":
            return self._collection in p.collection_ids
        return True
