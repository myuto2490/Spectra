"""訳文が見出しや別の段落と重なる・訳されないまま残る不具合の再発防止（Icarus 186 の Hesman et al. など）。"""

import json

import pymupdf

from spectra.core.analysis import analyze_document
from spectra.core.extract import extract_blocks
from spectra.core.models import BlockKind, DocumentAnalysis, Segment
from spectra.core.options import RenderOptions
from spectra.core.qa import _overlapping_lines
from spectra.core.render import Renderer, _overlapping
from spectra.core.textnorm import looks_untranslated
from spectra.translate.cache import TranslationCache, cache_key
from spectra.translate.dummy import DummyTranslator
from spectra.translate.service import translate_texts


def _split_math_line_page():
    """段落の最終行が "and 2.2⁺⁰·⁶₋₀.₄ in the lower …" で、上付きの直後の空白だけが上付きの高さにある。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for i, t in enumerate(
        [
            "The calculated abundances in this work are estimated to be",
            "within the expected range for both of the regions, and the",
            "results agree with the previous measurements of the CO line",
        ]
    ):
        page.insert_text((60, 100 + i * 12), t, fontname="tiro", fontsize=10)
    y = 136
    page.insert_text((60, y), "and 2.2", fontname="tiro", fontsize=10)
    x = 60 + pymupdf.get_text_length("and 2.2", "tiro", 10)
    page.insert_text((x, y - 4), "+0.6", fontname="tiro", fontsize=7.5)
    page.insert_text((x, y + 3), "-0.4", fontname="tiro", fontsize=7.5)
    x2 = x + pymupdf.get_text_length("+0.6", "tiro", 7.5)
    font = pymupdf.Font("tiro")
    tw = pymupdf.TextWriter(page.rect)
    tw.append((x2, y - 3.6), " ", font=font, fontsize=10)  # 空白だけが上付きの高さ
    tw.append((x2 + font.text_length(" ", 10), y), "in the lower and upper regions, respectively.", font=font, fontsize=10)
    tw.write_text(page)
    page.insert_text((60, y + 26), "5. Discussion", fontname="tibo", fontsize=10)
    return doc


def test_leading_space_at_superscript_height_does_not_lift_the_baseline():
    # PyMuPDF はスパンの基準点を先頭の文字（上付きの高さの空白）にするので、本文の文字の位置で取り直す
    blocks = extract_blocks(_split_math_line_page()[0])
    line = next(ln for b in blocks for ln in b.lines if "in the lower" in ln.text)
    span = next(s for s in line.spans if "in the lower" in s.text)
    assert abs(span.origin[1] - 136.0) < 0.5
    assert abs(line.baseline - 136.0) < 0.5


def _renderer(segments=()):
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    return Renderer(doc, DocumentAnalysis(pages=[], segments=list(segments), body_size=10.0), RenderOptions())


def test_frames_of_one_paragraph_do_not_overlap_each_other():
    # 1 行が数式で左右のブロックに分かれると、続きのブロックの枠が前の枠の最終行の高さから始まる
    seg = Segment(id="s", kind=BlockKind.BODY, source="x", runs={}, regions=[])
    r = _renderer()
    earlier = [(301.7, 612.7, 552.9, 670.4)]
    clipped = r._clip_to_neighbors(seg, 0, (301.7, 656.8, 552.8, 694.1), earlier)
    assert clipped[1] >= 670.4
    # 左右に離れた枠（別の段）は動かさない
    assert r._clip_to_neighbors(seg, 0, (40, 656.8, 280, 694.1), earlier)[1] == 656.8


def test_overlapping_translations_are_found():
    from spectra.core.layout import LayoutResult

    a = Segment(id="a", kind=BlockKind.BODY, source="x", runs={}, regions=[])
    b = Segment(id="b", kind=BlockKind.BODY, source="y", runs={}, regions=[])
    c = Segment(id="c", kind=BlockKind.BODY, source="z", runs={}, regions=[])
    la = LayoutResult([], 1.0, True, 2, used=[(0, (300, 610, 553, 672))], line_height=10.0)
    lb = LayoutResult([], 1.0, True, 1, used=[(0, (300, 662, 360, 672))], line_height=10.0)  # a の最終行に食い込む
    lc = LayoutResult([], 1.0, True, 3, used=[(0, (300, 671, 553, 720)), (1, (40, 600, 280, 700))], line_height=10.0)  # 1pt 接するだけ
    assert _overlapping([(a, la), (b, lb), (c, lc)]) == {"a", "b"}
    # 同じ段落の 2 つの枠が重なる場合も見つける
    ld = LayoutResult([], 1.0, True, 3, used=[(0, (300, 610, 553, 672)), (0, (300, 660, 553, 694))], line_height=10.0)
    assert _overlapping([(a, ld)]) == {"a"}


def test_qa_finds_translated_lines_drawn_on_top_of_each_other():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((300, 660), "下層領域と上層領域においてそれぞれ", fontname="japan", fontsize=9)
    page.insert_text((300, 661), "る。5. 考察", fontname="japan", fontsize=9)  # 前の行に重ねて描いた
    page.insert_text((300, 700), "これは次の段落の一行目である。", fontname="japan", fontsize=9)
    page.insert_text((300, 711), "これは次の段落の二行目である。", fontname="japan", fontsize=9)  # 詰めた行送りは重なりではない
    assert _overlapping_lines(page, [tuple(page.rect)]) == 1
    # 書き換えていない範囲（原文のまま）の行は数えない
    assert _overlapping_lines(page, []) == 0


def _split_paragraph_doc():
    """前のページの段落が文の途中で終わり、次のページの段落が括弧から始まる。"""
    doc = pymupdf.open()
    first = doc.new_page(width=595, height=842)
    for i, t in enumerate(
        [
            "The cooler temperature profile provides a better fit to the data",
            "over the entire line than the nominal and warmer profiles. This",
            "implies that the tropospheric temperature is better represented",
            "by a smaller increase over the Voyager radio occultation profile",
        ]
    ):
        first.insert_text((60, 700 + i * 12), t, fontname="tiro", fontsize=10)
    second = doc.new_page(width=595, height=842)
    for i, t in enumerate(
        [
            "(3-7 K rather than 6-10 K). The best-fit CO profiles corresponding",
            "to the nominal and warmer temperature profiles are shown in the",
            "figure, together with the calculated spectra for these profiles.",
        ]
    ):
        second.insert_text((60, 100 + i * 12), t, fontname="tiro", fontsize=10)
    return pymupdf.open("pdf", doc.tobytes())


def test_paragraph_continuing_with_parenthesis_on_next_page_is_one_segment():
    a = analyze_document(_split_paragraph_doc())
    seg = next(s for s in a.segments if "radio occultation profile" in s.source)
    assert "(3-7 K rather than 6-10 K)" in seg.source
    assert {r.page for r in seg.regions} == {0, 1}


def test_english_echo_is_not_a_translation():
    src = "(3–7 K rather than 6–10 K). The best-fit CO profiles are shown in Fig. 9."
    assert looks_untranslated(src, src)
    assert not looks_untranslated(src, "（6〜10 K ではなく 3〜7 K）。最適な CO プロファイルを図 9 に示す。")
    assert not looks_untranslated("NASA Ames Research Center", "NASA Ames Research Center")  # 固有名詞だけ
    assert not looks_untranslated('Fig. <x id="0"/>', 'Fig. <x id="0"/>')


def test_english_echo_from_engine_or_cache_is_retranslated(tmp_path):
    src = "The best-fit CO profiles corresponding to the nominal profiles are shown here."

    class Echo(DummyTranslator):
        def _translate_batch(self, texts):
            return list(texts)  # 原文をそのまま返す

    cache = TranslationCache(tmp_path / "cache.db")
    # 英語のまま返った段落は、結果にもキャッシュにも入れない（原文のまま残し、警告に出す）
    assert translate_texts([src, "OK"], Echo(), cache) == {"OK": "OK"}
    assert cache.count() == 1
    assert translate_texts([src], DummyTranslator(), cache)[src] != src
    # 以前に英語のまま保存された訳はキャッシュから使わない
    poisoned = TranslationCache(tmp_path / "poisoned.db")
    t = DummyTranslator()
    poisoned.put_many({cache_key(t.cache_namespace(), src): src})
    assert translate_texts([src], t, poisoned)[src] != src


def test_gemini_retries_an_item_returned_in_english():
    from tests.test_translators import gemini, gemini_reply

    src = "The best-fit CO profiles corresponding to the nominal profiles are shown here."
    calls = []

    def handler(req):
        items = json.loads(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        calls.append(len(items))
        if len(calls) == 1:
            return gemini_reply([{"id": i, "text": it["text"]} for i, it in enumerate(items)])  # 英語のまま
        return gemini_reply([{"id": 0, "text": "最適な CO プロファイルを示す。"}])

    out = gemini(handler).translate([src, "another sentence that is long enough here"])
    assert out[0] == "最適な CO プロファイルを示す。"
    assert calls[0] == 2 and len(calls) == 3  # 英語のまま返った 2 件を 1 件ずつ訳し直した


def test_symbol_heavy_equation_line_with_squeezed_letters_is_math():
    # OCR の数式の行が 1 つにつながると、字間の空いた "a e x p" が詰まって長い単語 "aexp" に見える
    from spectra.core.classify import BlockStats, _looks_like_equation
    from spectra.core.models import Line, Span, TextBlock

    text = 'p ( y ) = (2,)n/»|LALT|i/a e x p < - ^ ( y - y ) T L A " l L T ( y - ?>} <2-12>'
    span = Span(text, "Times-Roman", 9.9, 0, 0, (80, 227, 403, 243), (80, 240.6))
    block = TextBlock(0, 0, (80, 227, 403, 243), [Line([span], (80, 227, 403, 243))])
    assert _looks_like_equation(block, BlockStats(block))
    sentence = "where x = 5 and y is the data"
    span = Span(sentence, "Times-Roman", 9.9, 0, 0, (80, 227, 300, 243), (80, 240.6))
    block = TextBlock(0, 0, (80, 227, 300, 243), [Line([span], (80, 227, 300, 243))])
    assert not _looks_like_equation(block, BlockStats(block))
