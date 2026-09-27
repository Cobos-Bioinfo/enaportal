"""Replays of real ENA responses through the real parsers.

The other offline tests use responses written by hand, which can only encode
what their author believed ENA sends. These use what it did send, recorded by
scripts/record_fixtures.py. Each also checks the library still sends the
request the recording was made from.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path
from xml.etree import ElementTree

import pytest
import respx

from enaportal._http import BROWSER_BASE_URL, ENAHTTPClient
from enaportal.browser import BrowserClient
from enaportal.errors import ENANotFoundError, ENAQueryError
from enaportal.files import file_urls, to_manifest
from enaportal.portal import PortalClient
from enaportal.schema import SchemaClient
from recorded import FIXTURES, load

RECORDER = Path(__file__).resolve().parent.parent / "scripts" / "record_fixtures.py"

SEARCH_FIELDS = ["run_accession", "study_title", "fastq_ftp", "fastq_md5", "fastq_bytes"]


@pytest.fixture
def slept() -> list[float]:
    return []


@pytest.fixture
def portal(tmp_path: Path, slept: list[float]) -> Iterator[PortalClient]:
    schema = SchemaClient(cache_dir=tmp_path, offline=True)
    with pytest.warns(UserWarning, match="packaged schema snapshot"):
        schema.results()
        for result in ("read_run", "sample"):
            schema.return_fields(result)
            schema.search_fields(result)
    with ENAHTTPClient(rate_limit=None, sleep=slept.append) as http:
        yield PortalClient(http=http, schema=schema)


@pytest.fixture
def browser() -> Iterator[BrowserClient]:
    with ENAHTTPClient(BROWSER_BASE_URL, max_retries=0, rate_limit=None) as http:
        yield BrowserClient(http=http)


@pytest.fixture
def quiet() -> Iterator[None]:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        yield


def test_every_recording_the_script_defines_is_on_disk() -> None:
    spec = importlib.util.spec_from_file_location("record_fixtures", RECORDER)
    assert spec is not None and spec.loader is not None
    recorder = importlib.util.module_from_spec(spec)
    # dataclasses looks its module up by name while building the class.
    sys.modules[spec.name] = recorder
    try:
        spec.loader.exec_module(recorder)
    finally:
        del sys.modules[spec.name]

    assert {path.stem for path in FIXTURES.glob("*.json")} == set(recorder.RECORDINGS)


@respx.mock
def test_count_reads_the_number_under_its_header(portal: PortalClient) -> None:
    recording = load("portal_count")
    route = recording.mock()

    total = portal.count("read_run", query="tax_tree(4932)")

    recording.assert_sent_by(route)
    assert total == int(recording.body.split()[1])


@respx.mock
def test_search_reads_real_tsv_with_bare_quotes_intact(portal: PortalClient) -> None:
    recording = load("portal_search_tsv")
    route = recording.mock()

    frame = portal.search(
        "read_run", query="secondary_study_accession=SRP265601", fields=SEARCH_FIELDS, limit=3
    )

    recording.assert_sent_by(route)
    lines = [line.split("\t") for line in recording.body.splitlines()]
    assert frame.columns == lines[0]
    assert [list(row) for row in frame.rows()] == [
        [value or None for value in line] for line in lines[1:]
    ]
    assert '"Candidatus' in frame["study_title"][0]


@respx.mock
def test_json_and_tsv_searches_give_the_same_frame(portal: PortalClient) -> None:
    routes = [load(name).mock() for name in ("portal_search_tsv", "portal_search_json")]
    arguments = {
        "query": "secondary_study_accession=SRP265601",
        "fields": SEARCH_FIELDS,
        "limit": 3,
    }

    from_tsv = portal.search("read_run", format="tsv", **arguments)  # type: ignore[arg-type]
    from_json = portal.search("read_run", format="json", **arguments)  # type: ignore[arg-type]

    load("portal_search_json").assert_sent_by(routes[1])
    assert from_tsv.equals(from_json)


@respx.mock
def test_a_real_paired_run_resolves_to_aligned_files(portal: PortalClient) -> None:
    recording = load("portal_filereport_paired")
    route = recording.mock()
    fields = recording.params["fields"].split(",")

    frame = portal.filereport("ERR315859", fields=fields)
    files = file_urls(frame)

    recording.assert_sent_by(route)
    row = dict(zip(frame.columns, frame.row(0), strict=True))
    assert files["filename"].to_list() == [
        path.split("/")[-1] for path in row["fastq_ftp"].split(";")
    ]
    assert files["md5"].to_list() == row["fastq_md5"].split(";")
    assert files["bytes"].to_list() == [int(size) for size in row["fastq_bytes"].split(";")]
    assert to_manifest(frame, "nf-core").splitlines()[1].count("https://") == 2


@respx.mock
def test_the_wrong_result_for_an_accession_names_the_accepted_forms(
    portal: PortalClient,
) -> None:
    recording = load("portal_filereport_wrong_result")
    route = recording.mock()

    with pytest.raises(ENAQueryError, match=r"Supported accession types are") as caught:
        portal.filereport("ERR164407", result="sample")

    recording.assert_sent_by(route)
    assert str(caught.value) == recording.body.strip()


@respx.mock
def test_offset_is_refused_as_a_query_error(portal: PortalClient) -> None:
    recording = load("portal_search_offset")
    recording.mock()

    with pytest.raises(ENAQueryError, match=r"^Unsupported param offset$"):
        portal._http.get_text("search", params=recording.params, shape="tsv")


@respx.mock
def test_an_unknown_endpoint_is_not_found(portal: PortalClient) -> None:
    recording = load("portal_not_found")
    recording.mock()

    with pytest.raises(ENANotFoundError, match=r"HTTP 404 for \S+no_such_endpoint$"):
        portal._http.get_text("no_such_endpoint")


@respx.mock
def test_an_unknown_search_field_is_a_query_error_and_not_retried(
    portal: PortalClient, slept: list[float]
) -> None:
    recording = load("portal_count_unknown_field")
    route = recording.mock()

    with pytest.raises(ENAQueryError, match=r"^Unknown search field:not_a_field$"):
        portal.count("read_run", query="not_a_field=1", validate=False)

    recording.assert_sent_by(route)
    assert route.call_count == 1
    assert slept == []


@respx.mock
def test_real_run_xml_counts_as_two_records(browser: BrowserClient, quiet: None) -> None:
    recording = load("browser_xml_runs")
    route = recording.mock()

    xml = browser.fetch(["ERR164407", "ERR164408"])

    recording.assert_sent_by(route)
    assert xml.splitlines() == recording.body.splitlines()
    root = ElementTree.fromstring(xml.encode())
    assert [run.get("accession") for run in root] == ["ERR164407", "ERR164408"]


@pytest.mark.parametrize(
    ("name", "accession", "root_tag"),
    [
        ("browser_xml_taxon", "9606", "TAXON_SET"),
        ("browser_xml_sample", "SAMEA1571379", "SAMPLE_SET"),
    ],
)
@respx.mock
def test_awkwardly_laid_out_xml_still_counts_as_one_record(
    browser: BrowserClient, quiet: None, name: str, accession: str, root_tag: str
) -> None:
    recording = load(name)
    recording.mock()

    xml = browser.fetch(accession)

    assert ElementTree.fromstring(xml.encode()).tag == root_tag
    assert len(ElementTree.fromstring(xml.encode())) == 1


@respx.mock
def test_a_real_dropped_accession_warns(browser: BrowserClient) -> None:
    recording = load("browser_xml_dropped")
    recording.mock()

    with pytest.warns(UserWarning, match="Only 1 of 2 accessions came back"):
        browser.fetch(["ERR164407", "ERR99999999"])


@respx.mock
def test_a_real_unknown_accession_is_not_found(browser: BrowserClient) -> None:
    load("browser_xml_not_found").mock()

    with pytest.raises(ENANotFoundError, match=r"^ENA has no xml record for ERR99999999$"):
        browser.fetch("ERR99999999")


@pytest.mark.parametrize(
    ("name", "accessions", "format", "message"),
    [
        (
            "browser_xml_mixed_types",
            ["PRJEB1787", "ERR164407"],
            "xml",
            "All accessions must be of the same data type as the first accession, which was "
            "PROJECT.",
        ),
        (
            "browser_embl_wrong_format",
            ["PRJEB1787"],
            "embl",
            "Format embl is not available for record PRJEB1787 in data type PROJECT",
        ),
    ],
)
@respx.mock
def test_a_real_rejection_is_reported_in_enas_words(
    browser: BrowserClient, name: str, accessions: list[str], format: str, message: str
) -> None:
    load(name).mock()

    with pytest.raises(ENAQueryError) as caught:
        browser.fetch(accessions, format=format)  # type: ignore[arg-type]

    assert str(caught.value) == message


@respx.mock
def test_real_embl_passes_through_and_counts_once(browser: BrowserClient, quiet: None) -> None:
    recording = load("browser_embl")
    route = recording.mock()

    assert browser.fetch("A00145", format="embl") == recording.body
    recording.assert_sent_by(route)


@respx.mock
def test_real_annotation_only_embl_has_no_sequence(browser: BrowserClient, quiet: None) -> None:
    recording = load("browser_embl_annotation_only")
    route = recording.mock()

    embl = browser.fetch("A00145", format="embl", annotation_only=True)

    recording.assert_sent_by(route)
    assert "\nSQ   " not in embl
    assert embl.rstrip().endswith("//")


@respx.mock
def test_real_fasta_counts_one_record_per_header(browser: BrowserClient, quiet: None) -> None:
    recording = load("browser_fasta")
    route = recording.mock()

    assert browser.fetch(["A00145", "A00146"], format="fasta") == recording.body
    recording.assert_sent_by(route)


@respx.mock
def test_real_text_search_unquotes_like_a_csv_reader(browser: BrowserClient) -> None:
    recording = load("browser_textsearch")
    route = recording.mock()

    hits = browser.textsearch("Candidatus Phytoplasma", result="read_study", limit=20)

    recording.assert_sent_by(route)
    expected = list(csv.reader(io.StringIO(recording.body), delimiter="\t"))
    assert hits.columns == expected[0]
    assert [list(row) for row in hits.rows()] == [row for row in expected[1:] if row]
    assert any('"' in description for description in hits["description"])


@respx.mock
def test_real_text_search_count_is_a_string_number(browser: BrowserClient) -> None:
    recording = load("browser_textsearch_count")
    route = recording.mock()

    assert browser.textsearch_count("Tara oceans", result="read_study") == 48
    recording.assert_sent_by(route)


@respx.mock
def test_real_text_search_rejection_arrives_with_http_200(browser: BrowserClient) -> None:
    recording = load("browser_textsearch_bad_result")
    route = recording.mock()

    assert recording.status == 200
    with pytest.raises(ENAQueryError, match=r"^Invalid result type 'nonsense'\.$"):
        browser.textsearch("Tara oceans", result="nonsense")
    recording.assert_sent_by(route)
