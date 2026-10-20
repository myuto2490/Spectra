"""テスト用の合成論文 PDF を生成する。

実在の論文は著作権の都合でリポジトリに含めないため、論文に典型的な要素
（タイトル・著者・2 段組・インライン数式・独立数式・図・表・脚注・参考文献・回転スタンプ）
を PyMuPDF で描いて再現する。
"""

from __future__ import annotations

import pymupdf

BODY = "tiro"  # Times-Roman
BOLD = "tibo"  # Times-Bold
MATH = "symb"  # Symbol（数式フォントとして判定される）
SIZE = 10.0
PITCH = 12.0

LEFT_COL = (56.0, 290.0)
RIGHT_COL = (305.0, 539.0)

LOREM = ["Deep", "neural", "networks", "have", "achieved", "remarkable", "results", "in", "many", "tasks", "such", "as", "image", "recognition", "and", "machine", "translation", "while", "requiring", "large", "amounts", "of", "labeled", "data", "for", "training", "which", "is", "often", "expensive", "to", "collect", "in", "practice", "for", "new", "domains"]


def write_runs(page: pymupdf.Page, x: float, y: float, runs: list[tuple[str, str, float]], rise: float = 0.0) -> float:
    """(テキスト, フォント, サイズ) の列を 1 行に書き、終端の x を返す。"""
    for text, font, size in runs:
        page.insert_text((x, y - rise), text, fontname=font, fontsize=size)
        x += pymupdf.get_text_length(text, fontname=font, fontsize=size)
    return x


def write_paragraph(page: pymupdf.Page, col: tuple[float, float], y: float, words: list[str], lines: int) -> float:
    """col の幅で単語を詰めて lines 行書く。次の行のベースライン y を返す。"""
    x0, x1 = col
    width = x1 - x0
    idx = 0
    for _ in range(lines):
        line: list[str] = []
        while True:
            w = words[idx % len(words)]
            trial = " ".join([*line, w])
            if pymupdf.get_text_length(trial, fontname=BODY, fontsize=SIZE) > width and line:
                break
            line.append(w)
            idx += 1
        page.insert_text((x0, y), " ".join(line), fontname=BODY, fontsize=SIZE)
        y += PITCH
    return y


def make_paper(path: str | None = None) -> pymupdf.Document:
    doc = pymupdf.open()
    _page_one(doc.new_page(width=595, height=842))
    _page_two(doc.new_page(width=595, height=842))
    _page_three(doc.new_page(width=595, height=842))
    if path:
        doc.save(path)
    return doc


def _page_one(page: pymupdf.Page) -> None:
    title = "Learning Robust Representations for Testing"
    tw = pymupdf.get_text_length(title, fontname=BOLD, fontsize=17)
    page.insert_text(((595 - tw) / 2, 72), title, fontname=BOLD, fontsize=17)
    # 2 行目のタイトル（行ごとに別ブロックになる）
    sub = "with Synthetic Papers"
    sw = pymupdf.get_text_length(sub, fontname=BOLD, fontsize=17)
    page.insert_text(((595 - sw) / 2, 92), sub, fontname=BOLD, fontsize=17)
    for i, name in enumerate(["Alice Example", "Bob Sample", "Carol Test"]):
        page.insert_text((110 + i * 140, 130), name, fontname=BODY, fontsize=SIZE)
        page.insert_text((110 + i * 140, 142), "Example University", fontname=BODY, fontsize=SIZE)

    aw = pymupdf.get_text_length("Abstract", fontname=BOLD, fontsize=12)
    page.insert_text(((595 - aw) / 2, 180), "Abstract", fontname=BOLD, fontsize=12)
    y = write_paragraph(page, (100, 495), 200, LOREM, 4)

    # インライン数式を含む段落（Symbol フォントの α, β）
    y += 8
    x = write_runs(page, 56, y, [("We set the learning rate ", BODY, SIZE), ("a", MATH, SIZE)])
    x = write_runs(page, x, y + 2, [("t", BODY, 7)])  # 下付き
    write_runs(page, x, y, [(" to a small value and the momentum ", BODY, SIZE), ("b", MATH, SIZE), (" to one.", BODY, SIZE)])
    y += PITCH
    page.insert_text((56, y), "This choice makes the training stable across all of our runs.", fontname=BODY, fontsize=SIZE)

    # 独立数式
    y += 30
    write_runs(page, 230, y, [("a", MATH, SIZE), (" = ", MATH, SIZE), ("b", MATH, SIZE), (" + ", MATH, SIZE), ("g", MATH, SIZE)])
    page.insert_text((520, y), "(1)", fontname=BODY, fontsize=SIZE)

    # 見出しと本文
    y += 30
    page.insert_text((56, y), "1 Introduction", fontname=BOLD, fontsize=12)
    y = write_paragraph(page, (56, 539), y + 18, LOREM[5:] + LOREM, 3)

    # 脚注
    page.insert_text((56, 780), "1 Code is available from the authors upon reasonable request.", fontname=BODY, fontsize=8)

    # arXiv 風の回転スタンプ
    page.insert_text((30, 600), "arXiv:2401.00001v1 [cs.LG] 1 Jan 2024", fontname=BODY, fontsize=18, rotate=90)


def _page_two(page: pymupdf.Page) -> None:
    # ベクター図: 枠・折れ線・目盛ラベル（小さな文字）
    fig = pymupdf.Rect(100, 70, 300, 230)
    shape = page.new_shape()
    shape.draw_rect(fig)
    shape.draw_polyline([(110, 220), (150, 180), (200, 150), (250, 100), (290, 90)])
    for i in range(5):
        shape.draw_line((110 + i * 45, 230), (110 + i * 45, 226))
    shape.finish(color=(0, 0, 0.6), width=1)
    shape.commit()
    for i in range(5):
        page.insert_text((106 + i * 45, 240), f"{i * 10}", fontname=BODY, fontsize=7)
    page.insert_text((180, 250), "training steps", fontname=BODY, fontsize=7)

    # ラスタ画像の図
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 48), 0)
    pix.clear_with(200)
    page.insert_image(pymupdf.Rect(340, 70, 500, 190), pixmap=pix)
    page.insert_text((360, 130), "Encoder", fontname=BODY, fontsize=9)

    page.insert_text((56, 270), "Figure 1: Training loss of the proposed model over time.", fontname=BODY, fontsize=9)

    # 2 段組で段を跨ぐ段落（左段は文の途中で終わり、右段は小文字で始まる）
    write_paragraph(page, LEFT_COL, 320, LOREM, 6)
    page.insert_text((LEFT_COL[0], 320 + 6 * PITCH), "which suggests that the learned features", fontname=BODY, fontsize=SIZE)
    write_paragraph(page, RIGHT_COL, 320, ["generalize", "well", "to", "unseen", "domains", "and", *LOREM], 4)
    page.insert_text((RIGHT_COL[0], 320 + 4 * PITCH), "are robust to noise.", fontname=BODY, fontsize=SIZE)

    # 罫線の無い表（同じベースラインに複数セル）
    y = 500
    page.insert_text((240, y - 18), "Table 1: Accuracy on the benchmark.", fontname=BODY, fontsize=9)
    for row in (["Model", "Acc", "F1"], ["Baseline", "71.2", "65.0"], ["Ours", "78.9", "74.3"]):
        for j, cell in enumerate(row):
            page.insert_text((200 + j * 90, y), cell, fontname=BODY, fontsize=9)
        y += 12


def _page_three(page: pymupdf.Page) -> None:
    page.insert_text((56, 80), "2 Conclusion", fontname=BOLD, fontsize=12)
    write_paragraph(page, (56, 539), 100, LOREM, 3)
    page.insert_text((56, 160), "References", fontname=BOLD, fontsize=12)
    refs = [
        "[1] A. Example and B. Sample. A study of robust learning. In Proceedings of Testing, 2020.",
        "[2] C. Test. Another paper about representations. Journal of Examples, 12(3):1-10, 2021.",
    ]
    for i, r in enumerate(refs):
        page.insert_text((56, 180 + i * 14), r, fontname=BODY, fontsize=9)
    page.insert_text((56, 240), "A Appendix", fontname=BOLD, fontsize=12)
    write_paragraph(page, (56, 539), 260, LOREM[3:] + LOREM, 2)
    # 数式フォントを使わない独立数式（Word 製の論文に多い）
    x = 240
    for text, font in (("y", "tiit"), (" = max(0, ", BODY), ("x", "tiit"), (") + ", BODY), ("b", "tiit")):
        page.insert_text((x, 320), text, fontname=font, fontsize=SIZE)
        x += pymupdf.get_text_length(text, fontname=font, fontsize=SIZE)
    page.insert_text((520, 320), "(2)", fontname=BODY, fontsize=SIZE)
    # 「Figure 1 shows」で始まる本文（キャプションではない）
    page.insert_text((56, 350), "Figure 1 shows that the loss decreases steadily during training.", fontname=BODY, fontsize=SIZE)


PAPER = (0.96, 0.94, 0.86)  # スキャンした紙の色


def make_scanned(path: str | None = None) -> pymupdf.Document:
    """スキャンした論文を模した PDF：見える文字はページ全体の画像で、その上に透明な OCR テキストを重ねる。"""
    src = make_paper()
    doc = pymupdf.open()
    for sp in src:
        sp.draw_rect(sp.rect, color=None, fill=PAPER, overlay=False)
        pix = sp.get_pixmap(dpi=150)
        page = doc.new_page(width=sp.rect.width, height=sp.rect.height)
        page.insert_image(page.rect, pixmap=pix)
        for block in sp.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                if line["dir"] != (1.0, 0.0):
                    continue
                for span in line["spans"]:
                    font = MATH if "Symbol" in span["font"] else BOLD if "Bold" in span["font"] else BODY
                    page.insert_text(span["origin"], span["text"], fontname=font, fontsize=span["size"], render_mode=3)
    if path:
        doc.save(path)
    return doc
