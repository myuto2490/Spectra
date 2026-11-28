"""ライブラリの論文一覧のモデル（表の列・並べ替え・絞り込み）。"""

from __future__ import annotations

import re
import time
import unicodedata
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QKeySequence

from ..library import Paper
from ..library.fulltext import TRANSLATED as TRANSLATED_PDF
from ..library.fulltext import make_snippet, query_terms
from . import icons, theme
from .widgets import plain_tooltip

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
FavoriteRole = Qt.ItemDataRole.UserRole + 3  # お気に入りかどうか（スターの列の描画に使う）
MatchRole = Qt.ItemDataRole.UserRole + 4  # 検索で書誌以外に一致したときの (見出し, 抜粋, 一致した範囲)。なければ None

# 検索で一致した場所（並べる順もこの順）
META, NOTE, BODY = "meta", "note", "body"
MAX_MATCHES = 3  # 詳細パネルに出す一致した箇所の数

FAVORITE_SHORTCUT = "Ctrl+D"  # 選んだ論文のお気に入りを切り替える（メニューの「ライブラリ」）


def favorite_action_text(on: bool) -> str:
    """お気に入りを切り替える操作の名前（ツールチップ・メニュー）。on は今お気に入りかどうか。"""
    return "お気に入りから外す" if on else "お気に入りに追加"


# 絞り込み（サイドバー）
ALL, RECENT, FAVORITES, TRANSLATED, UNTRANSLATED = "all", "recent", "favorites", "translated", "untranslated"
TRASH = "trash"
TRASH_DAYS = 30  # ゴミ箱に残す日数（core の purge_expired と同じ）
RECENT_DAYS = 7


_KATAKANA = {c: c - 0x60 for c in range(0x30A1, 0x30F7)}  # ァ..ヶ → ぁ..ゖ
_DOI_PREFIX = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/|^doi:", re.IGNORECASE)


def normalize_text(text: str) -> str:
    """全角の英数字・記号・空白を半角にそろえる（NFKC）。"""
    return unicodedata.normalize("NFKC", text)


def search_key(text: str) -> str:
    """検索で比べるための形：全角と半角、大文字と小文字、カタカナとひらがなを区別しない。"""
    return normalize_text(text).casefold().translate(_KATAKANA)


def _strip_doi_prefix(term: str) -> str:
    """DOI を URL のまま貼っても見つかるよう、「https://doi.org/」「doi:」を取り除く。"""
    return _DOI_PREFIX.sub("", term)


def _term_map(current: list[str], fetched: list[str]) -> list[set[int]]:
    """今の語ごとに、本文の結果のどの語で代わりに判定するか。

    同じ語ならその語。打鍵の途中（"aeroso" の結果で "aerosol" を探す、またはその逆）では、
    一方がもう一方を含む語を使う（届くまでの近似。次の結果で正しくなる）。
    """
    out = []
    for t in current:
        tk = t.casefold()
        out.append({j for j, f in enumerate(fetched) if f.casefold() in tk or tk in f.casefold()})
    return out


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
        if role == FavoriteRole:
            return p.favorite
        if role == Qt.ItemDataRole.DisplayRole:
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
            # ツールチップは HTML らしい文字列をリッチテキストとして描くので、外から来た文字列はエスケープする
            if key == "title":
                state = local_state(p, self._downloads.get(p.id))
                return plain_tooltip(f"{p.display_title}\n{state[1]}" if state else p.display_title)
            if key == "authors" and md.authors:
                return plain_tooltip(", ".join(a.full() for a in md.authors))
            if key == "favorite":
                shortcut = QKeySequence(FAVORITE_SHORTCUT).toString(QKeySequence.SequenceFormat.NativeText)
                return f"{favorite_action_text(p.favorite)}（{shortcut}）"
        if role == Qt.ItemDataRole.AccessibleTextRole:
            if key == "title":
                state = local_state(p, self._downloads.get(p.id))
                return f"{p.display_title}（{state[1]}）" if state else p.display_title
            if key == "favorite":
                return "お気に入り" if p.favorite else "お気に入りではない"
            if key == "status":
                return translation_status(p, self._jobs.get(p.id), self._percent.get(p.id))[0].lstrip("✓◐⟳…⚠ ")
        if role == Qt.ItemDataRole.AccessibleDescriptionRole and key == "favorite":
            return f"クリックで{favorite_action_text(p.favorite)}"
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
        self._text = ""  # 今の検索の文字列
        self._raw_terms: list[str] = []  # 書誌・メモと比べる前の語（NFKC）
        self._body_terms: list[str] = []  # 本文の索引に問い合わせた語
        self._fulltext: dict | None = None  # sha256 → fulltext.Match（最後に届いた本文の結果）
        self._fulltext_terms: list[str] = []  # その結果を問い合わせた語
        # 今の語 i → 本文の結果の語の番号。結果が今の語のものなら {i}。打鍵の途中では、前の語の結果を
        # 今の語の近似として使う（次の結果が届くまで、本文で一致した行が消えて行の高さが揺れないように）
        self._term_map: list[set[int]] = []
        self._comments: dict[int, str] = {}  # paper_id → 注釈のコメント
        self._hits: dict[int, tuple[str, int]] = {}  # paper_id → (一致した場所, 本文で一致したページ数)
        self._match_cache: dict[int, list[tuple[str, str, tuple]]] = {}
        self._user_sorted = False  # 検索中に、列の見出しで並べ替えを指定した

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
        raw = [t for t in normalize_text(text).split() if t]
        terms = [search_key(_strip_doi_prefix(t)) for t in raw]
        body_terms = query_terms(text)

        def change():
            if not terms:
                self._user_sorted = False
            self._text = text
            self._terms = terms
            self._raw_terms = raw
            self._body_terms = body_terms if len(body_terms) == len(terms) else []
            self._term_map = _term_map(self._body_terms, self._fulltext_terms)
            self._hits = {}
            self._match_cache = {}

        self._refilter(change)

    def set_fulltext(self, text: str, found: dict | None) -> None:
        """本文の問い合わせの結果（今の検索の文字列に対するものだけを使う。None なら本文は探さない）。"""
        if text != self._text:
            return

        def change():
            self._fulltext = found
            self._fulltext_terms = list(self._body_terms) if found is not None else []
            self._term_map = _term_map(self._body_terms, self._fulltext_terms)
            self._hits = {}
            self._match_cache = {}

        self._refilter(change)
        self._resort()

    def _resort(self) -> None:
        """関連度が変わったので並べ直す（絞り込みのやり直しだけでは、残った行の順が古いままになる）。"""
        if self.relevance_order() and self.sortColumn() >= 0:
            self.invalidate()

    def set_comments(self, comments: dict[int, str]) -> None:
        """注釈のコメント（ノートと合わせて「メモ」として探す）。"""
        if comments == self._comments:
            return

        def change():
            self._comments = dict(comments)
            self._hits = {}
            self._match_cache = {}

        if self._terms:
            self._refilter(change)
            self._resort()
        else:
            change()

    def has_search(self) -> bool:
        return bool(self._terms)

    def has_extra_matches(self) -> bool:
        """書誌以外（本文・メモ）だけで一致した論文が表示されているか。"""
        return any(kind != META for kind, _ in self._hits.values())

    def set_user_sorted(self, on: bool) -> None:
        if on != self._user_sorted:
            self._user_sorted = on
            self.invalidate()

    def relevance_order(self) -> bool:
        """検索中で、列の並べ替えを指定していない（書誌の一致 → 本文の一致の多い順に並べる）。"""
        return bool(self._terms) and not self._user_sorted

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        model: PaperTableModel = self.sourceModel()
        p = model._papers[row]
        if not self._in_scope(p, model):
            return False
        if not self._terms:
            return True
        self._match_cache.pop(p.id, None)
        md = p.metadata
        hay = search_key(
            " ".join(
                [
                    p.display_title,
                    p.file_name,
                    " ".join(a.full() for a in md.authors),
                    md.venue,
                    str(md.year or ""),
                    md.doi,
                    md.arxiv_id,
                ]
            )
        )
        in_meta = [t in hay for t in self._terms]
        if all(in_meta):
            self._hits[p.id] = (META, 0)
            return True
        memo = search_key(f"{p.note}\n{self._comments.get(p.id, '')}")
        in_note = [t in memo for t in self._terms]
        body = self._fulltext.get(p.sha256) if self._fulltext and self._body_terms else None
        in_body = [body is not None and bool(body.terms & self._term_map[i]) if i < len(self._term_map) else False
                   for i in range(len(self._terms))]
        if not all(a or b or c for a, b, c in zip(in_meta, in_note, in_body)):
            self._hits.pop(p.id, None)
            return False
        kind = NOTE if all(a or b for a, b in zip(in_meta, in_note)) else BODY
        self._hits[p.id] = (kind, body.page_count if body is not None else 0)
        return True

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        if self.relevance_order():
            a, b = self._rank(left.row()), self._rank(right.row())
            if a != b:
                # 降順の並べ替えでも、関連度の順はそのまま（降順では lessThan の答えが逆に使われる）
                return a < b if self.sortOrder() == Qt.SortOrder.AscendingOrder else a > b
        return super().lessThan(left, right)

    def _rank(self, source_row: int) -> tuple[int, int]:
        p = self.sourceModel()._papers[source_row]
        kind, pages = self._hits.get(p.id, (META, 0))
        return ((META, NOTE, BODY).index(kind), -pages)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if role == MatchRole:
            if not index.isValid() or COLUMNS[index.column()][0] != "title":
                return None
            pid = super().data(index, PaperIdRole)
            matches = self.matches(pid)
            return matches[0] if matches else None
        return super().data(index, role)

    def matches(self, paper_id: int) -> list[tuple[str, str, tuple]]:
        """書誌以外で一致した箇所の (見出し, 抜粋, 一致した範囲) の列（本文は最大 MAX_MATCHES 件）。"""
        hit = self._hits.get(paper_id)
        if hit is None or hit[0] == META:
            return []
        if paper_id in self._match_cache:
            return self._match_cache[paper_id]
        p = self.sourceModel().paper(paper_id)
        out: list[tuple[str, str, tuple]] = []
        if p is not None:
            pattern = re.compile("|".join(re.escape(t) for t in sorted(self._raw_terms, key=len, reverse=True)), re.IGNORECASE)
            if hit[0] == BODY and self._fulltext:
                m = self._fulltext.get(p.sha256)
                for snip in (m.snippets if m is not None else [])[:MAX_MATCHES]:
                    where = "訳文" if snip.kind == TRANSLATED_PDF else "原文"
                    out.append((f"本文に一致・{where} p.{snip.page + 1}", snip.text, snip.spans))
                if not out:
                    out.append(("本文に一致", "", ()))
            for memo in (p.note, self._comments.get(p.id, "")):
                if len(out) >= MAX_MATCHES:
                    break
                snip = make_snippet(" ".join(normalize_text(memo).split()), pattern) if memo else None
                if snip is not None:
                    out.append(("メモに一致", snip[0], snip[1]))
            if not out:
                out.append(("メモに一致", "", ()))
        self._match_cache[paper_id] = out
        return out

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
