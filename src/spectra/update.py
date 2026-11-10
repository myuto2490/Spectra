"""更新の確認（通知だけ）。GitHub の最新リリースを問い合わせ、新しい版があれば知らせる。

- 問い合わせ先は ``https://api.github.com/repos/myuto2490/Spectra/releases/latest`` だけ。
  送るのは通常の HTTPS の要求と User-Agent のみで、端末の ID やライブラリの情報は送らない。
- ダウンロードと入れ替えは利用者がブラウザで行う。開く URL は Spectra のリリースのページに限る。
- 画面に依存しない。設定（update_check など）の読み書きは呼び出し側が行う。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import httpx

from . import __version__
from .errors import SpectraError

LATEST_URL = "https://api.github.com/repos/myuto2490/Spectra/releases/latest"
RELEASES_URL = "https://github.com/myuto2490/Spectra/releases/"
USER_AGENT = "Spectra (+https://github.com/myuto2490/Spectra)"
CHECK_INTERVAL = 24 * 60 * 60  # 起動時の確認は前回から 24 時間以上あけて行う
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_BODY = 1_000_000  # 応答がこれより大きければ読まない（想定外の応答を避ける）

_VERSION = re.compile(r"^v?(\d+(?:\.\d+){0,3})$")
_LEADING = re.compile(r"^v?(\d+(?:\.\d+){0,3})")
_RELEASE_PATH = re.compile(r"[A-Za-z0-9._/+-]*")


class UpdateCheckError(SpectraError):
    title = "更新を確認できませんでした"
    hint = "インターネットへの接続を確認して、あとでもう一度お試しください。"


@dataclass(frozen=True)
class UpdateInfo:
    version: str  # 表示用（先頭の v は除く）
    url: str  # 開くページ（RELEASES_URL で始まるもの）


def parse_version(text: str) -> tuple[int, ...] | None:
    """「0.4.0」「v0.4.0」を数字の組にする。0.4.0rc1 のような版や読めない文字列は None。"""
    m = _VERSION.match(text.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def _current_tuple(current: str) -> tuple[int, ...] | None:
    """今の版は 0.4.0a1 のような版でも、先頭の数字の部分で比べる。"""
    m = _LEADING.match(current.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def is_newer(latest: str, current: str) -> bool:
    """latest が current より新しいか。どちらかが読めなければ False（通知しない）。"""
    a, b = parse_version(latest), _current_tuple(current)
    if a is None or b is None:
        return False
    a, b = _pad(a, b)
    return a > b


def is_release_url(url: object) -> bool:
    """開いてよい URL か。応答の URL をそのまま信じず、Spectra のリリースのページだけを許す。"""
    if not isinstance(url, str) or not url.startswith(RELEASES_URL):
        return False
    # 続きは、リリースのページの URL に出てくる文字だけ（"../" で別のページへ出る形や、問い合わせ・空白は通さない）
    rest = url[len(RELEASES_URL):]
    return _RELEASE_PATH.fullmatch(rest) is not None and ".." not in rest


def is_due(last_checked: float, now: float | None = None) -> bool:
    """起動時に確認する時期か。時計が戻っていた場合（last_checked が未来）も確認する。"""
    now = time.time() if now is None else now
    return last_checked <= 0 or last_checked > now or now - last_checked >= CHECK_INTERVAL


def should_check_on_startup(update_check: str, last_checked: float, now: float | None = None) -> bool:
    """起動時に問い合わせるか。update_check が "on"（"ask" は未回答、"off" はオフ）で、前回から 24 時間以上たっているときだけ。"""
    return update_check == "on" and is_due(last_checked, now)


def parse_release(data: object, current: str = __version__) -> UpdateInfo | None:
    """リリースの応答から、current より新しい版の情報を取り出す。なければ None。"""
    if not isinstance(data, dict):
        raise UpdateCheckError("応答の形式が想定と違います。")
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str) or parse_version(tag) is None:
        return None
    if not is_newer(tag, current):
        return None
    version = tag.strip().lstrip("v")
    url = data.get("html_url")
    if not is_release_url(url):
        # 想定外の URL は使わず、リリース一覧のページへ案内する
        url = RELEASES_URL
    return UpdateInfo(version=version, url=url)


def fetch_latest(client: httpx.Client | None = None) -> object:
    """最新リリースの応答（JSON）を取得する。失敗は UpdateCheckError。"""
    owns = client is None
    c = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
    try:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        with c.stream("GET", LATEST_URL, headers=headers) as r:
            if r.status_code == 404:
                return {}  # リリースがまだ無い
            if r.status_code in (403, 429):
                raise UpdateCheckError("GitHub への問い合わせが多すぎます。", hint="しばらくしてからお試しください。")
            if r.status_code != 200:
                raise UpdateCheckError(f"GitHub から想定外の応答がありました（HTTP {r.status_code}）。")
            body = bytearray()
            for chunk in r.iter_bytes():  # 上限を超えたところで読むのをやめる
                body += chunk
                if len(body) > MAX_BODY:
                    raise UpdateCheckError("応答が大きすぎます。")
        return json.loads(bytes(body))
    except httpx.TimeoutException as e:
        raise UpdateCheckError("接続がタイムアウトしました。") from e
    except httpx.HTTPError as e:
        raise UpdateCheckError() from e
    except ValueError as e:
        raise UpdateCheckError("応答の形式が想定と違います。") from e
    finally:
        if owns:
            c.close()


def check_for_update(
    current: str = __version__, client: httpx.Client | None = None, skipped: str = ""
) -> UpdateInfo | None:
    """新しい版があれば UpdateInfo を返す。なければ None。失敗は UpdateCheckError。

    skipped は「この版を通知しない」と選んだ版。同じ版なら None を返す（もっと新しい版が出れば通知する）。
    """
    info = parse_release(fetch_latest(client), current)
    if info is not None and skipped and parse_version(skipped) == parse_version(info.version):
        return None
    return info
