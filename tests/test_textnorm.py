from spectra.core.textnorm import normalize_translation


def test_spaces_inside_fullwidth_parens_are_removed():
    assert normalize_translation("当性が高い（ 説明 ）。") == "当性が高い（説明）。"


def test_latin_only_parens_become_halfwidth():
    assert normalize_translation("高い（ Moses, 1992 ）。") == "高い(Moses, 1992)。"
    assert normalize_translation('フラックス（<x id="0"/>）は') == 'フラックス(<x id="0"/>)は'


def test_japanese_content_keeps_fullwidth_parens():
    assert normalize_translation("（例：Orton et al. 1991）") == "（例：Orton et al. 1991）"
