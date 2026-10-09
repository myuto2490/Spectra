import pytest

from spectra.core.fonts import (
    base_name,
    decode_symbol_font,
    is_math_font,
    is_math_span,
    is_mono_font,
    is_sans_font,
)


@pytest.mark.parametrize("name", ["CMMI10", "ABCDEF+CMSY7", "CMEX10", "MSBM10", "Symbol", "STIXTwoMath-Regular", "CambriaMath", "LMMathItalic10-Regular"])
def test_math_fonts(name):
    assert is_math_font(name)


@pytest.mark.parametrize("name", ["CMR10", "NimbusRomNo9L-Regu", "Times-Roman", "Helvetica", "CMTI10", "LinLibertineT"])
def test_text_fonts_are_not_math(name):
    assert not is_math_font(name)


def test_subset_prefix_removed():
    assert base_name("QWERTY+CMMI10") == "CMMI10"


def test_cmr_is_math_only_when_body_is_not_cm():
    assert is_math_span("CMR10", "softmax", body_font="NimbusRomNo9L-Regu")
    assert not is_math_span("CMR10", "softmax", body_font="CMR10")


def test_mono_and_sans():
    assert is_mono_font("SFTT1000") and is_mono_font("Courier")
    assert is_sans_font("Helvetica-Bold") and not is_sans_font("Times-Roman")


def test_symbol_font_decoding():
    assert decode_symbol_font("Symbol", "a + b") == "α + β"
    assert decode_symbol_font("ABCDEF+SymbolMT", "W") == "Ω"
    assert decode_symbol_font("CMMI10", "a") == "a"
    assert decode_symbol_font("Symbol", "∑") == "∑"
