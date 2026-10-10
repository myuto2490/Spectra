"""出力形式（翻訳のみ / 左右対訳 / 交互対訳）。"""

from __future__ import annotations

import contextlib

import pymupdf

from .options import OutputMode

# garbage=4：不要なオブジェクトを捨て、中身（ストリーム）まで同一のオブジェクトを 1 つにまとめる。
# 段落中の数式を元の見た目のまま移すと、元の論文のフォントがページごとに複製されるため、これでまとめる。
# 小さなオブジェクトはオブジェクトストリームにまとめて圧縮する。
SAVE_OPTIONS = {"garbage": 4, "deflate": True, "use_objstms": 1}


def build_output(original: pymupdf.Document, translated: pymupdf.Document, mode: OutputMode, pages: range) -> pymupdf.Document:
    """mode に応じた出力文書を作る。translated は original と同じページ構成。"""
    if mode == OutputMode.TRANSLATED:
        return translated
    out = pymupdf.open()
    if mode == OutputMode.INTERLEAVE:
        for pno in pages:
            out.insert_pdf(original, from_page=pno, to_page=pno)
            out.insert_pdf(translated, from_page=pno, to_page=pno)
        return out
    # 左右対訳：原文と訳文を 1 枚の横長ページに並べる
    for pno in pages:
        r = original[pno].rect
        page = out.new_page(width=r.width * 2, height=r.height)
        page.show_pdf_page(pymupdf.Rect(0, 0, r.width, r.height), original, pno)
        page.show_pdf_page(pymupdf.Rect(r.width, 0, r.width * 2, r.height), translated, pno)
    return out


def finalize(doc: pymupdf.Document) -> bytes:
    """フォントをサブセット化して保存用のバイト列にする。"""
    # 既にサブセット化したフォントの再処理で出る MuPDF の警告は表示しない
    pymupdf.TOOLS.mupdf_display_errors(False)
    try:
        # サブセット化に失敗してもファイルサイズが増えるだけなので続行する
        with contextlib.suppress(Exception):
            doc.subset_fonts()
    finally:
        pymupdf.TOOLS.mupdf_display_errors(True)
    return doc.tobytes(**SAVE_OPTIONS)
