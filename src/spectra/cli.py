"""コマンドラインインターフェース。

例:
    spectra translate paper.pdf                    # paper_ja.pdf を作成
    spectra translate paper.pdf --mode side-by-side --pages 1-5
    spectra estimate paper.pdf                     # 翻訳する文字数を見積もる
    spectra key set deepl                          # API キーを保存（入力は表示されない）
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import threading
from pathlib import Path

from . import APP_NAME, __version__
from .config.fonts import FONT_CHOICES
from .config.secrets import SecretStore
from .config.settings import Settings
from .core.options import Formality, OutputMode
from .errors import SpectraError

MODES = {"translated": OutputMode.TRANSLATED, "side-by-side": OutputMode.SIDE_BY_SIDE, "interleave": OutputMode.INTERLEAVE}
FORMALITY = {"default": Formality.DEFAULT, "plain": Formality.PLAIN, "polite": Formality.POLITE}


def parse_pages(spec: str | None, page_count: int | None = None) -> tuple[int, int | None]:
    """'3' や '2-5'、'4-' を 0 始まりの (first, last) に変換する。"""
    if not spec:
        return 0, None
    try:
        if "-" in spec:
            a, b = spec.split("-", 1)
            first = int(a) - 1 if a.strip() else 0
            last = int(b) - 1 if b.strip() else None
        else:
            first = last = int(spec) - 1
    except ValueError:
        raise SystemExit(f"ページ指定が不正です: {spec}（例: 3, 2-5, 4-）") from None
    if first < 0 or (last is not None and last < first):
        raise SystemExit(f"ページ指定が不正です: {spec}")
    return first, last


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spectra", description=f"{APP_NAME} — 論文 PDF をレイアウトを保ったまま日本語に翻訳します。")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="詳細なログを表示する")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def job_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("input", type=Path, help="翻訳する PDF")
        sp.add_argument("--engine", choices=["deepl", "google", "dummy"], help="翻訳エンジン（既定: 設定値）")
        sp.add_argument("--pages", help="翻訳するページ（例: 3, 2-5, 4-）")
        sp.add_argument("--include-references", action="store_true", help="参考文献も翻訳する")
        sp.add_argument("--no-captions", action="store_true", help="図表のキャプションを翻訳しない")
        sp.add_argument("--no-footnotes", action="store_true", help="脚注を翻訳しない")
        sp.add_argument("--no-cache", action="store_true", help="翻訳キャッシュを使わない")

    t = sub.add_parser("translate", help="PDF を翻訳する")
    job_args(t)
    t.add_argument("-o", "--output", type=Path, help="出力先（既定: <入力名>_ja.pdf）")
    t.add_argument("--mode", choices=list(MODES), help="出力形式（既定: 設定値）")
    t.add_argument("--formality", choices=list(FORMALITY), help="文体（DeepL のみ）: plain=である調, polite=です・ます調")
    t.add_argument("--font", help=f"和文フォント（{', '.join(k for k in FONT_CHOICES if k != 'custom')} またはフォントファイルのパス）")
    t.add_argument("--no-verify", action="store_true", help="レイアウト検査を省略する")
    t.add_argument("-f", "--force", action="store_true", help="出力先を上書きする")

    e = sub.add_parser("estimate", help="翻訳する文字数を見積もる（API は使わない）")
    job_args(e)

    k = sub.add_parser("key", help="API キーを管理する")
    k.add_argument("action", choices=["set", "delete", "status"])
    k.add_argument("engine", nargs="?", choices=["deepl", "google"])

    sub.add_parser("gui", help="GUI を起動する")
    return p


def _translator(engine: str, settings: Settings, secrets: SecretStore):
    from .translate import registry
    from .translate.errors import MissingKeyError

    key = secrets.get(engine) if engine != "dummy" else ""
    if engine != "dummy" and not key:
        raise MissingKeyError(hint=f"`spectra key set {engine}` で API キーを保存するか、環境変数を設定してください。")
    return registry.create(engine, api_key=key, options=settings.translate_options())


def _cache(args, settings: Settings):
    from .config import paths
    from .translate.cache import TranslationCache

    if args.no_cache or not settings.use_cache:
        return None
    return TranslationCache(paths.translation_cache_file())


def _apply_args(args, settings: Settings) -> None:
    if args.engine:
        settings.engine = args.engine
    if args.include_references:
        settings.skip_references = False
    if args.no_captions:
        settings.translate_captions = False
    if args.no_footnotes:
        settings.translate_footnotes = False
    if getattr(args, "mode", None):
        settings.output_mode = MODES[args.mode].value
    if getattr(args, "formality", None):
        settings.formality = FORMALITY[args.formality].value
    font = getattr(args, "font", None)
    if font:
        if font in FONT_CHOICES and font != "custom":
            settings.font = font
        else:
            settings.font, settings.custom_font_path = "custom", font


class _ProgressPrinter:
    def __init__(self):
        self.last = ""

    def __call__(self, p) -> None:
        width = 28
        filled = int(p.fraction * width)
        line = f"\r[{'#' * filled}{'.' * (width - filled)}] {p.fraction * 100:5.1f}%  {p.label}"
        if line != self.last:
            sys.stderr.write(line + " " * 8)
            sys.stderr.flush()
            self.last = line

    def done(self) -> None:
        if self.last:
            sys.stderr.write("\n")


def _cmd_translate(args) -> int:
    from .core.pipeline import TranslationJob

    settings = Settings.load()
    _apply_args(args, settings)
    first, last = parse_pages(args.pages)
    output = args.output or args.input.with_name(f"{args.input.stem}_ja.pdf")
    if output.exists() and not args.force:
        print(f"{output} は既に存在します。上書きするには --force を指定してください。", file=sys.stderr)
        return 1
    if output.resolve() == args.input.resolve():
        print("入力と同じファイルには出力できません。", file=sys.stderr)
        return 1

    translator = _translator(settings.engine, settings, SecretStore())
    printer = _ProgressPrinter()
    cancel = threading.Event()
    job = TranslationJob(
        str(args.input),
        translator,
        cache=_cache(args, settings),
        analysis_options=settings.analysis_options(first, last),
        render_options=settings.render_options(),
        progress=printer,
        cancel_event=cancel,
        verify=not args.no_verify and settings.verify_layout,
    )
    try:
        result = job.run()
    except KeyboardInterrupt:
        cancel.set()
        printer.done()
        print("中断しました。翻訳済みの段落はキャッシュに保存されています。", file=sys.stderr)
        return 130
    finally:
        translator.close()
    printer.done()
    output.write_bytes(result.pdf)
    print(f"保存しました: {output}")
    print(f"  {result.segments} 段落 / {result.translated_chars:,} 文字 / {result.elapsed:.1f} 秒")
    for w in result.warnings:
        print(f"  注意: {w}")
    for issue in result.qa.issues:
        print(f"  検査: {issue.page + 1} ページ — {issue.message}")
    return 0


def _cmd_estimate(args) -> int:
    from .core.pipeline import TranslationJob
    from .translate import registry

    settings = Settings.load()
    _apply_args(args, settings)
    first, last = parse_pages(args.pages)
    translator = registry.create(settings.engine, api_key="", options=settings.translate_options())
    est = TranslationJob(
        str(args.input), translator, cache=_cache(args, settings), analysis_options=settings.analysis_options(first, last)
    ).estimate()
    print(f"{est.pages} ページ / {est.segments} 段落")
    print(f"翻訳する文字数: {est.total_chars:,} 文字（うちキャッシュ済み {est.cached_chars:,} 文字）")
    print(f"API で消費する見込み: {est.pending_chars:,} 文字")
    return 0


def _cmd_key(args) -> int:
    secrets = SecretStore()
    engines = [args.engine] if args.engine else ["deepl", "google"]
    if args.action == "status":
        for eng in engines:
            src = secrets.source(eng)
            print(f"{eng:7} {'設定済み（' + src + '）' if src else '未設定'}")
        if not secrets.secure:
            print("注意: OS の資格情報ストアが使えないため、キーは設定ファイルに保存されます。")
        return 0
    if not args.engine:
        print("エンジン（deepl / google）を指定してください。", file=sys.stderr)
        return 2
    if args.action == "delete":
        secrets.delete(args.engine)
        print(f"{args.engine} の API キーを削除しました。")
        return 0
    value = getpass.getpass(f"{args.engine} の API キー: ")
    if not value.strip():
        print("入力が空のため保存しませんでした。", file=sys.stderr)
        return 1
    secrets.set(args.engine, value)
    print(f"{args.engine} の API キーを保存しました（{secrets.backend_name}）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "gui":
        from .ui.app import main as gui_main

        return gui_main()
    try:
        return {"translate": _cmd_translate, "estimate": _cmd_estimate, "key": _cmd_key}[args.command](args)
    except SpectraError as e:
        print(f"\nエラー: {e.user_message()}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"エラー: ファイルが見つかりません: {e.filename}", file=sys.stderr)
        return 1
