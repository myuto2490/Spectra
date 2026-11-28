"""論文の本文の索引（全文検索）。

原文・訳文の PDF から取り出した文字を、SQLite の FTS5（trigram）でページごとに索引にする。
索引はライブラリとは別のファイル（キャッシュ）に置く派生データで、消しても作り直せる。
同期もバックアップもせず、外部にも送らない。

- キーは論文の SHA-256 と種類（原文 / 訳文）。端末ごとに作る。
- 3 文字以上の語は FTS5 の MATCH、1〜2 文字の語（「光」「放射」など）は LIKE で探す（trigram の索引が効かないため）。
- 暗号化・壊れた・文字のない・巨大な PDF でも止まらない。失敗したものは、ファイルが変わるまで試し直さない。
- 索引が壊れた・版が違う・ディスクがいっぱい、のときは FulltextUnavailable を投げる（呼び出し側は書誌の検索だけに退く）。
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

INDEX_VERSION = 1
ORIGINAL, TRANSLATED = "original", "translated"
KINDS = (ORIGINAL, TRANSLATED)

# 状態
OK = "ok"
NO_TEXT = "no_text"  # 文字がない（スキャンの PDF など）
ENCRYPTED = "encrypted"  # パスワードが要る
BROKEN = "broken"  # 開けない・読めない
TOO_LARGE = "too_large"  # 上限まで索引にして打ち切った
FULL = "full"  # 索引の大きさの上限に達したので索引にしなかった（上限を外せば作り直す）
STATUSES = (OK, NO_TEXT, ENCRYPTED, BROKEN, TOO_LARGE, FULL)

MAX_PAGES = 2000  # 1 本の PDF で索引にするページ数の上限
MAX_CHARS = 5_000_000  # 1 本の PDF で索引にする文字数の上限
MIN_TEXT_CHARS = 20  # これより少なければ「文字がない」とみなす
MAX_INDEX_BYTES = 2 * 1024**3  # 索引のファイルの大きさの上限（trigram は本文の数倍になる）
WARN_INDEX_BYTES = int(MAX_INDEX_BYTES * 0.8)

SNIPPET_BEFORE = 30
SNIPPET_AFTER = 70
MAX_SNIPPETS = 3  # 論文ごとに返す抜粋の数
MAX_SNIPPET_PAPERS = 300  # 抜粋を作る論文の数（一致したページの多い順）

_CJK = "　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯"
_HYPHEN_BREAK = re.compile(r"(?<=[a-z])-\n(?=[a-z])")
_CJK_BREAK = re.compile(rf"(?<=[{_CJK}])\n(?=[{_CJK}])")
_SPACES = re.compile(r"\s+")
# 索引の行の rowid は「PDF の番号 << PAGE_BITS | ページ」。FTS5 の列には索引がないので、PDF ごとの削除や
# ページの取り出しは rowid の範囲で行う（列で絞ると全体を読むことになる）
PAGE_BITS = 20


class FulltextUnavailable(Exception):
    """索引を使えない（壊れた・版が違う・ディスクがいっぱい・この SQLite に FTS5 がない）。"""


@dataclass(frozen=True)
class Source:
    """索引にする PDF。"""

    sha256: str
    kind: str  # ORIGINAL / TRANSLATED
    path: Path


@dataclass(frozen=True)
class Snippet:
    kind: str
    page: int  # 0 始まり
    text: str
    spans: tuple[tuple[int, int], ...]  # text の中で一致した範囲（太字にする）


@dataclass
class Match:
    """1 本の論文の本文での一致。"""

    sha256: str
    terms: set[int] = field(default_factory=set)  # 本文で見つかった語の番号
    pages: set[tuple[str, int]] = field(default_factory=set)  # 一致したページ（種類, ページ）
    snippets: list[Snippet] = field(default_factory=list)
    rowids: dict[tuple[str, int], int] = field(default_factory=dict, repr=False)  # ページ → 索引の行

    @property
    def page_count(self) -> int:
        return len(self.pages)


@dataclass(frozen=True)
class Stats:
    documents: int  # 索引にした PDF の数（状態を問わない）
    indexed: int  # 本文を索引にできた PDF の数
    pages: int
    bytes: int
    by_status: dict[str, int]


def normalize(text: str) -> str:
    """索引・検索で使う形：NFKC、行末のハイフンで切れた語をつなぐ、改行を空白に（和文どうしはつなぐ）。"""
    text = unicodedata.normalize("NFKC", text)
    text = _HYPHEN_BREAK.sub("", text)
    text = _CJK_BREAK.sub("", text)
    return _SPACES.sub(" ", text).strip()


def query_terms(text: str) -> list[str]:
    """検索の文字列を語に分ける（空白区切り、AND）。"""
    return [t for t in normalize(text).split(" ") if t]


def extract(path: Path, cancel: threading.Event | None = None) -> tuple[str, list[str]]:
    """PDF の本文をページごとに取り出す。(状態, ページの文字の列)。例外は投げない。"""
    import pymupdf

    try:
        doc = pymupdf.open(str(path))
    except Exception:  # noqa: BLE001 - 壊れた PDF は種類を問わず「壊れた」とする
        log.debug("fulltext: cannot open %s", path.name, exc_info=True)
        return BROKEN, []
    try:
        if doc.needs_pass:
            return ENCRYPTED, []
        pages: list[str] = []
        total = 0
        status = OK
        for i in range(doc.page_count):
            if cancel is not None and cancel.is_set():
                break
            if i >= MAX_PAGES or total >= MAX_CHARS:
                status = TOO_LARGE
                break
            try:
                text = normalize(doc[i].get_text("text"))
            except Exception:  # noqa: BLE001 - 読めないページは空として続ける
                log.debug("fulltext: cannot read page %d of %s", i, path.name, exc_info=True)
                text = ""
            text = text[: MAX_CHARS - total]
            total += len(text)
            pages.append(text)
        if not pages and doc.page_count and status == OK:
            return BROKEN, []
        if total < MIN_TEXT_CHARS and status == OK:
            return NO_TEXT, []
        return status, pages
    except Exception:  # noqa: BLE001
        log.debug("fulltext: cannot read %s", path.name, exc_info=True)
        return BROKEN, []
    finally:
        doc.close()


def _signature(path: Path) -> tuple[int, int] | None:
    """ファイルの (大きさ, 更新時刻 ns)。ファイルがなければ None。"""
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return st.st_size, st.st_mtime_ns


def _is_full(e: sqlite3.Error) -> bool:
    return isinstance(e, sqlite3.OperationalError) and "full" in str(e).lower()


class FulltextIndex:
    """本文の索引。スレッドをまたいで使える（中で 1 本の接続を錠で守る）。"""

    def __init__(self, path: Path, *, max_bytes: int = MAX_INDEX_BYTES):
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._open()

    # -- 開く・作る ----------------------------------------------------------

    def _open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = self._connect()
        except FulltextUnavailable:
            raise
        except sqlite3.DatabaseError as e:
            if _is_full(e):
                raise FulltextUnavailable("disk is full") from e
            # 壊れている・版が違う：派生データなので消して作り直す
            log.warning("fulltext index is unusable, rebuilding: %s", e)
            self._discard_files()
            try:
                self._db = self._connect()
            except sqlite3.Error as e2:
                raise FulltextUnavailable(str(e2)) from e2
        except OSError as e:
            raise FulltextUnavailable(str(e)) from e

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version != INDEX_VERSION or not {"docs", "pages"} <= tables:
                if tables:
                    raise sqlite3.DatabaseError(f"index version {version} != {INDEX_VERSION}")
                self._create(db)
            db.execute("SELECT count(*) FROM docs").fetchone()
            db.execute("SELECT rowid FROM pages LIMIT 1").fetchall()
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def _create(db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE docs (id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL, kind TEXT NOT NULL,"
            " size INTEGER NOT NULL, mtime INTEGER NOT NULL, status TEXT NOT NULL, pages INTEGER NOT NULL,"
            " indexed_at REAL NOT NULL, UNIQUE (sha256, kind))"
        )
        try:
            db.execute("CREATE VIRTUAL TABLE pages USING fts5(text, tokenize='trigram remove_diacritics 1')")
        except sqlite3.OperationalError:
            try:  # remove_diacritics は SQLite 3.45 から
                db.execute("CREATE VIRTUAL TABLE pages USING fts5(text, tokenize='trigram')")
            except sqlite3.OperationalError as e:
                raise FulltextUnavailable(f"FTS5 trigram is not available: {e}") from e
        db.execute(f"PRAGMA user_version={INDEX_VERSION}")

    def _discard_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            p = self.path.with_name(self.path.name + suffix)
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                raise FulltextUnavailable(str(e)) from e

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            raise FulltextUnavailable("index is closed")
        return self._db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                finally:
                    self._db = None

    def clear(self) -> None:
        """索引を消す（ファイルごと消して、空の索引を作り直す）。"""
        with self._lock:
            self.close()
            self._discard_files()
            self._open()

    def delete_files(self) -> None:
        """索引のファイルを消して閉じる（本文検索をオフにしたとき）。"""
        with self._lock:
            self.close()
            self._discard_files()

    # -- 差分 ----------------------------------------------------------------

    def _call(self, fn: Callable[[sqlite3.Connection], object]):
        with self._lock:
            try:
                return fn(self._conn())
            except FulltextUnavailable:
                raise
            except sqlite3.Error as e:
                raise FulltextUnavailable(str(e)) from e

    def known(self) -> dict[tuple[str, str], tuple[int, int, str]]:
        """索引にある PDF：(sha256, kind) → (大きさ, 更新時刻, 状態)。"""
        rows = self._call(lambda db: db.execute("SELECT sha256, kind, size, mtime, status FROM docs").fetchall())
        return {(r[0], r[1]): (r[2], r[3], r[4]) for r in rows}

    def stale(self, sources: Iterable[Source]) -> list[Source]:
        """索引にする（し直す）必要がある PDF。手元にファイルがないものは含めない。"""
        known = self.known()
        out = []
        for s in sources:
            sig = _signature(s.path)
            if sig is None:
                continue  # まだ届いていない（同期）・消えた
            entry = known.get((s.sha256, s.kind))
            if entry is None or (entry[0], entry[1]) != sig or (entry[2] == FULL and not self._near_limit()):
                out.append(s)
        return out

    def prune(self, keep: Iterable[tuple[str, str]]) -> int:
        """keep にない PDF を索引から除く。除いた数を返す。"""
        keep = set(keep)
        gone = [k for k in self.known() if k not in keep]
        for sha, kind in gone:
            self.remove(sha, kind)
        return len(gone)

    def remove(self, sha256: str, kind: str | None = None) -> None:
        def run(db: sqlite3.Connection) -> None:
            db.execute("BEGIN")
            try:
                kinds = KINDS if kind is None else (kind,)
                for k in kinds:
                    row = db.execute("SELECT id FROM docs WHERE sha256 = ? AND kind = ?", (sha256, k)).fetchone()
                    if row is not None:
                        _delete_pages(db, int(row[0]))
                        db.execute("DELETE FROM docs WHERE id = ?", (row[0],))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

        self._call(run)

    # -- 索引にする ----------------------------------------------------------

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal"):
            with contextlib.suppress(OSError):
                total += self.path.with_name(self.path.name + suffix).stat().st_size
        return total

    def _near_limit(self) -> bool:
        return self.size_bytes() >= self.max_bytes

    def index(self, source: Source, cancel: threading.Event | None = None) -> str | None:
        """1 本の PDF を索引にする。状態を返す（ファイルがない・キャンセルしたら None）。"""
        sig = _signature(source.path)
        if sig is None:
            return None
        if self._near_limit():
            status, pages = FULL, []
        else:
            status, pages = extract(source.path, cancel)
        if cancel is not None and cancel.is_set():
            return None
        if _signature(source.path) != sig:
            return None  # 読んでいる間に書き換わった。次の差分で読み直す

        def run(db: sqlite3.Connection) -> None:
            db.execute("BEGIN")
            try:
                db.execute(
                    "INSERT INTO docs (sha256, kind, size, mtime, status, pages, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (sha256, kind) DO UPDATE SET size = excluded.size, mtime = excluded.mtime,"
                    " status = excluded.status, pages = excluded.pages, indexed_at = excluded.indexed_at",
                    (source.sha256, source.kind, sig[0], sig[1], status, len(pages), time.time()),
                )
                doc_id = int(
                    db.execute("SELECT id FROM docs WHERE sha256 = ? AND kind = ?", (source.sha256, source.kind)).fetchone()[0]
                )
                _delete_pages(db, doc_id)
                db.executemany(
                    "INSERT INTO pages (rowid, text) VALUES (?, ?)",
                    [((doc_id << PAGE_BITS) | i, t) for i, t in enumerate(pages) if t],
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

        self._call(run)
        return status

    def optimize(self) -> None:
        """索引を詰める（たくさん追加したあと）。"""
        self._call(lambda db: db.execute("INSERT INTO pages(pages) VALUES ('optimize')"))

    # -- 検索 ----------------------------------------------------------------

    def search(self, terms: list[str], *, cancel: threading.Event | None = None) -> dict[str, Match]:
        """語の列（normalize 済み）を本文で探す。論文（sha256）ごとに、見つかった語・ページ・抜粋を返す。

        語ごとに見つかった論文を返す（AND は呼び出し側で、書誌情報・メモと合わせて決める）。
        """
        terms = [t for t in terms if t]
        if not terms:
            return {}

        def run(db: sqlite3.Connection) -> dict[str, Match]:
            docs = {int(r[0]): (r[1], r[2]) for r in db.execute("SELECT id, sha256, kind FROM docs")}
            found: dict[str, Match] = {}
            for i, term in enumerate(terms):
                if cancel is not None and cancel.is_set():
                    return {}
                if len(term) >= 3:
                    rows = db.execute(
                        "SELECT rowid FROM pages WHERE pages MATCH ?", ('"' + term.replace('"', '""') + '"',)
                    )
                else:
                    pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                    rows = db.execute(
                        "SELECT rowid FROM pages WHERE text LIKE ? ESCAPE '\\'", (pattern,)
                    )
                for (rowid,) in rows:
                    doc = docs.get(rowid >> PAGE_BITS)
                    if doc is None:
                        continue
                    sha, kind = doc
                    page = rowid & ((1 << PAGE_BITS) - 1)
                    m = found.get(sha)
                    if m is None:
                        m = found[sha] = Match(sha)
                    m.terms.add(i)
                    m.pages.add((kind, int(page)))
                    m.rowids[(kind, int(page))] = int(rowid)
            return found

        found = self._call(run)
        if cancel is not None and cancel.is_set():
            return {}
        self._add_snippets(found, terms, cancel)
        return found

    def _add_snippets(self, found: dict[str, Match], terms: list[str], cancel: threading.Event | None) -> None:
        pattern = re.compile("|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.IGNORECASE)
        ranked = sorted(found.values(), key=lambda m: (-len(m.terms), -m.page_count))[:MAX_SNIPPET_PAPERS]
        for m in ranked:
            if cancel is not None and cancel.is_set():
                return
            # 訳文 → 原文の順、ページの順で、語の多いページから
            pages = sorted(m.pages, key=lambda kp: (kp[0] != TRANSLATED, kp[1]))
            # 列の sha256・kind・page には索引がないので、行は rowid で引く
            texts = self._call(
                lambda db, pages=pages[:20], m=m: [
                    (kind, page, (db.execute("SELECT text FROM pages WHERE rowid = ?", (m.rowids[(kind, page)],)).fetchone() or [""])[0])
                    for kind, page in pages
                ]
            )
            texts.sort(key=lambda x: -len({t.casefold() for t in pattern.findall(x[2])}))
            for kind, page, text in texts[:MAX_SNIPPETS]:
                snip = make_snippet(text, pattern)
                if snip is not None:
                    m.snippets.append(Snippet(kind, page, snip[0], snip[1]))

    # -- 統計 ----------------------------------------------------------------

    def stats(self) -> Stats:
        def run(db: sqlite3.Connection) -> Stats:
            by_status = {r[0]: r[1] for r in db.execute("SELECT status, count(*) FROM docs GROUP BY status")}
            docs = sum(by_status.values())
            pages = db.execute("SELECT coalesce(sum(pages), 0) FROM docs").fetchone()[0]
            indexed = by_status.get(OK, 0) + by_status.get(TOO_LARGE, 0)
            return Stats(docs, indexed, int(pages), self.size_bytes(), by_status)

        return self._call(run)


def _delete_pages(db: sqlite3.Connection, doc_id: int) -> None:
    first = doc_id << PAGE_BITS
    db.execute("DELETE FROM pages WHERE rowid BETWEEN ? AND ?", (first, first + (1 << PAGE_BITS) - 1))


def make_snippet(text: str, pattern: re.Pattern) -> tuple[str, tuple[tuple[int, int], ...]] | None:
    """最初の一致の前後を切り出す。(抜粋, 一致した範囲)。一致がなければ None。"""
    first = pattern.search(text)
    if first is None:
        return None
    start = max(0, first.start() - SNIPPET_BEFORE)
    end = min(len(text), first.end() + SNIPPET_AFTER)
    # 単語の途中で切らない（英文）
    if start > 0:
        space = text.find(" ", start, first.start())
        if 0 <= space < first.start() and space - start < 15:
            start = space + 1
    if end < len(text):
        space = text.rfind(" ", first.end(), end)
        if space > first.end() and end - space < 15:
            end = space
    body = text[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    spans = tuple((m.start() + len(prefix), m.end() + len(prefix)) for m in pattern.finditer(body))
    return prefix + body + suffix, spans
