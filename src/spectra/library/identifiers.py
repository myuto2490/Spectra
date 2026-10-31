"""本文から DOI と arXiv ID を見つけて正規化する。"""

from __future__ import annotations

import re

_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)
_DOI_PREFIX = re.compile(r"^(?:https?://)?(?:dx\.)?(?:doi\.org/)|^doi:\s*", re.IGNORECASE)
_TRAILING = ".,;:"
_PAIRS = {")": "(", "]": "[", "}": "{"}

# 新形式（2007 年以降）: YYMM.NNNN(N)。接頭辞 arXiv: か arxiv.org の URL、DOI 10.48550/arXiv. を手がかりにする
_ARXIV_NEW = re.compile(
    r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/|10\.48550/arxiv\.)(\d{2}(?:0[1-9]|1[0-2])\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
# 旧形式: hep-th/9901001、math.AG/0601001。誤検出を避けるため接頭辞を必須にする
_ARXIV_OLD = re.compile(
    r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)([a-z]+(?:-[a-z]+)*(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?",
    re.IGNORECASE,
)


def normalize_doi(s: str) -> str:
    """小文字にし、doi.org/ や doi: を外し、末尾の句読点と対応しない閉じ括弧を除く。"""
    doi = _DOI_PREFIX.sub("", s.strip()).strip().lower()
    while doi:
        last = doi[-1]
        unmatched = last in _PAIRS and doi.count(_PAIRS[last]) < doi.count(last)
        if last not in _TRAILING and not unmatched:
            break
        doi = doi[:-1]
    return doi


def find_doi(text: str) -> str | None:
    for m in _DOI.finditer(text):
        doi = normalize_doi(m.group(1))
        if "/" in doi and not doi.endswith("/"):
            return doi
    return None


def find_arxiv(text: str) -> str | None:
    """最初に見つかった arXiv ID をバージョンなしで返す。"""
    m = _ARXIV_NEW.search(text) or _ARXIV_OLD.search(text)
    if not m:
        return None
    ident = m.group(1)
    # 旧形式のアーカイブ名は小文字、サブジェクトクラスは大文字（math.AG）
    if "/" in ident:
        archive, number = ident.split("/", 1)
        name, _, cls = archive.partition(".")
        ident = f"{name.lower()}{'.' + cls.upper() if cls else ''}/{number}"
    return ident
