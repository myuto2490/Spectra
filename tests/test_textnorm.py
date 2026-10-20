from spectra.core.textnorm import is_degenerate, normalize_translation


def test_spaces_inside_fullwidth_parens_are_removed():
    assert normalize_translation("当性が高い（ 説明 ）。") == "当性が高い（説明）。"


def test_latin_only_parens_become_halfwidth():
    assert normalize_translation("高い（ Moses, 1992 ）。") == "高い(Moses, 1992)。"
    assert normalize_translation('フラックス（<x id="0"/>）は') == 'フラックス(<x id="0"/>)は'


def test_japanese_content_keeps_fullwidth_parens():
    assert normalize_translation("（例：Orton et al. 1991）") == "（例：Orton et al. 1991）"


def test_leaked_mathml_tags_are_removed():
    assert normalize_translation("確率&lt;math&gt;&lt;mi&gt;p&lt;/mi&gt;&lt;/math&gt;で") == "確率pで"
    assert normalize_translation("x &lt; y") == "x &lt; y"


def test_runs_of_combining_marks_are_removed():
    garbage = "".join(chr(c) for c in range(0x0334, 0x0350))
    assert normalize_translation("誤差" + garbage + "は") == "誤差は"
    assert is_degenerate("誤差" + garbage)
    assert not is_degenerate("café がぎ")
