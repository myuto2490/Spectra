import pytest
from pdf_factory import make_library_paper, make_paper

from spectra.library.identifiers import find_arxiv, find_doi, normalize_doi
from spectra.library.metadata import extract_local, is_junk_title


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1145/3065386", "10.1145/3065386"),
        ("https://doi.org/10.1145/3065386.", "10.1145/3065386"),
        ("http://dx.doi.org/10.1145/3065386", "10.1145/3065386"),
        ("doi: 10.1038/NATURE14539", "10.1038/nature14539"),
        ("DOI:10.1038/nature14539;", "10.1038/nature14539"),
        # 括弧を含む DOI は、対応する閉じ括弧を残す
        ("10.1016/S0140-6736(20)30183-5", "10.1016/s0140-6736(20)30183-5"),
        ("10.1002/(sici)1097-4636(199607)32:3", "10.1002/(sici)1097-4636(199607)32:3"),
        ("10.1000/abc(1))", "10.1000/abc(1)"),
        ("10.1000/xyz]", "10.1000/xyz"),
    ],
)
def test_normalize_doi(raw, expected):
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Published online. DOI: 10.1038/nature14539.", "10.1038/nature14539"),
        ("See https://doi.org/10.1145/3065386, accessed", "10.1145/3065386"),
        ("(doi:10.1016/S0140-6736(20)30183-5)", "10.1016/s0140-6736(20)30183-5"),
        ("version 10.1 / no doi here", None),
        ("", None),
    ],
)
def test_find_doi(text, expected):
    assert find_doi(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("arXiv:2401.00001v1 [cs.LG] 1 Jan 2024", "2401.00001"),
        ("arXiv: 1706.03762", "1706.03762"),
        ("https://arxiv.org/abs/1706.03762v7", "1706.03762"),
        ("https://arxiv.org/pdf/0704.0001", "0704.0001"),
        ("https://doi.org/10.48550/arXiv.2312.12345", "2312.12345"),
        ("arXiv:hep-th/9901001v2", "hep-th/9901001"),
        ("arXiv:math.AG/0601001", "math.AG/0601001"),
        ("arXiv:MATH.ag/0601001", "math.AG/0601001"),
        ("Table 1706.03762 shows", None),  # 接頭辞のない数字列は拾わない
        ("arXiv:2413.00001", None),  # 13 月はない
        ("hep-th/9901001", None),
    ],
)
def test_find_arxiv(text, expected):
    assert find_arxiv(text) == expected


@pytest.mark.parametrize(
    "title",
    ["", "untitled", "Microsoft Word - draft_v3.docx", "paper.pdf", "ms_12345", "arXiv:2401.00001", "12", "main"],
)
def test_junk_titles(title):
    assert is_junk_title(title, "main.pdf")


@pytest.mark.parametrize("title", ["Attention Is All You Need", "GANs Revisited", "Deep Learning"])
def test_real_titles(title):
    assert not is_junk_title(title, "main.pdf")


def test_extract_from_synthetic_paper():
    """回転した arXiv 刻印から ID を拾い、刻印（最大フォント）ではなく横書きのタイトルを採る。"""
    md = extract_local(make_paper(), "paper.pdf")
    assert md.arxiv_id == "2401.00001"
    assert md.title == "Learning Robust Representations for Testing with Synthetic Papers"
    assert md.source == "heuristic"


def test_embedded_metadata_wins():
    doc = make_library_paper(
        info={"title": "Embedded Title of the Paper", "author": "Ashish Vaswani, Noam Shazeer and Niki Parmar"},
        body=["https://doi.org/10.5555/abc.123"],
    )
    md = extract_local(doc, "x.pdf")
    assert md.title == "Embedded Title of the Paper"
    assert [a.display() for a in md.authors] == ["Vaswani, A.", "Shazeer, N.", "Parmar, N."]
    assert md.doi == "10.5555/abc.123"
    assert md.source == "embedded"


def test_junk_embedded_title_falls_back_to_heuristic():
    doc = make_library_paper(info={"title": "Microsoft Word - nips2017.docx", "author": "Vaswani, Ashish"})
    md = extract_local(doc, "nips2017.pdf")
    assert md.title == "Attention Is All You Need"
    assert md.authors == []  # 「姓, 名」の並びは区切りと見分けられないので使わない
    assert md.source == "heuristic"


def test_xmp_doi_and_title():
    xmp = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">'
        "<dc:title><rdf:Alt><rdf:li xml:lang=\"x-default\">XMP Title Wins Here</rdf:li></rdf:Alt></dc:title>"
        "<prism:doi>10.9999/XMP.1</prism:doi>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    md = extract_local(make_library_paper(xmp=xmp, body=["doi:10.1111/other.2"]), "x.pdf")
    assert md.title == "XMP Title Wins Here"
    assert md.doi == "10.9999/xmp.1"


def test_header_and_page_number_are_not_title():
    doc = make_library_paper(header="Journal of Synthetic Papers 12 (2024) 101-115", body=["DOI: 10.1234/jsp.2024.7"])
    md = extract_local(doc, "x.pdf")
    assert md.title == "Attention Is All You Need"
    assert md.doi == "10.1234/jsp.2024.7"


def test_no_title_found():
    md = extract_local(make_library_paper(title=""), "x.pdf")
    assert md.doi == "" and md.arxiv_id == ""
    assert md.title != "12"  # 大きなページ番号はタイトルにしない
