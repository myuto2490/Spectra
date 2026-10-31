"""論文を BibTeX のエントリに変換する。"""

from __future__ import annotations

import re
import unicodedata

from .models import Paper

_STOP_WORDS = {"a", "an", "the", "on", "of", "in", "for", "to", "and", "with", "towards", "toward"}
_SPECIAL = re.compile(r"([&%$#_])")


def cite_key(paper: Paper) -> str:
    """筆頭著者の姓 + 年 + タイトルの最初の（冠詞などを除いた）語。例: vaswani2017attention"""
    md = paper.metadata
    family = md.authors[0].family if md.authors else ""
    words = [w for w in re.findall(r"[^\W_]+", _ascii(md.title).lower()) if w not in _STOP_WORDS]
    key = f"{_ascii(family).lower()}{md.year or ''}{words[0] if words else ''}"
    key = re.sub(r"[^a-z0-9]", "", key)
    return key or f"paper{paper.id}"


def to_bibtex(paper: Paper) -> str:
    md = paper.metadata
    is_article = bool(md.venue and md.venue != "arXiv" and (md.volume or md.doi))
    fields: list[tuple[str, str]] = [
        ("title", f"{{{_escape(md.title or paper.display_title)}}}"),
        ("author", " and ".join(f"{a.family}, {a.given}" if a.given else a.family for a in md.authors)),
        ("year", str(md.year or "")),
        ("journal" if is_article else "howpublished", _escape(md.venue)),
        ("volume", md.volume),
        ("number", md.issue),
        ("pages", md.pages.replace("-", "--") if "--" not in md.pages else md.pages),
        ("doi", md.doi),
    ]
    if md.arxiv_id:
        fields += [("eprint", md.arxiv_id), ("archivePrefix", "arXiv")]
    fields.append(("url", md.url))
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields if value)
    return f"@{'article' if is_article else 'misc'}{{{cite_key(paper)},\n{body}\n}}\n"


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _escape(text: str) -> str:
    return _SPECIAL.sub(r"\\\1", text)
