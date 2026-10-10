import pytest

from spectra.core.fontbook import FontBook
from spectra.core.layout import NO_START, Frame, layout
from spectra.core.markup import Token, parse
from spectra.core.models import Align, InlinePiece, InlineRun, TextStyle


@pytest.fixture(scope="module")
def fonts():
    return FontBook()


def style(align=Align.JUSTIFY, size=10.0, pitch=12.0, indent=0.0):
    return TextStyle(size=size, color=0, bold=False, serif=True, line_pitch=pitch, first_indent=indent, align=align)


def frame(x0, y0, x1, y1, st=None, page=0):
    return Frame(page=page, rect=(x0, y0, x1, y1), style=st or style())


def lines_of(result):
    """配置済みグリフをベースラインごとの文字列にまとめる。"""
    rows: dict[tuple[int, float], list] = {}
    for g in result.placed:
        rows.setdefault((g.page, round(g.y, 1)), []).append(g)
    return ["".join(g.text for g in sorted(gs, key=lambda g: g.x)) for _, gs in sorted(rows.items())]


TEXT = "本研究では、大規模な言語モデルを用いて論文の翻訳精度を評価した。その結果、提案手法は既存手法よりも高い性能を示した。"


def test_fits_without_shrinking(fonts):
    r = layout([Token(text=TEXT)], {}, [frame(0, 0, 300, 100)], fonts, text_scale=1.0)
    assert r.fits and r.scale == pytest.approx(1.0)
    assert "".join(lines_of(r)) == TEXT


def test_no_line_starts_with_prohibited_char(fonts):
    for width in range(60, 200, 7):
        r = layout([Token(text=TEXT)], {}, [frame(0, 0, width, 500)], fonts, text_scale=1.0)
        for line in lines_of(r)[1:]:
            assert line[0] not in NO_START, (width, line)


def test_shrinks_to_fit_and_never_overflows(fonts):
    f = frame(0, 0, 120, 30)
    r = layout([Token(text=TEXT * 2)], {}, [f], fonts, text_scale=1.0)
    assert r.fits and r.scale < 1.0
    for g in r.placed:
        assert g.y <= 30 + 0.01 and g.x >= -0.01
        assert g.x + fonts.width(g.text, g.role, g.size) <= 120 + g.size * 0.6  # 追い込み分の許容


def test_justified_lines_fill_width(fonts):
    r = layout([Token(text=TEXT * 2)], {}, [frame(0, 0, 200, 500)], fonts, text_scale=1.0)
    rows: dict[float, list] = {}
    for g in r.placed:
        rows.setdefault(round(g.y, 1), []).append(g)
    ys = sorted(rows)
    for y in ys[:-1]:  # 最終行以外は右端まで揃う
        last = max(rows[y], key=lambda g: g.x)
        right = last.x + fonts.width(last.text, last.role, last.size)
        assert right == pytest.approx(200, abs=1.0)


def test_center_alignment(fonts):
    r = layout([Token(text="概要")], {}, [frame(100, 0, 300, 20, style(Align.CENTER))], fonts, text_scale=1.0)
    xs = [g.x for g in r.placed]
    assert min(xs) > 180


def test_first_line_indent(fonts):
    r = layout([Token(text=TEXT)], {}, [frame(0, 0, 200, 200, style(indent=10))], fonts, text_scale=1.0)
    first_y = min(g.y for g in r.placed)
    assert min(g.x for g in r.placed if g.y == first_y) == pytest.approx(10)


def test_autospace_between_japanese_and_latin(fonts):
    r = layout([Token(text="これはTransformerです")], {}, [frame(0, 0, 400, 20, style(Align.LEFT))], fonts, text_scale=1.0)
    ha = next(g for g in r.placed if g.text == "は")
    latin = next(g for g in r.placed if g.text == "Transformer")
    gap = latin.x - (ha.x + fonts.width("は", ha.role, ha.size))
    assert gap == pytest.approx(2.5, abs=0.01)  # 0.25em


def test_flows_across_frames(fonts):
    frames = [frame(0, 0, 100, 25), frame(200, 0, 300, 200, page=1)]
    r = layout([Token(text=TEXT * 2)], {}, frames, fonts, text_scale=1.0)
    assert r.fits and r.scale == pytest.approx(1.0)
    assert {g.page for g in r.placed} == {0, 1}
    assert all(g.x < 100.5 for g in r.placed if g.page == 0)


def test_long_word_is_split(fonts):
    url = "https://example.com/" + "a" * 80
    r = layout([Token(text=url)], {}, [frame(0, 0, 100, 400)], fonts, text_scale=1.0)
    assert r.fits and len(lines_of(r)) > 1


def test_inline_run_kept_whole_with_positions(fonts):
    run = InlineRun(
        (
            InlinePiece("d", 1.0, 0.0, True, False, 0, True, dx_ratio=0.0),
            InlinePiece("k", 0.7, -0.15, True, False, 0, True, dx_ratio=0.52),
        ),
        width_ratio=0.95,
    )
    tokens = parse('次元 <x id="0"/> のキー', {0})
    r = layout(tokens, {0: run}, [frame(0, 0, 300, 20, style(Align.LEFT))], fonts, text_scale=1.0)
    d = next(g for g in r.placed if g.text == "d")
    k = next(g for g in r.placed if g.text == "k")
    assert k.x - d.x == pytest.approx(5.2) and k.y > d.y and k.size == pytest.approx(7.0)
    assert len(r.runs) == 1 and r.runs[0].x == pytest.approx(d.x)


def test_empty_translation(fonts):
    r = layout([], {}, [frame(0, 0, 100, 100)], fonts)
    assert r.fits and r.placed == []


def test_bold_tokens_use_bold_fonts(fonts):
    tokens = parse("<b>結果 Results.</b> 本文", set())
    r = layout(tokens, {}, [frame(0, 0, 300, 20, style(Align.LEFT))], fonts, text_scale=1.0)
    by_text = {g.text: g for g in r.placed}
    assert by_text["Results."].role.name == "LATIN_BOLD"
    assert by_text["結"].role.name == "JP_BOLD"  # 同梱の Noto Sans JP Bold
    assert by_text["本"].role.name == "JP" and not by_text["本"].bold
