"""ライブラリの詳細パネル（「論文の詳細」と「AI翻訳の詳細」のタブ）と、書誌情報の編集ダイアログ。"""

from __future__ import annotations

import html
from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..library import Author, Metadata, Paper
from .library_model import days_left, format_date, local_state, translation_status
from .widgets import FormGroup, button, label, link_button, set_prop


def _selectable(lb: QLabel) -> QLabel:
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lb


def _value(text: str = "") -> QLabel:
    lb = QLabel(text)
    lb.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    lb.setProperty("role", "secondary")
    return lb


def _scroll_page() -> tuple[QScrollArea, QVBoxLayout]:
    """タブの中身（縦にスクロールできる）。"""
    area = QScrollArea()
    area.setObjectName("Inspector")
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    body = QWidget()
    body.setObjectName("Inspector")
    lay = QVBoxLayout(body)
    lay.setContentsMargins(18, 16, 18, 20)
    lay.setSpacing(14)
    area.setWidget(body)
    return area, lay


def _breakable(text: str) -> str:
    """DOI など空白のない長い文字列を、/ と . の後で折り返せるようにする（ゼロ幅スペース）。"""
    return text.replace("/", "/\u200b").replace(".", ".\u200b")


META_SOURCES = {
    "crossref": "Crossref から取得した情報",
    "arxiv": "arXiv から取得した情報",
    "manual": "手で編集した情報",
    "embedded": "PDF に埋め込まれた情報",
    "heuristic": "PDF から読み取った情報",
}

FORMALITY_NAMES = {"default": "である調", "prefer_less": "である調", "prefer_more": "です・ます調"}

PAPER_TAB, TRANSLATION_TAB, OTHER_PAGE, TRASH_PAGE = 0, 1, 2, 3


class _InfoGrid(QWidget):
    """「項目名 ── 値」を 2 列に並べる（Mendeley の Key information のような表）。値は折り返す。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(10)
        self._grid.setColumnStretch(1, 1)
        self._rows: dict[str, tuple[QLabel, QLabel]] = {}

    def add(self, key: str, title: str, link: bool = False) -> None:
        name = label(title, "caption")
        name.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        name.setMinimumWidth(64)
        value = QLabel()
        value.setWordWrap(True)
        value.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        # 長い値でパネルの幅が広がらないよう、列の幅に合わせて折り返す
        value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        if link:
            value.setTextFormat(Qt.TextFormat.RichText)
            value.setOpenExternalLinks(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        else:
            value.setTextFormat(Qt.TextFormat.PlainText)
            _selectable(value)
        value.setAccessibleName(title)
        row = self._grid.rowCount()
        self._grid.addWidget(name, row, 0)
        self._grid.addWidget(value, row, 1)
        self._rows[key] = (name, value)

    def set(self, key: str, text: str, placeholder: bool = False) -> None:
        """値を表示する。空なら行を隠す（placeholder なら「—」を出して残す）。"""
        name, value = self._rows[key]
        shown = bool(text) or placeholder
        name.setVisible(shown)
        value.setVisible(shown)
        value.setText(text or "—")
        set_prop(value, "role", None if text else "tertiary")

    def set_link(self, key: str, text: str, url: str) -> None:
        name, value = self._rows[key]
        name.setVisible(bool(text))
        value.setVisible(bool(text))
        value.setText(f'<a href="{html.escape(url)}">{html.escape(_breakable(text))}</a>' if text else "")


class PaperDetail(QWidget):
    """選んだ論文の詳細。「論文の詳細」（書誌情報）と「AI翻訳の詳細」（翻訳とファイル）のタブに分ける。"""

    readRequested = Signal(int)
    translateRequested = Signal(list)  # 論文 ID の一覧
    cancelRequested = Signal(int)
    editRequested = Signal(int)
    retryLookupRequested = Signal(int)
    enableLookupRequested = Signal()
    removeRequested = Signal(list)
    copyBibtexRequested = Signal(list)
    restoreRequested = Signal(list)  # ゴミ箱から戻す
    purgeRequested = Signal(list)  # 完全に削除
    cancelDownloadRequested = Signal(int)
    retryDownloadRequested = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Inspector")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(324)
        self._download = None
        self._sync_on = False
        self._sync_available = True
        self._ids: list[int] = []
        self._paper: Paper | None = None
        self._job: str | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # タブ（論文を選び直しても、選んでいたタブはそのまま）
        self.tabs = QWidget()
        self.tabs.setObjectName("DetailTabs")
        self.tabs.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        tl = QHBoxLayout(self.tabs)
        tl.setContentsMargins(18, 4, 18, 0)
        tl.setSpacing(18)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self.tab_buttons: list[QPushButton] = []
        for i, text in enumerate(("論文の詳細", "AI翻訳の詳細")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setAccessibleName(f"{text}のタブ")
            self._tab_group.addButton(b, i)
            tl.addWidget(b)
            self.tab_buttons.append(b)
        tl.addStretch(1)
        self.tab_buttons[PAPER_TAB].setChecked(True)
        self._tab_group.idClicked.connect(self.set_tab)
        outer.addWidget(self.tabs)

        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)
        self.pages.addWidget(self._build_paper_page())
        self.pages.addWidget(self._build_translation_page())
        self.pages.addWidget(self._build_other_page())
        self.pages.addWidget(self._build_trash_page())
        self.show_papers([])

    # -- 「論文の詳細」タブ ------------------------------------------------------

    def _build_paper_page(self) -> QWidget:
        area, lay = _scroll_page()
        head = QVBoxLayout()
        head.setSpacing(6)
        self.source = label("", "section")
        self.title = _selectable(label("", "title", wrap=True))
        head.addWidget(self.source)
        head.addWidget(self.title)
        lay.addLayout(head)

        read_row = QHBoxLayout()
        self.read_btn = button("読む", primary=True)
        self.read_btn.setMinimumWidth(112)
        self.read_btn.clicked.connect(lambda: self._paper and self.readRequested.emit(self._paper.id))
        read_row.addWidget(self.read_btn)
        read_row.addStretch(1)
        lay.addLayout(read_row)

        # 手元にない論文の取得（割合が分かれば割合、分からなければ動き続けるバー）
        self.fetch_box = QWidget()
        fl = QVBoxLayout(self.fetch_box)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(4)
        frow = QHBoxLayout()
        frow.setSpacing(8)
        self.fetch_bar = QProgressBar()
        self.fetch_bar.setAccessibleName("ファイルの取得の進捗")
        self.fetch_cancel = link_button("キャンセル")
        self.fetch_cancel.clicked.connect(lambda: self._paper and self.cancelDownloadRequested.emit(self._paper.id))
        frow.addWidget(self.fetch_bar, 1)
        frow.addWidget(self.fetch_cancel)
        fl.addLayout(frow)
        self.fetch_text = label("", "caption", wrap=True)
        fl.addWidget(self.fetch_text)
        self.fetch_retry = link_button("再試行")
        self.fetch_retry.clicked.connect(lambda: self._paper and self.retryDownloadRequested.emit(self._paper.id))
        fl.addWidget(self.fetch_retry, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.fetch_box)

        # 書誌情報の取得状況
        self.notice = QWidget()
        nl = QVBoxLayout(self.notice)
        nl.setContentsMargins(0, 0, 0, 0)
        nl.setSpacing(4)
        self.notice_text = label("", "caption", wrap=True)
        nl.addWidget(self.notice_text)
        nlinks = QHBoxLayout()
        nlinks.setSpacing(12)
        self.enter_doi = link_button("DOI を入力…")
        self.enter_doi.clicked.connect(lambda: self.editRequested.emit(self._paper.id))
        self.retry = link_button("再試行")
        self.retry.clicked.connect(lambda: self.retryLookupRequested.emit(self._paper.id))
        self.enable_lookup = link_button("照会を有効にする")
        self.enable_lookup.clicked.connect(self.enableLookupRequested)
        for b in (self.enter_doi, self.retry, self.enable_lookup):
            nlinks.addWidget(b)
        nlinks.addStretch(1)
        nl.addLayout(nlinks)
        lay.addWidget(self.notice)

        lay.addWidget(label("基本情報", "section"))
        self.info = _InfoGrid()
        for key, title in (
            ("authors", "著者"),
            ("year", "年"),
            ("venue", "雑誌・会議"),
            ("volume", "巻"),
            ("issue", "号"),
            ("pages", "ページ"),
        ):
            self.info.add(key, title)
        self.info.add("doi", "DOI", link=True)
        self.info.add("arxiv", "arXiv", link=True)
        lay.addWidget(self.info)

        self.abstract_title = label("概要", "section")
        self.abstract = _selectable(label("", "secondary", wrap=True))
        lay.addWidget(self.abstract_title)
        lay.addWidget(self.abstract)

        tools = QHBoxLayout()
        tools.setSpacing(16)
        edit = link_button("情報を編集…")
        edit.clicked.connect(lambda: self.editRequested.emit(self._paper.id))
        bib = link_button("BibTeX をコピー")
        bib.clicked.connect(lambda: self.copyBibtexRequested.emit([self._paper.id]))
        tools.addWidget(edit)
        tools.addWidget(bib)
        tools.addStretch(1)
        lay.addLayout(tools)
        lay.addStretch(1)
        return area

    # -- 「AI翻訳の詳細」タブ -----------------------------------------------------

    def _build_translation_page(self) -> QWidget:
        area, lay = _scroll_page()
        head = QVBoxLayout()
        head.setSpacing(4)
        self.tr_headline = label("", "title")
        self.tr_summary = label("", "secondary", wrap=True)
        head.addWidget(self.tr_headline)
        head.addWidget(self.tr_summary)
        lay.addLayout(head)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.translate_btn = button("翻訳する", primary=True)
        self.translate_btn.clicked.connect(self._on_translate)
        self.tr_read_btn = button("読む")
        self.tr_read_btn.clicked.connect(lambda: self._paper and self.readRequested.emit(self._paper.id))
        actions.addWidget(self.translate_btn, 1)
        actions.addWidget(self.tr_read_btn, 1)
        lay.addLayout(actions)

        tr = FormGroup("翻訳")
        self.tr_status = _value()
        self.tr_pages = _value()
        self.tr_model = _value()
        self.tr_formality = _value()
        self.tr_segments = _value()
        self.tr_date = _value()
        tr.add_row("状態", self.tr_status)
        self.tr_rows = [
            tr.add_row("ページ", self.tr_pages),
            tr.add_row("モデル", self.tr_model),
            tr.add_row("文体", self.tr_formality),
            tr.add_row("段落", self.tr_segments),
            tr.add_row("翻訳した日", self.tr_date),
        ]
        lay.addWidget(tr)

        info = FormGroup("ファイル")
        self.file_name = _value()
        self.file_pages = _value()
        self.added = _value()
        self.opened = _value()
        info.add_row("ファイル名", self.file_name)
        info.add_row("ページ数", self.file_pages)
        info.add_row("追加した日", self.added)
        self.opened_row = info.add_row("最後に開いた日", self.opened)
        self.local = _value()
        self.local_row = info.add_row("このパソコン", self.local)
        lay.addWidget(info)
        lay.addStretch(1)
        return area

    # -- 何も選んでいない・複数選んだとき -------------------------------------------

    def _build_other_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Inspector")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(18, 20, 18, 20)
        lay.setSpacing(16)
        self.placeholder = label("", "secondary", wrap=True)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.placeholder)
        self.multi = QWidget()
        ml = QVBoxLayout(self.multi)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(8)
        self.multi_translate = button("翻訳キューに追加", primary=True)
        self.multi_translate.clicked.connect(lambda: self.translateRequested.emit(list(self._ids)))
        self.multi_bibtex = button("BibTeX をコピー")
        self.multi_bibtex.clicked.connect(lambda: self.copyBibtexRequested.emit(list(self._ids)))
        self.multi_remove = button("ライブラリから削除…", destructive=True)
        self.multi_remove.clicked.connect(lambda: self.removeRequested.emit(list(self._ids)))
        self.multi_restore = button("ゴミ箱から戻す", primary=True)
        self.multi_restore.clicked.connect(lambda: self.restoreRequested.emit(list(self._ids)))
        self.multi_purge = button("完全に削除…", destructive=True)
        self.multi_purge.clicked.connect(lambda: self.purgeRequested.emit(list(self._ids)))
        for b in (self.multi_translate, self.multi_bibtex, self.multi_remove, self.multi_restore, self.multi_purge):
            ml.addWidget(b)
        lay.addWidget(self.multi)
        lay.addStretch(1)
        return page

    # -- ゴミ箱の論文 ----------------------------------------------------------

    def _build_trash_page(self) -> QWidget:
        area, lay = _scroll_page()
        lay.addWidget(label("ゴミ箱", "section"))
        self.trash_title = _selectable(label("", "title", wrap=True))
        self.trash_authors = _selectable(label("", "secondary", wrap=True))
        self.trash_left = label("", "caption", wrap=True)
        lay.addWidget(self.trash_title)
        lay.addWidget(self.trash_authors)
        lay.addWidget(self.trash_left)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.restore_btn = button("ゴミ箱から戻す", primary=True)
        self.restore_btn.clicked.connect(lambda: self.restoreRequested.emit(list(self._ids)))
        self.purge_btn = button("完全に削除…", destructive=True)
        self.purge_btn.clicked.connect(lambda: self.purgeRequested.emit(list(self._ids)))
        actions.addWidget(self.restore_btn, 1)
        actions.addWidget(self.purge_btn, 1)
        lay.addLayout(actions)
        lay.addStretch(1)
        return area

    # -- タブ ------------------------------------------------------------

    def current_tab(self) -> int:
        return self._tab_group.checkedId()

    def set_tab(self, index: int) -> None:
        self.tab_buttons[index].setChecked(True)
        if self._paper is not None and self._paper.trashed_at is None:
            self.pages.setCurrentIndex(index)

    # -- 表示 ------------------------------------------------------------

    def show_papers(
        self,
        papers: list[Paper],
        job: str | None = None,
        looking_up: bool = False,
        lookup_enabled: bool = False,
        download=None,
        sync_on: bool = False,
        sync_available: bool = True,
    ) -> None:
        self._ids = [p.id for p in papers]
        self._paper = papers[0] if len(papers) == 1 else None
        self._job = job
        self._download = download
        self._sync_on = sync_on
        self._sync_available = sync_available
        trashed = bool(papers) and all(p.trashed_at is not None for p in papers)
        self.tabs.setVisible(self._paper is not None and not trashed)
        if self._paper is not None and trashed:
            self.pages.setCurrentIndex(TRASH_PAGE)
            self._show_trashed(self._paper)
            return
        if self._paper is None:
            self.pages.setCurrentIndex(OTHER_PAGE)
            self.multi.setVisible(len(papers) > 1)
            for b in (self.multi_translate, self.multi_bibtex, self.multi_remove):
                b.setVisible(not trashed)
            for b in (self.multi_restore, self.multi_purge):
                b.setVisible(trashed)
            self.placeholder.setText(
                f"{len(papers)} 件の論文を選択しています" if papers else "論文を選ぶと、ここに詳細が表示されます。"
            )
            return
        self.pages.setCurrentIndex(self.current_tab())
        self._show_paper(self._paper, looking_up, lookup_enabled)
        self._show_translation(self._paper, job)
        self._show_local(self._paper)

    def _show_trashed(self, p: Paper) -> None:
        self.trash_title.setText(p.display_title)
        self.trash_authors.setText(", ".join(a.full() for a in p.metadata.authors))
        self.trash_authors.setVisible(bool(p.metadata.authors))
        n = days_left(p)
        self.trash_left.setText(f"あと {n} 日で完全に削除されます。戻すと、読んだり翻訳したりできます。")

    def _show_local(self, p: Paper) -> None:
        """手元にない論文の取得の状態（「読む」の下と、ファイルの欄の「このパソコン」）。"""
        d = self._download
        state = local_state(p, d)
        missing = not p.has_original
        self.local_row.setVisible(self._sync_on)
        if self.local_row.separator is not None:
            self.local_row.separator.setVisible(self._sync_on)
        self.local.setText(state[1] if state else "保存済み")
        running = d is not None and d.state in ("queued", "running")
        problem = d is not None and d.state in ("waiting", "failed")
        self.fetch_box.setVisible(running or problem)
        self.fetch_bar.setVisible(running)
        self.fetch_cancel.setVisible(running)
        self.fetch_retry.setVisible(problem)
        if running:
            pct = d.percent
            if d.state == "running" and pct is not None:
                self.fetch_bar.setRange(0, 100)
                self.fetch_bar.setValue(pct)
                self.fetch_text.setText(f"ファイルを取得中… {pct}%")
            else:
                self.fetch_bar.setRange(0, 0)  # 割合が分からないときは動き続けるバー
                self.fetch_text.setText("ファイルを取得中…" if d.state == "running" else "取得の順番を待っています…")
        elif problem:
            err = d.error
            title = getattr(err, "title", "") or "ファイルを取得できませんでした"
            hint = getattr(err, "hint", "")
            self.fetch_text.setText(f"{title}。{hint}" if hint else title)
        # 手元になく、同期先も読めないときは、読む・翻訳するを止める（手元にある論文は今までどおり）
        blocked = missing and not self._sync_available
        reason = "同期先のフォルダを読めないため、このパソコンにない論文は開けません" if blocked else ""
        for b in (self.read_btn, self.tr_read_btn):
            b.setEnabled(not blocked and not running)
            b.setToolTip(reason)
        if self._job is None:
            self.translate_btn.setEnabled(not blocked)
            self.translate_btn.setToolTip(reason)

    def _show_paper(self, p: Paper, looking_up: bool, lookup_enabled: bool) -> None:
        md = p.metadata
        self.source.setText(META_SOURCES.get(md.source, ""))
        self.source.setVisible(md.source in META_SOURCES)
        self.title.setText(p.display_title)
        self.info.set("authors", ", ".join(a.full() for a in md.authors), placeholder=True)
        self.info.set("year", str(md.year) if md.year else "", placeholder=True)
        self.info.set("venue", md.venue, placeholder=True)
        self.info.set("volume", md.volume)
        self.info.set("issue", md.issue)
        self.info.set("pages", md.pages)
        self.info.set_link("doi", md.doi, f"https://doi.org/{md.doi}")
        self.info.set_link("arxiv", md.arxiv_id, f"https://arxiv.org/abs/{md.arxiv_id}")
        self.abstract.setText(md.abstract)
        self.abstract.setVisible(bool(md.abstract))
        self.abstract_title.setVisible(bool(md.abstract))

        # 書誌情報の取得状況
        nid = doi_ok = False
        if looking_up:
            text, tone = "書誌情報を取得しています…", None
        elif p.meta_status == "failed":
            text, tone = "書誌情報を取得できませんでした。DOI を入力すると取得し直します。", "warning"
            nid = doi_ok = True
        elif p.meta_status == "pending" and lookup_enabled:
            text, tone = "書誌情報をまだ取得できていません（照会先に接続できませんでした）。", None
            doi_ok = True
        elif p.meta_status in ("local", "pending") and not lookup_enabled and md.source != "manual":
            text, tone = "PDF から読み取った情報です。照会を有効にすると、DOI から正確な書誌情報を取得できます。", None
            nid = True
        else:
            text, tone = "", None
        self.notice_text.setText(text)
        set_prop(self.notice_text, "tone", tone)
        self.notice.setVisible(bool(text))
        self.enter_doi.setVisible(doi_ok)
        self.retry.setVisible(doi_ok and lookup_enabled and bool(md.doi or md.arxiv_id))
        self.enable_lookup.setVisible(nid and not lookup_enabled)

    def _show_translation(self, p: Paper, job: str | None) -> None:
        t = p.translation
        status = translation_status(p, job)[0]
        self.tr_headline.setText(status)
        self.tr_status.setText(status)
        if job == "running":
            summary = "バックグラウンドで翻訳しています。翻訳中もほかの論文を読めます。"
        elif job == "queued":
            summary = "翻訳の順番を待っています。"
        elif t is None:
            summary = "まだ翻訳していません。翻訳すると、原文と訳文を並べて読めるようになります。"
        elif t.complete:
            summary = f"{_model_name(t.model)} で全ページを翻訳しました。"
        else:
            summary = "一部のページ・段落だけを翻訳しています。もう一度翻訳すると、キャッシュから続きを翻訳します。"
        self.tr_summary.setText(summary)

        # 主ボタンは 1 つ：未翻訳なら「翻訳する」、翻訳済みなら「読む」（再翻訳は控えめに）
        if job in ("running", "queued"):
            self.translate_btn.setText("翻訳をキャンセル")
            set_prop(self.translate_btn, "primary", False)
        elif t is not None:
            self.translate_btn.setText("再翻訳")
            set_prop(self.translate_btn, "primary", False)
        else:
            self.translate_btn.setText("翻訳する")
            set_prop(self.translate_btn, "primary", True)
        self.tr_read_btn.setVisible(t is not None or job in ("running", "queued"))
        set_prop(self.tr_read_btn, "primary", t is not None and job is None)

        for row in self.tr_rows:
            row.setVisible(t is not None)
            if row.separator is not None:
                row.separator.setVisible(t is not None)
        if t is not None:
            last = t.last_page + 1 if t.last_page is not None else p.page_count
            self.tr_pages.setText("すべて" if t.first_page == 0 and t.last_page is None else f"{t.first_page + 1}–{last}")
            self.tr_model.setText(_model_name(t.model))
            self.tr_formality.setText(FORMALITY_NAMES.get(t.formality, t.formality or "—"))
            self.tr_segments.setText(
                f"{t.segments:,}" + (f"（原文のまま {t.untranslated:,}）" if t.untranslated else "")
            )
            self.tr_date.setText(format_date(t.created_at))
        # 長いファイル名でパネルの幅が広がらないよう、中央を省略する
        fm = self.file_name.fontMetrics()
        self.file_name.setText(fm.elidedText(p.file_name, Qt.TextElideMode.ElideMiddle, 170))
        self.file_name.setToolTip(p.file_name)
        self.file_pages.setText(f"{p.page_count}")
        self.added.setText(format_date(p.added_at))
        self.opened.setText(format_date(p.last_opened_at))
        self.opened_row.setVisible(bool(p.last_opened_at))
        if self.opened_row.separator is not None:
            self.opened_row.separator.setVisible(bool(p.last_opened_at))

    def current_id(self) -> int | None:
        return self._paper.id if self._paper else None

    # -- 操作 ------------------------------------------------------------

    def _on_translate(self) -> None:
        p = self._paper
        if p is None:
            return
        if self._job in ("running", "queued"):
            self.cancelRequested.emit(p.id)
        else:
            self.translateRequested.emit([p.id])


def _model_name(model_id: str) -> str:
    from ..translate.gemini import MODELS

    info = MODELS.get(model_id)
    return info.name if info else model_id


# ======================================================================
# 書誌情報の編集
# ======================================================================


def format_authors(authors: list[Author]) -> str:
    return "; ".join(f"{a.family}, {a.given}" if a.given else a.family for a in authors)


def parse_authors(text: str) -> list[Author]:
    """「姓, 名; 姓, 名」または「名 姓; 名 姓」。"""
    out = []
    for part in text.replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "," in part:
            family, given = (s.strip() for s in part.split(",", 1))
        else:
            *given_parts, family = part.split()
            given = " ".join(given_parts)
        out.append(Author(family=family, given=given))
    return out


class MetadataDialog(QDialog):
    """書誌情報を手で直す。直した項目は、以後の自動取得で上書きしない。"""

    FIELDS = [
        ("title", "タイトル"),
        ("authors", "著者"),
        ("year", "年"),
        ("venue", "雑誌・会議"),
        ("volume", "巻"),
        ("issue", "号"),
        ("pages", "ページ"),
        ("doi", "DOI"),
        ("arxiv_id", "arXiv ID"),
    ]

    def __init__(self, paper: Paper, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("書誌情報を編集")
        self.setMinimumWidth(560)
        self.paper = paper
        md = paper.metadata
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(12)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.edits: dict[str, QLineEdit] = {}
        for key, text in self.FIELDS:
            e = QLineEdit(self._initial(md, key))
            e.setAccessibleName(text)
            if key == "authors":
                e.setPlaceholderText("姓, 名; 姓, 名")
            if key == "doi":
                e.setPlaceholderText("10.xxxx/…")
            form.addRow(text, e)
            self.edits[key] = e
        self.abstract = QPlainTextEdit(md.abstract)
        self.abstract.setAccessibleName("概要")
        self.abstract.setFixedHeight(110)
        form.addRow("概要", self.abstract)
        lay.addLayout(form)
        lay.addWidget(label("ここで直した項目は、書誌情報を取得し直しても上書きされません。", "caption", wrap=True))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.edits["title"].setFocus()

    @staticmethod
    def _initial(md: Metadata, key: str) -> str:
        if key == "authors":
            return format_authors(md.authors)
        if key == "year":
            return str(md.year) if md.year else ""
        return str(getattr(md, key) or "")

    def result_metadata(self) -> tuple[Metadata, set[str]]:
        """(編集後の書誌情報, 変更した項目)。"""
        from ..library.identifiers import find_arxiv, normalize_doi

        md = self.paper.metadata
        values: dict = {}
        # 変えたかどうかは入力欄の文字列で判断する（著者などは文字列との往復で元の値に戻らないことがあるため、
        # 触っていない項目は解析しない）
        for key, _ in self.FIELDS:
            text = self.edits[key].text().strip()
            if text == self._initial(md, key).strip():
                continue
            if key == "authors":
                values[key] = parse_authors(text)
            elif key == "year":
                values[key] = int(text) if text.isdigit() else None
            elif key == "doi":
                values[key] = normalize_doi(text) if text else ""
            elif key == "arxiv_id":
                values[key] = (find_arxiv(text) or find_arxiv(f"arXiv:{text}") or text) if text else ""
            else:
                values[key] = text
        abstract = self.abstract.toPlainText().strip()
        if abstract != md.abstract.strip():
            values["abstract"] = abstract
        changed = {k for k, v in values.items() if v != getattr(md, k)}
        new = replace(md, **values)
        if changed:
            new = replace(new, source="manual") if md.source in ("", "heuristic", "embedded") else new
        return new, changed
