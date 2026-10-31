"""PDF 自体から書誌情報を取り出す（外部に通信しない）。

優先順位: XMP・文書情報の埋め込みメタデータ → 1〜2 ページ目の本文の DOI / arXiv ID
（左余白の回転した arXiv 刻印を含む）→ 1 ページ目上部の最大フォントの行をタイトルとみなす。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

import pymupdf

from .identifiers import find_arxiv, find_doi, normalize_doi
from .models import Author, Metadata

log = logging.getLogger(__name__)

TEXT_FLAGS = pymupdf.TEXT_MEDIABOX_CLIP  # 合字（ﬁ など）は分解する
ID_PAGES = 2

_JUNK_TITLE = re.compile(
    r"^(?:untitled|title|no title|document\d*|microsoft word\b.*|.*\.(?:pdf|docx?|tex|dvi|ps|indd|qxd)"
    r"|arxiv:.*|doi:.*|10\.\d{4,9}/.*|[\w\-]*\d[\w\-]*|.*\bpdf\b.*\bcreated\b.*)$",
    re.IGNORECASE,
)
_XMP_DOI = re.compile(r"<(?:prism:doi|pdfx:doi|crossmark:DOI)>\s*([^<]+?)\s*</", re.IGNORECASE)
_XMP_TITLE = re.compile(r"<dc:title>.*?<rdf:li[^>]*>\s*([^<]+?)\s*</rdf:li>", re.IGNORECASE | re.DOTALL)
_SPACES = re.compile(r"\s+")


def clean_text(text: str) -> str:
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def is_junk_title(title: str, file_name: str = "") -> bool:
    """'Microsoft Word - draft.docx' やファイル名そのものなど、タイトルとして使えない値か。"""
    t = clean_text(title)
    if len(t) < 4 or not re.search(r"[^\W\d_]", t):
        return True
    if file_name and t.lower() == Path(file_name).stem.lower():
        return True
    return bool(_JUNK_TITLE.match(t))


def extract_local(doc: pymupdf.Document, file_name: str) -> Metadata:
    embedded = _embedded(doc, file_name)
    text = "\n".join(doc[i].get_text("text", flags=TEXT_FLAGS) for i in range(min(ID_PAGES, doc.page_count)))
    found = Metadata(doi=find_doi(text) or "", arxiv_id=find_arxiv(text) or "")
    md = embedded.merged(found)
    if not md.title and doc.page_count:
        title = _largest_title(doc[0])
        if title and not is_junk_title(title, file_name):
            md.title = title
            md.source = md.source or "heuristic"
    return md


def _embedded(doc: pymupdf.Document, file_name: str) -> Metadata:
    info = doc.metadata or {}
    try:
        xmp = doc.get_xml_metadata() or ""
    except Exception:  # 壊れた XMP は無視する
        log.debug("unreadable XMP", exc_info=True)
        xmp = ""
    md = Metadata()
    title = clean_text(info.get("title") or "")
    if not title and (m := _XMP_TITLE.search(xmp)):
        title = clean_text(m.group(1))
    if title and not is_junk_title(title, file_name):
        md.title = title
    if m := _XMP_DOI.search(xmp):
        md.doi = normalize_doi(m.group(1))
    else:
        md.doi = find_doi(" ".join(info.get(k) or "" for k in ("subject", "keywords"))) or ""
    md.authors = _parse_authors(info.get("author") or "")
    if not md.is_empty():
        md.source = "embedded"
    return md


def _parse_authors(raw: str) -> list[Author]:
    """'Alice Example, Bob Sample and Carol Test' の形だけを解釈する。

    'Example, Alice' のような姓・名の順の表記は区切りと見分けられないので、
    1 語だけの要素が混じる場合は諦める。
    """
    raw = clean_text(raw)
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"\s*(?:;|,|\band\b|&)\s*", raw) if p.strip()]
    if not parts or any(len(p.split()) < 2 for p in parts):
        return []
    return [Author(family=p.split()[-1], given=" ".join(p.split()[:-1])) for p in parts]


def _largest_title(page: pymupdf.Page) -> str:
    """ページ上半分の横書きの行のうち、最も大きいフォントで連続する行をつなげる。"""
    lines = []
    for block in page.get_text("dict", flags=TEXT_FLAGS)["blocks"]:
        for line in block.get("lines", []):
            dx, dy = line["dir"]
            if abs(dy) > 0.01 or dx < 0:  # 回転した刻印などは除く
                continue
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            bbox = pymupdf.Rect(line["bbox"])
            if bbox.y0 > page.rect.height * 0.5:
                continue
            text = clean_text("".join(s["text"] for s in spans))
            if len(re.findall(r"[^\W\d_]", text)) < 3:  # ページ番号・記号だけの行
                continue
            lines.append((round(max(s["size"] for s in spans), 1), bbox, text))
    if not lines:
        return ""
    size = max(s for s, _, _ in lines)
    picked = sorted(((b, t) for s, b, t in lines if s >= size - 0.5), key=lambda bt: (bt[0].y0, bt[0].x0))
    out = [picked[0][1]]
    prev = picked[0][0]
    for bbox, text in picked[1:]:
        if bbox.y0 - prev.y1 > size * 1.2:  # 離れた位置の大きな文字はタイトルの続きではない
            break
        if abs(bbox.y0 - prev.y0) < 1:  # 同じ行で分かれたスパン
            out[-1] += " " + text
        else:
            out.append(text)
        prev = bbox
    return clean_text(" ".join(out))
