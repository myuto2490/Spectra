"""原文テキストの消去（redaction）。

MuPDF の redaction は、ページから参照される図の部品（フォーム XObject）の中まで調べ、
中身に手を加えない場合でも部品を丸ごと複製する。図を細かな部品で描いた PDF では数万個の複製ができ、
処理が遅くなるうえにファイルが数倍に膨らむ。

訳すのはページ本体の本文だけで、図の中の文字は消さないため、消去の間だけ図の部品を
空の仮部品に差し替え、終わったら元の部品に戻す。部品の中に本文が入っている特殊な PDF で
消し残りが出た場合は、部品の中まで処理する通常の方法でやり直す。

また MuPDF は redaction でページの描画命令を書き直すとき、行送り（TL）を使う改行の命令
（T*・'・"）を正しく扱えないことがある（行送りの値を取り違える、消した文字と一緒に改行まで落とす）。
すると、消していない数式や本文までが別の位置へずれる（Ghostscript で作られた PDF に多い）。
書き直す前に、それらの命令を移動量を明示した同じ意味の命令に置き換えておく。
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
        _resolve_form_line_feeds(page)
        _redact(page, rects)


def _redact(page: pymupdf.Page, rects: list[Rect]) -> None:
    for r in rects:
        rect = pymupdf.Rect(erase_rect(r))
        if not rect.is_empty:
            page.add_redact_annot(rect, fill=False, cross_out=False)
    apply_redactions(
        page,
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )


def apply_redactions(page: pymupdf.Page, **kwargs) -> None:
    """page.apply_redactions の代わり。描画命令を書き直す前に、行送りを使う改行の命令を置き換えておく。"""
    resolve_line_feeds(page)
    page.apply_redactions(**kwargs)


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


# ---- 行送りを使う改行命令の書き換え ---------------------------------------------

_LINE_FEED_OPS = (b"T*", b"'", b'"')
# 字句: 空白・注釈・辞書の括弧・16 進文字列・配列の括弧・名前・その他（数値と命令）。文字列 ( ) は別に読む
_TOKEN = re.compile(
    rb"[\x00\t\n\f\r ]+|%[^\r\n]*|<<|>>|<[0-9A-Fa-f\x00\t\n\f\r ]*>|[\[\]{}]|/[^\x00\t\n\f\r ()<>\[\]{}/%]*"
    rb"|[^\x00\t\n\f\r ()<>\[\]{}/%]+"
)
_INLINE_IMAGE_END = re.compile(rb"[\x00\t\n\f\r ]EI(?=[\x00\t\n\f\r /%\[<(]|$)")


def resolve_line_feeds(page: pymupdf.Page) -> bool:
    """行送りを使う改行の命令（T*・'・"）を、移動量を明示した同じ意味の命令（Td・Tj）に置き換える。

    描画の結果は変わらない。書き換えたら True を返す。読めない描画命令なら何もしない。
    """
    try:
        if not page.get_contents():
            return False
        stream = page.read_contents()
        if not any(op in stream for op in _LINE_FEED_OPS):
            return False
        fixed = resolve_line_feeds_in(stream)
        if fixed is None or fixed == stream:
            return False
        doc = page.parent
        # 他のページと共有している描画命令もあるので、新しいオブジェクトに書いてページから参照する
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, fixed)
        page.set_contents(xref)
        return True
    except Exception:
        log.debug("page %d: could not resolve line feeds", page.number + 1, exc_info=True)
        return False


def _resolve_form_line_feeds(page: pymupdf.Page) -> None:
    """ページが直接使う図の部品（フォーム XObject）の中の改行の命令も置き換える（部品の中まで消すとき）。

    部品は他のページと共有されることがあるが、置き換えても描画の結果は変わらないので、そのまま書き戻す。
    """
    doc = page.parent
    for xref in {x[0] for x in page.get_xobjects()}:
        try:
            if doc.xref_get_key(xref, "Subtype")[1] != "/Form":
                continue
            stream = doc.xref_stream(xref)
            if not stream or not any(op in stream for op in _LINE_FEED_OPS):
                continue
            fixed = resolve_line_feeds_in(stream, leading=None)
            if fixed is not None and fixed != stream:
                doc.update_stream(xref, fixed)
        except Exception:
            log.debug("page %d: could not resolve line feeds in form %d", page.number + 1, xref, exc_info=True)


def resolve_line_feeds_in(stream: bytes, leading: float | None = 0.0) -> bytes | None:
    """描画命令の列の T*・'・" を、Td・Tj・Tw・Tc の組に置き換えたものを返す。解釈できなければ None。

    T* は「0 -行送り Td」、(s) ' は「0 -行送り Td (s) Tj」、aw ac (s) " は「aw Tw ac Tc 0 -行送り Td (s) Tj」と
    同じ意味。行送りは TL と TD で設定され、q / Q で保存・復元される（BT では初期化されない）。
    leading: 始まりの行送り。ページは 0。図の部品（フォーム XObject）は呼び出し側の値を引き継ぐため
    分からない（None）。分からないまま改行の命令に出会ったら、書き換えずに None を返す。
    """
    edits: list[tuple[int, int, bytes]] = []  # (開始, 終了, 置き換える内容)
    stack: list[float | None] = []
    operands: list[tuple[bytes, int, int]] = []
    pos, n = 0, len(stream)
    while pos < n:
        c = stream[pos]
        if c == 0x28:  # "(" 文字列（入れ子の括弧と \ による退避を考慮）
            end = _string_end(stream, pos)
            if end is None:
                return None
            operands.append((b"()", pos, end))
            pos = end
            continue
        m = _TOKEN.match(stream, pos)
        if m is None:
            return None
        tok, start, pos = m.group(), m.start(), m.end()
        first = tok[0]
        if first in b"\x00\t\n\f\r %":
            continue
        if first in b"/[]{}<>" or _is_number(tok) or tok in (b"true", b"false", b"null"):
            operands.append((tok, start, pos))
            continue
        # 命令
        if tok in _LINE_FEED_OPS:
            if leading is None:
                return None
            feed = b"0 " + _format(-leading) + b" Td "
        if tok == b"q":
            stack.append(leading)
        elif tok == b"Q":
            leading = stack.pop() if stack else leading
        elif tok == b"TL" and operands:
            leading = _number(operands[-1][0])
            if leading is None:
                return None
        elif tok == b"TD" and len(operands) >= 2:
            ty = _number(operands[-1][0])
            if ty is None:
                return None
            leading = -ty
        elif tok == b"T*":
            edits.append((start, pos, feed))
        elif tok == b"'" and operands:
            text = operands[-1]
            edits.append((text[1], text[1], feed))
            edits.append((start, pos, b"Tj"))
        elif tok == b'"' and len(operands) >= 3:
            aw, ac, text = operands[-3:]
            spacing = stream[aw[1] : aw[2]] + b" Tw " + stream[ac[1] : ac[2]] + b" Tc "
            edits.append((aw[1], text[1], spacing + feed))
            edits.append((start, pos, b"Tj"))
        elif tok == b"ID":  # インライン画像の中身（バイナリ）を飛ばす
            end = _INLINE_IMAGE_END.search(stream, pos)
            if end is None:
                return None
            pos = end.end()
        operands = []
    if not edits:
        return stream
    out: list[bytes] = []
    last = 0
    for a, b, text in edits:
        out.append(stream[last:a])
        out.append(text)
        last = b
    out.append(stream[last:])
    return b"".join(out)


def _string_end(stream: bytes, pos: int) -> int | None:
    depth, i, n = 0, pos, len(stream)
    while i < n:
        c = stream[i]
        if c == 0x5C:  # "\\"
            i += 2
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


_NUMBER = re.compile(rb"[+-]?(\d+\.?\d*|\.\d+)")


def _is_number(tok: bytes) -> bool:
    return _NUMBER.fullmatch(tok) is not None


def _number(tok: bytes) -> float | None:
    try:
        return float(tok)
    except ValueError:
        return None


def _format(value: float) -> bytes:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return (text if text not in ("", "-0") else "0").encode()
