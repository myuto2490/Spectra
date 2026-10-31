"""同期フォルダに置くファイルの形と、読み込むときの検証。

同期フォルダの中身は外部入力として扱う: 形・型・長さを検証し、合わないものは捨てる（例外で落ちない）。
"""

from __future__ import annotations

import json
import math
import re
import unicodedata

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
MAX_OPTIONS_BYTES = 10_000
_MAX_TIME = 4_102_444_800.0  # 2100 年。時刻として受け付ける上限（無限大・巨大な値で表示が落ちないように）
_KEEP = {"\n", "\t"}
# ゼロ幅非接合子・接合子は、ペルシア語やインド系の文字、絵文字の組み合わせに必要なので、文章では残す
_JOINERS = {"\u200c", "\u200d"}


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
        obj = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):  # 深すぎる入れ子も捨てる
        return None
    return obj if isinstance(obj, dict) else None


# -- 検証 ------------------------------------------------------------------------


def _reject_constant(name: str):
    raise ValueError(f"{name} is not allowed")  # NaN・Infinity は JSON の規格外なので受け付けない


def clean_text(value: str, *, strict: bool = False) -> str:
    """ほかの端末から来た文字列から、見えない文字を除く（見た目が同じで中身の違う文字列によるなりすましを防ぐ）。

    除くのは、制御文字（Unicode の一般カテゴリ Cc。改行とタブは残す）と書式文字（Cf: ゼロ幅スペース U+200B〜U+200F、
    U+2060〜U+2064、BOM U+FEFF、表示の向きを入れ替える U+202A〜U+202E・U+2066〜U+2069、ソフトハイフンなど）。
    ゼロ幅非接合子・接合子（U+200C・U+200D）は、文章では残し、strict（端末名・ファイル名）では除く。
    """
    keep = _KEEP if strict else _KEEP | _JOINERS
    return "".join(c for c in value if c in keep or unicodedata.category(c) not in ("Cc", "Cf"))


def _time(value) -> float | None:
    """時刻（秒）として妥当な値だけを通す。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 < value < _MAX_TIME else None


def _count(value, default: int = 0, upper: int = 10**9) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(0, min(value, upper))


def clean_file_name(value) -> str:
    """ほかの端末から来たファイル名。表示と保存の候補にだけ使うので、最後の要素だけを残し、危ない文字を除く。"""
    if not isinstance(value, str):
        return ""
    name = clean_text(value, strict=True).replace("\\", "/").rsplit("/", 1)[-1].strip().strip(".")
    return name[:MAX_NAME]


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
            out.append({"family": clean_text(family), "given": clean_text(given)})
        return out
    if isinstance(value, str) and len(value) <= _TEXT_LIMITS.get(name, _DEFAULT_TEXT_LIMIT):
        return clean_text(value)
    raise KeyError(name)


def parse_paper_state(obj: dict, sha: str) -> dict | None:
    """papers/<sha>/state.<端末>.json の中身を検証して正規化する。"""
    if obj.get("kind") != "paper" or obj.get("sha") != sha or not isinstance(obj.get("device"), str):
        return None
    if not HEX32_RE.match(obj["device"]) or not isinstance(obj.get("v"), int):
        return None
    fields = {}
    fields_raw = obj.get("fields") if isinstance(obj.get("fields"), dict) else {}
    for name, entry in fields_raw.items():
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
    file = obj.get("file") if isinstance(obj.get("file"), dict) else {}
    name = clean_file_name(file.get("name"))
    pages = _count(file.get("pages"), upper=100_000)
    added = _time(file.get("added_at"))
    opened = _time(obj.get("opened"))
    members = {}
    members_raw = obj.get("collections") if isinstance(obj.get("collections"), dict) else {}
    for uid, entry in members_raw.items():
        if HEX32_RE.match(str(uid)) and isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], bool) and _hlc(entry[1]):
            members[uid] = [entry[0], entry[1]]
    return {
        "device": obj["device"],
        "fields": fields,
        "life": life,
        "file": {"name": name, "pages": pages, "added_at": added},
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
        first = _count(info.get("first_page"), upper=100_000)
        last = info.get("last_page")
        if last is not None and (isinstance(last, bool) or not isinstance(last, int) or not first <= last <= 100_000):
            return None
        created = _time(info.get("created_at"))
        options = info.get("options") if isinstance(info.get("options"), dict) else {}
        if len(json.dumps(options, ensure_ascii=False, default=str)) > MAX_OPTIONS_BYTES:
            options = {}
        if created is None:
            return None
        info = {
            "model": clean_text(str(info.get("model", "")))[:200],
            "formality": clean_text(str(info.get("formality", "")))[:50],
            "first_page": first,
            "last_page": last,
            "options": options,
            "segments": _count(info.get("segments")),
            "untranslated": _count(info.get("untranslated")),
            "chars": _count(info.get("chars")),
            "created_at": created,
        }
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
        name = [clean_text(name[0]).strip()[:MAX_NAME], name[1]]
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
