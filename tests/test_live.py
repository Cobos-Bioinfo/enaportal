"""Contract tests against the live ENA API.

Excluded from the default run. These check that the shapes enaportal relies on
still hold, not that any particular record exists.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest

from enaportal.portal import PortalClient
from enaportal.schema import SchemaClient

pytestmark = pytest.mark.live


@pytest.fixture
def client(tmp_path: Path) -> Iterator[PortalClient]:
    with PortalClient(cache_dir=tmp_path) as portal:
        yield portal


def test_results_still_lists_every_result_type(client: PortalClient) -> None:
    results = client.results()

    assert len(results) >= 15
    assert "read_run" in [result.result_id for result in results]


def test_read_run_field_counts_are_stable(client: PortalClient) -> None:
    assert len(client.return_fields("read_run")) >= 190
    assert len(client.search_fields("read_run")) >= 155


def test_field_metadata_still_has_the_documented_shape(client: PortalClient) -> None:
    fields = client.return_fields("read_run")

    assert all(field.column_id for field in fields)
    assert all(field.type is not None for field in fields), "an unrecognised untyped field"
    assert {field.type for field in fields} <= {
        "text",
        "number",
        "date",
        "boolean",
        "latlon",
        "list",
        "taxonomy",
        "controlled value",
        "indexed",
        None,
    }


def test_first_public_is_still_a_searchable_date(client: PortalClient) -> None:
    """M5 partitions on a date field, and this is its default."""
    dates = [field.column_id for field in client.schema.date_search_fields("read_run")]

    assert "first_public" in dates


def test_count_is_cheap_and_numeric(client: PortalClient) -> None:
    assert client.count("read_run", query="tax_tree(4932)") > 100_000


def test_the_readme_example_runs(client: PortalClient) -> None:
    frame = client.search(
        "read_run",
        query='tax_tree(4932) AND library_strategy="RNA-Seq"',
        fields=["run_accession", "fastq_ftp", "read_count"],
        limit=5,
    )

    assert frame.height == 5
    assert frame.columns == ["run_accession", "fastq_ftp", "read_count"]
    assert set(frame.schema.values()) == {pl.String}


def test_json_and_tsv_give_the_same_columns(client: PortalClient) -> None:
    """Row identity cannot be compared: ENA does not order results stably."""
    query = 'tax_tree(4932) AND library_strategy="RNA-Seq"'
    fields = ["run_accession", "read_count"]
    tsv = client.search("read_run", query=query, fields=fields, limit=5, format="tsv")
    payload = client.search("read_run", query=query, fields=fields, limit=5, format="json")

    assert tsv.columns == payload.columns == fields
    assert tsv.height == payload.height == 5


def test_results_are_not_stably_ordered(client: PortalClient) -> None:
    """Documents why M5 partitions by query range and never by row position.

    Two identical limited queries return different rows, so no offset-free
    paging or reproducible sampling is possible on top of `limit` alone.
    """
    query = 'tax_tree(4932) AND library_strategy="RNA-Seq"'
    runs = [
        set(
            client.search("read_run", query=query, fields=["run_accession"], limit=5)[
                "run_accession"
            ]
        )
        for _ in range(3)
    ]

    assert not (runs[0] == runs[1] == runs[2]), "ENA ordering became stable; recheck PLAN.md"


def test_offset_is_still_rejected(client: PortalClient) -> None:
    """The fact M5 exists for. If this ever passes, the architecture can simplify."""
    from enaportal.errors import ENAQueryError

    with pytest.raises(ENAQueryError):
        client._http.get_text(
            "search",
            params={"result": "read_run", "limit": 5, "offset": 5, "format": "tsv"},
            shape="tsv",
        )


def test_the_snapshot_has_not_drifted_beyond_recognition(tmp_path: Path) -> None:
    live = SchemaClient(cache_dir=tmp_path)
    offline = SchemaClient(cache_dir=tmp_path / "empty", offline=True)

    live_ids = set(live.result_ids())
    with pytest.warns(UserWarning, match="packaged schema snapshot"):
        snapshot_ids = set(offline.result_ids())

    assert live_ids == snapshot_ids
