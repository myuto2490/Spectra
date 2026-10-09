import pymupdf
import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen

from spectra.core.fontbook import FontBook, FontRole, subset_font_file


@pytest.fixture()
def cff_font(tmp_path):
    """「あ」「い」を四角形として持つ最小の CFF OpenType フォントを作る。"""
    fb = FontBuilder(1000, isTTF=False)
    names = [".notdef", "a_jp", "i_jp", "space"]
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({ord("あ"): "a_jp", ord("い"): "i_jp", 0x20: "space"})
    charstrings = {}
    for name in names:
        pen = T2CharStringPen(1000, None)
        if name != "space":
            pen.moveTo((100, 0))
            pen.lineTo((900, 0))
            pen.curveTo((900, 300), (900, 600), (900, 800))
            pen.lineTo((100, 800))
            pen.closePath()
        charstrings[name] = pen.getCharString()
    fb.setupCFF("TestJP", {"FullName": "Test JP"}, charstrings, {})
    fb.setupHorizontalMetrics({n: (1000, 100) for n in names})
    fb.setupHorizontalHeader(ascent=880, descent=-120)
    fb.setupNameTable({"familyName": "Test JP", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=880, usWinAscent=880, usWinDescent=120)
    fb.setupPost()
    path = tmp_path / "TestJP.otf"
    fb.save(str(path))
    return path


def _dark_pixels(font: pymupdf.Font, text: str) -> int:
    doc = pymupdf.open()
    page = doc.new_page()
    tw = pymupdf.TextWriter(page.rect)
    tw.append((50, 100), text, font=font, fontsize=40)
    tw.write_text(page)
    pix = pymupdf.open(stream=doc.tobytes(), filetype="pdf")[0].get_pixmap(clip=pymupdf.Rect(40, 50, 200, 110), dpi=72)
    return sum(1 for i in range(0, len(pix.samples), pix.n) if pix.samples[i] < 128)


def test_cff_subset_is_converted_and_renders(cff_font):
    data = subset_font_file(str(cff_font), "あ")
    assert data is not None
    font = pymupdf.Font(fontbuffer=data)
    assert font.has_glyph(ord("あ"))
    assert _dark_pixels(font, "あ") > 100


def test_fontbook_prepare_swaps_to_subset(cff_font):
    fb = FontBook(str(cff_font), serif=True)
    before = fb.width("あい", FontRole.JP, 10)
    fb.prepare("あい")
    assert fb.width("あい", FontRole.JP, 10) == pytest.approx(before)
    assert len(fb.jp.buffer) < cff_font.stat().st_size * 2


def test_builtin_font_and_fallback(tmp_path):
    bad = tmp_path / "bad.ttf"
    bad.write_bytes(b"nope")
    fb = FontBook(str(bad))
    assert fb.jp.has_glyph(ord("翻"))
    assert fb.resolve("α", FontRole.LATIN) == FontRole.LATIN
    assert fb.jp_role(bold=True) == FontRole.JP  # 太字ファイルが無ければ擬似ボールド
