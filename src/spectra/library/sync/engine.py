"""同期の本体。ライブラリと同期フォルダの間で、書き出しと取り込みを行う。

約束:
- 同期フォルダの読み書きは、UI のスレッドから決して行わない（すべてワーカーから呼ぶ）。
- PDF は手元のライブラリにコピーし、sha256 を検証してから使う。
- 時刻の記録（HLC）は sync.sqlite3 に閉じ込め、UI からは見えない。

1 回の同期（sync_now）の順序:
  途中で落ちた反映の後始末 → 他の端末のファイルの読み込み（変わったものだけ）→ 形式の確認
  → ローカルの走査（変更に HLC を付ける）→ 書き出し → マージと反映 → 端末の情報の更新。
ローカルの走査を反映より先に行うのは、まだ時刻の付いていないローカルの変更が反映で上書きされないようにするため。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ... import __version__
from ...translate.errors import CancelledError
from ..errors import HashMismatch, PaperNotFound, RemoteMissing, SyncUnavailable
from ..models import Author, Metadata, Paper, Translation
from ..store import Library
from . import files as F
from .device import DeviceInfo
from .hlc import HLC, ZERO, HybridClock
from .state import SyncState, dumps, loads
from .storage import Progress, SyncStorage

log = logging.getLogger(__name__)

SYNC_DB_NAME = "sync.sqlite3"
FOLDER_NAME = "Spectra Sync"
FORMAT = 1  # 同期フォルダの形式の版（library.sqlite3 の版とは別）

KINDS = ("added", "metadata", "translation", "removed", "collections", "file")


@dataclass(frozen=True)
class RemoteChange:
    """他の端末の変更を反映した結果（UI に知らせる）。

    kind: added / metadata / translation / removed / collections / file
    removed のときの paper_id は削除する前の id、fields は metadata のとき変わった項目。
    """

    kind: str
    sha256: str = ""
    paper_id: int | None = None
    fields: frozenset[str] = frozenset()


@dataclass
class SyncReport:
    """1 回の同期の結果（状態の表示とログ用）。"""

    exported: int = 0
    imported: int = 0
    changes: list[RemoteChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    read_only: bool = False  # 同期フォルダの形式が新しすぎる


def snapshot(p: Paper) -> dict:
    """ライブラリの論文の、同期する値（HLC で比べるもの）。"""
    md = p.metadata
    values = {name: getattr(md, name) for name in Metadata.FIELDS}
    values["authors"] = [a.to_dict() for a in md.authors]
    values["source"] = md.source
    values["favorite"] = p.favorite
    return {"values": values, "edited": sorted(p.edited), "alive": p.trashed_at is None}


def to_metadata(values: dict) -> Metadata:
    kw = {name: values[name] for name in Metadata.FIELDS if name in values}
    kw["authors"] = [Author.from_dict(a) for a in values.get("authors", [])]
    return Metadata(**kw, source=values.get("source", ""))


def translation_info(t: Translation | None) -> dict | None:
    if t is None:
        return None
    return {
        "model": t.model, "formality": t.formality, "first_page": t.first_page, "last_page": t.last_page,
        "options": t.options, "segments": t.segments, "untranslated": t.untranslated, "chars": t.chars,
        "created_at": t.created_at,
    }


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or value is False


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _winner_register(regs: list) -> list | None:
    """[値, HLC] の登録のうち、HLC が最大のもの。"""
    regs = [r for r in regs if r]
    return max(regs, key=lambda r: r[1]) if regs else None


class SyncEngine:
    def __init__(
        self,
        library: Library,
        storage: SyncStorage,
        device: DeviceInfo,
        *,
        clock: HybridClock | None = None,
        state_path: Path | None = None,
    ):
        self.library = library
        self.storage = storage
        self.device = device
        self.state = SyncState(state_path or library.root / SYNC_DB_NAME)
        try:
            saved = self.state.get_meta("clock")
            last = HLC.parse(saved) if saved else ZERO
        except ValueError:
            last = ZERO
        self.clock = clock or HybridClock(device.id, last)
        if clock is not None and last != ZERO:
            self.clock.observe(last)
        self._listeners: list[Callable[[list[RemoteChange]], None]] = []
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()

    # -- 通知 ------------------------------------------------------------------

    def add_remote_listener(self, fn: Callable[[list[RemoteChange]], None]) -> Callable[[], None]:
        """他の端末の変更を反映したとき、1 回の反映でまとめて 1 回だけ呼ぶ（約束は Library.add_listener と同じ）。"""
        with self._lock:
            self._listeners.append(fn)

        def remove() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)

        return remove

    def _notify(self, changes: list[RemoteChange]) -> None:
        if not changes:
            return
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(list(changes))
            except Exception:
                log.exception("remote listener failed")

    # -- 同期 ------------------------------------------------------------------

    def sync_now(self, cancel: threading.Event | None = None) -> SyncReport:
        """ローカルを走査して書き出し、他の端末の変更を反映する（ワーカーから呼ぶ）。"""
        with self._sync_lock:
            if not self.storage.available():
                raise SyncUnavailable()
            report = SyncReport()
            self._recover_pending()
            listing = self.storage.list()
            names = {e.path for e in listing}
            remote = self._refresh_remote(listing, report)
            if self._newer_format(remote):
                report.read_only = True
                return report
            if "format.json" not in names:
                self.storage.write_atomic("format.json", F.encode({"format": FORMAT}))
            self._check_cancel(cancel)
            self.backup_before_first_sync()
            self._first_join(remote)
            self._scan_local()
            self._check_cancel(cancel)
            self._export(names, report)
            changes = self._merge(remote, report)
            self._write_device(names)
            self.state.set_meta("clock", str(self.clock.last))
            report.changes = changes
        self._notify(changes)
        return report

    @staticmethod
    def _check_cancel(cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise CancelledError

    # -- 他の端末のファイル ------------------------------------------------------

    def _refresh_remote(self, listing, report: SyncReport) -> dict:
        """変わった JSON だけを読み直し、解析済みの中身（他の端末の分）を返す。"""
        known = {r["path"]: r for r in self.state.query("SELECT path, size, mtime FROM remote_files")}
        seen = set()
        for e in listing:
            key = self._classify(e.path)
            if key is None:
                continue
            seen.add(e.path)
            row = known.get(e.path)
            if row is not None and row["size"] == e.size and row["mtime"] == e.mtime:
                continue
            try:
                obj = F.decode(self.storage.read_bytes(e.path, limit=F.MAX_JSON_BYTES))
                data = self._parse(key, obj) if obj is not None else None
            except (RemoteMissing, ValueError):
                data = None
            except SyncUnavailable as err:
                report.errors.append(f"{e.path}: {err}")
                continue
            with self.state.tx() as db:
                db.execute(
                    "INSERT OR REPLACE INTO remote_files (path, size, mtime, data) VALUES (?, ?, ?, ?)",
                    (e.path, e.size, e.mtime, dumps(data) if data is not None else None),
                )
        gone = set(known) - seen
        if gone:
            with self.state.tx() as db:
                db.executemany("DELETE FROM remote_files WHERE path = ?", [(p,) for p in gone])

        remote: dict = {"papers": {}, "translations": {}, "collections": {}, "devices": []}
        areas = {"paper": "papers", "translation": "translations", "collection": "collections"}
        for r in self.state.query("SELECT data FROM remote_files WHERE data IS NOT NULL"):
            data = loads(r["data"])
            if not isinstance(data, dict):
                continue
            if data.get("_kind") == "device":
                remote["devices"].append(data)
                continue
            if data.get("device") == self.device.id:
                continue  # 自分のファイル（とその競合コピー）は sync.sqlite3 の記録が正しい
            self._observe(data)
            remote[areas[data["_kind"]]].setdefault(data["_key"], []).append(data)
        return remote

    @staticmethod
    def _classify(path: str) -> tuple[str, str] | None:
        """解析する JSON なら (場所, キー)。名前は問わない（競合コピーも読む）。隠しファイル・一時ファイルは除く。"""
        parts = path.split("/")
        if not path.endswith(".json") or parts[-1].startswith("."):
            return None
        if len(parts) == 3 and parts[0] == "papers" and F.SHA_RE.match(parts[1]):
            return ("papers", parts[1])
        if len(parts) == 3 and parts[0] == "collections" and F.HEX32_RE.match(parts[1]):
            return ("collections", parts[1])
        if len(parts) == 2 and parts[0] == "devices":
            return ("devices", "")
        return None

    @staticmethod
    def _parse(key: tuple[str, str], obj: dict) -> dict | None:
        area, k = key
        if area == "papers":
            if obj.get("kind") == "paper":
                data, kind = F.parse_paper_state(obj, k), "paper"
            elif obj.get("kind") == "translation":
                data, kind = F.parse_translation(obj, k), "translation"
            else:
                return None
        elif area == "collections":
            data, kind = F.parse_collection(obj, k), "collection"
        else:
            # 端末の情報は形式の版だけを使う。古い・別の書き方（"kind" なし、"id"）も受け付ける
            fmt = obj.get("format")
            if not isinstance(fmt, int) or isinstance(fmt, bool):
                return None
            return {"_kind": "device", "device": str(obj.get("device") or obj.get("id") or "")[:32], "format": fmt}
        if data is None:
            return None
        return {**data, "_kind": kind, "_key": k}

    def _observe(self, data: dict) -> None:
        stamps = [e[1] for e in data.get("fields", {}).values()]
        for key in ("life", "name"):
            if data.get(key):
                stamps.append(data[key][1])
        stamps += [e[1] for e in data.get("collections", {}).values()]
        if data.get("hlc"):
            stamps.append(data["hlc"])
        for s in stamps:
            with contextlib.suppress(ValueError):
                self.clock.observe(HLC.parse(s))

    def _newer_format(self, remote: dict) -> bool:
        """ほかの端末が、この版より新しい形式を書いていれば True（同期を読み取り専用にする）。"""
        return any(d["format"] > FORMAT for d in remote["devices"] if d.get("device") != self.device.id)

    def _write_device(self, names: set[str]) -> None:
        """端末の情報。中身が変わったときだけ書く（変更がなければ同期フォルダに書かない）。"""
        body = {"kind": "device", "device": self.device.id, "name": self.device.name, "app": __version__, "format": FORMAT}
        path = f"devices/{self.device.id}.json"
        if path in names and self.state.get_meta("device") == dumps(body):
            return
        self.storage.write_atomic(path, F.encode(body))
        self.state.set_meta("device", dumps(body))

    # -- 初回の接続 ----------------------------------------------------------------

    def backup_before_first_sync(self) -> Path | None:
        """初めて同期する前に一度だけ、手元のライブラリの DB をバックアップしてその場所を返す（sync_now も最初に呼ぶ）。

        場所は library_dir() の隣の library-backups/<日時>/。PDF はコピーしない。2 回目以降と、空のライブラリでは None。
        戻すときは、Library を閉じてから spectra.library.store.restore_backup(path) を呼ぶ。
        """
        if self.state.get_meta("backup") or self.state.get_meta("joined"):
            return None
        if not self.library.papers() and not self.library.trashed_papers() and not self.library.collections():
            self.state.set_meta("backup", "-")
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = self.library.root.parent / "library-backups" / stamp
        n = 1
        while folder.exists():
            n += 1
            folder = folder.with_name(f"{stamp}-{n}")
        self.library.backup_to(folder)
        self.state.set_meta("backup", str(folder))
        return folder

    @property
    def backup_path(self) -> Path | None:
        """初回の同期の前に作ったバックアップ（なければ None）。"""
        value = self.state.get_meta("backup")
        return Path(value) if value and value != "-" else None

    def _first_join(self, remote: dict) -> None:
        """初回だけ、同じ名前のコレクションを 1 つにまとめる（uid のない手元のコレクションを、同期先の uid に対応づける）。"""
        if self.state.get_meta("joined"):
            return
        names: dict[str, str] = {}
        for uid, entries in remote["collections"].items():
            name = _winner_register([e["name"] for e in entries if e.get("name")])
            life = _winner_register([e["life"] for e in entries if e.get("life")])
            if name and (life is None or life[0]):
                names.setdefault(name[0].casefold(), uid)
        mapped = {r["local_id"] for r in self.state.query("SELECT local_id FROM collections WHERE local_id IS NOT NULL")}
        with self.state.tx() as db:
            for c in self.library.collections():
                uid = names.pop(c.name.casefold(), None)
                if c.id in mapped or uid is None:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO collections (uid, local_id, baseline, own, exported) VALUES (?, ?, ?, ?, 1)",
                    (uid, c.id, dumps({"name": c.name, "alive": True}), dumps({"name": None, "life": None})),
                )
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('joined', '1')")

    # -- ローカルの走査 ------------------------------------------------------------

    def _scan_local(self) -> None:
        """ライブラリと基準を比べ、変わった項目にだけ HLC を付ける（時刻は updated_at を使う）。"""
        self._scan_collections()
        papers = self.library.papers() + self.library.trashed_papers()
        members = self._scan_members(papers)
        rows = {r["sha"]: r for r in self.state.query("SELECT sha, baseline, own FROM papers")}
        trows = {r["sha"]: r for r in self.state.query("SELECT sha, baseline, own, applied FROM translations")}
        present = set()
        with self.state.tx() as db:
            for p in papers:
                present.add(p.sha256)
                snap = snapshot(p)
                row = rows.get(p.sha256)
                base = loads(row["baseline"]) if row else None
                own = loads(row["own"], {}) if row else {}
                own.setdefault("fields", {})
                changed = p.sha256 in members
                if not isinstance(base, dict) or base.get("gone"):
                    at = p.updated_at
                    for name, value in snap["values"].items():
                        if not _empty(value):  # 初めての登録では空の値を書かない（ほかの端末の値を消さないように）
                            own["fields"][name] = [value, str(self.clock.tick(at)), name in p.edited]
                    own["life"] = [snap["alive"], str(self.clock.tick(p.added_at if snap["alive"] else p.trashed_at))]
                    own["file"] = {"name": p.file_name, "pages": p.page_count, "added_at": p.added_at}
                    changed = True
                else:
                    stamp = None
                    old_edited = set(base.get("edited", []))
                    for name, value in snap["values"].items():
                        newly_manual = name in p.edited and name not in old_edited
                        if base["values"].get(name) != value or newly_manual:
                            stamp = stamp or str(self.clock.tick(p.updated_at))
                            own["fields"][name] = [value, stamp, name in p.edited]
                            changed = True
                    if base.get("alive") != snap["alive"]:
                        at = p.updated_at if snap["alive"] else p.trashed_at
                        own["life"] = [snap["alive"], str(self.clock.tick(at))]
                        changed = True
                    if "file" not in own:
                        own["file"] = {"name": p.file_name, "pages": p.page_count, "added_at": p.added_at}
                if p.last_opened_at and (own.get("opened") or 0) < p.last_opened_at:
                    own["opened"] = p.last_opened_at
                    changed = True
                if changed:
                    db.execute(
                        "INSERT OR REPLACE INTO papers (sha, baseline, own, exported) VALUES (?, ?, ?, 0)",
                        (p.sha256, dumps(snap), dumps(own)),
                    )
                elif base != snap:
                    db.execute("UPDATE papers SET baseline = ? WHERE sha = ?", (dumps(snap), p.sha256))
                self._scan_translation(db, p, trows.get(p.sha256))
            # ライブラリから完全に消えた論文は、削除の記録（tombstone）を書く
            for sha, row in rows.items():
                base = loads(row["baseline"])
                if sha in present or not isinstance(base, dict) or base.get("gone"):
                    continue
                own = loads(row["own"], {})
                own["life"] = [False, str(self.clock.tick())]
                db.execute(
                    "UPDATE papers SET baseline = ?, own = ?, exported = 0 WHERE sha = ?",
                    (dumps({"gone": True}), dumps(own), sha),
                )

    def _scan_translation(self, db, p: Paper, row) -> None:
        info = translation_info(p.translation)
        base = loads(row["baseline"]) if row else None
        if row is not None and base == info:
            return
        own = loads(row["own"], {}) if row else {}
        applied = loads(row["applied"], {}) if row else {}
        exported = 0
        if info is not None and p.has_translation_file:
            pdf_sha = _sha_file(p.translated_path)
            own = {"hlc": str(self.clock.tick(info["created_at"])), "alive": True, "pdf_sha": pdf_sha, "info": info}
            applied = {"device": self.device.id, "pdf_sha": pdf_sha}
        elif info is None and base is not None:
            own = {"hlc": str(self.clock.tick()), "alive": False, "pdf_sha": "", "info": None}
            applied = {}
        elif info is None:
            return
        else:
            exported = 1  # 訳文の情報はあるが手元にファイルがない: 書き出すものはない
        db.execute(
            "INSERT OR REPLACE INTO translations (sha, baseline, own, applied, exported) VALUES (?, ?, ?, ?, ?)",
            (p.sha256, dumps(info), dumps(own), dumps(applied), exported),
        )

    def _scan_collections(self) -> None:
        local = {c.id: c for c in self.library.collections()}
        rows = {r["local_id"]: r for r in self.state.query("SELECT uid, local_id, baseline, own FROM collections")}
        with self.state.tx() as db:
            for cid, c in local.items():
                row = rows.get(cid)
                if row is None:
                    own = {"name": [c.name, str(self.clock.tick())], "life": [True, str(self.clock.tick())]}
                    db.execute(
                        "INSERT INTO collections (uid, local_id, baseline, own, exported) VALUES (?, ?, ?, ?, 0)",
                        (uuid.uuid4().hex, cid, dumps({"name": c.name, "alive": True}), dumps(own)),
                    )
                    continue
                base, own = loads(row["baseline"], {}), loads(row["own"], {})
                if base.get("name") != c.name:
                    own["name"] = [c.name, str(self.clock.tick())]
                    db.execute(
                        "UPDATE collections SET baseline = ?, own = ?, exported = 0 WHERE uid = ?",
                        (dumps({"name": c.name, "alive": True}), dumps(own), row["uid"]),
                    )
            for cid, row in rows.items():
                if cid is None or cid in local:
                    continue
                base, own = loads(row["baseline"], {}), loads(row["own"], {})
                if base.get("alive"):
                    own["life"] = [False, str(self.clock.tick())]
                    db.execute(
                        "UPDATE collections SET local_id = NULL, baseline = ?, own = ?, exported = 0 WHERE uid = ?",
                        (dumps({"name": base.get("name", ""), "alive": False}), dumps(own), row["uid"]),
                    )

    def _scan_members(self, papers: list[Paper]) -> set[str]:
        """コレクションの所属の変更に HLC を付け、変わった論文の sha を返す。"""
        uid_of = {
            r["local_id"]: r["uid"]
            for r in self.state.query("SELECT uid, local_id FROM collections WHERE local_id IS NOT NULL")
        }
        current = {(uid_of[cid], p.sha256) for p in papers for cid in p.collection_ids if cid in uid_of}
        rows = {(r["uid"], r["sha"]): r for r in self.state.query("SELECT uid, sha, baseline FROM members")}
        changed: set[str] = set()
        with self.state.tx() as db:
            for key in current | {k for k, r in rows.items() if r["baseline"]}:
                now_in = key in current
                row = rows.get(key)
                if row is not None and bool(row["baseline"]) == now_in:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO members (uid, sha, baseline, own) VALUES (?, ?, ?, ?)",
                    (key[0], key[1], int(now_in), dumps([now_in, str(self.clock.tick())])),
                )
                changed.add(key[1])
        return changed

    # -- 書き出し ------------------------------------------------------------------

    def _own_members(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.state.query("SELECT uid, sha, own FROM members"):
            own = loads(r["own"])
            if own:
                out.setdefault(r["sha"], {})[r["uid"]] = own
        return out

    def _export(self, names: set[str], report: SyncReport) -> None:
        members = self._own_members()
        for r in self.state.query("SELECT sha, own FROM papers WHERE exported = 0"):
            sha, own = r["sha"], loads(r["own"], {})
            paper = self.library.find_by_sha(sha)
            if paper is not None and paper.has_original and F.original_path(sha) not in names:
                self.storage.put_file(F.original_path(sha), paper.pdf_path)
                names.add(F.original_path(sha))
            body = {
                "kind": "paper", "v": F.FILE_VERSION, "device": self.device.id, "sha": sha,
                "fields": own.get("fields", {}), "life": own.get("life"), "file": own.get("file", {}),
                "opened": own.get("opened"), "collections": members.get(sha, {}),
            }
            self.storage.write_atomic(F.state_path(sha, self.device.id), F.encode(body))
            with self.state.tx() as db:
                db.execute("UPDATE papers SET exported = 1 WHERE sha = ?", (sha,))
            report.exported += 1
        for r in self.state.query("SELECT sha, own FROM translations WHERE exported = 0"):
            sha, own = r["sha"], loads(r["own"], {})
            if own:
                pdf = F.translation_pdf_path(sha, self.device.id)
                if own.get("alive"):
                    paper = self.library.find_by_sha(sha)
                    if paper is None or not paper.has_translation_file:
                        continue
                    self.storage.put_file(pdf, paper.translated_path)
                elif pdf in names:
                    self.storage.delete(pdf)
                body = {"kind": "translation", "v": F.FILE_VERSION, "device": self.device.id, "sha": sha, **own}
                self.storage.write_atomic(F.translation_json_path(sha, self.device.id), F.encode(body))
                report.exported += 1
            with self.state.tx() as db:
                db.execute("UPDATE translations SET exported = 1 WHERE sha = ?", (sha,))
        for r in self.state.query("SELECT uid, own FROM collections WHERE exported = 0"):
            own = loads(r["own"], {})
            if own.get("name") or own.get("life"):
                body = {
                    "kind": "collection", "v": F.FILE_VERSION, "device": self.device.id, "uid": r["uid"],
                    "name": own.get("name"), "life": own.get("life"),
                }
                self.storage.write_atomic(F.collection_path(r["uid"], self.device.id), F.encode(body))
                report.exported += 1
            with self.state.tx() as db:
                db.execute("UPDATE collections SET exported = 1 WHERE uid = ?", (r["uid"],))

    # -- マージと反映 --------------------------------------------------------------

    def _merge(self, remote: dict, report: SyncReport) -> list[RemoteChange]:
        changes: list[RemoteChange] = []
        changes += self._merge_collections(remote)
        rows = {r["sha"]: r for r in self.state.query("SELECT sha, own FROM papers")}
        own_members = self._own_members()
        for sha in sorted(set(remote["papers"]) | set(rows)):
            row = rows.get(sha)
            own = loads(row["own"], {}) if row else {}
            entries = remote["papers"].get(sha, [])
            if not entries:
                continue  # 自分の記録だけなら、ライブラリはすでにその状態
            try:
                changes += self._merge_paper(sha, own, entries, own_members.get(sha, {}), report)
            except PaperNotFound:
                continue
        changes += self._merge_translations(remote)
        return changes

    def _merge_paper(self, sha: str, own: dict, entries: list[dict], own_members: dict, report: SyncReport) -> list[RemoteChange]:
        lives = ([own["life"]] if own.get("life") else []) + [e["life"] for e in entries if e.get("life")]
        life = _winner_register(lives)
        if life is None:
            return []
        alive = life[0]
        regs: dict[str, list] = {}
        for source in [own.get("fields", {})] + [e["fields"] for e in entries]:
            for name, reg in source.items():
                cur = regs.get(name)
                # 手で直した値を優先し、同じなら HLC の新しいほう
                if cur is None or (reg[2], reg[1]) > (cur[2], cur[1]):
                    regs[name] = reg
        opened = max([x for x in [own.get("opened")] + [e.get("opened") for e in entries] if x] or [0]) or None

        paper = self.library.find_by_sha(sha)
        changes: list[RemoteChange] = []
        if paper is None:
            if not alive:
                return []
            info = min((e["file"] for e in entries), key=lambda f: f.get("added_at") or float("inf"))
            with self.library.quiet():
                paper = self.library.sync_insert(
                    sha, info.get("name") or f"{sha[:12]}.pdf", info.get("pages", 0), info.get("added_at") or time.time()
                )
            changes.append(RemoteChange("added", sha, paper.id))
            report.imported += 1
        snap = snapshot(paper)
        target_values = dict(snap["values"])
        for name, reg in regs.items():
            target_values[name] = reg[0]
        target_edited = {n for n in snap["edited"] if n not in regs} | {n for n, r in regs.items() if r[2]}
        target = {"values": target_values, "edited": sorted(target_edited), "alive": alive}
        self._set_pending(sha, target)
        with self.library.quiet():
            if alive and paper.trashed_at is not None:
                self.library.restore(paper.id)
                changes.append(RemoteChange("added", sha, paper.id))
            elif not alive and paper.trashed_at is None:
                self.library.trash(paper.id)
                changes.append(RemoteChange("removed", sha, paper.id))
            diff = frozenset(n for n in target_values if target_values[n] != snap["values"].get(n))
            newer_opened = opened is not None and (paper.last_opened_at or 0) < opened
            if diff or set(target_edited) != set(snap["edited"]) or newer_opened:
                self.library.sync_apply(
                    paper.id,
                    metadata=to_metadata(target_values),
                    edited=target_edited,
                    favorite=bool(target_values.get("favorite")),
                    last_opened_at=max(opened or 0, paper.last_opened_at or 0) or None,
                )
                if diff and not any(c.kind == "added" for c in changes):
                    changes.append(RemoteChange("metadata", sha, paper.id, diff))
        changes += self._apply_members(paper, own_members, entries)
        self._finish_pending(sha, target)
        return changes

    def _apply_members(self, paper: Paper, own_members: dict, entries: list[dict]) -> list[RemoteChange]:
        regs: dict[str, list] = dict(own_members)
        for e in entries:
            for uid, reg in e.get("collections", {}).items():
                if uid not in regs or reg[1] > regs[uid][1]:
                    regs[uid] = reg
        local_of = {
            r["uid"]: r["local_id"]
            for r in self.state.query("SELECT uid, local_id FROM collections WHERE local_id IS NOT NULL")
        }
        moves = []
        for uid, (present, _hlc) in regs.items():
            cid = local_of.get(uid)
            if cid is None or present == (cid in paper.collection_ids):
                continue
            moves.append((uid, cid, present))
        if not moves:
            return []
        with self.library.quiet():
            for _uid, cid, present in moves:
                if present:
                    self.library.add_to_collection(cid, [paper.id])
                else:
                    self.library.remove_from_collection(cid, [paper.id])
        with self.state.tx() as db:
            for uid, _cid, present in moves:
                row = db.execute("SELECT own FROM members WHERE uid = ? AND sha = ?", (uid, paper.sha256)).fetchone()
                db.execute(
                    "INSERT OR REPLACE INTO members (uid, sha, baseline, own) VALUES (?, ?, ?, ?)",
                    (uid, paper.sha256, int(present), row["own"] if row else "null"),
                )
        return [RemoteChange("collections", paper.sha256, paper.id)]

    def _merge_collections(self, remote: dict) -> list[RemoteChange]:
        rows = {r["uid"]: r for r in self.state.query("SELECT uid, local_id, own, exported FROM collections")}
        changed = False
        for uid in sorted(set(remote["collections"]) | set(rows)):
            entries = remote["collections"].get(uid, [])
            if not entries:
                continue
            row = rows.get(uid)
            own = loads(row["own"], {}) if row else {}
            name = _winner_register(([own["name"]] if own.get("name") else []) + [e["name"] for e in entries if e.get("name")])
            life = _winner_register(([own["life"]] if own.get("life") else []) + [e["life"] for e in entries if e.get("life")])
            if life is None:
                continue
            local_id = row["local_id"] if row else None
            existing = {c.id: c for c in self.library.collections()}
            label = (name[0] if name else "") or "コレクション"
            with self.library.quiet():
                if life[0] and (local_id is None or local_id not in existing):
                    local_id = self.library.create_collection(label).id
                    changed = True
                elif life[0] and existing[local_id].name != label:
                    self.library.rename_collection(local_id, label)
                    changed = True
                elif not life[0] and local_id in existing:
                    self.library.delete_collection(local_id)
                    local_id = None
                    changed = True
            with self.state.tx() as db:
                db.execute(
                    "INSERT OR REPLACE INTO collections (uid, local_id, baseline, own, exported) VALUES (?, ?, ?, ?, ?)",
                    (uid, local_id, dumps({"name": label, "alive": life[0]}),
                     dumps(own or {"name": None, "life": None}), row["exported"] if row else 1),
                )
        return [RemoteChange("collections")] if changed else []

    def _merge_translations(self, remote: dict) -> list[RemoteChange]:
        rows = {r["sha"]: r for r in self.state.query("SELECT sha, own, applied, exported FROM translations")}
        changes = []
        for sha in sorted(remote["translations"]):
            row = rows.get(sha)
            own = loads(row["own"], {}) if row else {}
            applied = loads(row["applied"], {}) if row else {}
            entries = list(remote["translations"][sha])
            if own:
                entries.append({**own, "device": self.device.id})
            deleted = max([e["hlc"] for e in entries if not e["alive"]] or [""])
            alive = [e for e in entries if e["alive"] and e["hlc"] > deleted]
            winner = max(alive, key=F.translation_rank) if alive else None
            paper = self.library.find_by_sha(sha)
            if paper is None:
                continue
            if winner is None:
                if paper.translation is None:
                    continue
                with self.library.quiet():
                    self.library.sync_set_translation(paper.id, None)
                new_applied, base = {}, None
            else:
                if applied.get("device") == winner["device"] and applied.get("pdf_sha") == winner["pdf_sha"]:
                    continue
                new_applied = {"device": winner["device"], "pdf_sha": winner["pdf_sha"]}
                if winner["device"] == self.device.id:
                    base = translation_info(paper.translation)
                else:
                    info = Translation(paper_id=paper.id, **winner["info"])
                    with self.library.quiet():
                        self.library.sync_set_translation(paper.id, info)
                    base = translation_info(info)
                    if own.get("alive"):
                        # 自分の訳文は負けた: 各端末は自分のファイルだけを消す（手元の訳文は上で別物に置き換わった）
                        self.storage.delete(F.translation_pdf_path(sha, self.device.id))
                        self.storage.delete(F.translation_json_path(sha, self.device.id))
                        own = {}
            with self.state.tx() as db:
                db.execute(
                    "INSERT OR REPLACE INTO translations (sha, baseline, own, applied, exported) VALUES (?, ?, ?, ?, ?)",
                    (sha, dumps(base), dumps(own), dumps(new_applied), row["exported"] if row else 1),
                )
            changes.append(RemoteChange("translation", sha, paper.id))
        return changes

    # -- 反映の途中で落ちたとき（エコーの防止） ---------------------------------------

    def _set_pending(self, sha: str, target: dict) -> None:
        with self.state.tx() as db:
            db.execute("INSERT OR REPLACE INTO pending_apply (sha, target) VALUES (?, ?)", (sha, dumps(target)))

    def _finish_pending(self, sha: str, target: dict) -> None:
        with self.state.tx() as db:
            self._store_baseline(db, sha, target)
            db.execute("DELETE FROM pending_apply WHERE sha = ?", (sha,))

    @staticmethod
    def _store_baseline(db, sha: str, target: dict) -> None:
        row = db.execute("SELECT sha FROM papers WHERE sha = ?", (sha,)).fetchone()
        if row is None:
            db.execute("INSERT INTO papers (sha, baseline, own, exported) VALUES (?, ?, '{}', 1)", (sha, dumps(target)))
        else:
            db.execute("UPDATE papers SET baseline = ? WHERE sha = ?", (dumps(target), sha))

    def _recover_pending(self) -> None:
        """反映の途中で落ちていたら、ライブラリの今の値と照らして基準を直す（反映が済んでいた部分だけを反映済みとみなす）。"""
        for r in self.state.query("SELECT sha, target FROM pending_apply"):
            target = loads(r["target"], {})
            paper = self.library.find_by_sha(r["sha"])
            with self.state.tx() as db:
                if paper is not None and target:
                    snap = snapshot(paper)
                    row = db.execute("SELECT baseline FROM papers WHERE sha = ?", (r["sha"],)).fetchone()
                    base = loads(row["baseline"]) if row else None
                    fixed = dict(base) if isinstance(base, dict) and not base.get("gone") else dict(snap)
                    if snap["values"] == target.get("values"):
                        fixed["values"], fixed["edited"] = snap["values"], snap["edited"]
                    if snap["alive"] == target.get("alive"):
                        fixed["alive"] = snap["alive"]
                    self._store_baseline(db, r["sha"], fixed)
                db.execute("DELETE FROM pending_apply WHERE sha = ?", (r["sha"],))

    # -- 取得と、手元のコピーの削除 -----------------------------------------------------

    def _applied_translation(self, sha: str) -> dict:
        rows = self.state.query("SELECT applied FROM translations WHERE sha = ?", (sha,))
        return loads(rows[0]["applied"], {}) if rows else {}

    def fetch(
        self,
        paper_id: int,
        kind: str,
        progress: Progress | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        """原文（original）か訳文（translation）を手元に取得してパスを返す（ワーカーから呼ぶ）。

        手元にあれば同期フォルダに触らずにすぐ返す。失敗は SyncUnavailable / RemoteMissing / HashMismatch、
        論文が無ければ PaperNotFound、キャンセルは CancelledError。
        """
        if kind not in ("original", "translation"):
            raise ValueError(f"unknown kind: {kind}")
        paper = self.library.get(paper_id)
        if paper is None:
            raise PaperNotFound(f"論文が見つかりません（id={paper_id}）")
        if kind == "original":
            if paper.has_original:
                return paper.pdf_path
            src, expected, dest = F.original_path(paper.sha256), paper.sha256, paper.pdf_path
        else:
            if paper.has_translation_file:
                return paper.translated_path
            applied = self._applied_translation(paper.sha256) if paper.translation is not None else {}
            if not applied.get("device"):
                raise RemoteMissing()
            src = F.translation_pdf_path(paper.sha256, applied["device"])
            expected, dest = applied["pdf_sha"], paper.translated_path
        self._check_cancel(cancel)
        if not self.storage.available():
            raise SyncUnavailable()
        if not self.storage.exists(src):
            if self.storage.request_download(src):
                raise RemoteMissing(hint="iCloud からファイルを取り寄せています。しばらく待ってから、もう一度開いてください。")
            raise RemoteMissing()
        tmp = dest.with_name(dest.name + ".tmp")
        actual = self.storage.copy_to(src, tmp, progress, cancel)
        if actual != expected:
            tmp.unlink(missing_ok=True)
            raise HashMismatch()
        os.replace(tmp, dest)
        self._notify([RemoteChange("file", paper.sha256, paper.id)])
        return dest

    def evict(self, paper_id: int) -> None:
        """このパソコンのコピーを削除する（クラウドには残す）。同期先にあるファイルだけを消す（唯一のコピーは消さない）。"""
        paper = self.library.get(paper_id)
        if paper is None:
            raise PaperNotFound(f"論文が見つかりません（id={paper_id}）")
        if not self.storage.available():
            raise SyncUnavailable()
        if paper.has_original:
            if not self.storage.exists(F.original_path(paper.sha256)):
                raise RemoteMissing("同期先に原文がまだないため、このパソコンのコピーを削除できません。")
            paper.pdf_path.unlink(missing_ok=True)
        if paper.has_translation_file:
            applied = self._applied_translation(paper.sha256)
            if applied.get("device") and self.storage.exists(F.translation_pdf_path(paper.sha256, applied["device"])):
                paper.translated_path.unlink(missing_ok=True)
        self._notify([RemoteChange("file", paper.sha256, paper.id)])

    def close(self) -> None:
        self.state.close()
