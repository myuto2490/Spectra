"""論文ライブラリのデータ型（Qt 非依存）。"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import ClassVar

META_STATUSES = ("pending", "done", "failed", "local")  # local: オンライン照会をしない設定
NOTE_MAX_CHARS = 100_000  # 論文ごとのノートの長さの上限
ANNOTATION_KINDS = ("highlight", "underline", "note")  # マーカー・下線・付箋
ANNOTATION_COLORS = ("yellow", "green", "blue", "pink", "purple")
MAX_ANNOTATIONS = 5_000  # 1 本の論文あたり（同期のファイルの大きさの上限に収める）
MAX_QUOTE = 2_000
MAX_COMMENT = 10_000


@dataclass(frozen=True)
class Author:
    family: str
    given: str = ""

    def display(self) -> str:
        """'Vaswani, A.' の形（名は頭文字）。"""
        initials = " ".join(f"{p[0]}." for p in self.given.replace(".", " ").split() if p)
        return f"{self.family}, {initials}" if initials else self.family

    def full(self) -> str:
        return f"{self.given} {self.family}".strip()

    def to_dict(self) -> dict:
        return {"family": self.family, "given": self.given}

    @classmethod
    def from_dict(cls, raw: dict) -> Author:
        return cls(str(raw.get("family", "")), str(raw.get("given", "")))


@dataclass
class Metadata:
    title: str = ""
    authors: list[Author] = field(default_factory=list)
    year: int | None = None
    venue: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doi: str = ""
    arxiv_id: str = ""
    abstract: str = ""
    url: str = ""
    source: str = ""  # embedded / heuristic / crossref / arxiv / manual

    # 利用者が編集できる項目（source は記録用なので含めない）
    FIELDS: ClassVar[tuple[str, ...]] = (
        "title", "authors", "year", "venue", "volume", "issue", "pages", "doi", "arxiv_id", "abstract", "url",
    )

    def merged(self, other: Metadata) -> Metadata:
        """self の空の項目を other の値で埋めた複製を返す。"""
        values = {}
        for f in fields(self):
            mine = getattr(self, f.name)
            values[f.name] = getattr(other, f.name) if _empty(mine) else mine
        return replace(self, **values)

    def is_empty(self) -> bool:
        return all(_empty(getattr(self, name)) for name in self.FIELDS)


def _empty(value) -> bool:
    return value is None or value == "" or value == []


@dataclass
class Translation:
    paper_id: int
    model: str
    formality: str
    first_page: int
    last_page: int | None
    options: dict
    segments: int
    untranslated: int
    chars: int
    created_at: float

    @property
    def complete(self) -> bool:
        """全ページを訳し、訳せなかった段落がないか。"""
        return self.first_page == 0 and self.last_page is None and self.untranslated == 0


@dataclass(frozen=True)
class LibraryEvent:
    """ライブラリの変更の合図（同期の層が受け取る）。中身は最小限で、詳しくは読み直してもらう。

    kind: added / metadata / favorite / opened / removed / translation / collections / note / annotations
    """

    kind: str
    paper_id: int | None = None
    sha256: str = ""


@dataclass(frozen=True)
class AddResult:
    paper: Paper
    status: str  # new（新しく追加）/ existing（既にあった）/ restored（ゴミ箱から戻した）


@dataclass(frozen=True)
class Annotation:
    """原文に付けた注釈（docs/ANNOTATIONS.md）。位置は原文のページの座標（pt）で持つ。"""

    id: str  # uuid4 の 16 進 32 桁
    paper_id: int
    kind: str  # ANNOTATION_KINDS
    page: int  # 0 始まり
    quads: tuple[tuple[float, float, float, float], ...]  # 行ごとの矩形。付箋は (x, y, x, y) の 1 つ
    quote: str
    color: str  # ANNOTATION_COLORS
    comment: str
    created_at: float
    updated_at: float
    comment_merged: bool = False  # 同期で同時の編集を連結した（次にコメントを保存すると False）

    @property
    def rect(self) -> tuple[float, float, float, float]:
        """全体を囲む矩形。"""
        return (
            min(q[0] for q in self.quads),
            min(q[1] for q in self.quads),
            max(q[2] for q in self.quads),
            max(q[3] for q in self.quads),
        )

    @property
    def has_comment(self) -> bool:
        return bool(self.comment.strip())


@dataclass(frozen=True)
class Collection:
    id: int
    name: str


@dataclass
class Paper:
    id: int
    sha256: str
    file_name: str
    source_path: str
    page_count: int
    added_at: float
    updated_at: float
    last_opened_at: float | None
    favorite: bool
    meta_status: str
    metadata: Metadata
    edited: frozenset[str]
    collection_ids: frozenset[int]
    translation: Translation | None
    root: Path
    # 手元にファイルがあるか（読み込むときに 1 回だけ決める。同期フォルダには触らない）
    has_original: bool = True
    has_translation_file: bool = False  # 訳文があり、かつ訳文のファイルが手元にある
    trashed_at: float | None = None  # ゴミ箱に入れた時刻（None なら通常の論文）
    note: str = ""  # 論文ごとのメモ（プレーンテキスト）
    note_updated_at: float | None = None
    note_merged: bool = False  # 同期で、ほかのパソコンでの同時の編集と連結した（次に保存すると False に戻る）

    @property
    def folder(self) -> Path:
        return self.root / "files" / str(self.id)

    @property
    def pdf_path(self) -> Path:
        return self.folder / "original.pdf"

    @property
    def translated_path(self) -> Path:
        return self.folder / "translated.pdf"

    @property
    def display_title(self) -> str:
        return self.metadata.title or Path(self.file_name).stem

    @property
    def authors_short(self) -> str:
        names = [a.family or a.given for a in self.metadata.authors]
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} & {names[1]}"
        return f"{names[0]} et al."
