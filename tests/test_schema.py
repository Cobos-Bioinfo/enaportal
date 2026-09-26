"""Unit tests for schema introspection. Every response is mocked or snapshotted."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import httpx
import pytest
import respx

from enaportal._http import PORTAL_BASE_URL, ENAHTTPClient
from enaportal.errors import ENAQueryError, ENASchemaError
from enaportal.schema import Field, Result, SchemaClient, read_snapshot

RESULTS_URL = f"{PORTAL_BASE_URL}results"
RETURN_FIELDS_URL = f"{PORTAL_BASE_URL}returnFields"
SEARCH_FIELDS_URL = f"{PORTAL_BASE_URL}searchFields"

RESULTS_PAYLOAD = [
    {
        "resultId": "read_run",
        "description": "Raw reads",
        "primaryAccessionType": "run_accession",
        "recordCount": "37000000",
        "lastUpdated": "2026-09-26 00:00",
    },
    {"resultId": "sample", "description": "Samples"},
]

FIELDS_PAYLOAD = [
    {"columnId": "run_accession", "description": "accession number", "type": "text"},
    {"columnId": "first_public", "description": "date when made public", "type": "date"},
    {"columnId": "aligned", "description": "boolean"},
]


@pytest.fixture
def http() -> Iterator[ENAHTTPClient]:
    with ENAHTTPClient(max_retries=0) as client:
        yield client


@pytest.fixture
def client(http: ENAHTTPClient, tmp_path: Path) -> SchemaClient:
    return SchemaClient(http=http, cache_dir=tmp_path)


class Routes(NamedTuple):
    results: respx.Route
    return_fields: respx.Route
    search_fields: respx.Route


def mock_schema_routes() -> Routes:
    """Register the three introspection endpoints and hand back their routes."""
    return Routes(
        results=respx.get(RESULTS_URL).mock(return_value=httpx.Response(200, json=RESULTS_PAYLOAD)),
        return_fields=respx.get(RETURN_FIELDS_URL).mock(
            return_value=httpx.Response(200, json=FIELDS_PAYLOAD)
        ),
        search_fields=respx.get(SEARCH_FIELDS_URL).mock(
            return_value=httpx.Response(200, json=FIELDS_PAYLOAD)
        ),
    )


def test_result_parses_a_payload() -> None:
    result = Result.from_payload(RESULTS_PAYLOAD[0])

    assert result.result_id == "read_run"
    assert result.record_count == 37000000
    assert result.primary_accession_type == "run_accession"


def test_result_tolerates_missing_optional_keys() -> None:
    result = Result.from_payload(RESULTS_PAYLOAD[1])

    assert result.record_count is None
    assert result.primary_accession_type is None


def test_field_parses_a_declared_type() -> None:
    field = Field.from_payload(FIELDS_PAYLOAD[1])

    assert field == Field("first_public", "date when made public", "date")
    assert field.is_date


def test_field_recovers_a_type_ena_left_out() -> None:
    field = Field.from_payload({"columnId": "aligned", "description": "boolean"})

    assert field == Field("aligned", "", "boolean")


def test_field_recovers_a_date_type_ena_left_out() -> None:
    field = Field.from_payload({"columnId": "collection_date", "description": "date"})

    assert field.is_date


def test_field_keeps_a_description_that_is_not_a_type_word() -> None:
    field = Field.from_payload({"columnId": "age", "description": "Age when sampled"})

    assert field == Field("age", "Age when sampled", None)


@respx.mock
def test_reads_results_from_ena(client: SchemaClient) -> None:
    mock_schema_routes()

    assert [result.result_id for result in client.results()] == ["read_run", "sample"]


@respx.mock
def test_requests_json_format(client: SchemaClient) -> None:
    routes = mock_schema_routes()

    client.results()

    assert routes.results.calls[0].request.url.params["format"] == "json"


@respx.mock
def test_a_second_lookup_does_not_hit_the_network(client: SchemaClient) -> None:
    routes = mock_schema_routes()

    client.return_fields("read_run")
    client.return_fields("read_run")

    assert routes.return_fields.call_count == 1


@respx.mock
def test_a_fresh_cache_is_used_by_a_new_client(http: ENAHTTPClient, tmp_path: Path) -> None:
    routes = mock_schema_routes()
    SchemaClient(http=http, cache_dir=tmp_path).results()

    SchemaClient(http=http, cache_dir=tmp_path).results()

    assert routes.results.call_count == 1


@respx.mock
def test_refresh_refetches(client: SchemaClient) -> None:
    routes = mock_schema_routes()
    client.results()

    client.refresh()
    client.results()

    assert routes.results.call_count == 2


@respx.mock
def test_an_expired_cache_is_refetched(http: ENAHTTPClient, tmp_path: Path) -> None:
    routes = mock_schema_routes()
    SchemaClient(http=http, cache_dir=tmp_path, ttl=0.0).results()

    SchemaClient(http=http, cache_dir=tmp_path, ttl=0.0).results()

    assert routes.results.call_count == 2


@respx.mock
def test_falls_back_to_a_stale_cache_when_ena_is_down(http: ENAHTTPClient, tmp_path: Path) -> None:
    routes = mock_schema_routes()
    SchemaClient(http=http, cache_dir=tmp_path).results()
    routes.results.mock(side_effect=httpx.ConnectError("down"))

    offline_client = SchemaClient(http=http, cache_dir=tmp_path, ttl=0.0)
    with pytest.warns(UserWarning, match="expired cached schema"):
        results = offline_client.results()

    assert [result.result_id for result in results] == ["read_run", "sample"]


@respx.mock
def test_falls_back_to_the_snapshot_when_there_is_no_cache(
    http: ENAHTTPClient, tmp_path: Path
) -> None:
    respx.get(RESULTS_URL).mock(side_effect=httpx.ConnectError("down"))

    client = SchemaClient(http=http, cache_dir=tmp_path)
    with pytest.warns(UserWarning, match="packaged schema snapshot"):
        results = client.results()

    assert "read_run" in [result.result_id for result in results]


def test_offline_mode_never_touches_the_network(tmp_path: Path) -> None:
    client = SchemaClient(cache_dir=tmp_path, offline=True)

    with pytest.warns(UserWarning, match="packaged schema snapshot"):
        fields = client.return_fields("read_run")

    assert "run_accession" in [field.column_id for field in fields]


@respx.mock
def test_raises_when_nothing_can_supply_the_schema(http: ENAHTTPClient, tmp_path: Path) -> None:
    respx.get(RESULTS_URL).mock(
        return_value=httpx.Response(200, json=[{"resultId": "invented", "description": "x"}])
    )
    respx.get(RETURN_FIELDS_URL).mock(side_effect=httpx.ConnectError("down"))

    client = SchemaClient(http=http, cache_dir=tmp_path)

    with pytest.raises(ENASchemaError, match="Could not read"):
        client.return_fields("invented")


@respx.mock
def test_rejects_a_non_list_payload(client: SchemaClient) -> None:
    respx.get(RESULTS_URL).mock(return_value=httpx.Response(200, json={"oops": 1}))

    with pytest.raises(ENASchemaError, match="unexpected shape"):
        client.results()


@respx.mock
def test_unknown_result_type_is_rejected_locally(client: SchemaClient) -> None:
    mock_schema_routes()

    with pytest.raises(ENAQueryError, match="Did you mean 'read_run'"):
        client.return_fields("read_runs")


@respx.mock
def test_unknown_return_field_is_rejected_locally(client: SchemaClient) -> None:
    mock_schema_routes()

    with pytest.raises(ENAQueryError, match="Did you mean 'run_accession'"):
        client.validate_return_fields("read_run", ["run_accession", "run_acession"])


@respx.mock
def test_known_fields_validate_silently(client: SchemaClient) -> None:
    mock_schema_routes()

    client.validate_return_fields("read_run", ["run_accession", "first_public"])
    client.validate_search_fields("read_run", ["first_public"])


@respx.mock
def test_date_search_fields_are_identified(client: SchemaClient) -> None:
    mock_schema_routes()

    assert [field.column_id for field in client.date_search_fields("read_run")] == ["first_public"]


def test_the_snapshot_covers_every_result_type() -> None:
    results = read_snapshot("results")

    assert results is not None
    for raw in results:
        result = raw["resultId"]
        assert read_snapshot(f"return_fields.{result}"), result
        assert read_snapshot(f"search_fields.{result}"), result


def test_a_missing_snapshot_key_is_none() -> None:
    assert read_snapshot("return_fields.not_a_result") is None
