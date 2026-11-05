"""ノートの同期: 版ベクトルで比べ、2 台で同時に書いたときは両方を残して連結する。

ノートの版は {"text": 本文, "hlc": 最後に書いた HLC, "vv": {端末 ID: その端末が最後に書いた HLC}}。
- 片方の vv がもう片方の vv 以上（どの端末についても）なら、後から書いた方なのでそれを採る。
- どちらでもなければ同時に書かれたので、新しい方（hlc の大きい方）の本文の後に、古い方の本文を区切り線で続ける。
  vv は端末ごとの最大にする。連結の結果はどの端末でも同じになり（端末の名前などの手元の情報を使わない）、
  vv が等しくなるので、再び連結されることはない。
"""

from __future__ import annotations

from ..models import NOTE_MAX_CHARS

SEPARATOR = "\n\n――― ほかのパソコンで同時に編集された内容 ―――\n"


def new_version(text: str, hlc: str, device: str, base: dict | None) -> dict:
    """この端末で書いたノートの版。base はそのもとにした版（手元のライブラリにあった版）。"""
    vv = dict(base["vv"]) if base else {}
    vv[device] = hlc
    return {"text": text, "hlc": hlc, "vv": vv}


def _covers(a: dict, b: dict) -> bool:
    """a が b を含んでいる（a は b を見てから書かれた、または同じ）。"""
    return all(a["vv"].get(dev, "") >= h for dev, h in b["vv"].items())


def merge(versions: list[dict]) -> tuple[dict | None, bool]:
    """版をまとめて 1 つにする。戻り値は (版, 連結したか)。"""
    heads: list[dict] = []
    for v in sorted(versions, key=lambda v: (v["hlc"], v["text"])):
        if any(_covers(h, v) and (h["vv"] != v["vv"] or (h["hlc"], h["text"]) >= (v["hlc"], v["text"])) for h in heads):
            continue
        heads = [h for h in heads if not _covers(v, h)]
        heads.append(v)
    if not heads:
        return None, False
    if len(heads) == 1:
        return heads[0], False
    heads.sort(key=lambda v: (v["hlc"], v["text"]))
    text = heads[0]["text"]
    for v in heads[1:]:
        if v["text"] == text or not text:
            text = v["text"]
        elif v["text"]:
            text = v["text"] + SEPARATOR + text
    vv: dict[str, str] = {}
    for v in heads:
        for dev, h in v["vv"].items():
            vv[dev] = max(vv.get(dev, ""), h)
    return {"text": text[:NOTE_MAX_CHARS], "hlc": heads[-1]["hlc"], "vv": vv}, True
