"""利用者の設定（API キー以外）。JSON で保存する。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..core.options import AnalysisOptions, Formality, OutputMode, RenderOptions, TranslateOptions
from . import paths

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2  # 2: 既定モデルを 3.8 Flash から 3.5 Flash-Lite に変更


@dataclass
class Settings:
    engine: str = "gemini"
    gemini_model: str = "gemini-3.5-flash-lite"
    auto_fallback: bool = True  # 混雑・上限時に他のモデルへ自動で切り替える
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
        self.schema = SCHEMA_VERSION

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
