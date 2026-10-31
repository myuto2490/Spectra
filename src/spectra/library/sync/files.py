"""同期フォルダに置くファイルの形と、読み込むときの検証。

同期フォルダの中身は外部入力として扱う: 形・型・長さを検証し、合わないものは捨てる（例外で落ちない）。
"""

from __future__ import annotations

import json
import re

from ..models import Metadata
from .hlc import HLC

FILE_VERSION = 1
MAX_JSON_BYTES = 2 * 1024 * 1024

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")

# 同期する項目（HLC で比べる）。last_opened_at は最大値で比べるので別に扱う
SYNC_FIELDS: tuple[str, ...] = (*Metadata.FIELDS, "source", "favorite")
_TEXT_LIMITS = {"abstract": 50_000, "title": 2_000, "url": 2_000}
_DEFAULT_TEXT_LIMIT = 500
MAX_AUTHORS = 1_000
MAX_NAME = 300


def paper_dir(sha: str) -> str:
    return f"papers/{sha}"


def state_path(sha: str, device: str) -> str:
    return f"papers/{sha}/state.{device}.json"


def translation_json_path(sha: str, device: str) -> str:
    return f"papers/{sha}/translation.{device}.json"


def translation_pdf_path(sha: str, device: str) -> str:
    return f"papers/{sha}/translated.{device}.pdf"


def original_path(sha: str) -> str:
    return f"papers/{sha}/original.pdf"


def collection_path(uid: str, device: str) -> str:
    return f"collections/{uid}/state.{device}.json"


def encode(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")


def decode(data: bytes) -> dict | None:
    if len(data) > MAX_JSON_BYTES:
        return None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


# -- 検証 ------------------------------------------------------------------------


def _hlc(value) -> str | None:
    try:
        HLC.parse(value)
    except (ValueError, TypeError, AttributeError):
        return None
    return str(value)


def clean_field(name: str, value):
    """項目の値を検証し、正しければ正規化した値、だめなら KeyError。"""
    if name == "favorite":
        if isinstance(value, bool):
            return value
        raise KeyError(name)
    if name == "year":
        if value is None or (isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10000):
            return value
        raise KeyError(name)
    if name == "authors":
        if not isinstance(value, list) or len(value) > MAX_AUTHORS:
            raise KeyError(name)
        out = []
        for a in value:
            if not isinstance(a, dict):
                raise KeyError(name)
            family, given = a.get("family", ""), a.get("given", "")
            if not isinstance(family, str) or not isinstance(given, str) or len(family) > MAX_NAME or len(given) > MAX_NAME:
                raise KeyError(name)
            out.append({"family": family, "given": given})
        return out
    if isinstance(value, str) and len(value) <= _TEXT_LIMITS.get(name, _DEFAULT_TEXT_LIMIT):
        return value
    raise KeyError(name)


def parse_paper_state(obj: dict, sha: str) -> dict | None:
    """papers/<sha>/state.<端末>.json の中身を検証して正規化する。"""
    if obj.get("kind") != "paper" or obj.get("sha") != sha or not isinstance(obj.get("device"), str):
        return None
    if not HEX32_RE.match(obj["device"]) or not isinstance(obj.get("v"), int):
        return None
    fields = {}
    for name, entry in (obj.get("fields") or {}).items():
        if name not in SYNC_FIELDS or not isinstance(entry, list) or len(entry) != 3:
            continue  # 知らない項目は無視する
        value, hlc, manual = entry
        hlc = _hlc(hlc)
        if hlc is None or not isinstance(manual, bool):
            continue
        try:
            fields[name] = [clean_field(name, value), hlc, manual]
        except KeyError:
            continue
    life = obj.get("life")
    if not (isinstance(life, list) and len(life) == 2 and isinstance(life[0], bool) and _hlc(life[1])):
        life = None
    file = obj.get("file") or {}
    name = file.get("name") if isinstance(file.get("name"), str) else ""
    pages = file.get("pages") if isinstance(file.get("pages"), int) and not isinstance(file.get("pages"), bool) else 0
    added = file.get("added_at") if isinstance(file.get("added_at"), (int, float)) else None
    opened = obj.get("opened") if isinstance(obj.get("opened"), (int, float)) and not isinstance(obj.get("opened"), bool) else None
    members = {}
    for uid, entry in (obj.get("collections") or {}).items():
        if HEX32_RE.match(str(uid)) and isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], bool) and _hlc(entry[1]):
            members[uid] = [entry[0], entry[1]]
    return {
        "device": obj["device"],
        "fields": fields,
        "life": life,
        "file": {"name": name[:MAX_NAME], "pages": max(0, min(pages, 100_000)), "added_at": added},
        "opened": opened,
        "collections": members,
    }


def parse_translation(obj: dict, sha: str) -> dict | None:
    if obj.get("kind") != "translation" or obj.get("sha") != sha or not isinstance(obj.get("device"), str):
        return None
    if not HEX32_RE.match(obj["device"]) or not _hlc(obj.get("hlc")) or not isinstance(obj.get("alive"), bool):
        return None
    pdf_sha = obj.get("pdf_sha", "")
    info = obj.get("info")
    if obj["alive"] and (not isinstance(pdf_sha, str) or not SHA_RE.match(pdf_sha) or not isinstance(info, dict)):
        return None
    if obj["alive"]:
        try:
            info = {
                "model": str(info.get("model", ""))[:200],
                "formality": str(info.get("formality", ""))[:50],
                "first_page": int(info.get("first_page", 0)),
                "last_page": None if info.get("last_page") is None else int(info["last_page"]),
                "options": info.get("options") if isinstance(info.get("options"), dict) else {},
                "segments": int(info.get("segments", 0)),
                "untranslated": int(info.get("untranslated", 0)),
                "chars": int(info.get("chars", 0)),
                "created_at": float(info.get("created_at", 0)),
            }
        except (TypeError, ValueError):
            return None
    return {"device": obj["device"], "hlc": obj["hlc"], "alive": obj["alive"], "pdf_sha": pdf_sha if obj["alive"] else "", "info": info if obj["alive"] else None}


def parse_collection(obj: dict, uid: str) -> dict | None:
    if obj.get("kind") != "collection" or obj.get("uid") != uid or not isinstance(obj.get("device"), str):
        return None
    if not HEX32_RE.match(obj["device"]):
        return None
    name = obj.get("name")
    if not (isinstance(name, list) and len(name) == 2 and isinstance(name[0], str) and _hlc(name[1])):
        name = None
    else:
        name = [name[0].strip()[:MAX_NAME], name[1]]
    life = obj.get("life")
    if not (isinstance(life, list) and len(life) == 2 and isinstance(life[0], bool) and _hlc(life[1])):
        life = None
    return {"device": obj["device"], "name": name, "life": life}


def translation_rank(entry: dict) -> tuple:
    """2 台で翻訳したときに表示する訳文: 全ページを欠けなく → 訳した範囲が広い → 新しい（HLC）。"""
    info = entry["info"]
    complete = info["first_page"] == 0 and info["last_page"] is None and info["untranslated"] == 0
    last = info["last_page"] if info["last_page"] is not None else 10**9
    return (complete, last - info["first_page"], entry["hlc"])
