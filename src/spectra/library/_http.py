"""書誌情報サービスへの HTTP 呼び出しの共通処理。送るのは識別子だけで、本文は送らない。"""

from __future__ import annotations

import httpx

from .. import __version__
from .errors import LookupFailed

USER_AGENT = f"Spectra/{__version__} (+https://github.com/myuto2490/Spectra)"
TIMEOUT = 10.0


def get(client: httpx.Client, url: str, *, params: dict | None = None, service: str) -> httpx.Response | None:
    """GET して応答を返す。404 なら None、それ以外の失敗は LookupFailed を送出する。"""
    try:
        r = client.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise LookupFailed(f"{service} に接続できません（{type(e).__name__}）") from e
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise LookupFailed(f"{service} がエラーを返しました（HTTP {r.status_code}）")
    return r
