"""Unit tests for partitioning and resumable bulk retrieval.

Every response is mocked. The fake below reproduces the one ENA behaviour the
partitioning depends on: a date range written `f>=A AND f<=B` selects
`A <= f < B`, so adjacent partitions tile exactly.
"""

from __future__ import annotations

import json
import re
import tempfile
import warnings
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import httpx
import polars as pl
import pytest
import respx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from enaportal._checkpoint import MANIFEST_NAME, Checkpoint
from enaportal._http import PORTAL_BASE_URL, ENAHTTPClient
from enaportal.bulk import (
    EPOCH,
    HORIZON_YEARS,
    BulkPlan,
    Partition,
    bisect_counts,
    job_key,
    outside_query,
    range_query,
)
from enaportal.errors import ENACheckpointError
from enaportal.portal import PortalClient
from enaportal.schema import SchemaClient

SEARCH_URL = f"{PORTAL_BASE_URL}search"
COUNT_URL = f"{PORTAL_BASE_URL}count"

_BOUND = re.compile(r"\w+(>=|<=)(\d{4}-\d{2}-\d{2})")


class FakeENA:
    """Enough of the Portal API to plan and fetch against.

    Rows are (accession, first_public); a None date stands for a row no date
    range can reach, which is what the residual partition exists for.
    """

    def __init__(self, rows: list[tuple[str, date | None]]) -> None:
        self.rows = rows
        self.counted: list[str] = []
        self.fetched: list[str] = []
        self.fail_on: Callable[[str], bool] = lambda query: False

    def count(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        self.counted.append(query)
        return httpx.Response(200, text=f"count\n{len(self._select(query))}\n")

    def search(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        self.fetched.append(query)
        if self.fail_on(query):
            raise httpx.ConnectError("ENA went away")
        rows = self._select(query)
        body = "run_accession\tfirst_public\n" + "".join(
            f"{accession}\t{when.isoformat() if when else ''}\n" for accession, when in rows
        )
        return httpx.Response(200, text=body)

    def _select(self, query: str) -> list[tuple[str, date | None]]:
        bounds = dict(_BOUND.findall(query))
        lower, upper = bounds.get(">="), bounds.get("<=")
        if lower is None or upper is None:
            return list(self.rows)
        start, end = date.fromisoformat(lower), date.fromisoformat(upper)
        inside = [row for row in self.rows if row[1] is not None and start <= row[1] < end]
        if "NOT (" in query:
            return [row for row in self.rows if row not in inside]
        return inside


@pytest.fixture
def http() -> Iterator[ENAHTTPClient]:
    """Throttling off: these tests make many counts and none of them measure it."""
    with ENAHTTPClient(max_retries=0, rate_limit=None) as client:
        yield client


@pytest.fixture
def client(http: ENAHTTPClient, tmp_path: Path) -> PortalClient:
    schema = SchemaClient(cache_dir=tmp_path / "schema", offline=True)
    with pytest.warns(UserWarning, match="packaged schema snapshot"):
        schema.results()
        for result in ("read_run", "taxon", "assembly"):
            schema.return_fields(result)
            schema.search_fields(result)
    return PortalClient(http=http, schema=schema)


@pytest.fixture
def ena() -> Iterator[FakeENA]:
    fake = FakeENA(_rows(900, date(2015, 1, 1), 3))
    with respx.mock:
        respx.get(COUNT_URL).mock(side_effect=fake.count)
        respx.get(SEARCH_URL).mock(side_effect=fake.search)
        yield fake


def _rows(count: int, start: date, step_days: int) -> list[tuple[str, date | None]]:
    return [
        (f"ERR{index:05d}", start + timedelta(days=index * step_days)) for index in range(count)
    ]


def test_range_query_parenthesises_the_caller_query() -> None:
    """Without the brackets ENA binds AND tighter than OR and returns far too much."""
    composed = range_query(
        'library_strategy="RNA-Seq" OR library_strategy="WGS"',
        "first_public",
        date(2020, 1, 1),
        date(2020, 7, 1),
    )

    assert composed == (
        '(library_strategy="RNA-Seq" OR library_strategy="WGS") AND '
        "first_public>=2020-01-01 AND first_public<=2020-07-01"
    )


def test_range_query_without_a_caller_query() -> None:
    composed = range_query(None, "first_public", date(2020, 1, 1), date(2021, 1, 1))

    assert composed == "first_public>=2020-01-01 AND first_public<=2021-01-01"


def test_outside_query_negates_the_same_bounds() -> None:
    composed = outside_query("tax_tree(4932)", "first_public", date(1982, 1, 1), date(2028, 1, 1))

    assert composed == (
        "(tax_tree(4932)) AND NOT (first_public>=1982-01-01 AND first_public<=2028-01-01)"
    )


def test_bisect_splits_until_every_piece_fits() -> None:
    pieces = bisect_counts(_uniform(4096), date(2000, 1, 1), date(2016, 1, 1), 4096, 500)

    assert pieces
    assert all(count <= 500 for _, _, count in pieces)


def test_bisect_pieces_tile_the_range_with_no_gap_or_overlap() -> None:
    start, end = date(2000, 1, 1), date(2016, 1, 1)

    pieces = bisect_counts(_uniform(4096), start, end, 4096, 300)

    assert pieces[0][0] == start
    assert pieces[-1][1] == end
    for (_, first_end, _), (second_start, _, _) in pairwise(pieces):
        assert first_end == second_start


def test_bisect_counts_only_one_child_per_split() -> None:
    """The other half is the difference, which halves the requests a plan costs."""
    calls: list[tuple[date, date]] = []

    def count_for(lower: date, upper: date) -> int:
        calls.append((lower, upper))
        return _uniform(4096)(lower, upper)

    pieces = bisect_counts(count_for, date(2000, 1, 1), date(2016, 1, 1), 4096, 500)

    assert len(calls) == len(pieces) - 1


def test_bisect_prunes_an_empty_half_without_descending_into_it() -> None:
    populated = (date(2015, 1, 1), date(2016, 1, 1))

    def count_for(lower: date, upper: date) -> int:
        overlap = min(upper, populated[1]) - max(lower, populated[0])
        return max(overlap.days, 0) * 10

    pieces = bisect_counts(count_for, date(1982, 1, 1), date(2028, 1, 1), 3650, 500)

    assert all(count > 0 for _, _, count in pieces)
    assert pieces[0][1] > date(2000, 1, 1), "the empty early years were not collapsed"


def test_bisect_emits_a_single_day_that_is_still_too_large() -> None:
    """A day is the finest range ENA can express, so this is the floor."""
    busy = date(2020, 6, 30)

    def count_for(lower: date, upper: date) -> int:
        return 9000 if lower <= busy < upper else 0

    pieces = bisect_counts(count_for, date(2020, 1, 1), date(2021, 1, 1), 9000, 100)

    assert pieces == [(busy, busy + timedelta(days=1), 9000)]


def test_bisect_rejects_a_threshold_below_one() -> None:
    with pytest.raises(ValueError, match="threshold"):
        bisect_counts(_uniform(10), date(2020, 1, 1), date(2021, 1, 1), 10, 0)


def test_the_job_key_ignores_counts_but_not_the_query() -> None:
    first = job_key("read_run", "tax_tree(4932)", ["run_accession"], "first_public", 50_000)
    same = job_key("read_run", "tax_tree(4932)", ["run_accession"], "first_public", 50_000)
    other = job_key("read_run", "tax_tree(9606)", ["run_accession"], "first_public", 50_000)

    assert first == same
    assert first != other


def test_a_partition_names_its_own_part_file() -> None:
    dated = Partition(3, "q", 10, date(2020, 1, 1), date(2020, 7, 1))

    assert dated.stem == "part-0003_20200101-20200701"
    assert Partition(0, "q", 10).stem == "part-0000_all"
    assert Partition(7, "q", 10).stem == "part-0007_rest"


def test_a_plan_round_trips_through_its_manifest_form() -> None:
    plan = BulkPlan(
        result="read_run",
        query="tax_tree(4932)",
        fields=("run_accession",),
        partition_field="first_public",
        threshold=100,
        total=250,
        covered=250,
        partitions=(Partition(0, "q0", 120, date(2020, 1, 1), date(2021, 1, 1)),),
    )

    assert BulkPlan.from_dict(plan.to_dict()) == plan


def test_a_small_query_is_not_partitioned(client: PortalClient, ena: FakeENA) -> None:
    plan = client.plan_partitions("read_run", query="tax_tree(4932)", threshold=5000)

    assert plan.total == 900
    assert len(plan.partitions) == 1
    assert plan.partitions[0].query == "tax_tree(4932)"
    assert not plan.resumable


def test_a_large_query_is_split_on_first_public(client: PortalClient, ena: FakeENA) -> None:
    plan = client.plan_partitions("read_run", query="tax_tree(4932)", threshold=100)

    assert plan.partition_field == "first_public"
    assert plan.resumable
    assert plan.total == plan.covered == 900
    assert sum(part.count for part in plan.partitions) == 900
    assert not plan.oversized
    assert [part.index for part in plan.partitions] == list(range(len(plan.partitions)))


def test_the_plan_bounds_run_from_the_epoch_to_past_today(
    client: PortalClient, ena: FakeENA
) -> None:
    """Embargoed records carry a future first_public, so the ceiling leads today.

    The bounds show up in the first counted range rather than in the
    partitions, because empty years are pruned instead of being fetched.
    """
    client.plan_partitions("read_run", threshold=100)

    horizon = date(date.today().year + HORIZON_YEARS, 1, 1)
    assert ena.counted[1] == range_query(None, "first_public", EPOCH, horizon)
    assert horizon > date.today()


def test_rows_the_date_field_cannot_reach_become_a_residual_partition(
    client: PortalClient,
) -> None:
    fake = FakeENA([*_rows(400, date(2015, 1, 1), 7), ("ERR99999", None)])
    with respx.mock:
        respx.get(COUNT_URL).mock(side_effect=fake.count)

        plan = client.plan_partitions("read_run", threshold=100)

    assert plan.total == 401
    assert plan.covered == 401
    residual = plan.partitions[-1]
    assert residual.count == 1
    assert residual.start is None
    assert "NOT (" in (residual.query or "")


def test_a_result_type_with_no_date_field_warns_loudly(client: PortalClient) -> None:
    """taxon is the one result type that cannot be partitioned at all."""
    fake = FakeENA(_rows(900, date(2015, 1, 1), 3))
    with respx.mock:
        respx.get(COUNT_URL).mock(side_effect=fake.count)

        with pytest.warns(UserWarning, match="cannot be resumed"):
            plan = client.plan_partitions("taxon", threshold=100)

    assert plan.partition_field is None
    assert len(plan.partitions) == 1
    assert not plan.resumable


def test_a_result_type_with_only_last_updated_says_so(client: PortalClient) -> None:
    fake = FakeENA(_rows(900, date(2015, 1, 1), 3))
    with respx.mock:
        respx.get(COUNT_URL).mock(side_effect=fake.count)

        with pytest.warns(UserWarning, match="last_updated"):
            plan = client.plan_partitions("assembly", threshold=100)

    assert plan.partition_field == "last_updated"


def test_an_unsplittable_day_is_reported(client: PortalClient) -> None:
    crowded = [(f"ERR{index:05d}", date(2020, 6, 30)) for index in range(500)]
    fake = FakeENA(crowded)
    with respx.mock:
        respx.get(COUNT_URL).mock(side_effect=fake.count)

        with pytest.warns(UserWarning, match="single day cannot be split"):
            plan = client.plan_partitions("read_run", threshold=100)

    assert plan.oversized
    assert plan.oversized[0].count == 500


def test_a_partition_field_that_is_not_a_date_is_refused(client: PortalClient) -> None:
    with pytest.raises(ValueError, match="searchable date field"):
        client.plan_partitions("read_run", partition_field="run_accession")


def test_bulk_search_returns_every_row(client: PortalClient, ena: FakeENA, tmp_path: Path) -> None:
    frame = client.bulk_search(
        "read_run", threshold=100, checkpoint_dir=tmp_path / "job", concurrency=1
    )

    assert frame.height == 900
    assert frame.columns == ["run_accession", "first_public"]
    assert frame["run_accession"].n_unique() == 900
    assert set(frame.schema.values()) == {pl.String}


def test_bulk_search_survives_being_killed_and_resumes(
    client: PortalClient, ena: FakeENA, tmp_path: Path
) -> None:
    """M5's acceptance criterion, and the reason the milestone exists."""
    directory = tmp_path / "job"
    plan = client.plan_partitions("read_run", threshold=100)
    doomed = plan.partitions[len(plan.partitions) // 2].query
    ena.fail_on = lambda query: query == doomed

    with pytest.raises(Exception, match="Could not reach"):
        client.bulk_search("read_run", threshold=100, checkpoint_dir=directory, concurrency=1)

    finished = sorted(path.name for path in directory.glob("*.tsv"))
    assert finished, "the run died without checkpointing anything"
    assert len(finished) < len(plan.partitions)

    ena.fail_on = lambda query: False
    ena.fetched.clear()

    frame = client.bulk_search("read_run", threshold=100, checkpoint_dir=directory, concurrency=1)

    assert frame.height == 900
    assert frame["run_accession"].n_unique() == 900
    refetched = [query for query in ena.fetched if query in _queries_for(directory, finished, plan)]
    assert refetched == [], "the resume refetched partitions that were already on disk"
    assert len(ena.fetched) == len(plan.partitions) - len(finished)


def test_a_resume_reuses_the_stored_plan_without_recounting(
    client: PortalClient, ena: FakeENA, tmp_path: Path
) -> None:
    directory = tmp_path / "job"
    client.bulk_search("read_run", threshold=100, checkpoint_dir=directory, concurrency=1)
    ena.counted.clear()

    client.bulk_search("read_run", threshold=100, checkpoint_dir=directory, concurrency=1)

    assert ena.counted == []


def test_a_checkpoint_from_another_job_is_refused(
    client: PortalClient, ena: FakeENA, tmp_path: Path
) -> None:
    directory = tmp_path / "job"
    client.bulk_search("read_run", query="tax_tree(4932)", threshold=100, checkpoint_dir=directory)

    with pytest.raises(ENACheckpointError, match="different bulk job"):
        client.bulk_search(
            "read_run", query="tax_tree(9606)", threshold=100, checkpoint_dir=directory
        )


def test_resume_false_starts_the_job_over(
    client: PortalClient, ena: FakeENA, tmp_path: Path
) -> None:
    directory = tmp_path / "job"
    client.bulk_search("read_run", threshold=100, checkpoint_dir=directory, concurrency=1)
    ena.fetched.clear()

    client.bulk_search(
        "read_run", threshold=100, checkpoint_dir=directory, concurrency=1, resume=False
    )

    assert len(ena.fetched) > 1


def test_an_explicit_checkpoint_directory_is_kept(
    client: PortalClient, ena: FakeENA, tmp_path: Path
) -> None:
    directory = tmp_path / "job"

    client.bulk_search("read_run", threshold=100, checkpoint_dir=directory, concurrency=1)

    assert (directory / MANIFEST_NAME).exists()
    assert list(directory.glob("*.tsv"))


def test_a_default_checkpoint_directory_is_cleaned_up(
    client: PortalClient, ena: FakeENA, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody asked for these files, so a finished job must not leave stale rows."""
    monkeypatch.setenv("ENAPORTAL_CACHE_DIR", str(tmp_path / "cache"))

    frame = client.bulk_search("read_run", threshold=100, concurrency=1)

    assert frame.height == 900
    assert not list((tmp_path / "cache").rglob("*.tsv"))


def test_on_partition_reports_each_piece_once(
    client: PortalClient, ena: FakeENA, tmp_path: Path
) -> None:
    seen: list[Partition] = []

    client.bulk_search(
        "read_run",
        threshold=100,
        checkpoint_dir=tmp_path / "job",
        concurrency=4,
        on_partition=seen.append,
    )

    assert len(seen) == len({part.index for part in seen})
    assert len(seen) > 1


def test_concurrent_and_serial_fetches_agree(
    client: PortalClient, ena: FakeENA, tmp_path: Path
) -> None:
    serial = client.bulk_search(
        "read_run", threshold=100, checkpoint_dir=tmp_path / "serial", concurrency=1
    )
    parallel = client.bulk_search(
        "read_run", threshold=100, checkpoint_dir=tmp_path / "parallel", concurrency=4
    )

    assert sorted(serial["run_accession"]) == sorted(parallel["run_accession"])


def test_bulk_search_rejects_concurrency_below_one(client: PortalClient) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        client.bulk_search("read_run", concurrency=0)


def test_search_to_file_writes_a_tsv_and_counts_its_rows(
    client: PortalClient, ena: FakeENA, tmp_path: Path
) -> None:
    target = tmp_path / "part.tsv"

    written = client.search_to_file(target, "read_run", query="tax_tree(4932)")

    assert written == 900
    assert target.read_text().splitlines()[0] == "run_accession\tfirst_public"


def _uniform(total: int) -> Callable[[date, date], int]:
    """A count spread evenly over 2000 to 2016, zero outside it."""
    start, end = date(2000, 1, 1), date(2016, 1, 1)
    span = (end - start).days

    def count_for(lower: date, upper: date) -> int:
        overlap = min(upper, end) - max(lower, start)
        return round(total * max(overlap.days, 0) / span)

    return count_for


def _queries_for(directory: Path, names: list[str], plan: BulkPlan) -> set[str]:
    checkpoint = Checkpoint(directory)
    return {
        part.query or "" for part in plan.partitions if checkpoint.path_for(part.stem).name in names
    }


# Offsets in days. The fixed days alongside the whole span make heavy single-day
# clusters, which are the one case bisection has to hand back oversized.
SPAN_DAYS = 3000
ORIGIN = date(2010, 1, 1)
_offsets = st.one_of(st.integers(0, SPAN_DAYS - 1), st.sampled_from([0, 1, 700, SPAN_DAYS - 1]))


@given(offsets=st.lists(_offsets, max_size=400), threshold=st.integers(1, 80))
def test_bisect_accounts_for_every_row_exactly_once(offsets: list[int], threshold: int) -> None:
    days = [ORIGIN + timedelta(days=offset) for offset in offsets]
    end = ORIGIN + timedelta(days=SPAN_DAYS)

    def count_for(lower: date, upper: date) -> int:
        return sum(lower <= day < upper for day in days)

    pieces = bisect_counts(count_for, ORIGIN, end, len(days), threshold)

    assert sum(count for _, _, count in pieces) == len(days)
    for lower, upper, count in pieces:
        assert ORIGIN <= lower < upper <= end
        assert count == count_for(lower, upper) > 0
        assert count <= threshold or upper - lower == timedelta(days=1)
    for (_, first_end, _), (second_start, _, _) in pairwise(pieces):
        assert first_end <= second_start


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    offsets=st.lists(st.one_of(st.none(), st.integers(0, 9000)), max_size=150),
    threshold=st.integers(1, 40),
)
def test_bulk_search_returns_every_row_exactly_once(
    client: PortalClient, offsets: list[int | None], threshold: int
) -> None:
    """The client is safe to share between examples; ENA and the checkpoint are not."""
    rows = [
        (f"ERR{index:05d}", None if offset is None else date(2000, 1, 1) + timedelta(days=offset))
        for index, offset in enumerate(offsets)
    ]
    fake = FakeENA(rows)

    with respx.mock, tempfile.TemporaryDirectory() as directory, warnings.catch_warnings():
        # An unsplittable day warns by design; that is not what this checks.
        warnings.simplefilter("ignore")
        respx.get(COUNT_URL).mock(side_effect=fake.count)
        respx.get(SEARCH_URL).mock(side_effect=fake.search)
        frame = client.bulk_search(
            "read_run", threshold=threshold, checkpoint_dir=directory, concurrency=2
        )

    returned = frame["run_accession"].to_list() if frame.height else []
    assert sorted(returned) == [accession for accession, _ in rows]


_maybe_dates = st.one_of(
    st.none(), st.tuples(st.dates(), st.dates()).map(lambda pair: tuple(sorted(pair)))
)


@given(
    query=st.one_of(st.none(), st.text(max_size=40)),
    fields=st.one_of(st.none(), st.lists(st.text(min_size=1, max_size=12), max_size=4)),
    parts=st.lists(
        st.tuples(st.one_of(st.none(), st.text(max_size=20)), st.integers(0, 10**9), _maybe_dates),
        max_size=6,
    ),
)
def test_a_plan_survives_its_manifest_as_json(
    query: str | None,
    fields: list[str] | None,
    parts: list[tuple[str | None, int, tuple[date, date] | None]],
) -> None:
    plan = BulkPlan(
        result="read_run",
        query=query,
        fields=tuple(fields) if fields is not None else None,
        partition_field="first_public",
        threshold=50_000,
        total=sum(count for _, count, _ in parts),
        covered=sum(count for _, count, _ in parts),
        partitions=tuple(
            Partition(
                index=index,
                query=part_query,
                count=count,
                start=bounds[0] if bounds else None,
                end=bounds[1] if bounds else None,
            )
            for index, (part_query, count, bounds) in enumerate(parts)
        ),
    )

    assert BulkPlan.from_dict(json.loads(json.dumps(plan.to_dict()))) == plan
