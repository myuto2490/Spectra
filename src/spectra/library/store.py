"""論文ライブラリの保存（SQLite + PDF ファイル）。

library_dir()/
    library.sqlite3
    state.sqlite3                     この端末だけの状態（ゴミ箱）。library.sqlite3 の版を上げずに済むよう別ファイルにする
    files/<paper_id>/original.pdf     取り込んだ PDF のコピー
    files/<paper_id>/translated.pdf   訳文のみの版（出力形式は PDF出力のときに作り直す）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..config import paths
from ..core.pipeline import open_document
from .errors import LibraryError, PaperNotFound
from .metadata import extract_local
from .models import (
    META_STATUSES,
    AddResult,
    Author,
    Collection,
    LibraryEvent,
    Metadata,
    Paper,
    Translation,
)

log = logging.getLogger(__name__)

DB_NAME = "library.sqlite3"
STATE_DB_NAME = "state.sqlite3"
TRASH_DAYS = 30

# 1 要素が 1 バージョン。PRAGMA user_version に適用済みの数を記録し、足りない分を順に流す。
# 既存の要素は書き換えず、変更は末尾に追加すること。
MIGRATIONS: list[str] = [
    """
    CREATE TABLE papers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sha256 TEXT NOT NULL UNIQUE,
        file_name TEXT NOT NULL,
        source_path TEXT NOT NULL DEFAULT '',
        page_count INTEGER NOT NULL DEFAULT 0,
        added_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_opened_at REAL,
        favorite INTEGER NOT NULL DEFAULT 0,
        meta_status TEXT NOT NULL DEFAULT 'pending',
        meta_source TEXT NOT NULL DEFAULT '',
        edited TEXT NOT NULL DEFAULT '[]',
        title TEXT NOT NULL DEFAULT '',
        authors TEXT NOT NULL DEFAULT '[]',
        year INTEGER,
        venue TEXT NOT NULL DEFAULT '',
        volume TEXT NOT NULL DEFAULT '',
        issue TEXT NOT NULL DEFAULT '',
        pages TEXT NOT NULL DEFAULT '',
        doi TEXT NOT NULL DEFAULT '',
        arxiv_id TEXT NOT NULL DEFAULT '',
        abstract TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX papers_doi ON papers (doi);
    CREATE INDEX papers_arxiv ON papers (arxiv_id);
    CREATE TABLE collections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL
    );
    CREATE TABLE collection_papers (
        collection_id INTEGER NOT NULL REFERENCES collections (id) ON DELETE CASCADE,
        paper_id INTEGER NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
        PRIMARY KEY (collection_id, paper_id)
    );
    CREATE TABLE translations (
        paper_id INTEGER PRIMARY KEY REFERENCES papers (id) ON DELETE CASCADE,
        model TEXT NOT NULL DEFAULT '',
        formality TEXT NOT NULL DEFAULT '',
        first_page INTEGER NOT NULL DEFAULT 0,
        last_page INTEGER,
        options TEXT NOT NULL DEFAULT '{}',
        segments INTEGER NOT NULL DEFAULT 0,
        untranslated INTEGER NOT NULL DEFAULT 0,
        chars INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    """,
]

# state.sqlite3（ATTACH して "state" として使う）の移行。考え方は MIGRATIONS と同じ
STATE_MIGRATIONS: list[str] = [
    """
    CREATE TABLE state.trash (
        paper_id INTEGER PRIMARY KEY,
        trashed_at REAL NOT NULL
    );
    """,
]

_TEXT_FIELDS = ("title", "venue", "volume", "issue", "pages", "doi", "arxiv_id", "abstract", "url")


class Library:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else paths.library_dir()
        (self.root / "files").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.root / DB_NAME), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._listeners: list[Callable[[LibraryEvent], None]] = []
        with self._lock:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._migrate()
            self._db.execute("ATTACH DATABASE ? AS state", (str(self.root / STATE_DB_NAME),))
            self._db.execute("PRAGMA state.journal_mode=WAL")
            self._migrate_state()
            # 完全に削除するときは 2 つの DB にまたがるので、途中で落ちた分の残りを掃除する
            self._db.execute("DELETE FROM state.trash WHERE paper_id NOT IN (SELECT id FROM main.papers)")

    # -- 接続・移行 ----------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _migrate(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > len(MIGRATIONS):
            self._db.close()
            raise LibraryError(
                "新しいバージョンの Spectra で作成されたライブラリです。",
                hint="Spectra を最新版に更新してください。",
            )
        for number, script in enumerate(MIGRATIONS[version:], version + 1):
            log.info("migrating library to version %d", number)
            try:
                self._db.executescript(f"BEGIN;\n{script.strip().rstrip(';')};\nPRAGMA user_version = {number};\nCOMMIT;")
            except sqlite3.Error:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _migrate_state(self) -> None:
        version = int(self._db.execute("PRAGMA state.user_version").fetchone()[0])
        if version > len(STATE_MIGRATIONS):
            self._db.close()
            raise LibraryError(
                "新しいバージョンの Spectra で作成されたライブラリです。",
                hint="Spectra を最新版に更新してください。",
            )
        for number, script in enumerate(STATE_MIGRATIONS[version:], version + 1):
            try:
                self._db.executescript(
                    f"BEGIN;\n{script.strip().rstrip(';')};\nPRAGMA state.user_version = {number};\nCOMMIT;"
                )
            except sqlite3.Error:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    # -- 変更の合図 ------------------------------------------------------------

    def add_listener(self, fn: Callable[[LibraryEvent], None]) -> Callable[[], None]:
        """変更の合図を受け取る関数を登録し、登録を解除する関数を返す。

        約束: コミットしてロックを離した後に呼ぶ。呼ばれるスレッドは決まっていない。
        例外はログに書いて握りつぶす（変更の操作は失敗させない）。受け取る側は合図だけを受け取り、重い処理はしない。
        """
        with self._lock:
            self._listeners.append(fn)

        def remove() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)

        return remove

    def _emit(self, kind: str, paper_id: int | None = None, sha256: str = "") -> None:
        with self._lock:
            listeners = list(self._listeners)
        event = LibraryEvent(kind, paper_id, sha256)
        for fn in listeners:
            try:
                fn(event)
            except Exception:
                log.exception("library listener failed")

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- 論文 ----------------------------------------------------------------

    def add(self, path: Path, metadata: Metadata | None = None) -> tuple[Paper, bool]:
        """PDF を取り込む。同じ内容のファイルが既にあれば (既存の論文, True) を返す。"""
        result = self.add_detailed(path, metadata)
        return result.paper, result.status != "new"

    def add_detailed(self, path: Path, metadata: Metadata | None = None) -> AddResult:
        """add と同じだが、新規・既存・ゴミ箱から戻した、を区別して返す。"""
        path = Path(path)
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if existing := self.find_by_sha(sha):
            if existing.trashed_at is None:
                return AddResult(existing, "existing")
            self.restore(existing.id)  # ゴミ箱の論文をもう一度追加したら、ゴミ箱から戻す
            return AddResult(self._require(existing.id), "restored")
        doc = open_document(data)
        try:
            page_count = doc.page_count
            md = metadata if metadata is not None else extract_local(doc, path.name)
        finally:
            doc.close()
        now = time.time()
        try:
            with self._tx() as db:
                cur = db.execute(
                    "INSERT INTO papers (sha256, file_name, source_path, page_count, added_at, updated_at, meta_status)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                    (sha, path.name, str(path.resolve()), page_count, now, now),
                )
                paper_id = int(cur.lastrowid)
                self._write_metadata(paper_id, md)
                folder = self.root / "files" / str(paper_id)
                if folder.exists():  # 以前の取り込みの残骸（id は再利用されないが念のため）
                    shutil.rmtree(folder)
                _write_atomic(folder / "original.pdf", data)
        except sqlite3.IntegrityError:
            # 別スレッドが同じファイルを先に取り込んだ
            if existing := self.find_by_sha(sha):
                return AddResult(existing, "existing")
            raise
        self._emit("added", paper_id, sha)
        return AddResult(self._require(paper_id), "new")

    def get(self, paper_id: int) -> Paper | None:
        papers = self._load("WHERE p.id = ?", (paper_id,))
        return papers[0] if papers else None

    def papers(self) -> list[Paper]:
        """ゴミ箱にない論文を、追加日時の新しい順に返す。"""
        return self._load("WHERE t.trashed_at IS NULL ORDER BY p.added_at DESC, p.id DESC")

    def trashed_papers(self) -> list[Paper]:
        """ゴミ箱の論文を、入れた時刻の新しい順に返す。"""
        return self._load("WHERE t.trashed_at IS NOT NULL ORDER BY t.trashed_at DESC, p.id DESC")

    def find_by_sha(self, sha256: str) -> Paper | None:
        papers = self._load("WHERE p.sha256 = ?", (sha256,))
        return papers[0] if papers else None

    def same_identifier(self, paper: Paper) -> list[Paper]:
        """DOI か arXiv ID が同じ別の論文（プレプリントと出版版など）。"""
        md = paper.metadata
        if not md.doi and not md.arxiv_id:
            return []
        return self._load(
            "WHERE p.id != ? AND t.trashed_at IS NULL"
            " AND ((p.doi != '' AND p.doi = ?) OR (p.arxiv_id != '' AND p.arxiv_id = ?))"
            " ORDER BY p.added_at DESC",
            (paper.id, md.doi, md.arxiv_id),
        )

    def update_metadata(
        self, paper_id: int, md: Metadata, *, status: str | None = None, manual_fields: set[str] | None = None
    ) -> Paper:
        """書誌情報を更新する。

        manual_fields を渡すと、その項目を md の値で上書きして「利用者が編集した項目」に加える。
        渡さない場合（自動更新）は、編集済みの項目と md 側が空の項目を変えない。
        """
        if status is not None:
            _check_status(status)
        current = self._require(paper_id)
        edited = set(current.edited)
        values = {}
        if manual_fields is not None:
            unknown = set(manual_fields) - set(Metadata.FIELDS)
            if unknown:
                raise ValueError(f"unknown metadata fields: {sorted(unknown)}")
            values = {name: getattr(md, name) for name in manual_fields}
            edited |= set(manual_fields)
            source = md.source or current.metadata.source
        else:
            for name in Metadata.FIELDS:
                value = getattr(md, name)
                if name not in edited and value not in (None, "", []):
                    values[name] = value
            source = md.source or current.metadata.source
        merged = Metadata(**{**{n: getattr(current.metadata, n) for n in Metadata.FIELDS}, **values}, source=source)
        with self._tx() as db:
            self._write_metadata(paper_id, merged)
            db.execute("UPDATE papers SET edited = ? WHERE id = ?", (json.dumps(sorted(edited)), paper_id))
            if status is not None:
                db.execute("UPDATE papers SET meta_status = ? WHERE id = ?", (status, paper_id))
        self._emit("metadata", paper_id, current.sha256)
        return self._require(paper_id)

    def set_meta_status(self, paper_id: int, status: str) -> None:
        """照会の状態はこの端末だけのものなので、updated_at を進めず、合図も出さない。"""
        _check_status(status)
        with self._tx() as db:
            db.execute("UPDATE papers SET meta_status = ? WHERE id = ?", (status, paper_id))

    def set_favorite(self, paper_id: int, on: bool) -> None:
        self._update(paper_id, "favorite = ?", (int(bool(on)),))
        self._emit("favorite", paper_id)

    def touch_opened(self, paper_id: int) -> None:
        with self._tx() as db:
            db.execute("UPDATE papers SET last_opened_at = ? WHERE id = ?", (time.time(), paper_id))
        self._emit("opened", paper_id)

    # -- ゴミ箱と削除 ----------------------------------------------------------

    def trash(self, paper_id: int) -> None:
        """ゴミ箱に入れる（「ライブラリから削除」）。ファイルと書誌情報は purge するまで残す。"""
        paper = self._require(paper_id)
        if paper.trashed_at is not None:
            return
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO state.trash (paper_id, trashed_at) VALUES (?, ?)", (paper_id, time.time()))
        self._emit("removed", paper_id, paper.sha256)

    def restore(self, paper_id: int) -> None:
        """ゴミ箱から戻す。"""
        paper = self._require(paper_id)
        if paper.trashed_at is None:
            return
        with self._tx() as db:
            db.execute("DELETE FROM state.trash WHERE paper_id = ?", (paper_id,))
            # 戻した操作も変更なので、同期で時刻を付けられるよう updated_at を進める
            db.execute("UPDATE papers SET updated_at = ? WHERE id = ?", (time.time(), paper_id))
        self._emit("added", paper_id, paper.sha256)

    def purge(self, paper_id: int) -> None:
        """レコードと PDF ファイルを完全に削除する（ゴミ箱を経ない）。"""
        with self._lock:
            row = self._db.execute("SELECT sha256 FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if row is None:
            return
        with self._tx() as db:
            db.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
        with self._tx() as db:  # 別の DB。落ちても起動時に掃除する
            db.execute("DELETE FROM state.trash WHERE paper_id = ?", (paper_id,))
        shutil.rmtree(self.root / "files" / str(paper_id), ignore_errors=True)
        self._emit("removed", paper_id, row["sha256"])

    remove = purge  # v0.2.0 からの名前

    def empty_trash(self) -> int:
        """ゴミ箱を空にし、消した件数を返す。"""
        papers = self.trashed_papers()
        for p in papers:
            self.purge(p.id)
        return len(papers)

    def purge_expired(self, max_age_days: float = TRASH_DAYS, *, now: float | None = None) -> int:
        """ゴミ箱に入れてから max_age_days を過ぎた論文を完全に削除し、件数を返す（起動時に呼ぶ）。"""
        limit = (now if now is not None else time.time()) - max_age_days * 86400
        expired = [p for p in self.trashed_papers() if p.trashed_at is not None and p.trashed_at <= limit]
        for p in expired:
            self.purge(p.id)
        return len(expired)

    # -- コレクション ----------------------------------------------------------

    def collections(self) -> list[Collection]:
        with self._lock:
            rows = self._db.execute("SELECT id, name FROM collections ORDER BY name COLLATE NOCASE, id").fetchall()
        return [Collection(int(r["id"]), r["name"]) for r in rows]

    def create_collection(self, name: str) -> Collection:
        name = _collection_name(name)
        with self._tx() as db:
            cur = db.execute("INSERT INTO collections (name) VALUES (?)", (name,))
        self._emit("collections")
        return Collection(int(cur.lastrowid), name)

    def rename_collection(self, collection_id: int, name: str) -> None:
        name = _collection_name(name)
        with self._tx() as db:
            db.execute("UPDATE collections SET name = ? WHERE id = ?", (name, collection_id))
        self._emit("collections")

    def delete_collection(self, collection_id: int) -> None:
        """コレクションだけを消す（中の論文は残る）。"""
        with self._tx() as db:
            db.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
        self._emit("collections")

    def add_to_collection(self, collection_id: int, paper_ids) -> None:
        with self._tx() as db:
            db.executemany(
                "INSERT OR IGNORE INTO collection_papers (collection_id, paper_id) VALUES (?, ?)",
                [(collection_id, pid) for pid in paper_ids],
            )
        self._emit("collections")

    def remove_from_collection(self, collection_id: int, paper_ids) -> None:
        with self._tx() as db:
            db.executemany(
                "DELETE FROM collection_papers WHERE collection_id = ? AND paper_id = ?",
                [(collection_id, pid) for pid in paper_ids],
            )
        self._emit("collections")

    # -- 訳文 ------------------------------------------------------------------

    def save_translation(self, paper_id: int, translated_pdf: bytes, info: Translation) -> Paper:
        """訳文の PDF を保存し、以前の訳文と置き換える。"""
        paper = self._require(paper_id)
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO translations (paper_id, model, formality, first_page, last_page, options,"
                " segments, untranslated, chars, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    paper_id, info.model, info.formality, info.first_page, info.last_page,
                    json.dumps(info.options, ensure_ascii=False), info.segments, info.untranslated, info.chars,
                    info.created_at,
                ),
            )
            db.execute("UPDATE papers SET updated_at = ? WHERE id = ?", (time.time(), paper_id))
            _write_atomic(paper.translated_path, translated_pdf)
        self._emit("translation", paper_id, paper.sha256)
        return self._require(paper_id)

    def remove_translation(self, paper_id: int) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM translations WHERE paper_id = ?", (paper_id,))
        (self.root / "files" / str(paper_id) / "translated.pdf").unlink(missing_ok=True)
        self._emit("translation", paper_id)

    # -- 内部 ------------------------------------------------------------------

    def _require(self, paper_id: int) -> Paper:
        paper = self.get(paper_id)
        if paper is None:
            raise PaperNotFound(f"論文が見つかりません（id={paper_id}）")
        return paper

    def _update(self, paper_id: int, assignment: str, params: tuple) -> None:
        with self._tx() as db:
            db.execute(f"UPDATE papers SET {assignment}, updated_at = ? WHERE id = ?", (*params, time.time(), paper_id))

    def _write_metadata(self, paper_id: int, md: Metadata) -> None:
        self._db.execute(
            f"UPDATE papers SET {', '.join(f'{n} = ?' for n in _TEXT_FIELDS)}, authors = ?, year = ?,"
            " meta_source = ?, updated_at = ? WHERE id = ?",
            (
                *(getattr(md, n) or "" for n in _TEXT_FIELDS),
                json.dumps([a.to_dict() for a in md.authors], ensure_ascii=False),
                md.year,
                md.source,
                time.time(),
                paper_id,
            ),
        )

    def _load(self, where: str, params: tuple = ()) -> list[Paper]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT p.*, t.trashed_at AS trashed_at FROM papers p LEFT JOIN state.trash t ON t.paper_id = p.id {where}",
                params,
            ).fetchall()
            if not rows:
                return []
            ids = [int(r["id"]) for r in rows]
            marks = ",".join("?" * len(ids))
            members: dict[int, set[int]] = {}
            for r in self._db.execute(
                f"SELECT paper_id, collection_id FROM collection_papers WHERE paper_id IN ({marks})", ids
            ):
                members.setdefault(int(r[0]), set()).add(int(r[1]))
            translations = {
                int(r["paper_id"]): r
                for r in self._db.execute(f"SELECT * FROM translations WHERE paper_id IN ({marks})", ids)
            }
        return [self._paper(r, frozenset(members.get(int(r["id"]), ())), translations.get(int(r["id"]))) for r in rows]

    def _paper(self, r: sqlite3.Row, collection_ids: frozenset[int], t: sqlite3.Row | None) -> Paper:
        md = Metadata(
            **{n: r[n] for n in _TEXT_FIELDS},
            authors=[Author.from_dict(a) for a in _json(r["authors"], [])],
            year=r["year"],
            source=r["meta_source"],
        )
        translation = None
        if t is not None:
            translation = Translation(
                paper_id=int(t["paper_id"]),
                model=t["model"],
                formality=t["formality"],
                first_page=int(t["first_page"]),
                last_page=t["last_page"],
                options=_json(t["options"], {}),
                segments=int(t["segments"]),
                untranslated=int(t["untranslated"]),
                chars=int(t["chars"]),
                created_at=float(t["created_at"]),
            )
        return Paper(
            id=int(r["id"]),
            sha256=r["sha256"],
            file_name=r["file_name"],
            source_path=r["source_path"],
            page_count=int(r["page_count"]),
            added_at=float(r["added_at"]),
            updated_at=float(r["updated_at"]),
            last_opened_at=r["last_opened_at"],
            favorite=bool(r["favorite"]),
            meta_status=r["meta_status"],
            metadata=md,
            edited=frozenset(_json(r["edited"], [])),
            collection_ids=collection_ids,
            translation=translation,
            root=self.root,
            has_original=(self.root / "files" / str(r["id"]) / "original.pdf").exists(),
            has_translation_file=translation is not None
            and (self.root / "files" / str(r["id"]) / "translated.pdf").exists(),
            trashed_at=r["trashed_at"],
        )


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _json(raw: str | None, default):
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


def _check_status(status: str) -> None:
    if status not in META_STATUSES:
        raise ValueError(f"unknown meta_status: {status}")


def _collection_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("collection name must not be empty")
    return name
