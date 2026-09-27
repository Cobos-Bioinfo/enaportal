"""Unit tests for the Browser API client. Every response is mocked; nothing hits ENA."""

from __future__ import annotations

import io
import json
import warnings
from collections.abc import Callable, Iterator
from itertools import count
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st

import enaportal
from enaportal import _api
from enaportal._http import BROWSER_BASE_URL, ENAHTTPClient
from enaportal.browser import (
    DEFAULT_TEXTSEARCH_LIMIT,
    BrowserClient,
    _merge_xml,
    _RecordCounter,
)
from enaportal.errors import ENAConnectionError, ENANotFoundError, ENAQueryError

XML_URL = f"{BROWSER_BASE_URL}xml"
EMBL_URL = f"{BROWSER_BASE_URL}embl"
FASTA_URL = f"{BROWSER_BASE_URL}fasta"
TEXTSEARCH_URL = f"{BROWSER_BASE_URL}tsv/textsearch"
TEXTSEARCH_COUNT_URL = f"{BROWSER_BASE_URL}tsv/textsearch/count"

NOT_FOUND = '{"status": 404, "message": "Failed to get response from SRA API. Response code 404"}'


def run_set(*accessions: str, declaration: bool = False) -> str:
    head = '<?xml version="1.0" encoding="UTF-8"?>\n' if declaration else ""
    records = "".join(
        f'<RUN accession="{one}">\n  <TITLE>{one}</TITLE>\n</RUN>\n' for one in accessions
    )
    return f"{head}<RUN_SET>\n{records}</RUN_SET>\n"


def embl(*accessions: str) -> str:
    return "".join(
        f"ID   {one}; SV 1; linear; mRNA; STD; MAM; 10 BP.\nXX\nAC   {one};\nXX\n"
        "SQ   Sequence 10 BP;\n     acgtacgtac                                                10\n"
        "//\n"
        for one in accessions
    )


def fasta(*accessions: str) -> str:
    return "".join(f">ENA|{one}|{one}.1 test\nACGTACGTAC\n" for one in accessions)


BUILDERS: dict[str, tuple[str, Callable[..., str]]] = {
    "xml": (XML_URL, run_set),
    "embl": (EMBL_URL, embl),
    "fasta": (FASTA_URL, fasta),
}


def answering(build: Callable[..., str]) -> Callable[[httpx.Request], httpx.Response]:
    """A route side effect that returns a record for every accession asked for."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=build(*sent(request)))

    return respond


def sent(request: httpx.Request) -> list[str]:
    accessions: list[str] = json.loads(request.content)["accessions"]
    return accessions


def body_of(route: respx.Route, index: int = 0) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(route.calls[index].request.content)
    return payload


@pytest.fixture
def browser() -> Iterator[BrowserClient]:
    with ENAHTTPClient(BROWSER_BASE_URL, max_retries=0, rate_limit=None) as http:
        yield BrowserClient(http=http)


@pytest.fixture
def small_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("enaportal.browser.MAX_ACCESSIONS_PER_REQUEST", 2)


@pytest.fixture
def no_warnings() -> Iterator[None]:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        yield


@respx.mock
def test_fetch_posts_the_accession_as_json(browser: BrowserClient) -> None:
    route = respx.post(XML_URL).mock(return_value=httpx.Response(200, text=run_set("ERR1")))

    browser.fetch("ERR1")

    assert body_of(route) == {"accessions": ["ERR1"]}


@pytest.mark.parametrize("declaration", [False, True])
@respx.mock
def test_fetch_returns_one_batch_unchanged(browser: BrowserClient, declaration: bool) -> None:
    body = run_set("ERR1", "ERR2", declaration=declaration)
    respx.post(XML_URL).mock(return_value=httpx.Response(200, text=body))

    assert browser.fetch(["ERR1", "ERR2"]) == body


@respx.mock
def test_fetch_sends_many_accessions_in_one_request(browser: BrowserClient) -> None:
    route = respx.post(XML_URL).mock(side_effect=answering(run_set))

    browser.fetch(["ERR1", "ERR2", "ERR3"])

    assert route.call_count == 1
    assert body_of(route)["accessions"] == ["ERR1", "ERR2", "ERR3"]


@respx.mock
def test_fetch_drops_repeats_and_blanks(browser: BrowserClient) -> None:
    route = respx.post(XML_URL).mock(side_effect=answering(run_set))

    browser.fetch([" ERR1 ", "ERR1", "", "ERR2"])

    assert body_of(route)["accessions"] == ["ERR1", "ERR2"]


@respx.mock
def test_fetch_of_no_accessions_sends_nothing(browser: BrowserClient) -> None:
    route = respx.post(XML_URL)

    assert browser.fetch([]) == ""
    assert not route.called


@respx.mock
def test_embl_options_are_sent_in_enas_names(browser: BrowserClient) -> None:
    route = respx.post(EMBL_URL).mock(side_effect=answering(embl))

    browser.fetch("A00145", format="embl", annotation_only=True, line_limit=5)

    assert body_of(route) == {"accessions": ["A00145"], "annotationOnly": True, "lineLimit": 5}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"format": "xml", "annotation_only": True}, "EMBL records only"),
        ({"format": "fasta", "annotation_only": True}, "EMBL records only"),
        ({"format": "xml", "line_limit": 3}, "EMBL and FASTA"),
        ({"format": "embl", "line_limit": 0}, "at least 1"),
        ({"format": "genbank"}, "format must be one of"),
    ],
)
def test_options_a_format_cannot_take_are_refused(
    browser: BrowserClient, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        browser.fetch("A00145", **kwargs)


@pytest.mark.parametrize("format", ["xml", "embl", "fasta"])
@respx.mock
def test_fetch_warns_when_ena_drops_an_accession(browser: BrowserClient, format: str) -> None:
    url, build = BUILDERS[format]
    respx.post(url).mock(return_value=httpx.Response(200, text=build("A1")))

    with pytest.warns(UserWarning, match="Only 1 of 2 accessions came back"):
        text = browser.fetch(["A1", "A2"], format=format)  # type: ignore[arg-type]

    assert "A1" in text


@pytest.mark.parametrize("format", ["xml", "embl", "fasta"])
@respx.mock
def test_fetch_is_quiet_when_every_accession_comes_back(
    browser: BrowserClient, format: str, no_warnings: None
) -> None:
    url, build = BUILDERS[format]
    respx.post(url).mock(side_effect=answering(build))

    browser.fetch(["A1", "A2"], format=format)  # type: ignore[arg-type]


@respx.mock
def test_a_record_cut_short_by_line_limit_still_counts(
    browser: BrowserClient, no_warnings: None
) -> None:
    body = "ID   A1; SV 1;\nXX\nAC   A1;\nID   A2; SV 1;\nXX\nAC   A2;\n"
    respx.post(EMBL_URL).mock(return_value=httpx.Response(200, text=body))

    browser.fetch(["A1", "A2"], format="embl", line_limit=3)


@respx.mock
def test_a_set_expanding_to_more_records_does_not_warn(
    browser: BrowserClient, no_warnings: None
) -> None:
    respx.post(FASTA_URL).mock(return_value=httpx.Response(200, text=fasta("C1", "C2", "C3")))

    browser.fetch("GCA_000146045.2", format="fasta")


TAXON = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<TAXON_SET>\n'
    '<taxon scientificName="Homo sapiens" taxId="9606">\n  <lineage>\n'
    '    <taxon scientificName="Homo" taxId="9605"/>\n'
    '    <taxon scientificName="Hominidae" taxId="9604"/>\n'
    "  </lineage>\n</taxon>\n</TAXON_SET>\n"
)


@respx.mock
def test_a_taxons_nested_lineage_is_not_counted_as_records(browser: BrowserClient) -> None:
    respx.post(XML_URL).mock(return_value=httpx.Response(200, text=TAXON))

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        browser.fetch("9606")
    with pytest.warns(UserWarning, match="Only 1 of 3"):
        browser.fetch(["9606", "10090", "7955"])


@respx.mock
def test_xml_that_cannot_be_parsed_is_returned_uncounted(
    browser: BrowserClient, no_warnings: None
) -> None:
    body = "<RUN_SET>\n<RUN accession='A1'>\n</RUN_SET>\n"
    respx.post(XML_URL).mock(return_value=httpx.Response(200, text=body))

    assert browser.fetch(["A1", "A2"]) == body


@respx.mock
def test_an_unknown_accession_raises_not_found(browser: BrowserClient) -> None:
    respx.post(XML_URL).mock(return_value=httpx.Response(404, text=NOT_FOUND))

    with pytest.raises(ENANotFoundError, match="ENA has no xml record for ERR9") as caught:
        browser.fetch("ERR9")

    assert caught.value.status_code == 404
    assert caught.value.body == NOT_FOUND


@respx.mock
def test_many_unknown_accessions_raise_not_found(browser: BrowserClient) -> None:
    respx.post(XML_URL).mock(return_value=httpx.Response(404, text=NOT_FOUND))

    with pytest.raises(ENANotFoundError, match="any of 2 accessions"):
        browser.fetch(["ERR8", "ERR9"])


@respx.mock
def test_a_format_the_record_lacks_raises_enas_message(browser: BrowserClient) -> None:
    body = (
        "timestamp=1\nstatus=400\nerror=Bad Request\n"
        "message=Format embl is not available for record PRJEB1787 in data type PROJECT\n"
    )
    respx.post(EMBL_URL).mock(return_value=httpx.Response(400, text=body))

    with pytest.raises(ENAQueryError, match=r"^Format embl is not available"):
        browser.fetch("PRJEB1787", format="embl")


@pytest.mark.usefixtures("small_batches")
@respx.mock
def test_accessions_beyond_the_ceiling_are_split_into_batches(browser: BrowserClient) -> None:
    route = respx.post(FASTA_URL).mock(side_effect=answering(fasta))

    text = browser.fetch(["A1", "A2", "A3", "A4", "A5"], format="fasta")

    assert [sent(call.request) for call in route.calls] == [["A1", "A2"], ["A3", "A4"], ["A5"]]
    assert text == fasta("A1", "A2", "A3", "A4", "A5")


@pytest.mark.usefixtures("small_batches")
@pytest.mark.parametrize("declaration", [False, True])
@respx.mock
def test_xml_batches_merge_into_one_document(browser: BrowserClient, declaration: bool) -> None:
    respx.post(XML_URL).mock(
        side_effect=lambda request: httpx.Response(
            200, text=run_set(*sent(request), declaration=declaration)
        )
    )

    text = browser.fetch(["A1", "A2", "A3", "A4", "A5"])

    root = ElementTree.fromstring(text.encode())
    assert root.tag == "RUN_SET"
    assert [run.get("accession") for run in root] == ["A1", "A2", "A3", "A4", "A5"]
    assert text.count("<RUN_SET>") == 1
    assert text.count("<?xml") == (1 if declaration else 0)


@pytest.mark.usefixtures("small_batches")
@pytest.mark.parametrize("missing", [0, 1, 2])
@respx.mock
def test_a_batch_ena_found_nothing_of_is_skipped(browser: BrowserClient, missing: int) -> None:
    batches = count()

    def respond(request: httpx.Request) -> httpx.Response:
        if next(batches) == missing:
            return httpx.Response(404, text=NOT_FOUND)
        return httpx.Response(200, text=run_set(*sent(request)))

    respx.post(XML_URL).mock(side_effect=respond)

    with pytest.warns(UserWarning, match=r"Only \d of 5"):
        text = browser.fetch(["A1", "A2", "A3", "A4", "A5"])

    root = ElementTree.fromstring(text.encode())
    found = [["A1", "A2"], ["A3", "A4"], ["A5"]]
    del found[missing]
    assert [run.get("accession") for run in root] == [one for batch in found for one in batch]


@pytest.mark.usefixtures("small_batches")
@respx.mock
def test_every_batch_not_found_raises(browser: BrowserClient) -> None:
    route = respx.post(XML_URL).mock(return_value=httpx.Response(404, text=NOT_FOUND))

    with pytest.raises(ENANotFoundError, match="any of 5 accessions"):
        browser.fetch(["A1", "A2", "A3", "A4", "A5"])

    assert route.call_count == 3


@pytest.mark.usefixtures("small_batches")
@respx.mock
def test_xml_in_a_layout_that_cannot_be_merged_is_refused(browser: BrowserClient) -> None:
    body = '<RUN_SET><RUN accession="A1"/></RUN_SET>\n'
    respx.post(XML_URL).mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(ENAQueryError, match="cannot join"):
        browser.fetch(["A1", "A2", "A3"])


@respx.mock
def test_fetch_to_file_writes_what_fetch_returns(browser: BrowserClient, tmp_path: Path) -> None:
    respx.post(FASTA_URL).mock(side_effect=answering(fasta))
    path = tmp_path / "records.fasta"

    written = browser.fetch_to_file(path, ["A1", "A2"], format="fasta")

    assert written == 2
    assert path.read_text() == browser.fetch(["A1", "A2"], format="fasta")
    assert list(tmp_path.iterdir()) == [path]


@respx.mock
def test_fetch_to_file_leaves_nothing_behind_when_cut_short(
    browser: BrowserClient, tmp_path: Path
) -> None:
    class CutShort(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield fasta("A1").encode()
            raise httpx.RemoteProtocolError("incomplete chunked read")

    respx.post(FASTA_URL).mock(return_value=httpx.Response(200, stream=CutShort()))
    path = tmp_path / "records.fasta"
    path.write_text("earlier contents\n")

    with pytest.raises(ENAConnectionError):
        browser.fetch_to_file(path, ["A1", "A2"], format="fasta")

    assert path.read_text() == "earlier contents\n"
    assert list(tmp_path.iterdir()) == [path]


@respx.mock
def test_fetch_to_file_writes_into_an_open_stream(
    browser: BrowserClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.post(FASTA_URL).mock(side_effect=answering(fasta))
    monkeypatch.chdir(tmp_path)
    stream = io.BytesIO()

    assert browser.fetch_to_file(stream, ["A1", "A2"], format="fasta") == 2
    assert stream.getvalue().decode() == fasta("A1", "A2")
    assert not stream.closed
    assert list(tmp_path.iterdir()) == []


def test_fetch_to_file_of_no_accessions_writes_an_empty_file(
    browser: BrowserClient, tmp_path: Path
) -> None:
    path = tmp_path / "records.xml"

    assert browser.fetch_to_file(path, []) == 0
    assert path.read_text() == ""


TEXTSEARCH = (
    "accession\tdescription\n"
    '"SRP265601"\t"Draft genome sequence of ""Candidatus Phytoplasma pruni"""\n'
    '"ERP009009"\t"Tara Oceans Ocean Microbiome project"\n'
)


@respx.mock
def test_textsearch_unquotes_enas_csv_style_values(browser: BrowserClient) -> None:
    respx.get(TEXTSEARCH_URL).mock(return_value=httpx.Response(200, text=TEXTSEARCH))

    hits = browser.textsearch("Candidatus", result="read_study")

    assert hits.columns == ["accession", "description"]
    assert hits["accession"].to_list() == ["SRP265601", "ERP009009"]
    assert hits["description"][0] == 'Draft genome sequence of "Candidatus Phytoplasma pruni"'


@respx.mock
def test_textsearch_sends_a_bounded_limit_by_default(browser: BrowserClient) -> None:
    route = respx.get(TEXTSEARCH_URL).mock(return_value=httpx.Response(200, text=TEXTSEARCH))

    browser.textsearch("Tara oceans", result="read_study")

    params = route.calls[0].request.url.params
    assert params["query"] == "Tara oceans"
    assert params["result"] == "read_study"
    assert params["limit"] == str(DEFAULT_TEXTSEARCH_LIMIT)
    assert "offset" not in params


@respx.mock
def test_textsearch_can_page_or_take_every_hit(browser: BrowserClient) -> None:
    route = respx.get(TEXTSEARCH_URL).mock(return_value=httpx.Response(200, text=TEXTSEARCH))

    browser.textsearch("Tara", result="read_study", limit=10, offset=20)
    browser.textsearch("Tara", result="read_study", limit=None)

    paged, everything = (call.request.url.params for call in route.calls)
    assert (paged["limit"], paged["offset"]) == ("10", "20")
    assert "limit" not in everything


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"limit": 0}, "limit must be at least 1"), ({"offset": -1}, "offset")],
)
def test_textsearch_refuses_a_limit_that_would_return_nothing(
    browser: BrowserClient, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        browser.textsearch("Tara", result="read_study", **kwargs)


@respx.mock
def test_textsearch_raises_enas_http_200_rejection(browser: BrowserClient) -> None:
    body = "<error>Invalid result type 'nonsense'.</error>"
    respx.get(TEXTSEARCH_URL).mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(ENAQueryError, match=r"^Invalid result type 'nonsense'\.$"):
        browser.textsearch("Tara", result="nonsense")


@respx.mock
def test_textsearch_with_no_hits_keeps_its_columns(browser: BrowserClient) -> None:
    respx.get(TEXTSEARCH_URL).mock(
        return_value=httpx.Response(200, text="accession\tdescription\n")
    )

    hits = browser.textsearch("zzzz", result="read_study")

    assert hits.columns == ["accession", "description"]
    assert hits.height == 0


@respx.mock
def test_textsearch_count_reads_the_string_count(browser: BrowserClient) -> None:
    route = respx.get(TEXTSEARCH_COUNT_URL).mock(
        return_value=httpx.Response(200, json={"count": "48"})
    )

    assert browser.textsearch_count("Tara oceans", result="read_study") == 48
    assert route.calls[0].request.url.params["result"] == "read_study"


@respx.mock
def test_textsearch_count_rejects_an_unexpected_body(browser: BrowserClient) -> None:
    respx.get(TEXTSEARCH_COUNT_URL).mock(return_value=httpx.Response(200, json={"hits": 1}))

    with pytest.raises(ENAQueryError, match="unexpected count"):
        browser.textsearch_count("Tara", result="read_study")


@respx.mock
def test_module_level_fetch_uses_a_shared_browser_client(
    browser: BrowserClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_api, "_browser", browser)
    respx.post(XML_URL).mock(side_effect=answering(run_set))

    assert enaportal.fetch("ERR1") == run_set("ERR1")


_nested_tags = st.sampled_from(["TITLE", "RUN", "taxon", "RUN_SET", "SAMPLE_ATTRIBUTES"])


@st.composite
def _element(draw: st.DrawFn, depth: int) -> list[str]:
    """An element's lines, indented anywhere from none to six spaces.

    Nested tags may reuse the record's own name, as a taxon's lineage does, and
    may sit at column 0, as sample XML's do.
    """
    tag = draw(_nested_tags)
    indent = " " * draw(st.integers(0, 6))
    children = draw(st.integers(0, 3)) if depth < 3 else 0
    if not children:
        return [f"{indent}<{tag}/>" if draw(st.booleans()) else f"{indent}<{tag}>x</{tag}>"]
    lines = [f"{indent}<{tag}>"]
    for _ in range(children):
        lines += draw(_element(depth + 1))
    return [*lines, f"{indent}</{tag}>"]


@st.composite
def _document(draw: st.DrawFn, accessions: list[str]) -> list[str]:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>'] if draw(st.booleans()) else []
    lines.append("<RUN_SET>")
    for accession in accessions:
        lines.append(f'<RUN accession="{accession}">')
        for _ in range(draw(st.integers(0, 3))):
            lines += draw(_element(1))
        lines.append("</RUN>")
    return [*lines, "</RUN_SET>"]


@given(st.data(), st.integers(0, 8))
def test_the_xml_counter_counts_records_however_they_are_laid_out(
    data: st.DataObject, records: int
) -> None:
    counter = _RecordCounter("xml")

    for line in data.draw(_document([f"A{index}" for index in range(records)])):
        counter.feed(line)

    assert counter.countable
    assert counter.records == records


@given(st.data(), st.lists(st.integers(0, 4), min_size=1, max_size=5))
def test_merged_batches_are_one_document_with_every_record_in_order(
    data: st.DataObject, sizes: list[int]
) -> None:
    accessions = [f"A{index}" for index in range(sum(sizes))]
    batches: list[list[str]] = []
    start = 0
    for size in sizes:
        empty_body = size == 0 and data.draw(st.booleans())
        batch = accessions[start : start + size]
        batches.append([] if empty_body else data.draw(_document(batch)))
        start += size

    merged = list(_merge_xml(iter(batch) for batch in batches))

    if all(not batch for batch in batches):
        assert merged == []
        return
    root = ElementTree.fromstring("\n".join(merged).encode())
    assert root.tag == "RUN_SET"
    assert [run.get("accession") for run in root] == accessions
