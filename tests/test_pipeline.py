import threading

import pymupdf
import pytest

from spectra.core.options import AnalysisOptions, OutputMode, RenderOptions
from spectra.core.pipeline import Stage, TranslationJob
from spectra.errors import DocumentError, NoTextError
from spectra.translate.cache import TranslationCache
from spectra.translate.dummy import DummyTranslator
from spectra.translate.errors import CancelledError


def run(path, **kw):
    return TranslationJob(str(path), DummyTranslator(), **kw).run()


def test_layout_is_preserved(paper_path, paper):
    result = run(paper_path)
    out = pymupdf.open(stream=result.pdf, filetype="pdf")
    assert out.page_count == paper.page_count
    for a, b in zip(paper, out):
        assert a.rect == b.rect
        assert len(a.get_image_info()) == len(b.get_image_info())
        assert len(a.get_drawings()) == len(b.get_drawings())
    assert result.qa.ok, result.qa.issues
    assert result.qa.regions_checked >= 2


def test_text_is_replaced_but_skipped_text_kept(paper_path):
    out = pymupdf.open(stream=run(paper_path).pdf, filetype="pdf")
    p0 = out[0].get_text()
    assert "擬似" in p0
    assert "Alice Example" in p0  # 著者
    assert "arXiv:2401" in p0  # 回転スタンプ
    assert "remarkable" not in out[0].get_text(clip=pymupdf.Rect(90, 185, 505, 250))  # 概要は置換
    p1 = out[1].get_text()
    assert "training steps" in p1 and "Baseline" in p1 and "78.9" in p1  # 図と表
    p2 = out[2].get_text()
    assert "[1] A. Example" in p2  # 参考文献


def test_fonts_are_embedded_and_subset(paper_path):
    out = pymupdf.open(stream=run(paper_path).pdf, filetype="pdf")
    names = {f[3] for f in out[0].get_fonts()}
    # 同梱の Noto Sans JP（日本語の字形）が、使った文字だけに絞って埋め込まれる
    assert any("Noto Sans JP" in n and "+" in n for n in names)


@pytest.mark.parametrize(
    ("mode", "pages", "width_factor"),
    [(OutputMode.SIDE_BY_SIDE, 3, 2), (OutputMode.INTERLEAVE, 6, 1)],
)
def test_output_modes(paper_path, mode, pages, width_factor):
    result = run(paper_path, render_options=RenderOptions(output_mode=mode))
    out = pymupdf.open(stream=result.pdf, filetype="pdf")
    assert out.page_count == pages
    assert out[0].rect.width == pytest.approx(595 * width_factor)
    assert pymupdf.open(stream=result.translated_pdf, filetype="pdf").page_count == 3


def test_page_range_only_touches_selected_pages(paper_path, paper):
    result = run(paper_path, analysis_options=AnalysisOptions(first_page=1, last_page=1))
    out = pymupdf.open(stream=result.pdf, filetype="pdf")
    assert out[0].get_text() == paper[0].get_text()
    assert "擬似" in out[1].get_text()


def test_rotated_page(tmp_path, paper):
    paper[1].set_rotation(90)
    path = tmp_path / "rot.pdf"
    paper.save(path)
    result = run(path)
    out = pymupdf.open(stream=result.pdf, filetype="pdf")
    assert out[1].rotation == 90 and result.qa.ok


def test_estimate_and_cache(paper_path):
    cache = TranslationCache(None)
    job = TranslationJob(str(paper_path), DummyTranslator(), cache=cache)
    est = job.estimate()
    assert est.segments > 5 and est.cached_chars == 0 and est.pages == 3
    job.run()
    again = TranslationJob(str(paper_path), DummyTranslator(), cache=cache).estimate()
    assert again.cached_chars == again.total_chars


def test_progress_reports_all_stages(paper_path):
    seen = []
    TranslationJob(str(paper_path), DummyTranslator(), progress=seen.append).run()
    assert {p.stage for p in seen} == set(Stage)
    fracs = [p.fraction for p in seen]
    assert fracs == sorted(fracs) and fracs[-1] == pytest.approx(1.0)


def test_cancel(paper_path):
    ev = threading.Event()
    ev.set()
    with pytest.raises(CancelledError):
        TranslationJob(str(paper_path), DummyTranslator(), cancel_event=ev).run()


def test_invalid_pdf(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(DocumentError):
        run(bad)


def test_no_text(tmp_path):
    doc = pymupdf.open()
    doc.new_page()
    path = tmp_path / "empty.pdf"
    doc.save(path)
    with pytest.raises(NoTextError):
        run(path)


def test_partial_translation_keeps_untranslated_paragraphs(paper_path, paper):
    from spectra.translate.errors import QuotaExceededError
    from spectra.translate.service import PartialTranslationError

    class Flaky(DummyTranslator):
        max_items = 3
        calls = 0

        def _translate_batch(self, texts):
            Flaky.calls += 1
            if Flaky.calls > 1:
                raise QuotaExceededError
            return super()._translate_batch(texts)

    with pytest.raises(PartialTranslationError):
        TranslationJob(str(paper_path), Flaky()).run()
    Flaky.calls = 0
    result = TranslationJob(str(paper_path), Flaky()).run(allow_partial=True)
    assert any("原文のまま" in w for w in result.warnings)
    out = pymupdf.open(stream=result.pdf, filetype="pdf")
    assert "擬似" in out[0].get_text()
    assert out[2].get_text() == paper[2].get_text()  # 訳せなかったページは完全に元のまま
    assert result.qa.ok


def _invisible_chars(page: pymupdf.Page) -> int:
    return sum(len(t["chars"]) for t in page.get_texttrace() if t["type"] == 3)


def test_scanned_pdf_covers_source_text_in_image(tmp_path):
    from pdf_factory import PAPER, make_scanned

    from spectra.core.scan import is_scanned

    path = tmp_path / "scanned.pdf"
    src = make_scanned(str(path))
    assert is_scanned(src[0])

    class Short(DummyTranslator):
        def _translate_batch(self, texts):
            return ["訳。" + "".join(_TAG.findall(t)) for t in texts]

    from spectra.translate.dummy import _TAG

    result = TranslationJob(str(path), Short()).run()
    out = pymupdf.open(stream=result.pdf, filetype="pdf")
    assert "訳" in out[0].get_text()
    # 概要の右側（短い訳文が届かない範囲）では、画像の中の英文が紙の色で塗りつぶされている
    pix = out[0].get_pixmap(clip=pymupdf.Rect(300, 190, 490, 240), dpi=72)
    paper = tuple(round(v * 255) for v in PAPER)
    s = pix.samples
    dark = sum(1 for i in range(0, len(s), pix.n) if sum(s[i : i + 3]) < sum(paper) - 120)
    assert dark == 0
    assert all(abs(a - b) <= 6 for a, b in zip(s[:3], paper))
    # 段落中の数式は画像から切り出して移し、透明な OCR テキストは持ち込まない
    assert _invisible_chars(out[0]) < _invisible_chars(src[0])
    assert len(out[0].get_image_info()) > len(src[0].get_image_info())


def test_scanned_detection_ignores_normal_pages(paper):
    from spectra.core.scan import is_scanned

    assert not any(is_scanned(p) for p in paper)
