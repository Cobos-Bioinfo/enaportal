"""Resumable bulk retrieval, by splitting a query into counted date ranges.

The Portal API has no cursor. `offset` and `sortFields` are both rejected and
the row order is not stable, so a large result set can be neither paged nor
resumed by anything ENA offers: it is one long response that is lost if it
breaks. This module partitions the query itself instead, counts each range
before fetching it, and checkpoints every part to disk.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import polars as pl

from enaportal._cache import default_cache_dir
from enaportal._checkpoint import Checkpoint
from enaportal._query import extract_field_names
from enaportal._tsv import read_ena_tsv
from enaportal.errors import ENACheckpointError

if TYPE_CHECKING:
    from enaportal.portal import PortalClient

DEFAULT_THRESHOLD: Final = 50_000

DEFAULT_CONCURRENCY: Final = 4

# EMBL's data library began in 1982, so no ENA record was released before it.
EPOCH: Final = date(1982, 1, 1)

# first_public carries a future release date for records under embargo, so the
# upper bound has to sit ahead of today rather than on it.
HORIZON_YEARS: Final = 2

# first_public and first_created never move. last_updated does, so a row can
# change partition between a killed run and its resume, which would drop it or
# fetch it twice. Hence last, and only for result types with nothing better.
PARTITION_FIELD_PREFERENCE: Final = ("first_public", "first_created", "last_updated")

MANIFEST_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class Partition:
    """One counted slice of a query, and the part file it checkpoints to."""

    index: int
    query: str | None
    count: int
    start: date | None = None
    end: date | None = None

    @property
    def stem(self) -> str:
        """The part file name, stable across runs so a resume finds it."""
        if self.start is None or self.end is None:
            return f"part-{self.index:04d}_{'rest' if self.index else 'all'}"
        return f"part-{self.index:04d}_{self.start:%Y%m%d}-{self.end:%Y%m%d}"

    def to_dict(self) -> dict[str, Any]:
        """The JSON form stored in the checkpoint manifest."""
        return {
            "index": self.index,
            "query": self.query,
            "count": self.count,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Partition:
        """Rebuild a partition from a stored manifest."""
        return cls(
            index=int(raw["index"]),
            query=raw["query"],
            count=int(raw["count"]),
            start=date.fromisoformat(raw["start"]) if raw.get("start") else None,
            end=date.fromisoformat(raw["end"]) if raw.get("end") else None,
        )


@dataclass(frozen=True, slots=True)
class BulkPlan:
    """How a query will be split, worked out before a single row is fetched.

    `total` is what the query matches. `covered` is what the partitions
    between them reach, and the two differ only if ENA holds matching rows the
    partition field cannot address.
    """

    result: str
    query: str | None
    fields: tuple[str, ...] | None
    partition_field: str | None
    threshold: int
    total: int
    covered: int
    partitions: tuple[Partition, ...]

    @property
    def resumable(self) -> bool:
        """Whether a killed run can pick up where it stopped."""
        return len(self.partitions) > 1

    @property
    def oversized(self) -> tuple[Partition, ...]:
        """Partitions too big for the threshold, which could not be split further."""
        return tuple(part for part in self.partitions if part.count > self.threshold)

    def job_key(self) -> str:
        """A digest of what this job is, ignoring counts."""
        return job_key(self.result, self.query, self.fields, self.partition_field, self.threshold)

    def to_dict(self) -> dict[str, Any]:
        """The JSON form stored in the checkpoint manifest."""
        return {
            "version": MANIFEST_VERSION,
            "job_key": self.job_key(),
            "result": self.result,
            "query": self.query,
            "fields": list(self.fields) if self.fields is not None else None,
            "partition_field": self.partition_field,
            "threshold": self.threshold,
            "total": self.total,
            "covered": self.covered,
            "partitions": [part.to_dict() for part in self.partitions],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BulkPlan:
        """Rebuild a plan from a stored manifest."""
        fields = raw.get("fields")
        return cls(
            result=str(raw["result"]),
            query=raw["query"],
            fields=tuple(fields) if fields is not None else None,
            partition_field=raw["partition_field"],
            threshold=int(raw["threshold"]),
            total=int(raw["total"]),
            covered=int(raw["covered"]),
            partitions=tuple(Partition.from_dict(part) for part in raw["partitions"]),
        )


def job_key(
    result: str,
    query: str | None,
    fields: Sequence[str] | None,
    partition_field: str | None,
    threshold: int,
) -> str:
    """A stable digest identifying one bulk job.

    Counts are deliberately excluded. They move as ENA grows, and a resume has
    to recognise yesterday's job even though re-counting it today would split
    it differently.
    """
    payload = json.dumps(
        {
            "result": result,
            "query": query,
            "fields": list(fields) if fields is not None else None,
            "partition_field": partition_field,
            "threshold": threshold,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def range_query(query: str | None, field: str, start: date, end: date) -> str:
    """A query restricted to one half-open range of a date field.

    ENA reads what looks like a closed range as half-open: `f>=A AND f<=B`
    selects `A <= f < B`. Adjacent partitions therefore share a boundary and
    tile the range exactly, with no day arithmetic between them.

    The caller's query is parenthesised because ENA binds AND tighter than OR,
    so appending a range to `a OR b` would otherwise restrict only `b` and
    return orders of magnitude too many rows.
    """
    clause = f"{field}>={start.isoformat()} AND {field}<={end.isoformat()}"
    return f"({query}) AND {clause}" if query else clause


def outside_query(query: str | None, field: str, start: date, end: date) -> str:
    """A query for the rows a date range misses, such as those with no date.

    ENA's NOT is an exact set complement, so this is the residual of
    `range_query` over the same bounds.
    """
    clause = f"NOT ({field}>={start.isoformat()} AND {field}<={end.isoformat()})"
    return f"({query}) AND {clause}" if query else clause


def bisect_counts(
    count_for: Callable[[date, date], int],
    start: date,
    end: date,
    total: int,
    threshold: int,
) -> list[tuple[date, date, int]]:
    """Split [start, end) until every piece holds at most `threshold` rows.

    Only one child of each split is counted: half-open halves tile the parent
    exactly, so the other child is the difference. That halves the number of
    requests, which matters because a wide split reaches hundreds of pieces.

    A single day that is still too large cannot be divided any further and is
    returned as it is.
    """
    if threshold < 1:
        raise ValueError("threshold must be at least 1")

    pieces: list[tuple[date, date, int]] = []
    stack = [(start, end, total)]
    while stack:
        lower, upper, count = stack.pop()
        if count <= 0:
            continue
        span = (upper - lower).days
        if count <= threshold or span <= 1:
            pieces.append((lower, upper, count))
            continue
        middle = lower + timedelta(days=span // 2)
        left = count_for(lower, middle)
        stack.append((middle, upper, max(count - left, 0)))
        stack.append((lower, middle, left))
    pieces.sort(key=lambda piece: piece[0])
    return pieces


def plan_partitions(
    client: PortalClient,
    result: str,
    *,
    query: str | None = None,
    fields: Sequence[str] | None = None,
    threshold: int = DEFAULT_THRESHOLD,
    partition_field: str | None = None,
    validate: bool = True,
) -> BulkPlan:
    """Work out how a query will be split, without fetching any rows.

    Costs roughly one /count per partition, which is cheap enough to run on
    its own to see what a long job is about to do.
    """
    if threshold < 1:
        raise ValueError("threshold must be at least 1")
    if validate:
        client.schema.validate_result(result)
        if fields:
            client.schema.validate_return_fields(result, fields)

    chosen = resolve_partition_field(client, result, partition_field)
    selected = tuple(fields) if fields is not None else None
    total = client.count(result, query=query, validate=validate)

    def build(covered: int, partitions: Sequence[Partition]) -> BulkPlan:
        plan = BulkPlan(
            result=result,
            query=query,
            fields=selected,
            partition_field=chosen,
            threshold=threshold,
            total=total,
            covered=covered,
            partitions=tuple(partitions),
        )
        _warn_about(plan)
        return plan

    if chosen is None or total <= threshold:
        return build(total, [Partition(index=0, query=query, count=total)])

    start, end = EPOCH, date(date.today().year + HORIZON_YEARS, 1, 1)

    def count_for(lower: date, upper: date) -> int:
        return client.count(result, query=range_query(query, chosen, lower, upper), validate=False)

    inside = count_for(start, end)
    partitions = [
        Partition(
            index=index,
            query=range_query(query, chosen, lower, upper),
            count=count,
            start=lower,
            end=upper,
        )
        for index, (lower, upper, count) in enumerate(
            bisect_counts(count_for, start, end, inside, threshold)
        )
    ]

    covered = inside
    if inside < total:
        residual = outside_query(query, chosen, start, end)
        rest = client.count(result, query=residual, validate=False)
        if rest > 0:
            partitions.append(Partition(index=len(partitions), query=residual, count=rest))
            covered += rest

    return build(covered, partitions)


def bulk_search(
    client: PortalClient,
    result: str,
    *,
    query: str | None = None,
    fields: Sequence[str] | None = None,
    threshold: int = DEFAULT_THRESHOLD,
    partition_field: str | None = None,
    checkpoint_dir: Path | str | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    resume: bool = True,
    validate: bool = True,
    on_partition: Callable[[Partition], None] | None = None,
) -> pl.DataFrame:
    """Fetch a whole result set in resumable, checkpointed pieces.

    Each partition is written to its own file as it completes, so a run that
    is killed loses only the partitions in flight. Running it again re-reads
    the stored plan and fetches whatever is missing.

    With no `checkpoint_dir` the parts go to a directory under the platform
    cache keyed by the job, and are removed once the whole job succeeds, so a
    later call fetches current rows rather than replaying old ones. Pass a
    directory to keep both the parts and the finished job, and `resume=False`
    to start one over.

    `on_partition` is called on the calling thread as each partition lands,
    which is enough to drive a progress bar.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if validate:
        client.schema.validate_result(result)
        if fields:
            client.schema.validate_return_fields(result, fields)
        if query:
            client.schema.validate_search_fields(result, extract_field_names(query))

    chosen = resolve_partition_field(client, result, partition_field)
    key = job_key(result, query, fields, chosen, threshold)
    ephemeral = checkpoint_dir is None
    checkpoint = Checkpoint(_default_dir(key) if checkpoint_dir is None else checkpoint_dir)
    if not resume:
        checkpoint.clear()

    plan = _restore(checkpoint, key)
    if plan is None:
        plan = plan_partitions(
            client,
            result,
            query=query,
            fields=fields,
            threshold=threshold,
            partition_field=chosen,
            validate=False,
        )
        checkpoint.write_manifest(plan.to_dict())

    _fetch(client, plan, checkpoint, concurrency, on_partition)
    frame = _assemble(checkpoint, plan)
    if ephemeral:
        checkpoint.discard()
    return frame


def resolve_partition_field(client: PortalClient, result: str, requested: str | None) -> str | None:
    """The date field to bisect on, or None if this result type has none."""
    dates = {field.column_id for field in client.schema.date_search_fields(result)}
    if requested is not None:
        if requested not in dates:
            raise ValueError(
                f"{requested!r} is not a searchable date field on {result}. Partitioning "
                f"bisects a date range, so the key has to be one of {sorted(dates)}."
                if dates
                else f"{result} has no searchable date field, so it cannot be partitioned."
            )
        return requested
    for candidate in PARTITION_FIELD_PREFERENCE:
        if candidate in dates:
            return candidate
    return min(dates, default=None)


def _restore(checkpoint: Checkpoint, key: str) -> BulkPlan | None:
    stored = checkpoint.read_manifest()
    if stored is None:
        return None
    if stored.get("job_key") != key or stored.get("version") != MANIFEST_VERSION:
        raise ENACheckpointError(
            f"{checkpoint.directory} already holds a different bulk job "
            f"({stored.get('result')!r}, query {stored.get('query')!r}). Point "
            "checkpoint_dir somewhere else, or pass resume=False to overwrite it."
        )
    return BulkPlan.from_dict(stored)


def _fetch(
    client: PortalClient,
    plan: BulkPlan,
    checkpoint: Checkpoint,
    concurrency: int,
    on_partition: Callable[[Partition], None] | None,
) -> None:
    pending = [part for part in plan.partitions if not checkpoint.is_done(part.stem)]
    if not pending:
        return

    def run(part: Partition) -> None:
        checkpoint.write_part(
            part.stem,
            lambda path: client.search_to_file(
                path, plan.result, query=part.query, fields=plan.fields, validate=False
            ),
        )

    if concurrency == 1 or len(pending) == 1:
        for part in pending:
            run(part)
            if on_partition is not None:
                on_partition(part)
        return

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures: dict[Future[None], Partition] = {pool.submit(run, part): part for part in pending}
        try:
            for future in as_completed(futures):
                future.result()
                if on_partition is not None:
                    on_partition(futures[future])
        except BaseException:
            # Completed parts are already on disk, so abandoning the rest costs
            # only the partitions in flight when the next run resumes.
            for future in futures:
                future.cancel()
            raise


def _assemble(checkpoint: Checkpoint, plan: BulkPlan) -> pl.DataFrame:
    frames = []
    for part in plan.partitions:
        path = checkpoint.path_for(part.stem)
        if not path.exists() or path.stat().st_size == 0:
            continue
        frame = read_ena_tsv(path)
        if frame.height:
            frames.append(frame)
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def _warn_about(plan: BulkPlan) -> None:
    if plan.partition_field is None and plan.total > plan.threshold:
        warnings.warn(
            f"{plan.result} has no searchable date field, so this fetch of {plan.total} rows "
            "cannot be partitioned and cannot be resumed. It will be one request, and "
            "killing it loses all of it.",
            stacklevel=4,
        )
    if plan.partition_field == "last_updated":
        warnings.warn(
            f"Partitioning {plan.result} on 'last_updated', the only searchable date field "
            "it has. That value moves, so a row edited between a killed run and its resume "
            "can land in a partition already fetched, or in none at all.",
            stacklevel=4,
        )
    if plan.covered < plan.total:
        warnings.warn(
            f"{plan.total - plan.covered} of {plan.total} matching rows are out of reach of "
            f"{plan.partition_field!r} and will be missing from the result.",
            stacklevel=4,
        )
    oversized = plan.oversized
    if oversized and plan.partition_field is not None:
        biggest = max(part.count for part in oversized)
        warnings.warn(
            f"{len(oversized)} of {len(plan.partitions)} partitions are over the "
            f"{plan.threshold} row threshold, the largest holding {biggest}, because a "
            "single day cannot be split further. Each is one unresumable request.",
            stacklevel=4,
        )


def _default_dir(key: str) -> Path:
    return default_cache_dir() / "bulk" / key[:16]


__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_THRESHOLD",
    "EPOCH",
    "HORIZON_YEARS",
    "PARTITION_FIELD_PREFERENCE",
    "BulkPlan",
    "Partition",
    "bisect_counts",
    "bulk_search",
    "job_key",
    "outside_query",
    "plan_partitions",
    "range_query",
    "resolve_partition_field",
]
