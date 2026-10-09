import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from pdf_factory import make_paper  # noqa: E402


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
