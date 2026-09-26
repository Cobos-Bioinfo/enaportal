"""Unit tests for search and count. Every response is mocked; nothing hits ENA."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import polars as pl
import pytest
import respx

from enaportal._http import PORTAL_BASE_URL, ENAHTTPClient
from enaportal.errors import ENAQueryError
from enaportal.portal import MAX_GET_LENGTH, PortalClient
from enaportal.schema import SchemaClient

SEARCH_URL = f"{PORTAL_BASE_URL}search"
COUNT_URL = f"{PORTAL_BASE_URL}count"

TSV = (
    "run_accession\tfastq_ftp\tread_count\n"
    "ERR1\tftp.sra.ebi.ac.uk/1_1.fastq.gz;ftp.sra.ebi.ac.uk/1_2.fastq.gz\t1000\n"
    "ERR2\tftp.sra.ebi.ac.uk/2.fastq.gz\t2000\n"
)


@pytest.fixture
def http() -> Iterator[ENAHTTPClient]:
    with ENAHTTPClient(max_retries=0) as client:
        yield client


@pytest.fixture
def client(http: ENAHTTPClient, tmp_path: Path) -> PortalClient:
    """A client whose schema comes from the packaged snapshot, never the network."""
    schema = SchemaClient(cache_dir=tmp_path, offline=True)
    with pytest.warns(UserWarning, match="packaged schema snapshot"):
        schema.results()
        schema.return_fields("read_run")
        schema.search_fields("read_run")
    return PortalClient(http=http, schema=schema)


@respx.mock
def test_count_skips_the_tsv_header(client: PortalClient) -> None:
    respx.get(COUNT_URL).mock(return_value=httpx.Response(200, text="count\n276447\n"))

    assert client.count("read_run", query="tax_tree(4932)") == 276447


@respx.mock
def test_count_accepts_a_bare_number(client: PortalClient) -> None:
    respx.get(COUNT_URL).mock(return_value=httpx.Response(200, text="276447\n"))

    assert client.count("read_run") == 276447


@respx.mock
def test_count_sends_result_and_query(client: PortalClient) -> None:
    route = respx.get(COUNT_URL).mock(return_value=httpx.Response(200, text="1"))

    client.count("read_run", query="tax_tree(4932)")

    params = route.calls[0].request.url.params
    assert params["result"] == "read_run"
    assert params["query"] == "tax_tree(4932)"


@respx.mock
def test_count_rejects_a_non_numeric_body(client: PortalClient) -> None:
    respx.get(COUNT_URL).mock(return_value=httpx.Response(200, text="count\nlots\n"))

    with pytest.raises(ENAQueryError, match="non-numeric count"):
        client.count("read_run")


@respx.mock
def test_search_returns_a_dataframe(client: PortalClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))

    frame = client.search("read_run", query="tax_tree(4932)", fields=["run_accession"])

    assert frame.shape == (2, 3)
    assert frame["run_accession"].to_list() == ["ERR1", "ERR2"]


@respx.mock
def test_every_column_is_a_string(client: PortalClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))

    frame = client.search("read_run")

    assert set(frame.schema.values()) == {pl.String}


@respx.mock
def test_multi_value_cells_are_left_intact(client: PortalClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))

    frame = client.search("read_run")

    assert frame["fastq_ftp"][0].count(";") == 1


@respx.mock
def test_bare_quotes_in_free_text_do_not_break_parsing(client: PortalClient) -> None:
    body = 'run_accession\tstudy_title\nERR1\tThe "best" study\nERR2\tAnother\n'
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=body))

    frame = client.search("read_run")

    assert frame["study_title"].to_list() == ['The "best" study', "Another"]


@respx.mock
def test_an_empty_response_gives_an_empty_frame(client: PortalClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=""))

    assert client.search("read_run").is_empty()


@respx.mock
def test_a_header_only_response_gives_a_frame_with_columns(client: PortalClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text="run_accession\n"))

    frame = client.search("read_run")

    assert frame.columns == ["run_accession"]
    assert frame.height == 0


@respx.mock
def test_fields_are_sent_comma_separated(client: PortalClient) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))

    client.search("read_run", fields=["run_accession", "fastq_ftp"])

    assert route.calls[0].request.url.params["fields"] == "run_accession,fastq_ftp"


@respx.mock
def test_limit_is_sent_when_given(client: PortalClient) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))

    client.search("read_run", limit=0)

    assert route.calls[0].request.url.params["limit"] == "0"


@respx.mock
def test_limit_is_omitted_when_not_given(client: PortalClient) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))

    client.search("read_run")

    assert "limit" not in route.calls[0].request.url.params


@respx.mock
def test_data_portal_and_metagenomes_are_sent(client: PortalClient) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))

    client.search("read_run", data_portal="metagenome", include_metagenomes=True)

    params = route.calls[0].request.url.params
    assert params["dataPortal"] == "metagenome"
    assert params["includeMetagenomes"] == "true"


@respx.mock
def test_json_format_returns_the_same_shape(client: PortalClient) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=[{"run_accession": "ERR1", "read_count": "10"}])
    )

    frame = client.search("read_run", format="json")

    assert frame.columns == ["run_accession", "read_count"]
    assert set(frame.schema.values()) == {pl.String}


@respx.mock
def test_a_long_query_is_sent_as_a_post(client: PortalClient) -> None:
    accessions = ",".join(f"ERR{index}" for index in range(400))
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))

    client.search("read_run", query=f'run_accession="{accessions}"', validate=False)

    assert route.call_count == 1
    assert len(accessions) > MAX_GET_LENGTH


@respx.mock
def test_a_short_query_is_sent_as_a_get(client: PortalClient) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))

    client.search("read_run", query="tax_tree(4932)")

    assert route.call_count == 1


def test_an_unknown_result_fails_before_sending(client: PortalClient) -> None:
    with respx.mock:
        route = respx.get(SEARCH_URL)
        with pytest.raises(ENAQueryError, match="Did you mean 'read_run'"):
            client.search("read_runs")
        assert route.call_count == 0


def test_an_unknown_return_field_fails_before_sending(client: PortalClient) -> None:
    with respx.mock:
        route = respx.get(SEARCH_URL)
        with pytest.raises(ENAQueryError, match="run_accession"):
            client.search("read_run", fields=["run_acession"])
        assert route.call_count == 0


def test_an_unknown_query_field_fails_before_sending(client: PortalClient) -> None:
    with respx.mock:
        route = respx.get(COUNT_URL)
        with pytest.raises(ENAQueryError, match="instrument_platform"):
            client.count("read_run", query='instrument_platfrom="ILLUMINA"')
        assert route.call_count == 0


@respx.mock
def test_validation_can_be_turned_off(client: PortalClient) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))

    client.search("read_run", fields=["not_a_field"], validate=False)

    assert route.call_count == 1


@respx.mock
def test_an_http_200_text_error_still_raises(client: PortalClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text="Unsupported param offset"))

    with pytest.raises(ENAQueryError, match="Unsupported param offset"):
        client.search("read_run")
