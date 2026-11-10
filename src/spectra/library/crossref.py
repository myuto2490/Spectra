"""Crossref REST API から DOI の書誌情報を取得する。"""

from __future__ import annotations

import html
import re
from urllib.parse import quote

import httpx

from . import _http
from .errors import LookupFailed
from .identifiers import normalize_doi
from .metadata import clean_text
from .models import Author, Metadata

API = "https://api.crossref.org/works/"
_JATS_TITLE = re.compile(r"<(jats:)?title[^>]*>.*?</(jats:)?title>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def fetch(doi: str, client: httpx.Client) -> Metadata | None:
    r = _http.get(client, API + quote(normalize_doi(doi), safe="/"), service="Crossref")
    if r is None:
        return None
    try:
        msg = r.json()["message"]
        return parse(msg)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as e:
        # 形の違う応答（項目の型が想定と違うなど）も、落とさずに照会の失敗として扱う
        raise LookupFailed("Crossref の応答を解釈できません") from e


def parse(msg: dict) -> Metadata:
    return Metadata(
        title=clean_text(_first(msg.get("title"))),
        authors=[_author(a) for a in msg.get("author") or [] if a.get("family") or a.get("name")],
        year=_year(msg),
        venue=clean_text(_first(msg.get("container-title"))),
        volume=str(msg.get("volume") or ""),
        issue=str(msg.get("issue") or ""),
        pages=str(msg.get("page") or "").replace("--", "-"),
        doi=normalize_doi(msg.get("DOI") or ""),
        abstract=strip_jats(msg.get("abstract") or ""),
        url=str(msg.get("URL") or ""),
        source="crossref",
    )


def strip_jats(text: str) -> str:
    """JATS XML の要約から見出し（<jats:title>Abstract</jats:title>）とタグを除く。"""
    return clean_text(html.unescape(_TAG.sub(" ", _JATS_TITLE.sub(" ", text))))


def _first(value) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _author(raw: dict) -> Author:
    if raw.get("family"):
        return Author(clean_text(raw["family"]), clean_text(raw.get("given") or ""))
    return Author(clean_text(raw["name"]))  # 団体著者


def _year(msg: dict) -> int | None:
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            return int(parts[0][0])
    return None
