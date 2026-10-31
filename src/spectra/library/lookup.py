"""識別子からオンラインで書誌情報を照会する（利用者が有効にした場合だけ使う）。"""

from __future__ import annotations

import httpx

from . import arxiv, crossref
from .errors import LookupFailed
from .identifiers import find_arxiv
from .models import Metadata


def lookup(md: Metadata, client: httpx.Client | None = None) -> Metadata | None:
    """DOI があれば Crossref、見つからないか失敗したら arXiv に照会する。

    識別子がなければ None。どちらも見つからなければ None、通信に失敗しただけなら LookupFailed を送出する。
    """
    if not md.doi and not md.arxiv_id:
        return None
    # arXiv の DOI（10.48550/arXiv.…）は Crossref ではなく DataCite の管理なので、arXiv に直接照会する
    arxiv_id = md.arxiv_id or (find_arxiv(md.doi) if md.doi else None)
    doi = "" if md.doi and not md.arxiv_id and arxiv_id else md.doi
    own = client is None
    client = client or httpx.Client(follow_redirects=True)
    failure: LookupFailed | None = None
    try:
        if doi:
            try:
                found = crossref.fetch(doi, client)
            except LookupFailed as e:
                failure, found = e, None
            if found:
                found.arxiv_id = found.arxiv_id or md.arxiv_id
                return found
        if arxiv_id:
            found = arxiv.fetch(arxiv_id, client)
            if found:
                found.doi = found.doi or md.doi
                return found
    finally:
        if own:
            client.close()
    if failure:
        raise failure
    return None

