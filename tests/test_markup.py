from spectra.core.markup import Token, escape, parse, placeholder, to_plain


def test_roundtrip_simple():
    tokens = parse(f"学習率 {placeholder(0)} を小さく設定する。", {0})
    assert tokens == [Token(text="学習率 "), Token(run_id=0), Token(text=" を小さく設定する。")]


def test_missing_placeholder_is_appended():
    tokens = parse("数式が消えた訳文", {0, 1})
    assert [t.run_id for t in tokens if t.run_id is not None] == [0, 1]


def test_duplicate_and_unknown_ids_are_dropped():
    tokens = parse(f'{placeholder(0)}と{placeholder(0)}と<x id="9"/>', {0})
    assert [t.run_id for t in tokens if t.run_id is not None] == [0]


def test_open_close_pair_and_entities():
    tokens = parse('A &amp; B <x id="0"></x> &lt;c&gt; <b>太字</b>', {0})
    text = "".join(t.text for t in tokens)
    assert "A & B" in text and "<c>" in text and "太字" in text and "<b>" not in text
    assert any(t.run_id == 0 for t in tokens)


def test_escape_and_plain():
    m = escape("a < b & c") + placeholder(3)
    assert to_plain(m) == "a < b & c"
