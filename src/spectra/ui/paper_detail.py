"""ライブラリの詳細パネル（選択中の論文の書誌情報・翻訳の状態・操作）と、書誌情報の編集ダイアログ。"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..library import Author, Metadata, Paper
from .library_model import format_date, translation_status
from .widgets import FormGroup, button, label, link_button, set_prop


def _selectable(lb: QLabel) -> QLabel:
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lb


def _value(text: str = "") -> QLabel:
    lb = QLabel(text)
    lb.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    lb.setProperty("role", "secondary")
    return lb


class PaperDetail(QScrollArea):
    readRequested = Signal(int)
    translateRequested = Signal(list)  # 論文 ID の一覧
    cancelRequested = Signal(int)
    editRequested = Signal(int)
    retryLookupRequested = Signal(int)
    enableLookupRequested = Signal()
    removeRequested = Signal(list)
    copyBibtexRequested = Signal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Inspector")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedWidth(324)
        body = QWidget()
        body.setObjectName("Inspector")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 20, 18, 20)
        lay.setSpacing(16)
        self.setWidget(body)
        self._ids: list[int] = []
        self._paper: Paper | None = None

        # 何も選んでいない・複数選んだとき
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
        for b in (self.multi_translate, self.multi_bibtex, self.multi_remove):
            ml.addWidget(b)
        lay.addWidget(self.multi)

        # 1 件を選んだとき
        self.single = QWidget()
        sl = QVBoxLayout(self.single)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(16)

        head = QVBoxLayout()
        head.setSpacing(4)
        self.title = _selectable(label("", "title", wrap=True))
        self.authors = _selectable(label("", "secondary", wrap=True))
        self.venue = _selectable(label("", "caption", wrap=True))
        head.addWidget(self.title)
        head.addWidget(self.authors)
        head.addWidget(self.venue)
        links = QVBoxLayout()
        links.setSpacing(2)
        self.doi_link = link_button("")
        self.doi_link.clicked.connect(lambda: self._open_url(f"https://doi.org/{self._paper.metadata.doi}"))
        self.arxiv_link = link_button("")
        self.arxiv_link.clicked.connect(lambda: self._open_url(f"https://arxiv.org/abs/{self._paper.metadata.arxiv_id}"))
        for b in (self.doi_link, self.arxiv_link):
            # 長い DOI でパネルの幅が広がらないようにする
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            links.addWidget(b)
        head.addLayout(links)
        sl.addLayout(head)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.primary = button("", primary=True)
        self.primary.clicked.connect(self._on_primary)
        self.secondary = button("")
        self.secondary.clicked.connect(self._on_secondary)
        actions.addWidget(self.primary, 1)
        actions.addWidget(self.secondary, 1)
        sl.addLayout(actions)

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
        self.enable_lookup = link_button("照会を有効にする…")
        self.enable_lookup.clicked.connect(self.enableLookupRequested)
        for b in (self.enter_doi, self.retry, self.enable_lookup):
            nlinks.addWidget(b)
        nlinks.addStretch(1)
        nl.addLayout(nlinks)
        sl.addWidget(self.notice)

        tr = FormGroup("翻訳")
        self.tr_status = _value()
        self.tr_pages = _value()
        self.tr_model = _value()
        self.tr_date = _value()
        tr.add_row("状態", self.tr_status)
        self.tr_pages_row = tr.add_row("ページ", self.tr_pages)
        self.tr_model_row = tr.add_row("モデル", self.tr_model)
        self.tr_date_row = tr.add_row("翻訳した日", self.tr_date)
        sl.addWidget(tr)

        info = FormGroup("ファイル")
        self.file_name = _value()
        self.file_pages = _value()
        self.added = _value()
        info.add_row("ファイル名", self.file_name)
        info.add_row("ページ数", self.file_pages)
        info.add_row("追加した日", self.added)
        sl.addWidget(info)

        self.abstract_title = label("概要", "section")
        self.abstract = _selectable(label("", "secondary", wrap=True))
        sl.addWidget(self.abstract_title)
        sl.addWidget(self.abstract)

        tools = QHBoxLayout()
        tools.setSpacing(16)
        edit = link_button("情報を編集…")
        edit.clicked.connect(lambda: self.editRequested.emit(self._paper.id))
        bib = link_button("BibTeX をコピー")
        bib.clicked.connect(lambda: self.copyBibtexRequested.emit([self._paper.id]))
        tools.addWidget(edit)
        tools.addWidget(bib)
        tools.addStretch(1)
        sl.addLayout(tools)
        lay.addWidget(self.single)
        lay.addStretch(1)
        self.show_papers([])

    # -- 表示 ------------------------------------------------------------

    def show_papers(
        self,
        papers: list[Paper],
        job: str | None = None,
        looking_up: bool = False,
        lookup_enabled: bool = False,
    ) -> None:
        self._ids = [p.id for p in papers]
        self._paper = papers[0] if len(papers) == 1 else None
        self.single.setVisible(self._paper is not None)
        self.multi.setVisible(len(papers) > 1)
        self.placeholder.setVisible(len(papers) != 1)
        if not papers:
            self.placeholder.setText("論文を選択すると、ここに詳しい情報が表示されます。")
            return
        if len(papers) > 1:
            self.placeholder.setText(f"{len(papers)} 件の論文を選択しています")
            return
        self._show_one(self._paper, job, looking_up, lookup_enabled)

    def _show_one(self, p: Paper, job: str | None, looking_up: bool, lookup_enabled: bool) -> None:
        md = p.metadata
        self.title.setText(p.display_title)
        self.authors.setText(", ".join(a.full() for a in md.authors))
        self.authors.setVisible(bool(md.authors))
        venue = md.venue
        if md.volume:
            venue += f" {md.volume}" + (f"({md.issue})" if md.issue else "")
        if md.pages:
            venue += f", {md.pages}"
        parts = [s for s in (venue.strip(), str(md.year) if md.year else "") if s]
        self.venue.setText(" · ".join(parts))
        self.venue.setVisible(bool(parts))
        self.doi_link.setText(f"DOI: {md.doi}")
        self.doi_link.setVisible(bool(md.doi))
        self.arxiv_link.setText(f"arXiv: {md.arxiv_id}")
        self.arxiv_link.setVisible(bool(md.arxiv_id))

        # 主ボタンは 1 つ：翻訳済みなら「読む」、未翻訳なら「翻訳する」
        if job in ("running", "queued"):
            self.primary.setText("読む")
            self.secondary.setText("翻訳をキャンセル")
        elif p.translation is not None:
            self.primary.setText("読む")
            self.secondary.setText("再翻訳")
        else:
            self.primary.setText("翻訳する")
            self.secondary.setText("原文を読む")

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

        t = p.translation
        self.tr_status.setText(translation_status(p, job)[0])
        for row in (self.tr_pages_row, self.tr_model_row, self.tr_date_row):
            row.setVisible(t is not None)
            if row.separator is not None:
                row.separator.setVisible(t is not None)
        if t is not None:
            last = t.last_page + 1 if t.last_page is not None else p.page_count
            self.tr_pages.setText("すべて" if t.first_page == 0 and t.last_page is None else f"{t.first_page + 1}–{last}")
            self.tr_model.setText(_model_name(t.model))
            self.tr_date.setText(format_date(t.created_at))
        # 長いファイル名でパネルの幅が広がらないよう、中央を省略する
        fm = self.file_name.fontMetrics()
        self.file_name.setText(fm.elidedText(p.file_name, Qt.TextElideMode.ElideMiddle, 170))
        self.file_name.setToolTip(p.file_name)
        self.file_pages.setText(f"{p.page_count}")
        self.added.setText(format_date(p.added_at))
        self.abstract.setText(md.abstract)
        self.abstract.setVisible(bool(md.abstract))
        self.abstract_title.setVisible(bool(md.abstract))

    def current_id(self) -> int | None:
        return self._paper.id if self._paper else None

    # -- 操作 ------------------------------------------------------------

    def _on_primary(self) -> None:
        p = self._paper
        if p is None:
            return
        if self.primary.text() == "翻訳する":
            self.translateRequested.emit([p.id])
        else:
            self.readRequested.emit(p.id)

    def _on_secondary(self) -> None:
        p = self._paper
        if p is None:
            return
        text = self.secondary.text()
        if text == "翻訳をキャンセル":
            self.cancelRequested.emit(p.id)
        elif text == "再翻訳":
            self.translateRequested.emit([p.id])
        else:
            self.readRequested.emit(p.id)

    @staticmethod
    def _open_url(url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))


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
