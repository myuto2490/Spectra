"""arXiv API から arXiv ID の書誌情報を取得する。"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from . import _http
from .errors import LookupFailed
from .identifiers import normalize_doi
from .metadata import clean_text
from .models import Author, Metadata

API = "https://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def fetch(arxiv_id: str, client: httpx.Client) -> Metadata | None:
    r = _http.get(client, API, params={"id_list": arxiv_id, "max_results": "1"}, service="arXiv")
    if r is None:
        return None
    return parse(r.text, arxiv_id)


def parse(xml: str, arxiv_id: str) -> Metadata | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise LookupFailed("arXiv の応答を解釈できません") from e
    entry = root.find("atom:entry", NS)
    if entry is None:
        return None
    # 存在しない ID でも、id が errors を指すエントリが 1 件返る
    if "/api/errors" in (entry.findtext("atom:id", "", NS) or "") or not entry.findtext("atom:title", "", NS).strip():
        return None
    published = entry.findtext("atom:published", "", NS)
    return Metadata(
        title=clean_text(entry.findtext("atom:title", "", NS)),
        authors=[_author(a.findtext("atom:name", "", NS)) for a in entry.findall("atom:author", NS)],
        year=int(published[:4]) if published[:4].isdigit() else None,
        venue=clean_text(entry.findtext("arxiv:journal_ref", "", NS)) or "arXiv",
        doi=normalize_doi(entry.findtext("arxiv:doi", "", NS)),
        arxiv_id=arxiv_id,
        abstract=clean_text(entry.findtext("atom:summary", "", NS)),
        url=f"https://arxiv.org/abs/{arxiv_id}",
        source="arxiv",
    )


def _author(name: str) -> Author:
    parts = clean_text(name).split()
    if len(parts) < 2:
        return Author(" ".join(parts))
    return Author(parts[-1], " ".join(parts[:-1]))
