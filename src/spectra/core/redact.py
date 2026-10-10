"""原文テキストの消去（redaction）。

MuPDF の redaction は、ページから参照される図の部品（フォーム XObject）の中まで調べ、
中身に手を加えない場合でも部品を丸ごと複製する。図を細かな部品で描いた PDF では数万個の複製ができ、
処理が遅くなるうえにファイルが数倍に膨らむ。

訳すのはページ本体の本文だけで、図の中の文字は消さないため、消去の間だけ図の部品を
空の仮部品に差し替え、終わったら元の部品に戻す。部品の中に本文が入っている特殊な PDF で
消し残りが出た場合は、部品の中まで処理する通常の方法でやり直す。
"""

from __future__ import annotations

import logging
import re

import pymupdf

from .models import Rect, erase_rect, rect_area, rect_intersect

log = logging.getLogger(__name__)

_REF = re.compile(r"/([^\s/<>\[\]()]+)\s+(\d+)\s+0\s+R")
_STUB_KEY = "SpectraStub"


def erase_text(page: pymupdf.Page, rects: list[Rect]) -> None:
    """rects（原文の行・スパンの範囲）の文字だけを消す。画像・図形・背景はそのまま残す。"""
    if not rects:
        return
    doc = page.parent
    stubbed = _stub_forms(doc, page)
    try:
        _redact(page, rects)
    finally:
        if stubbed:
            _restore_forms(doc, page, stubbed)
    if stubbed and _has_leftover_text(page, rects):
        log.info("page %d: text inside form XObjects, redacting with recursion", page.number + 1)
        _redact(page, rects)


def _redact(page: pymupdf.Page, rects: list[Rect]) -> None:
    for r in rects:
        rect = pymupdf.Rect(erase_rect(r))
        if not rect.is_empty:
            page.add_redact_annot(rect, fill=False, cross_out=False)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )


def _has_leftover_text(page: pymupdf.Page, rects: list[Rect]) -> bool:
    zones = [erase_rect(r) for r in rects]
    for w in page.get_text("words"):
        box = (w[0], w[1], w[2], w[3])
        if w[4].strip() and any(rect_area(rect_intersect(box, z)) > rect_area(box) * 0.5 for z in zones):
            return True
    return False


# ---- 図の部品の差し替え ---------------------------------------------------


def _xobject_owner(doc: pymupdf.Document, page: pymupdf.Page) -> tuple[int | None, str]:
    """/XObject 辞書を持つオブジェクトの (xref, キーのパスの接頭辞) を返す。"""
    kind, val = doc.xref_get_key(page.xref, "Resources")
    if kind == "xref":
        owner, prefix = int(val.split()[0]), ""
    elif kind == "dict":
        owner, prefix = page.xref, "Resources/"
    else:
        return None, ""  # 親から継承した資源（まれ）は差し替えない
    kind, val = doc.xref_get_key(owner, prefix + "XObject")
    if kind == "xref":
        return int(val.split()[0]), ""
    if kind == "dict":
        return owner, prefix + "XObject/"
    return None, ""


def _entries(doc: pymupdf.Document, owner: int, path: str) -> dict[str, int]:
    text = doc.xref_get_key(owner, path.rstrip("/"))[1] if path else doc.xref_object(owner, compressed=True)
    return {m.group(1): int(m.group(2)) for m in _REF.finditer(text)}


def stub_forms(doc: pymupdf.Document, page: pymupdf.Page) -> None:
    """ページが参照する図の部品を、空の仮部品に置き換える（元に戻さない）。"""
    _stub_forms(doc, page)


def _stub_forms(doc: pymupdf.Document, page: pymupdf.Page) -> tuple[int, str, dict[str, int]] | None:
    try:
        owner, path = _xobject_owner(doc, page)
        if owner is None:
            return None
        forms = {
            name: x for name, x in _entries(doc, owner, path).items() if doc.xref_get_key(x, "Subtype")[1] == "/Form"
        }
        if not forms:
            return None
        for name, x in forms.items():
            stub = doc.get_new_xref()
            doc.update_object(stub, f"<</Type/XObject/Subtype/Form/BBox[0 0 1 1]/{_STUB_KEY} {x}>>")
            doc.update_stream(stub, b" ")
            doc.xref_set_key(owner, path + name, f"{stub} 0 R")
        return owner, path, forms
    except Exception:
        log.debug("page %d: could not stub forms", page.number + 1, exc_info=True)
        return None


def _restore_forms(doc: pymupdf.Document, page: pymupdf.Page, stubbed: tuple[int, str, dict[str, int]]) -> None:
    owner, path, forms = stubbed
    # 他のページと共有している資源辞書（ページ以外のオブジェクト）を元に戻す。
    # ページ自身に書かれた資源は redaction が名前を付け替えて書き直すため、ここでは触らない
    # （古い名前で上書きすると、別の部品を指してしまい図が二重に描かれる）
    if owner != page.xref:
        for name, x in forms.items():
            doc.xref_set_key(owner, path + name, f"{x} 0 R")
    # redaction で書き直されたページの資源の中で、仮部品の複製を元の部品に戻す
    new_owner, new_path = _xobject_owner(doc, page)
    if new_owner is None:
        return
    for name, x in _entries(doc, new_owner, new_path).items():
        kind, val = doc.xref_get_key(x, _STUB_KEY)
        if kind == "int":
            doc.xref_set_key(new_owner, new_path + name, f"{val} 0 R")
