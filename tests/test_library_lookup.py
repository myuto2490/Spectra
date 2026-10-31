import json
from pathlib import Path

import httpx
import pytest

from spectra.library import LookupFailed, Metadata, arxiv, crossref
from spectra.library._http import USER_AGENT
from spectra.library.lookup import lookup

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_crossref_parse():
    md = crossref.parse(json.loads(fixture("crossref_work.json"))["message"])
    assert md.title == "Learning Robust Representations for Testing"
    assert [a.full() for a in md.authors] == ["Alice Example", "Bob Sample", "The Spectra Consortium"]
    assert md.year == 2024  # issued より published-print を優先
    assert (md.venue, md.volume, md.issue, md.pages) == ("Journal of Synthetic Papers", "12", "3", "101-115")
    assert md.doi == "10.1234/spectra.2024.001"
    assert md.abstract == "We study robust representations & their limits."
    assert md.source == "crossref"


def test_crossref_fetch_sends_only_identifier():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=fixture("crossref_work.json"))

    with client_for(handler) as client:
        md = crossref.fetch("https://doi.org/10.1016/S0140-6736(20)30183-5", client)
    assert md is not None
    req = seen[0]
    assert req.method == "GET"
    assert req.url.path == "/works/10.1016/s0140-6736(20)30183-5"
    assert req.headers["User-Agent"] == USER_AGENT
    assert "mailto" not in str(req.url)


def test_crossref_not_found_and_errors():
    with client_for(lambda r: httpx.Response(404)) as client:
        assert crossref.fetch("10.1/none", client) is None
    with client_for(lambda r: httpx.Response(503)) as client, pytest.raises(LookupFailed, match="HTTP 503"):
        crossref.fetch("10.1/x", client)
    with client_for(lambda r: httpx.Response(200, text="not json")) as client, pytest.raises(LookupFailed):
        crossref.fetch("10.1/x", client)


def test_connection_error_raises_lookup_failed():
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    with client_for(handler) as client, pytest.raises(LookupFailed) as info:
        arxiv.fetch("2401.00001", client)
    assert "接続できません" in info.value.user_message()


def test_arxiv_parse():
    md = arxiv.parse(fixture("arxiv_entry.xml"), "2401.00001")
    assert md.title == "Learning Robust Representations with Synthetic Papers"
    assert [a.display() for a in md.authors] == ["Example, A.", "Sample, B."]
    assert md.year == 2024
    assert md.venue == "Proc. Synthetic Conf. 2024"
    assert md.doi == "10.1234/spectra.2024.002"
    assert md.abstract == "Deep neural networks have achieved remarkable results."
    assert md.url == "https://arxiv.org/abs/2401.00001"
    assert md.source == "arxiv"


@pytest.mark.parametrize("name", ["arxiv_not_found.xml", "arxiv_empty.xml"])
def test_arxiv_not_found(name):
    with client_for(lambda r: httpx.Response(200, text=fixture(name))) as client:
        assert arxiv.fetch("9912.99999", client) is None


def test_arxiv_fetch_query():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=fixture("arxiv_entry.xml"))

    with client_for(handler) as client:
        arxiv.fetch("2401.00001", client)
    assert seen[0].url.host == "export.arxiv.org"
    assert seen[0].url.params["id_list"] == "2401.00001"


def test_lookup_prefers_crossref_and_keeps_arxiv_id():
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, text=fixture("crossref_work.json"))

    with client_for(handler) as client:
        md = lookup(Metadata(doi="10.1234/spectra.2024.001", arxiv_id="2401.00001"), client)
    assert hosts == ["api.crossref.org"]
    assert md.source == "crossref" and md.arxiv_id == "2401.00001"


@pytest.mark.parametrize("crossref_status", [404, 500])
def test_lookup_falls_back_to_arxiv(crossref_status):
    def handler(request):
        if request.url.host == "api.crossref.org":
            return httpx.Response(crossref_status)
        return httpx.Response(200, text=fixture("arxiv_entry.xml"))

    with client_for(handler) as client:
        md = lookup(Metadata(doi="10.48550/arxiv.2401.00001", arxiv_id="2401.00001"), client)
    assert md.source == "arxiv"


def test_lookup_arxiv_doi_goes_straight_to_arxiv():
    """arXiv の DOI しか分からなくても、arXiv ID を導いて arXiv に照会する（Crossref には無い）。"""
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, text=fixture("arxiv_entry.xml"))

    with client_for(handler) as client:
        md = lookup(Metadata(doi="10.48550/arxiv.2401.00001"), client)
    assert hosts == ["export.arxiv.org"]
    assert md.source == "arxiv" and md.arxiv_id == "2401.00001"
    assert md.doi == "10.1234/spectra.2024.002"  # 出版版の DOI が arXiv に登録されていればそれを使う


def test_lookup_results():
    assert lookup(Metadata(title="no identifiers")) is None  # 通信しない
    with client_for(lambda r: httpx.Response(404)) as client:
        assert lookup(Metadata(doi="10.1/none"), client) is None
    with client_for(lambda r: httpx.Response(500)) as client, pytest.raises(LookupFailed):
        lookup(Metadata(doi="10.1/x"), client)
