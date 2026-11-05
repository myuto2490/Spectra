"""利用者の設定（API キー以外）。JSON で保存する。"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..core.options import AnalysisOptions, Formality, OutputMode, RenderOptions, TranslateOptions
from . import paths

log = logging.getLogger(__name__)

SCHEMA_VERSION = 6  # 2: 既定モデルを 3.8 Flash から 3.5 Flash-Lite に変更 / 3: 書誌情報の照会を既定でオンに変更 / 4: 同期の設定を追加 / 5: 端末名 / 6: 複数の API キー・更新の確認・右のパネル・読書の位置

KEY_PROVIDERS = ("gemini",)
MAX_KEYS = 20
MAX_KEY_LABEL = 40
_KEY_ID = re.compile(r"^(default|[0-9a-f]{32})$")
UPDATE_CHECK = ("ask", "on", "off")
SIDE_PANEL_TABS = ("details", "translation", "note")
FIT_MODES = ("none", "width", "page")
MAX_READING_STATES = 200


@dataclass
class Settings:
    engine: str = "gemini"
    gemini_model: str = "gemini-3.5-flash-lite"
    auto_fallback: bool = True  # 混雑・上限時に他のモデルへ自動で切り替える
    # API キーの目録（この順に使う）。中身は資格情報ストアにだけ置く: {"id": "default" か 32 桁の 16 進, "provider": "gemini", "label": 名前}
    # "default" は以前からのキー（資格情報ストアのアカウント名 gemini-api-key）
    api_keys: list = field(default_factory=list)
    key_auto_switch: bool = True  # 1 日の上限に達したら次のキーに切り替える
    output_mode: str = OutputMode.TRANSLATED.value
    formality: str = Formality.DEFAULT.value
    translate_captions: bool = True
    translate_footnotes: bool = True
    skip_references: bool = True
    font: str = "builtin"  # builtin（同梱の Noto Sans JP）/ noto-serif / custom
    custom_font_path: str = ""
    verify_layout: bool = True
    use_cache: bool = True
    appearance: str = "auto"  # auto / light / dark
    metadata_lookup: bool = True  # 書誌情報を Crossref / arXiv に照会する（DOI・arXiv ID だけを送る）
    sync_enabled: bool = False  # クラウド同期（同期フォルダ方式）。既定はオフ
    sync_folder: str = ""  # 同期フォルダ（この中に「Spectra Sync」を作る）
    device_name: str = ""  # 同期でほかのパソコンに表示するこの端末の名前（空なら「Mac（1a2b）」の形の既定）
    update_check: str = "ask"  # 新しいバージョンを GitHub に確認するか（ask: まだ尋ねていない / on / off）
    update_last_checked: float = 0.0  # 最後に確認した時刻（秒）
    update_skipped_version: str = ""  # 「この版を通知しない」を選んだ版
    side_panel_tab: str = "details"  # 右のパネルで選んでいたタブ（details / translation / note）
    # 論文ごとの読書の位置（sha256 → {"zoom": 倍率, "fit": none/width/page, "page": 0 始まり, "ratio": 0〜1}）。新しい順に 200 件まで
    reading_state: dict = field(default_factory=dict)
    last_open_dir: str = ""
    last_save_dir: str = ""
    window: dict = field(default_factory=dict)
    schema: int = SCHEMA_VERSION

    # -- 読み書き ------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path or paths.settings_file()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError):
            log.warning("settings file is unreadable, using defaults: %s", path, exc_info=True)
            return cls()
        return cls.from_dict(raw if isinstance(raw, dict) else {})

    @classmethod
    def from_dict(cls, raw: dict) -> Settings:
        """未知のキーは無視し、型や値が不正な項目は既定値に戻す。"""
        default = cls()
        values = {}
        for f in fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            expected = type(getattr(default, f.name))
            if expected is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if isinstance(value, expected) and not (expected is int and isinstance(value, bool)):
                values[f.name] = value
        values.setdefault("schema", 1)
        # 以前は最初に論文を追加するときに照会してよいか尋ねていた（metadata_lookup_asked）。
        # 尋ねる前の「オフ」は選んだ値ではないので新しい既定（オン）にし、自分で選んだ値はそのまま残す
        if values["schema"] < 3 and raw.get("metadata_lookup_asked") is not True:
            values.pop("metadata_lookup", None)
        s = cls(**values)
        s._sanitize()
        return s

    def _sanitize(self) -> None:
        if self.output_mode not in {m.value for m in OutputMode}:
            self.output_mode = OutputMode.TRANSLATED.value
        if self.formality not in (Formality.DEFAULT.value, Formality.POLITE.value):
            self.formality = Formality.DEFAULT.value  # である調
        if self.font == "noto-sans":
            self.font = "builtin"  # Noto Sans JP は同梱になった
        if self.font not in {"builtin", "noto-serif", "custom"}:
            self.font = "builtin"
        from ..translate.gemini import DEFAULT_MODEL, MODELS

        # 以前のバージョンの設定（DeepL / Google / Microsoft）は Gemini に移行する
        if self.engine not in ("gemini", "dummy"):
            self.engine = "gemini"
        if self.gemini_model not in MODELS:
            self.gemini_model = DEFAULT_MODEL
        # 以前の既定（3.8 Flash）のままの設定は、新しい既定に移行する（自分で選び直した場合はそのまま）
        if self.schema < 2 and self.gemini_model == "gemini-3.8-flash":
            self.gemini_model = DEFAULT_MODEL
        if self.appearance not in ("auto", "light", "dark"):
            self.appearance = "auto"
        self.api_keys = _clean_keys(self.api_keys)
        if self.update_check not in UPDATE_CHECK:
            self.update_check = "ask"
        if not math.isfinite(self.update_last_checked) or self.update_last_checked < 0:
            self.update_last_checked = 0.0
        self.update_skipped_version = self.update_skipped_version.strip()[:50]
        if self.side_panel_tab not in SIDE_PANEL_TABS:
            self.side_panel_tab = "details"
        self.reading_state = _clean_reading_state(self.reading_state)
        self.schema = SCHEMA_VERSION

    # -- API キーの目録・読書の位置 ----------------------------------------------------

    def keys_for(self, provider: str = "gemini") -> list[dict]:
        """provider のキーの目録（使う順）。"""
        return [k for k in self.api_keys if k["provider"] == provider]

    def remember_reading(self, sha256: str, zoom: float, fit: str, page: int, ratio: float) -> None:
        """論文の読書の位置を覚える（いちばん新しいものとして、古いものから捨てる）。"""
        self.reading_state.pop(sha256, None)
        self.reading_state[sha256] = {"zoom": zoom, "fit": fit, "page": page, "ratio": ratio}
        self.reading_state = _clean_reading_state(self.reading_state)

    def save(self, path: Path | None = None) -> None:
        """一時ファイルに書いてから置き換える（書き込み中の異常終了で壊れないように）。"""
        path = path or paths.settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(asdict(self), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- コアの設定値へ変換 ------------------------------------------------------

    def analysis_options(self, first_page: int = 0, last_page: int | None = None) -> AnalysisOptions:
        return AnalysisOptions(
            first_page=first_page,
            last_page=last_page,
            translate_captions=self.translate_captions,
            translate_footnotes=self.translate_footnotes,
            skip_references=self.skip_references,
        )

    def translate_options(self) -> TranslateOptions:
        return TranslateOptions(formality=Formality(self.formality))

    def render_options(self) -> RenderOptions:
        from .fonts import resolve_font

        font = resolve_font(self.font, self.custom_font_path)
        return RenderOptions(
            font_path=font.regular,
            bold_font_path=font.bold,
            font_is_serif=font.serif,
            output_mode=OutputMode(self.output_mode),
        )


def _clean_keys(raw) -> list[dict]:
    """キーの目録を検証する。形の合わないもの・重複した ID は捨てる（キーの中身は持たない）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for k in raw if isinstance(raw, list) else []:
        if not isinstance(k, dict):
            continue
        key_id, provider, label = k.get("id"), k.get("provider"), k.get("label", "")
        if not isinstance(key_id, str) or not _KEY_ID.match(key_id) or key_id in seen or provider not in KEY_PROVIDERS:
            continue
        label = " ".join(label.split())[:MAX_KEY_LABEL] if isinstance(label, str) else ""
        seen.add(key_id)
        out.append({"id": key_id, "provider": provider, "label": label})
    return out[:MAX_KEYS]


def _clean_reading_state(raw) -> dict:
    out: dict = {}
    for sha, v in raw.items() if isinstance(raw, dict) else ():
        if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) and isinstance(v, dict)):
            continue
        zoom, fit, page, ratio = v.get("zoom"), v.get("fit"), v.get("page"), v.get("ratio")
        if not _number(zoom) or not 0.1 <= zoom <= 10 or fit not in FIT_MODES:
            continue
        if isinstance(page, bool) or not isinstance(page, int) or page < 0 or not _number(ratio) or not 0 <= ratio <= 1:
            continue
        out[sha] = {"zoom": float(zoom), "fit": fit, "page": page, "ratio": float(ratio)}
    return dict(list(out.items())[-MAX_READING_STATES:])


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
