"""翻訳ジョブ全体の制御：解析 → 翻訳 → 組版・描画 → 検査 → 保存。

UI に依存しないため、CLI・GUI・テストのいずれからも使える。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pymupdf

from ..errors import DocumentError, EncryptedDocumentError, NoTextError
from ..translate.base import Translator
from ..translate.cache import TranslationCache
from ..translate.errors import CancelledError
from ..translate.service import PartialTranslationError, TranslationPlan, plan, translate_texts
from . import qa
from .analysis import analyze_document, page_range
from .export import build_output, finalize
from .models import DocumentAnalysis
from .options import AnalysisOptions, RenderOptions
from .render import Renderer, RenderReport

log = logging.getLogger(__name__)


class Stage(str, Enum):
    ANALYZE = "analyze"
    TRANSLATE = "translate"
    RENDER = "render"
    VERIFY = "verify"
    SAVE = "save"


STAGE_LABELS = {
    Stage.ANALYZE: "レイアウトを解析しています",
    Stage.TRANSLATE: "翻訳しています",
    Stage.RENDER: "訳文を組版しています",
    Stage.VERIFY: "レイアウトを検査しています",
    Stage.SAVE: "PDF を書き出しています",
}

# 全体の進捗に占める各段階の比重
_WEIGHTS = {Stage.ANALYZE: 0.08, Stage.TRANSLATE: 0.72, Stage.RENDER: 0.12, Stage.VERIFY: 0.05, Stage.SAVE: 0.03}


@dataclass(frozen=True)
class Progress:
    stage: Stage
    done: int
    total: int
    fraction: float  # ジョブ全体の進捗 0..1

    @property
    def label(self) -> str:
        return STAGE_LABELS[self.stage]


ProgressFn = Callable[[Progress], None]


@dataclass
class Estimate:
    segments: int
    total_chars: int
    cached_chars: int
    pages: int

    @property
    def pending_chars(self) -> int:
        return self.total_chars - self.cached_chars


@dataclass
class JobResult:
    pdf: bytes
    translated_pdf: bytes  # 翻訳のみの版（プレビュー用、ページ構成が原文と同じ）
    render: RenderReport
    qa: qa.QAReport
    segments: int
    translated_chars: int
    elapsed: float
    warnings: list[str] = field(default_factory=list)


def open_document(source: str | Path | bytes) -> pymupdf.Document:
    try:
        doc = pymupdf.open(stream=source, filetype="pdf") if isinstance(source, bytes) else pymupdf.open(source)
    except Exception as e:
        raise DocumentError("ファイルが壊れているか、PDF ではありません。") from e
    if not doc.is_pdf:
        raise DocumentError("PDF ファイルを選択してください。")
    if doc.needs_pass and not doc.authenticate(""):
        raise EncryptedDocumentError
    if doc.page_count == 0:
        raise DocumentError("ページがありません。")
    return doc


@contextmanager
def unrotated(doc: pymupdf.Document):
    """回転指定のあるページを一時的に 0 度にして座標系を揃える。"""
    rotations = {p.number: p.rotation for p in doc if p.rotation}
    for pno in rotations:
        doc[pno].set_rotation(0)
    try:
        yield doc
    finally:
        for pno, rot in rotations.items():
            doc[pno].set_rotation(rot)


class TranslationJob:
    def __init__(
        self,
        source: str | Path | bytes,
        translator: Translator,
        cache: TranslationCache | None = None,
        analysis_options: AnalysisOptions | None = None,
        render_options: RenderOptions | None = None,
        progress: ProgressFn | None = None,
        cancel_event: threading.Event | None = None,
        verify: bool = True,
    ):
        self.source_bytes = source if isinstance(source, bytes) else Path(source).read_bytes()
        self.translator = translator
        self.cache = cache
        self.analysis_options = analysis_options or AnalysisOptions()
        self.render_options = render_options or RenderOptions()
        self._progress = progress
        self.cancel_event = cancel_event or translator.cancel_event
        translator.cancel_event = self.cancel_event
        self.verify = verify
        self._analysis: DocumentAnalysis | None = None

    # -- 進捗 ------------------------------------------------------------

    def _report(self, stage: Stage, done: int, total: int) -> None:
        if not self._progress:
            return
        order = list(Stage)
        before = sum(_WEIGHTS[s] for s in order[: order.index(stage)])
        frac = before + _WEIGHTS[stage] * (done / total if total else 1.0)
        self._progress(Progress(stage, done, total, min(1.0, frac)))

    def _cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def _check_cancel(self) -> None:
        if self._cancelled():
            raise CancelledError

    # -- 公開 API --------------------------------------------------------

    def analyze(self) -> DocumentAnalysis:
        if self._analysis is None:
            doc = open_document(self.source_bytes)
            with unrotated(doc):
                try:
                    self._analysis = analyze_document(
                        doc,
                        self.analysis_options,
                        progress=lambda d, t: self._report(Stage.ANALYZE, d, t),
                        should_cancel=self._cancelled,
                    )
                except InterruptedError:
                    raise CancelledError from None
            doc.close()
        return self._analysis

    def estimate(self) -> Estimate:
        a = self.analyze()
        p = self._plan(a)
        return Estimate(len(a.segments), p.total_chars, p.cached_chars, len(a.pages))

    def _plan(self, a: DocumentAnalysis) -> TranslationPlan:
        return plan([s.source for s in a.segments], self.translator.cache_namespace(), self.cache)

    def run(self, allow_partial: bool = False) -> JobResult:
        """翻訳を実行する。

        allow_partial: 無料枠超過などで一部しか訳せなかった場合も、訳せた段落だけで PDF を作る
        （訳せなかった段落は原文のまま一切変更しない）。
        """
        started = time.monotonic()
        a = self.analyze()
        if not a.segments:
            raise NoTextError
        self._check_cancel()

        # 翻訳
        self._report(Stage.TRANSLATE, 0, 1)
        partial_note = ""
        try:
            results = translate_texts(
                [s.source for s in a.segments],
                self.translator,
                self.cache,
                progress=lambda d, t: self._report(Stage.TRANSLATE, d, t),
            )
        except PartialTranslationError as e:
            if not allow_partial:
                raise
            results = e.results
            partial_note = e.title
        for s in a.segments:
            s.translated = results.get(s.source)
        self._check_cancel()
        untranslated = sum(1 for s in a.segments if not s.translated)

        # 組版・描画
        original = open_document(self.source_bytes)
        doc = open_document(self.source_bytes)
        written: dict[int, list] = defaultdict(list)
        with unrotated(doc), unrotated(original):
            renderer = Renderer(doc, a, self.render_options)
            try:
                render_report = renderer.render(
                    progress=lambda d, t: self._report(Stage.RENDER, d, t),
                    should_cancel=self._cancelled,
                )
            except InterruptedError:
                raise CancelledError from None
            for s in a.segments:
                for r in s.regions:
                    written[r.page].append(r.rect)

            # 検査
            self._report(Stage.VERIFY, 0, 1)
            report = qa.check(original, doc, a, written, pixels=self.verify) if self.verify else qa.QAReport()
            self._report(Stage.VERIFY, 1, 1)

        # 保存
        self._report(Stage.SAVE, 0, 1)
        translated_pdf = finalize(doc)
        pages = page_range(original, self.analysis_options)
        if self.render_options.output_mode.value == "translated":
            pdf = translated_pdf
        else:
            translated_doc = pymupdf.open(stream=translated_pdf, filetype="pdf")
            pdf = finalize(build_output(original, translated_doc, self.render_options.output_mode, pages))
        self._report(Stage.SAVE, 1, 1)

        warnings = list(getattr(self.translator, "notices", []))
        if untranslated:
            warnings.append(f"{partial_note or '一部の段落を翻訳できませんでした'}。{untranslated} 個の段落は原文のままです。")
        if render_report.shrunk:
            warnings.append(f"{len(render_report.shrunk)} 個の段落を、元の枠に収めるため小さめの文字で組みました。")
        if not report.ok:
            warnings.append("レイアウト検査で注意点が見つかりました。詳細はログを確認してください。")
        return JobResult(
            pdf=pdf,
            translated_pdf=translated_pdf,
            render=render_report,
            qa=report,
            segments=len(a.segments),
            translated_chars=sum(len(s.source) for s in a.segments),
            elapsed=time.monotonic() - started,
            warnings=warnings,
        )
