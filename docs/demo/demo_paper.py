"""README 用のデモ論文と、その訳文を使う翻訳エンジン。

本文・訳文ともにこのリポジトリのために書き下ろしたもの（著作権上の問題がない素材）。
使い方: python docs/demo/demo_paper.py  → docs/demo/demo.pdf
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

from spectra.core.markup import to_plain
from spectra.translate.base import Translator

W, H = 595, 842
L, R = (56, 290), (305, 539)
SIZE, PITCH = 9.6, 11.6

TEXT = {
    "abstract": (
        "Researchers increasingly read papers written in a second language. Machine translation helps, "
        "but copying text out of a PDF destroys figures, tables and equations. We present a method that "
        "replaces only the running text of a paper while leaving every figure, table and formula exactly "
        "where it was. Our system detects the regions that must not change, translates the remaining "
        "paragraphs, and typesets the result into the original boxes."
    ),
    "intro1": (
        "Reading a paper is a visual task. Readers move between a paragraph and the figure it describes, "
        "and they rely on the position of each element on the page. A translation that changes the layout "
        "forces the reader to search for content that used to be in a familiar place."
    ),
    "method1": (
        "Each text block is classified as body text, heading, caption or footnote. Blocks that belong to "
        "figures, tables or equations are left untouched. The remaining blocks are sent to the translation "
        "service, and the Japanese text is set with line-breaking rules so that punctuation never starts a line."
    ),
    "method2": (
        "Here the scale factor is the ratio between the width of the box and the natural width of the "
        "translated text. Paragraphs that already fit are never enlarged."
    ),
    "results": (
        "Figure 1 compares three approaches. Copying text into a translator keeps none of the layout, and "
        "optical character recognition loses most equations. Our method keeps the page structure intact "
        "while producing readable Japanese text."
    ),
}

JA = {
    "Layout-Preserving": "科学論文のレイアウトを保った翻訳",
    "Abstract": "概要",
    "Researchers": "研究者が第二言語で書かれた論文を読む機会は増えている。機械翻訳は役に立つが、PDF から文章をコピーすると図・表・数式が失われてしまう。本研究では、論文の本文だけを置き換え、すべての図・表・数式を元の位置にそのまま残す手法を提案する。提案システムは変更してはならない領域を検出し、残りの段落を翻訳して、その結果を元の枠に組版する。",
    "1 Introduction": "1 はじめに",
    "Reading a paper": "論文を読むことは視覚的な作業である。読者は段落とそれが説明する図の間を行き来し、ページ上の各要素の位置を手がかりにしている。レイアウトが変わる翻訳では、見慣れた場所にあった内容を探し直さなければならない。",
    "We control": "忠実さと読みやすさのバランスは、段落を枠に収めるためにどこまで縮小してよいかを制限する 1 つのパラメータ <x id=\"0\"/> で調整する。",
    "2 Method": "2 手法",
    "Each text block": "各テキストブロックを本文・見出し・キャプション・脚注に分類する。図・表・数式に属するブロックには一切手を加えない。残りのブロックを翻訳サービスに送り、句読点が行頭に来ないよう禁則処理を施して日本語の文章を組む。",
    "Here the scale": "ここで縮小率は、枠の幅と訳文の自然な幅との比である。すでに枠に収まる段落を拡大することはない。",
    "3 Results": "3 結果",
    "Figure 1 compares": "図 1 は 3 つの手法を比較したものである。翻訳サービスに文章をコピーする方法ではレイアウトがまったく保たれず、光学文字認識では数式の大半が失われる。提案手法は、読みやすい日本語の文章を生成しつつ、ページの構造をそのまま保つ。",
    "Figure 1:": "図 1：原文と翻訳後の論文におけるレイアウトの類似度。",
}


class DemoTranslator(Translator):
    """デモ論文専用：書き下ろしの訳文を返す。"""

    engine_id = "demo"
    display_name = "デモ"

    def _translate_batch(self, texts: list[str]) -> list[str]:
        out = []
        for t in texts:
            plain = to_plain(t).strip()
            out.append(next((ja for key, ja in JA.items() if plain.startswith(key)), t))
        return out


def _paragraph(page: pymupdf.Page, col: tuple[float, float], y: float, text: str, indent: float = 10) -> float:
    words = text.split()
    x0, x1 = col
    line: list[str] = []
    first = True
    while words:
        avail = (x1 - x0) - (indent if first else 0)
        while words and pymupdf.get_text_length(" ".join([*line, words[0]]), "tiro", SIZE) <= avail:
            line.append(words.pop(0))
        # 両端揃え
        x = x0 + (indent if first else 0)
        if words and len(line) > 1:
            natural = sum(pymupdf.get_text_length(w, "tiro", SIZE) for w in line)
            gap = (avail - natural) / (len(line) - 1)
            for w in line:
                page.insert_text((x, y), w, fontname="tiro", fontsize=SIZE)
                x += pymupdf.get_text_length(w, "tiro", SIZE) + gap
        else:
            page.insert_text((x, y), " ".join(line), fontname="tiro", fontsize=SIZE)
        y += PITCH
        line, first = [], False
    return y


def _center(page: pymupdf.Page, y: float, text: str, font: str, size: float) -> None:
    w = pymupdf.get_text_length(text, font, size)
    page.insert_text(((W - w) / 2, y), text, fontname=font, fontsize=size)


def make(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    _center(page, 78, "Layout-Preserving Translation of Scientific Documents", "tibo", 16)
    _center(page, 106, "Aiko Tanaka        Ben Carter", "tiro", 10.5)
    _center(page, 120, "Example Institute of Technology", "tiro", 10)
    _center(page, 152, "Abstract", "tibo", 11)
    y = _paragraph(page, (110, 485), 170, TEXT["abstract"], indent=0)
    page.draw_line((56, y + 6), (539, y + 6), color=(0.75, 0.75, 0.78), width=0.6)

    # 左段
    top = y + 30
    page.insert_text((L[0], top), "1 Introduction", fontname="tibo", fontsize=11)
    y = _paragraph(page, L, top + 18, TEXT["intro1"])
    # インライン数式（Symbol の α）
    y += 2
    x = L[0] + 10
    for text, font in (("We control the trade-off between fidelity and", "tiro"),):
        page.insert_text((x, y), text, fontname=font, fontsize=SIZE)
    y += PITCH
    x = L[0]
    for text, font in (("readability with a single parameter ", "tiro"), ("a", "symb"), (", which limits", "tiro")):
        page.insert_text((x, y), text, fontname=font, fontsize=SIZE)
        x += pymupdf.get_text_length(text, font, SIZE)
    y += PITCH
    page.insert_text((L[0], y), "how much a paragraph may shrink to fit its box.", fontname="tiro", fontsize=SIZE)
    y += PITCH + 14
    page.insert_text((L[0], y), "2 Method", fontname="tibo", fontsize=11)
    y = _paragraph(page, L, y + 18, TEXT["method1"])
    # 独立数式
    y += 8
    x = 120
    for text, font in (("s", "tiit"), (" = min(1, ", "tiro"), ("w", "tiit"), (" / ", "tiro"), ("w", "tiit")):
        page.insert_text((x, y), text, fontname=font, fontsize=10.5)
        x += pymupdf.get_text_length(text, font, 10.5)
    page.insert_text((x, y + 2.5), "0", fontname="tiro", fontsize=7.5)
    page.insert_text((x + 4, y), ")", fontname="tiro", fontsize=10.5)
    page.insert_text((L[1] - 14, y), "(1)", fontname="tiro", fontsize=SIZE)
    y = _paragraph(page, L, y + 20, TEXT["method2"])

    # 右段：ベクターの棒グラフ
    fx0, fy0, fx1, fy1 = R[0] + 20, top - 8, R[1] - 6, top + 150
    shape = page.new_shape()
    shape.draw_line((fx0, fy1), (fx1, fy1))
    shape.draw_line((fx0, fy0), (fx0, fy1))
    shape.finish(color=(0.3, 0.3, 0.35), width=0.8)
    bars = [("Copy", 0.22, (0.75, 0.75, 0.8)), ("OCR", 0.48, (0.55, 0.62, 0.85)), ("Ours", 0.97, (0.04, 0.52, 1.0))]
    bw = (fx1 - fx0) / (len(bars) * 2)
    for i, (name, v, color) in enumerate(bars):
        bx = fx0 + bw * (0.6 + i * 2)
        shape.draw_rect(pymupdf.Rect(bx, fy1 - (fy1 - fy0 - 8) * v, bx + bw, fy1))
        shape.finish(color=None, fill=color)
        tw = pymupdf.get_text_length(name, "helv", 7.5)
        page.insert_text((bx + (bw - tw) / 2, fy1 + 10), name, fontname="helv", fontsize=7.5)
    for i, t in enumerate(("0.0", "0.5", "1.0")):
        yy = fy1 - (fy1 - fy0 - 8) * (i / 2)
        page.insert_text((fx0 - 15, yy + 2.5), t, fontname="helv", fontsize=6.5)
    shape.commit()
    page.insert_text((R[0], fy1 + 28), "Figure 1: Layout similarity between the original", fontname="tiro", fontsize=8.8)
    page.insert_text((R[0], fy1 + 39), "and the translated paper.", fontname="tiro", fontsize=8.8)
    page.insert_text((R[0], fy1 + 70), "3 Results", fontname="tibo", fontsize=11)
    _paragraph(page, R, fy1 + 88, TEXT["results"])
    doc.set_metadata({"title": "Spectra demo paper", "author": "Spectra"})
    doc.save(path)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("demo.pdf")
    make(out)
    print(out)
