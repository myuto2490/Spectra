"""訳文が見出しや別の段落と重なる・訳されないまま残る不具合の再発防止（Icarus 186 の Hesman et al. など）。"""

import pymupdf

from spectra.core.extract import extract_blocks


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
