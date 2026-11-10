"""論文を BibTeX のエントリに変換する。"""

from __future__ import annotations

import re
import unicodedata

from .models import Paper

_STOP_WORDS = {"a", "an", "the", "on", "of", "in", "for", "to", "and", "with", "towards", "toward"}
_SPECIAL = re.compile(r"[&%$#_{}\\~^]")
_REPLACEMENTS = {
    **{c: f"\\{c}" for c in "&%$#_{}"},
    "\\": r"\textbackslash{}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_UNSAFE_VERBATIM = re.compile(r"[{}\\\s\x00-\x1f\x7f]")


def cite_key(paper: Paper) -> str:
    """筆頭著者の姓 + 年 + タイトルの最初の（冠詞などを除いた）語。例: vaswani2017attention"""
    md = paper.metadata
    family = md.authors[0].family if md.authors else ""
    words = [w for w in re.findall(r"[^\W_]+", _ascii(md.title).lower()) if w not in _STOP_WORDS]
    key = f"{_ascii(family).lower()}{md.year or ''}{words[0] if words else ''}"
    key = re.sub(r"[^a-z0-9]", "", key)
    return key or f"paper{paper.id}"


def to_bibtex(paper: Paper) -> str:
    """書誌情報は PDF・Crossref・ほかの端末から来るので、どの項目も TeX の命令や波括弧として働かない形にして書く。"""
    md = paper.metadata
    is_article = bool(md.venue and md.venue != "arXiv" and (md.volume or md.doi))
    pages = _escape(md.pages)
    fields: list[tuple[str, str]] = [
        ("title", f"{{{_escape(md.title or paper.display_title)}}}"),
        ("author", " and ".join(_author(a.family, a.given) for a in md.authors)),
        ("year", str(md.year or "")),
        ("journal" if is_article else "howpublished", _escape(md.venue)),
        ("volume", _escape(md.volume)),
        ("number", _escape(md.issue)),
        ("pages", pages.replace("-", "--") if "--" not in pages else pages),
        ("doi", _verbatim(md.doi)),
    ]
    if md.arxiv_id:
        fields += [("eprint", _verbatim(md.arxiv_id)), ("archivePrefix", "arXiv")]
    fields.append(("url", _verbatim(md.url)))
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields if value)
    return f"@{'article' if is_article else 'misc'}{{{cite_key(paper)},\n{body}\n}}\n"


def _author(family: str, given: str) -> str:
    family, given = _escape(family), _escape(given)
    return f"{family}, {given}" if given else family


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _escape(text: str) -> str:
    """文章の項目: TeX の特殊文字をそのままの文字として出るようにし、改行などの制御文字は空白にする。"""
    return _SPECIAL.sub(lambda m: _REPLACEMENTS[m.group(0)], _CONTROL.sub(" ", text))


def _verbatim(text: str) -> str:
    """DOI・URL・arXiv ID: そのまま書く項目なので、エスケープせずに、項目を閉じたり命令になったりする文字を取り除く。"""
    return _UNSAFE_VERBATIM.sub("", text)
