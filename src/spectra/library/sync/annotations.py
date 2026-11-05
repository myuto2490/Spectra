"""注釈の同期（docs/ANNOTATIONS.md §3）。

論文ごとに papers/<sha>/annotations.<端末>.json を書く。中身は、この端末が作った・変えた・消した注釈の記録:

    {"<注釈の id>": {"page", "quads", "quote", "created_at",            # 作ったときに決まり、変わらない
                     "kind": [値, HLC], "color": [値, HLC], "life": [生きているか, HLC],
                     "comment": {"text", "hlc", "vv"} か null}}         # ノートと同じ版ベクトル（notes.py）

- 種類・色・生死は、それぞれ HLC の新しい方を採る。手元で直すときは life にも [True, HLC] を付けるので、
  「片方が消し、もう片方が後で直した」ときは直した方が残り、後で消したなら消える。
- コメントは、2 台で同時に変えたら両方を残して連結する（notes.merge）。
"""

from __future__ import annotations

import time

from .. import annotations as A
from ..models import MAX_ANNOTATIONS, MAX_COMMENT, Annotation
from . import notes as N
from .files import HEX32_RE, _hlc, clean_text, parse_note
from .hlc import HLC

TOMBSTONE_DAYS = 90  # 消した記録を書き出し続ける日数
MAX_ITEMS = MAX_ANNOTATIONS * 2  # tombstone を含む

_FIXED = ("page", "quads", "quote", "created_at")


def path(sha: str, device: str) -> str:
    return f"papers/{sha}/annotations.{device}.json"


def body(a: Annotation) -> dict:
    """ライブラリの注釈の、同期で比べる値（基準 baseline の形）。"""
    return {
        "kind": a.kind, "color": a.color, "comment": a.comment,
        "page": a.page, "quads": [list(q) for q in a.quads], "quote": a.quote, "created_at": a.created_at,
    }


def local_register(a: Annotation, base: dict | None, own: dict, hlc: str, device: str) -> dict:
    """手元で作った・直した注釈の記録。base は前回の基準（初めてなら None）、own は今までの自分の記録（コメントの版だけのこともある）。"""
    reg = {**{k: v for k, v in own.items() if k not in _FIXED}, **{k: body(a)[k] for k in _FIXED}}
    if base is None or base.get("kind") != a.kind or "kind" not in reg:
        reg["kind"] = [a.kind, hlc]
    if base is None or base.get("color") != a.color or "color" not in reg:
        reg["color"] = [a.color, hlc]
    if (base or {}).get("comment", "") != a.comment:
        reg["comment"] = N.new_version(a.comment, hlc, device, reg.get("comment"))
    reg["life"] = [True, hlc]
    return reg


def deleted_register(own: dict, base: dict, hlc: str) -> dict:
    """手元で消した注釈の記録。ほかの端末で作った注釈でも、ほかの端末が検証できる形にする（位置などは基準から）。"""
    reg = {**{k: v for k, v in own.items() if k not in _FIXED}, **{k: base[k] for k in _FIXED}}
    reg.setdefault("kind", [base["kind"], hlc])
    reg.setdefault("color", [base["color"], hlc])
    reg["life"] = [False, hlc]
    return reg


def complete(reg: dict) -> bool:
    """書き出せる記録か（ほかの端末の注釈のコメントの版だけを持っている記録は書き出さない）。"""
    return all(k in reg for k in (*_FIXED, "kind", "color", "life"))


def merge(regs: list[dict]) -> tuple[dict | None, dict | None, bool]:
    """同じ注釈の記録をまとめる。戻り値は (反映する値（消えていれば None）, コメントの版, 連結したか)。"""
    regs = [r for r in regs if r]
    if not regs:
        return None, None, False
    fixed = min(regs, key=lambda r: (r["created_at"], r["quote"]))  # どの端末でも同じはずだが、決まった方を使う
    kind = max((r["kind"] for r in regs if r.get("kind")), key=lambda x: x[1], default=None)
    color = max((r["color"] for r in regs if r.get("color")), key=lambda x: x[1], default=None)
    life = max((r["life"] for r in regs if r.get("life")), key=lambda x: x[1], default=None)
    comment, merged = N.merge([r["comment"] for r in regs if r.get("comment")])
    if life is None or not life[0] or kind is None or color is None:
        return None, comment, merged
    target = {
        "kind": kind[0], "color": color[0], "comment": comment["text"] if comment else "",
        **{k: fixed[k] for k in _FIXED},
    }
    return target, comment, merged


def to_annotation(annotation_id: str, paper_id: int, target: dict, merged: bool) -> Annotation:
    return Annotation(
        id=annotation_id, paper_id=paper_id, kind=target["kind"], page=target["page"],
        quads=tuple(tuple(q) for q in target["quads"]), quote=target["quote"], color=target["color"],
        comment=target["comment"], created_at=target["created_at"], updated_at=time.time(), comment_merged=merged,
    )


def stamps(reg: dict) -> list[str]:
    out = [reg[k][1] for k in ("kind", "color", "life") if reg.get(k)]
    if reg.get("comment"):
        out.append(reg["comment"]["hlc"])
    return out


def expired(reg: dict, now: float) -> bool:
    """消した記録のうち、書き出しから外してよい古いもの。"""
    life = reg.get("life")
    if not life or life[0]:
        return False
    try:
        return HLC.parse(life[1]).wall_ms / 1000 < now - TOMBSTONE_DAYS * 86400
    except ValueError:
        return True


# -- ほかの端末から来たファイルの検証 ------------------------------------------------------


def parse_file(obj: dict, sha: str) -> dict | None:
    if obj.get("kind") != "annotations" or obj.get("sha") != sha or not isinstance(obj.get("device"), str):
        return None
    if not HEX32_RE.match(obj["device"]) or not isinstance(obj.get("items"), dict):
        return None
    items = {}
    for annotation_id, raw in list(obj["items"].items())[:MAX_ITEMS]:
        reg = _parse_register(raw) if isinstance(annotation_id, str) and HEX32_RE.match(annotation_id) else None
        if reg is not None:
            items[annotation_id] = reg
    return {"device": obj["device"], "items": items}


def _parse_register(raw) -> dict | None:
    """1 件の記録を検証する。合わなければ None（その注釈だけを捨てる）。"""
    if not isinstance(raw, dict):
        return None
    try:
        quads = A.clean_quads(raw.get("quads"))
        quote = clean_text(raw.get("quote", "")) if isinstance(raw.get("quote", ""), str) else None
        kind, color, life = raw.get("kind"), raw.get("color"), raw.get("life")
        if quote is None or not (_register(kind, str) and _register(color, str) and _register(life, bool)):
            return None
        A.check(kind[0], raw.get("page"), color[0], quote, "")
    except ValueError:
        return None
    created = raw.get("created_at")
    if isinstance(created, bool) or not isinstance(created, (int, float)) or not 0 < created < 4_102_444_800:
        return None
    comment = None
    if raw.get("comment") is not None:
        comment = parse_note(raw["comment"])
        if comment is None or len(comment["text"]) > MAX_COMMENT:
            return None
    return {
        "page": raw["page"], "quads": [list(q) for q in quads], "quote": quote, "created_at": float(created),
        "kind": [kind[0], kind[1]], "color": [color[0], color[1]], "life": [life[0], life[1]], "comment": comment,
    }


def _register(value, kind: type) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], kind) and bool(_hlc(value[1]))
