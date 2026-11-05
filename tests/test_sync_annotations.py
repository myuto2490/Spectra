"""注釈の同期（docs/ANNOTATIONS.md §3）: 2・3 台のシナリオ、競合、削除の記録、古い形のファイル、不正な値。"""

import json
import time

from test_sync_scenarios import (  # noqa: F401 -- pytest のフィクスチャ
    make_device,
    pdf,
    roundtrip,
    sync_root,
)

from spectra.library.sync import annotations as AN

Q = [(72.0, 100.0, 300.0, 112.0)]


def _paper(make_device, pdf, *names):  # noqa: F811 -- フィクスチャを引数で受け取る
    devices = [make_device(n) for n in names]
    paper = devices[0].lib.add(pdf(seed=1))[0]
    roundtrip(*devices)
    return paper, devices


def _ann(d, paper):
    return d.lib.annotations(d.paper(paper.sha256).id)


def test_annotation_added_on_one_device_appears_on_the_other(make_device, pdf, sync_root):  # noqa: F811
    paper, (a, b) = _paper(make_device, pdf, "A", "B")
    x = a.lib.add_annotation(paper.id, "highlight", 0, Q, quote="the text", color="green", comment="大事")
    a.lib.add_annotation(paper.id, "note", 0, [(50, 60, 50, 60)], comment="付箋")
    roundtrip(a, b)
    got = _ann(b, paper)
    g = b.lib.get_annotation(x.id)
    assert (g.kind, g.color, g.comment, g.quads, g.quote, g.page) == ("highlight", "green", "大事", x.quads, "the text", 0)
    assert g.paper_id == b.paper(paper.sha256).id and len(got) == 2 and any(c.kind == "annotations" for c in b.changes)
    assert (sync_root / AN.path(paper.sha256, a.engine.device.id)).exists()

    b.changes.clear()
    roundtrip(a, b)
    assert not [c for c in b.changes if c.kind == "annotations"]  # 変わらなければ反映しない（往復しない）


def test_edits_and_deletes_propagate_and_deleted_ones_do_not_come_back(make_device, pdf):  # noqa: F811
    paper, (a, b) = _paper(make_device, pdf, "A", "B")
    x = a.lib.add_annotation(paper.id, "highlight", 0, Q)
    roundtrip(a, b)
    b.lib.update_annotation(x.id, color="pink", kind="underline")  # ほかの端末で作った注釈を直す
    roundtrip(a, b)
    assert (a.lib.get_annotation(x.id).color, a.lib.get_annotation(x.id).kind) == ("pink", "underline")

    b.lib.delete_annotation(x.id)  # ほかの端末で作った注釈を消す
    roundtrip(a, b)
    roundtrip(a, b)
    assert _ann(a, paper) == [] and _ann(b, paper) == []


def test_two_devices_add_different_annotations(make_device, pdf):  # noqa: F811
    paper, (a, b) = _paper(make_device, pdf, "A", "B")
    a.lib.add_annotation(paper.id, "highlight", 0, Q, comment="A")
    b.lib.add_annotation(b.paper(paper.sha256).id, "underline", 0, [(72, 200, 200, 210)], comment="B")
    roundtrip(a, b)
    assert sorted(x.comment for x in _ann(a, paper)) == sorted(x.comment for x in _ann(b, paper)) == ["A", "B"]


def test_comment_edited_on_two_devices_at_once_is_concatenated(make_device, pdf):  # noqa: F811
    paper, (a, b) = _paper(make_device, pdf, "A", "B")
    x = a.lib.add_annotation(paper.id, "highlight", 0, Q, comment="共通")
    roundtrip(a, b)
    a.lib.update_annotation(x.id, comment="A の考え")
    time.sleep(0.01)
    b.lib.update_annotation(x.id, comment="B の考え", color="blue")
    roundtrip(a, b)
    texts = {d.lib.get_annotation(x.id).comment for d in (a, b)}
    assert len(texts) == 1
    text = texts.pop()
    assert "A の考え" in text and "B の考え" in text and "同時に編集された内容" in text
    assert all(d.lib.get_annotation(x.id).comment_merged and d.lib.get_annotation(x.id).color == "blue" for d in (a, b))
    roundtrip(a, b)
    roundtrip(a, b)
    assert a.lib.get_annotation(x.id).comment == b.lib.get_annotation(x.id).comment == text  # 重ならない

    a.lib.update_annotation(x.id, comment="まとめた")  # 連結を見てから書いたので、そのまま届く
    roundtrip(a, b)
    got = b.lib.get_annotation(x.id)
    assert got.comment == "まとめた" and not got.comment_merged


def test_delete_versus_later_edit(make_device, pdf):  # noqa: F811
    paper, (a, b) = _paper(make_device, pdf, "A", "B")
    x = a.lib.add_annotation(paper.id, "highlight", 0, Q)
    roundtrip(a, b)
    a.lib.delete_annotation(x.id)
    time.sleep(0.01)
    b.lib.update_annotation(x.id, comment="後で書いた")  # 後で直した方が残る
    roundtrip(a, b)
    roundtrip(a, b)
    assert a.lib.get_annotation(x.id).comment == "後で書いた" == b.lib.get_annotation(x.id).comment

    b.lib.update_annotation(x.id, color="green")
    roundtrip(a, b)
    time.sleep(0.01)
    a.lib.delete_annotation(x.id)  # 後で消したなら消える
    roundtrip(a, b)
    roundtrip(a, b)
    assert a.lib.get_annotation(x.id) is None and b.lib.get_annotation(x.id) is None


def test_three_devices_converge(make_device, pdf):  # noqa: F811
    paper, devices = _paper(make_device, pdf, "A", "B", "C")
    x = devices[0].lib.add_annotation(paper.id, "highlight", 0, Q, comment="もと")
    roundtrip(*devices)
    for d in devices:
        d.lib.update_annotation(x.id, comment=f"{d.name} の考え")
        time.sleep(0.01)
    roundtrip(*devices)
    roundtrip(*devices)
    assert len({d.lib.get_annotation(x.id).comment for d in devices}) == 1
    text = devices[0].lib.get_annotation(x.id).comment
    assert all(f"{n} の考え" in text for n in "ABC") and text.count("同時に編集された内容") == 2


def test_annotations_wait_for_the_paper(make_device, pdf, sync_root):  # noqa: F811
    """論文より先に注釈のファイルが届いても、論文が届いてから反映する。"""
    a, b = make_device("A"), make_device("B")
    paper = a.lib.add(pdf(seed=1))[0]
    x = a.lib.add_annotation(paper.id, "highlight", 0, Q)
    a.sync()
    state = sync_root / "papers" / paper.sha256 / f"state.{a.engine.device.id}.json"
    hidden = state.with_name("hold")
    state.rename(hidden)  # 論文の state ファイルがまだ届いていない
    b.sync()
    assert b.paper(paper.sha256) is None
    hidden.rename(state)
    b.sync()
    assert b.lib.get_annotation(x.id) is not None


def test_broken_and_old_files_are_ignored(make_device, pdf, sync_root):  # noqa: F811
    paper, (a, b) = _paper(make_device, pdf, "A", "B")
    good = a.lib.add_annotation(paper.id, "highlight", 0, Q)
    a.sync()
    path = sync_root / AN.path(paper.sha256, a.engine.device.id)
    body = json.loads(path.read_text(encoding="utf-8"))
    reg = body["items"][good.id]
    bad = {
        "1" * 32: {**reg, "quads": [[0, 0, 1e9, 1]]},
        "2" * 32: {**reg, "color": ["red", reg["color"][1]]},
        "3" * 32: {**reg, "page": -1},
        "4" * 32: {**reg, "comment": {"text": 3, "hlc": reg["life"][1], "vv": {}}},
        "../x": reg,
        "5" * 32: "nope",
    }
    body["items"].update(bad)
    path.write_text(json.dumps(body), encoding="utf-8")
    (sync_root / "papers" / paper.sha256 / f"annotations.{'e' * 32}.json").write_text("[" * 100000, encoding="utf-8")
    (sync_root / "papers" / paper.sha256 / f"annotations.{'f' * 32}.json").write_text(json.dumps({"kind": "annotations", "v": 1}), encoding="utf-8")
    b.sync()
    assert [x.id for x in _ann(b, paper)] == [good.id]  # 正しいものだけを取り込み、落ちない


def test_files_unrelated_to_annotations_are_unaffected_for_older_versions(make_device, pdf, sync_root):  # noqa: F811
    """注釈は別のファイルなので、論文の state ファイルの形は変わらない（古い版はそのまま読める）。"""
    paper, (a, b) = _paper(make_device, pdf, "A", "B")
    a.lib.add_annotation(paper.id, "highlight", 0, Q, comment="x")
    a.lib.set_favorite(paper.id, True)
    a.sync()
    state = json.loads((sync_root / "papers" / paper.sha256 / f"state.{a.engine.device.id}.json").read_text(encoding="utf-8"))
    assert "annotations" not in state and "items" not in state


def test_old_tombstones_are_dropped_from_the_file(make_device, pdf, sync_root, monkeypatch):  # noqa: F811
    paper, (a, b) = _paper(make_device, pdf, "A", "B")
    x = a.lib.add_annotation(paper.id, "highlight", 0, Q)
    y = a.lib.add_annotation(paper.id, "highlight", 0, Q)
    roundtrip(a, b)
    a.lib.delete_annotation(x.id)
    roundtrip(a, b)
    path = sync_root / AN.path(paper.sha256, a.engine.device.id)
    assert json.loads(path.read_text(encoding="utf-8"))["items"][x.id]["life"][0] is False
    a.lib.update_annotation(y.id, color="blue")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + (AN.TOMBSTONE_DAYS + 1) * 86400)  # 91 日後に書き出す
    a.sync()
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    assert x.id not in items and y.id in items
