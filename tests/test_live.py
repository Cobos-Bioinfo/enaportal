"""Contract tests against the live ENA API.

Excluded from the default run. These check that the shapes enaportal relies on
still hold, not that any particular record exists.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import polars as pl
import pytest

from enaportal.files import file_urls, to_manifest
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


def test_related_turns_a_study_into_its_runs(client: PortalClient) -> None:
    """M4's acceptance criterion: navigation in one call, no links endpoint."""
    runs = client.related("PRJEB1787", limit=5)

    assert runs.height == 5
    assert set(runs["study_accession"]) == {"PRJEB1787"}
    assert all(accession.startswith("ERR") for accession in runs["run_accession"])


def test_related_accepts_the_secondary_accession_form(client: PortalClient) -> None:
    primary = client.related("PRJEB1787", fields=["study_accession"], limit=1)
    secondary = client.related("ERP001736", fields=["study_accession"], limit=1)

    assert primary["study_accession"][0] == secondary["study_accession"][0] == "PRJEB1787"


def test_filereport_accepts_a_sample_result_despite_the_docs(client: PortalClient) -> None:
    """The docs claim read_run and analysis only. They are wrong."""
    frame = client.filereport(
        "SAMN00002139", result="sample", fields=["sample_accession", "scientific_name"]
    )

    assert frame["scientific_name"][0] == "Saccharomyces cerevisiae"


def test_filereport_stacks_several_accessions(client: PortalClient) -> None:
    frame = client.filereport(["ERR10003190", "ERR10003194"], fields=["run_accession", "fastq_ftp"])

    assert sorted(frame["run_accession"]) == ["ERR10003190", "ERR10003194"]


def test_resolved_urls_are_real_and_their_sizes_match(client: PortalClient) -> None:
    """The part fixtures cannot prove: that a manifest points at fetchable bytes."""
    frame = client.filereport(
        "ERR10003190", fields=["run_accession", "fastq_ftp", "fastq_md5", "fastq_bytes"]
    )
    files = file_urls(frame)

    assert files.height == 2
    assert files["md5"].null_count() == 0

    first = files.row(0, named=True)
    response = httpx.head(str(first["url"]), follow_redirects=True, timeout=60.0)

    assert response.status_code == 200
    assert int(response.headers["content-length"]) == first["bytes"]


def test_a_manifest_can_be_built_from_a_search_result(client: PortalClient) -> None:
    frame = client.search(
        "read_run",
        query='tax_tree(4932) AND library_layout="PAIRED"',
        fields=["run_accession", "fastq_ftp", "fastq_md5", "fastq_bytes"],
        limit=5,
    )

    aria = to_manifest(frame, "aria2c")
    samplesheet = to_manifest(frame, "nf-core", pipeline="rnaseq")
    accessions = to_manifest(frame, "accessions")

    assert aria.count("checksum=md5=") == aria.count("  out=")
    assert samplesheet.splitlines()[0] == "sample,fastq_1,fastq_2,strandedness"
    assert len(samplesheet.splitlines()) == 6
    assert len(accessions.splitlines()) == 5
