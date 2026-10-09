import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from pdf_factory import make_paper  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_user_data(tmp_path_factory, monkeypatch):
    """テストが利用者の設定・キャッシュ・API キー（キーチェーン）に触れないようにする。"""
    monkeypatch.setenv("SPECTRA_HOME", str(tmp_path_factory.mktemp("spectra-home")))
    for var in ("SPECTRA_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def paper_path(tmp_path: Path) -> Path:
    path = tmp_path / "paper.pdf"
    make_paper(str(path))
    return path


@pytest.fixture()
def paper(paper_path: Path):
    import pymupdf

    doc = pymupdf.open(paper_path)
    yield doc
    doc.close()
