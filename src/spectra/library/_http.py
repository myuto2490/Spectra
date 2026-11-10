"""書誌情報サービスへの HTTP 呼び出しの共通処理。送るのは識別子だけで、本文は送らない。"""

from __future__ import annotations

import httpx

from .. import __version__
from .errors import LookupFailed

USER_AGENT = f"Spectra/{__version__} (+https://github.com/myuto2490/Spectra)"
TIMEOUT = 10.0
MAX_BODY = 5_000_000  # 書誌情報 1 件の応答は、大きくても数百 KB


def get(client: httpx.Client, url: str, *, params: dict | None = None, service: str) -> httpx.Response | None:
    """GET して応答を返す。404 なら None、それ以外の失敗は LookupFailed を送出する。

    本文は MAX_BODY までしか読まない（想定外に大きな応答でメモリを使い切らない）。
    """
    try:
        with client.stream("GET", url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT) as r:
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise LookupFailed(f"{service} がエラーを返しました（HTTP {r.status_code}）")
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > MAX_BODY:
                    raise LookupFailed(f"{service} の応答が大きすぎます")
            # 読み終えた本文で応答を作り直す（本文は展開済みなので、圧縮の指定は引き継がない）
            headers = {"content-type": r.headers.get("content-type", "")}
            return httpx.Response(r.status_code, headers=headers, content=bytes(body), request=r.request)
    except httpx.HTTPError as e:
        raise LookupFailed(f"{service} に接続できません（{type(e).__name__}）") from e
