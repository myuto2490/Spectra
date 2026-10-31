"""ライブラリ画面：サイドバー（絞り込み・コレクション）・論文の一覧・詳細パネルの 3 列。"""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QSize, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..library import Library, Paper
from . import icons, theme
from .library_model import (
    ALL,
    COL,
    FAVORITES,
    PAPER_IDS_MIME,
    RECENT,
    TRANSLATED,
    TRASH,
    TRASH_DAYS,
    UNTRANSLATED,
    PaperFilterProxy,
    PaperIdRole,
    PaperTableModel,
)
from .paper_detail import PaperDetail
from .widgets import button, label, link_button, plain_box, set_prop

ScopeRole = Qt.ItemDataRole.UserRole + 10
CollectionRole = Qt.ItemDataRole.UserRole + 11

SCOPES = [
    (ALL, "すべての論文", "books"),
    (RECENT, "最近追加した論文", "clock"),
    (FAVORITES, "お気に入り", "star"),
    (TRANSLATED, "翻訳済み", "check"),
    (UNTRANSLATED, "未翻訳", "doc"),
]


class Sidebar(QListWidget):
    """絞り込みとコレクションの一覧。論文をコレクションへドラッグして加えられる。"""

    scopeChanged = Signal(str, object)  # (scope, collection_id)
    papersDropped = Signal(int, list)  # (collection_id, 論文 ID)
    renameRequested = Signal(int)
    deleteRequested = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(220)
        self.setIconSize(QSize(16, 16))
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setAccessibleName("ライブラリの絞り込み")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.currentItemChanged.connect(self._on_current)
        self._collections: list[tuple[int, str]] = []
        self._trash_count = 0
        self.rebuild([])

    def rebuild(self, collections: list[tuple[int, str]]) -> None:
        current = self.current_scope()
        self._collections = collections
        self.blockSignals(True)
        self.clear()
        self._header("ライブラリ")
        t = theme.current()
        for scope, text, icon in SCOPES:
            it = QListWidgetItem(icons.icon(icon, t.secondary, 16), text)
            it.setData(ScopeRole, scope)
            self.addItem(it)
        # ライブラリの項目の一番下にゴミ箱（件数があれば「ゴミ箱 3」）
        self.trash_item = QListWidgetItem(icons.icon("trash", t.secondary, 16), "")
        self.trash_item.setData(ScopeRole, TRASH)
        self.addItem(self.trash_item)
        self.set_trash_count(self._trash_count)
        self._header("コレクション")
        for cid, name in collections:
            it = QListWidgetItem(icons.icon("folder", t.secondary, 16), name)
            it.setData(ScopeRole, "collection")
            it.setData(CollectionRole, cid)
            self.addItem(it)
        self.blockSignals(False)
        if not self.select_scope(*current):
            self.select_scope(ALL)

    def set_trash_count(self, n: int) -> None:
        self._trash_count = n
        self.trash_item.setText(f"ゴミ箱 {n}" if n else "ゴミ箱")
        self.trash_item.setData(Qt.ItemDataRole.AccessibleTextRole, f"ゴミ箱、{n} 件" if n else "ゴミ箱、空")

    def _header(self, text: str) -> None:
        it = QListWidgetItem(text)
        it.setFlags(Qt.ItemFlag.NoItemFlags)
        f = it.font()
        f.setBold(True)
        f.setPixelSize(theme.TYPE["caption"])
        it.setFont(f)
        it.setSizeHint(QSize(0, 30))
        self.addItem(it)

    def current_scope(self) -> tuple[str, int | None]:
        it = self.currentItem()
        if it is None or it.data(ScopeRole) is None:
            return ALL, None
        return it.data(ScopeRole), it.data(CollectionRole)

    def select_scope(self, scope: str, collection: int | None = None) -> bool:
        for i in range(self.count()):
            it = self.item(i)
            if it.data(ScopeRole) == scope and (scope != "collection" or it.data(CollectionRole) == collection):
                self.setCurrentItem(it)
                return True
        return False

    def _on_current(self, item: QListWidgetItem | None, _prev) -> None:
        if item is not None and item.data(ScopeRole) is not None:
            self.scopeChanged.emit(item.data(ScopeRole), item.data(CollectionRole))

    def refresh_theme(self) -> None:
        self.rebuild(self._collections)

    # -- コレクションへのドロップ ------------------------------------------------

    def _collection_at(self, pos) -> int | None:
        it = self.itemAt(pos)
        if it is not None and it.data(ScopeRole) == "collection":
            return it.data(CollectionRole)
        return None

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(PAPER_IDS_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(PAPER_IDS_MIME) and self._collection_at(event.position().toPoint()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        cid = self._collection_at(event.position().toPoint())
        data = bytes(event.mimeData().data(PAPER_IDS_MIME)).decode()
        ids = [int(x) for x in data.split(",") if x]
        if cid is not None and ids:
            event.acceptProposedAction()
            self.papersDropped.emit(cid, ids)
        else:
            event.ignore()

    def _context_menu(self, pos) -> None:
        cid = self._collection_at(pos)
        if cid is None:
            return
        menu = QMenu(self)
        menu.addAction("名前を変更…", lambda: self.renameRequested.emit(cid))
        menu.addAction("コレクションを削除…", lambda: self.deleteRequested.emit(cid))
        menu.exec(self.viewport().mapToGlobal(pos))


class LibraryView(QWidget):
    """論文の一覧。開く・翻訳・削除などの操作はシグナルでメインウィンドウに任せる。"""

    openRequested = Signal(int)
    selectionChanged = Signal()
    searchReset = Signal()  # 検索欄を空にしてほしい
    addRequested = Signal()  # 論文を追加するダイアログを開いてほしい
    syncSetupRequested = Signal()  # 同期の設定を開いてほしい
    syncStatusClicked = Signal()  # サイドバーの同期の状態の行が押された
    emptyTrashRequested = Signal()
    settingsRequested = Signal()  # 設定（API キー）を開いてほしい

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.model = PaperTableModel(self)
        self.proxy = PaperFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        side = QWidget()
        side.setObjectName("SidebarPane")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        self.sidebar = Sidebar()
        self.sidebar.scopeChanged.connect(self._on_scope)
        self.sidebar.papersDropped.connect(self._add_to_collection)
        self.sidebar.renameRequested.connect(self.rename_collection)
        self.sidebar.deleteRequested.connect(self.delete_collection)
        sl.addWidget(self.sidebar, 1)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(8, 0, 8, 0)
        self.sync_status = QPushButton()
        self.sync_status.setObjectName("SyncStatus")
        self.sync_status.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sync_status.setIconSize(QSize(16, 16))
        self.sync_status.clicked.connect(self.syncStatusClicked)
        self.sync_status.hide()
        status_row.addWidget(self.sync_status, 1)
        sl.addLayout(status_row)
        new_row = QHBoxLayout()
        new_row.setContentsMargins(12, 6, 12, 10)
        self.new_collection_btn = link_button("＋ 新しいコレクション")
        self.new_collection_btn.clicked.connect(self.create_collection)
        new_row.addWidget(self.new_collection_btn)
        new_row.addStretch(1)
        sl.addLayout(new_row)
        self.side = side
        lay.addWidget(side)

        self.table = QTableView()
        self.table.setObjectName("PaperTable")
        self.table.setModel(self.proxy)
        self.table.setAccessibleName("論文の一覧")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL["added"], Qt.SortOrder.DescendingOrder)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setSectionsMovable(False)
        hh.setStretchLastSection(False)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        widths = {"favorite": 32, "authors": 140, "year": 52, "venue": 130, "status": 104, "added": 92}
        for key, col in COL.items():
            if key == "title":
                hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
            else:
                hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
                self.table.setColumnWidth(col, widths[key])
        hh.setSectionResizeMode(COL["favorite"], QHeaderView.ResizeMode.Fixed)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.clicked.connect(self._on_click)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.selectionChanged.emit())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        open_act = QAction(self.table)
        open_act.setShortcuts([QKeySequence(Qt.Key.Key_Return), QKeySequence(Qt.Key.Key_Enter)])
        open_act.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        open_act.triggered.connect(self._open_current)
        self.table.addAction(open_act)

        # 絞り込みの結果が 0 件のとき
        self.no_match = QWidget()
        nl = QVBoxLayout(self.no_match)
        nl.addStretch(1)
        self.no_match_text = label("", "secondary")
        self.no_match_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nl.addWidget(self.no_match_text)
        self.clear_search_btn = link_button("検索を解除")
        self.clear_search_btn.clicked.connect(self.searchReset)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.clear_search_btn)
        row.addStretch(1)
        nl.addLayout(row)
        nl.addStretch(2)

        # ライブラリに論文が 1 本もないとき（初めて起動したときなど）
        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.setSpacing(8)
        el.addStretch(1)
        self.empty_icon = QLabel()
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(self.empty_icon)
        el.addSpacing(4)
        empty_title = label("ライブラリに論文がありません", "title")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(empty_title)
        empty_hint = label(
            "論文の PDF をこのウィンドウにドロップするか、「論文を追加」から選んでください。\n"
            "複数のファイルやフォルダもまとめて追加できます。",
            "secondary",
        )
        empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(empty_hint)
        el.addSpacing(8)
        add_row = QHBoxLayout()
        add_row.addStretch(1)
        self.empty_add_btn = button("論文を追加…")
        self.empty_add_btn.clicked.connect(self.addRequested)
        add_row.addWidget(self.empty_add_btn)
        add_row.addStretch(1)
        el.addLayout(add_row)
        sync_row = QHBoxLayout()
        sync_row.addStretch(1)
        self.empty_sync_btn = link_button("別のパソコンのライブラリを同期…")
        self.empty_sync_btn.clicked.connect(self.syncSetupRequested)
        sync_row.addWidget(self.empty_sync_btn)
        sync_row.addStretch(1)
        el.addLayout(sync_row)
        el.addSpacing(8)
        notice_row = QHBoxLayout()
        notice_row.addStretch(1)
        self.engine_notice = label("", "caption", tone="warning")
        self.engine_settings_btn = link_button("API キーを設定…")
        self.engine_settings_btn.clicked.connect(self.settingsRequested)
        notice_row.addWidget(self.engine_notice)
        notice_row.addWidget(self.engine_settings_btn)
        notice_row.addStretch(1)
        el.addLayout(notice_row)
        el.addStretch(2)
        self.set_engine_notice("")

        self.trash_bar = QWidget()
        tb = QHBoxLayout(self.trash_bar)
        tb.setContentsMargins(16, 8, 16, 8)
        tb.addWidget(label(f"ゴミ箱の論文は、{TRASH_DAYS} 日たつと完全に削除されます。", "caption"), 1)
        self.empty_trash_btn = button("ゴミ箱を空にする…", destructive=True)
        self.empty_trash_btn.clicked.connect(self.emptyTrashRequested)
        tb.addWidget(self.empty_trash_btn)
        self.trash_bar.hide()

        self.center = QStackedWidget()
        self.center.setObjectName("LibraryCenter")
        self.center.addWidget(self.table)
        self.center.addWidget(self.no_match)
        self.center.addWidget(self.empty)
        middle = QWidget()
        ml = QVBoxLayout(middle)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        ml.addWidget(self.trash_bar)
        ml.addWidget(self.center, 1)
        lay.addWidget(middle, 1)

        self.detail = PaperDetail()
        lay.addWidget(self.detail)

        for sig in (
            self.proxy.rowsInserted,
            self.proxy.rowsRemoved,
            self.proxy.modelReset,
            self.proxy.layoutChanged,
            self.model.rowsInserted,
            self.model.rowsRemoved,
            self.model.modelReset,
        ):
            sig.connect(self._update_empty)
        self.refresh_theme()
        self._update_empty()

    # -- データ ------------------------------------------------------------

    def reload(self) -> None:
        selected = self.selected_ids()
        trashed = self.library.trashed_papers()
        self.model.set_papers(self.library.papers() + trashed)
        self.sidebar.rebuild([(c.id, c.name) for c in self.library.collections()])
        self.sidebar.set_trash_count(len(trashed))
        self.select_ids(selected)
        self._update_empty()

    def refresh_paper(self, paper_id: int) -> None:
        p = self.library.get(paper_id)
        if p is None:
            self.model.remove(paper_id)
        else:
            self.model.upsert(p)
        self.sidebar.set_trash_count(sum(1 for q in self.model.papers() if q.trashed_at is not None))
        self._update_empty()
        self.selectionChanged.emit()

    def paper_count(self) -> int:
        return self.model.rowCount()

    # -- 選択 ------------------------------------------------------------

    def selected_ids(self) -> list[int]:
        rows = self.table.selectionModel().selectedRows()
        return [self.proxy.data(r, PaperIdRole) for r in sorted(rows, key=lambda r: r.row())]

    def selected_papers(self) -> list[Paper]:
        return [p for p in (self.model.paper(i) for i in self.selected_ids()) if p is not None]

    def select_ids(self, ids: list[int]) -> None:
        sm = self.table.selectionModel()
        sm.clearSelection()
        first: QModelIndex | None = None
        for pid in ids:
            for row in range(self.proxy.rowCount()):
                idx = self.proxy.index(row, 0)
                if idx.data(PaperIdRole) == pid:
                    sm.select(idx, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
                    first = first or idx
                    break
        if first is not None:
            self.table.scrollTo(first)
            sm.setCurrentIndex(first, QItemSelectionModel.SelectionFlag.NoUpdate)

    def reveal(self, paper_id: int) -> None:
        """絞り込みや検索に隠れていても、その論文を表示して選択する。"""
        if not any(self.proxy.index(r, 0).data(PaperIdRole) == paper_id for r in range(self.proxy.rowCount())):
            self.proxy.set_search("")
            self.sidebar.select_scope(ALL)
            self.searchReset.emit()
        self.select_ids([paper_id])

    def _on_double_click(self, index: QModelIndex) -> None:
        if index.isValid() and index.column() != COL["favorite"]:
            self.openRequested.emit(index.data(PaperIdRole))

    def _open_current(self) -> None:
        ids = self.selected_ids()
        if len(ids) == 1:
            self.openRequested.emit(ids[0])

    def _on_click(self, index: QModelIndex) -> None:
        if index.isValid() and index.column() == COL["favorite"]:
            self.toggle_favorite([index.data(PaperIdRole)])

    def toggle_favorite(self, ids: list[int] | None = None) -> None:
        ids = ids if ids is not None else self.selected_ids()
        papers = [p for p in (self.model.paper(i) for i in ids) if p is not None]
        if not papers:
            return
        on = not all(p.favorite for p in papers)
        for p in papers:
            self.library.set_favorite(p.id, on)
            self.refresh_paper(p.id)

    # -- 絞り込み ----------------------------------------------------------

    def _on_scope(self, scope: str, collection: int | None) -> None:
        self.model.set_trash_mode(scope == TRASH)
        self.proxy.set_scope(scope, collection)
        self._update_empty()
        self.selectionChanged.emit()

    def in_trash(self) -> bool:
        return self.proxy.scope()[0] == TRASH

    def active_count(self) -> int:
        """ゴミ箱に入っていない論文の数。"""
        return sum(1 for p in self.model.papers() if p.trashed_at is None)

    def set_search(self, text: str) -> None:
        self.proxy.set_search(text)
        self._update_empty()

    def _update_empty(self, *_args) -> None:
        trash = self.in_trash()
        self.trash_bar.setVisible(trash)
        self.empty_trash_btn.setEnabled(trash and self.proxy.rowCount() > 0)
        if not trash and self.active_count() == 0:
            self.center.setCurrentWidget(self.empty)
            return
        empty = self.proxy.rowCount() == 0
        if empty:
            if self.proxy.has_search():
                self.no_match_text.setText("一致する論文はありません")
            elif trash:
                self.no_match_text.setText("ゴミ箱は空です")
            else:
                scope, _ = self.proxy.scope()
                self.no_match_text.setText(
                    "論文の行をここにドラッグすると、コレクションに加えられます" if scope == "collection" else "該当する論文はありません"
                )
        self.clear_search_btn.setVisible(empty and self.proxy.has_search())
        self.center.setCurrentWidget(self.no_match if empty else self.table)

    # -- コレクション --------------------------------------------------------

    def create_collection(self) -> None:
        name, ok = QInputDialog.getText(self, "新しいコレクション", "コレクションの名前:")
        name = name.strip()
        if ok and name:
            c = self.library.create_collection(name)
            ids = self.selected_ids()
            self.reload()
            self.sidebar.select_scope("collection", c.id)
            if ids:
                self.select_ids(ids)

    def rename_collection(self, cid: int) -> None:
        current = next((c.name for c in self.library.collections() if c.id == cid), "")
        name, ok = QInputDialog.getText(self, "名前を変更", "コレクションの名前:", text=current)
        name = name.strip()
        if ok and name and name != current:
            self.library.rename_collection(cid, name)
            self.reload()

    def delete_collection(self, cid: int) -> None:
        name = next((c.name for c in self.library.collections() if c.id == cid), "")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"コレクション「{name}」を削除しますか？")
        box.setInformativeText("コレクションの中の論文は、ライブラリに残ります。")
        delete = box.addButton("削除", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        plain_box(box).exec()
        if box.clickedButton() is delete:
            self.library.delete_collection(cid)
            self.reload()

    def _add_to_collection(self, cid: int, ids: list[int]) -> None:
        self.library.add_to_collection(cid, ids)
        for pid in ids:
            self.refresh_paper(pid)

    def remove_from_current_collection(self, ids: list[int]) -> None:
        scope, cid = self.proxy.scope()
        if scope == "collection" and cid is not None:
            self.library.remove_from_collection(cid, ids)
            for pid in ids:
                self.refresh_paper(pid)

    def collection_menu(self, ids: list[int]) -> QMenu:
        """「コレクションに追加」のサブメニュー。"""
        menu = QMenu("コレクションに追加", self)
        cols = self.library.collections()
        for c in cols:
            menu.addAction(c.name, lambda cid=c.id: self._add_to_collection(cid, ids))
        if cols:
            menu.addSeparator()
        menu.addAction("新しいコレクション…", self.create_collection)
        return menu

    def set_engine_notice(self, text: str) -> None:
        """空の状態に出す API キーの案内（空文字なら隠す）。"""
        self.engine_notice.setText(text)
        self.engine_notice.setVisible(bool(text))
        self.engine_settings_btn.setVisible(bool(text))

    def set_drag_active(self, active: bool) -> None:
        """PDF をウィンドウの上にドラッグしている間、一覧を枠で強調する。"""
        set_prop(self.center, "dragActive", active)

    def set_sync_status(self, icon: str, text: str, warning: bool = False, accessible: str = "") -> None:
        """サイドバーの下の同期の状態の行（text が空なら隠す。同期がオフのとき）。"""
        self._sync_icon = (icon, warning)
        self.sync_status.setText(text)
        self.sync_status.setVisible(bool(text))
        self.sync_status.setAccessibleName(accessible or f"同期の状態: {text}")
        set_prop(self.sync_status, "tone", "warning" if warning else None)
        self._apply_sync_icon()

    def _apply_sync_icon(self) -> None:
        icon, warning = getattr(self, "_sync_icon", ("", False))
        if icon:
            t = theme.current()
            self.sync_status.setIcon(icons.icon(icon, t.warning if warning else t.secondary, 16))

    def refresh_theme(self) -> None:
        self.sidebar.refresh_theme()
        self.empty_icon.setPixmap(icons.pixmap("books", theme.current().tertiary, 44))
        self._apply_sync_icon()
        self.model.refresh_icons()
