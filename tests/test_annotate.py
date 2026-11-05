"""注釈を出力の PDF に標準の PDF 注釈として書く（docs/ANNOTATIONS.md §5）。通信しない。"""

import hashlib

import pymupdf
import pytest
from pdf_factory import make_paper

from spectra.core.annotate import BAR_X, COLORS, MAC_OPACITY, apply_annotations
from spectra.core.export import build_output, finalize
from spectra.core.options import OutputMode
from spectra.library.models import Annotation


def ann(kind="highlight", page=0, quads=((72.0, 100.0, 300.0, 112.0), (72.0, 114.0, 200.0, 126.0)), color="green", comment="大事", quote="the text"):
    return Annotation("a" * 32, 1, kind, page, quads, quote, color, comment, 1.8e9, 1.8e9)


@pytest.fixture()
def docs(tmp_path):
    original = make_paper(str(tmp_path / "o.pdf"))
    translated = pymupdf.open(str(tmp_path / "o.pdf"))  # 訳文は原文と同じページ構成
    return original, translated


def reopen(doc: pymupdf.Document) -> pymupdf.Document:
    return pymupdf.open(stream=finalize(doc), filetype="pdf")


def annots(page: pymupdf.Page) -> list[pymupdf.Annot]:
    """注釈の一覧。ページ（と文書）は、呼ぶ側で変数に持っておくこと（先に解放されると注釈が使えない）。"""
    return list(page.annots())


def test_interleave_writes_marks_on_the_original_pages(docs):
    original, translated = docs
    out = build_output(original, translated, OutputMode.INTERLEAVE, range(original.page_count))
    items = [ann(), ann("underline", color="blue", comment=""), ann("note", quads=((50.0, 60.0, 50.0, 60.0),), comment="付箋")]
    assert apply_annotations(out, items, OutputMode.INTERLEAVE, range(original.page_count)) == 3
    doc = reopen(out)
    first, second = doc[0], doc[1]
    got = annots(first)
    assert [a.type[1] for a in got] == ["Highlight", "Underline", "Text"]
    assert annots(second) == []  # 訳文のページには書かない
    hl = got[0]
    assert hl.info["content"] == "大事" and tuple(round(c, 2) for c in hl.colors["stroke"]) == COLORS["green"]
    assert abs(hl.rect.x0 - 72) < 4 and abs(hl.rect.y0 - 100) < 4 and abs(hl.rect.y1 - 126) < 4  # マーカーは少し余白を取る
    assert got[2].info["content"] == "付箋"


def test_side_by_side_writes_on_the_left_half(docs):
    original, translated = docs
    out = build_output(original, translated, OutputMode.SIDE_BY_SIDE, range(original.page_count))
    apply_annotations(out, [ann()], OutputMode.SIDE_BY_SIDE, range(original.page_count))
    doc = reopen(out)
    page = doc[0]
    hl = annots(page)[0]
    assert hl.rect.x1 < original[0].rect.width  # 左の原文の側


def test_translated_only_draws_a_margin_bar_with_the_quote(docs):
    original, translated = docs
    out = build_output(original, translated, OutputMode.TRANSLATED, range(original.page_count))
    apply_annotations(out, [ann(), ann("note", quads=((50.0, 60.0, 50.0, 60.0),), comment="付箋")], OutputMode.TRANSLATED)
    doc = reopen(out)
    page = doc[0]
    got = annots(page)
    assert [a.type[1] for a in got] == ["Square", "Text"]
    bar = got[0]
    assert abs(bar.rect.x0 - BAR_X) < 2 and abs(bar.rect.y0 - 100) < 2 and abs(bar.rect.y1 - 126) < 2
    assert bar.info["content"] == "「the text」\n大事"
    # macOS のプレビューで色が出るよう、帯と付箋には不透明度を書く（マーカー・下線は不要）
    assert all(abs(a.opacity - MAC_OPACITY) < 0.005 for a in got)


def test_page_range_and_out_of_range_annotations(docs):
    original, translated = docs
    last = original.page_count - 1
    pages = range(last, last + 1)  # 最後のページだけを出力した
    out = build_output(original, translated, OutputMode.INTERLEAVE, pages)
    n = apply_annotations(out, [ann(page=0), ann(page=last), ann(page=99)], OutputMode.INTERLEAVE, pages)
    doc = reopen(out)
    page = doc[0]
    assert n == 1 and len(annots(page)) == 1  # 最後のページの注釈が、出力の 1 枚目に来る


def test_library_pdf_is_not_modified(tmp_path):
    path = tmp_path / "o.pdf"
    make_paper(str(path)).close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    original, translated = pymupdf.open(str(path)), pymupdf.open(str(path))
    out = build_output(original, translated, OutputMode.INTERLEAVE, range(original.page_count))
    apply_annotations(out, [ann()], OutputMode.INTERLEAVE)
    finalize(out)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    page = original[0]
    assert not list(page.annots())
