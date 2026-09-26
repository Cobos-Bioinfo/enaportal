"""Module-level helpers over one lazily created PortalClient.

Convenient for a script or a REPL. Anything doing repeated work should hold its
own PortalClient so the connection pool and the schema cache are reused.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import polars as pl

from enaportal.bulk import DEFAULT_CONCURRENCY, DEFAULT_THRESHOLD, BulkPlan, Partition
from enaportal.portal import Format, PortalClient
from enaportal.schema import Field, Result

_lock = threading.Lock()
_client: PortalClient | None = None


def default_client() -> PortalClient:
    """The shared client used by the module-level helpers."""
    global _client
    with _lock:
        if _client is None:
            _client = PortalClient()
        return _client


def search(
    result: str,
    *,
    query: str | None = None,
    fields: Sequence[str] | None = None,
    limit: int | None = None,
    data_portal: str | None = None,
    include_metagenomes: bool | None = None,
    format: Format = "tsv",
    validate: bool = True,
) -> pl.DataFrame:
    """Run a Portal query and return the rows as a Polars DataFrame."""
    return default_client().search(
        result,
        query=query,
        fields=fields,
        limit=limit,
        data_portal=data_portal,
        include_metagenomes=include_metagenomes,
        format=format,
        validate=validate,
    )


def count(
    result: str,
    *,
    query: str | None = None,
    data_portal: str | None = None,
    include_metagenomes: bool | None = None,
    validate: bool = True,
) -> int:
    """How many records match a Portal query."""
    return default_client().count(
        result,
        query=query,
        data_portal=data_portal,
        include_metagenomes=include_metagenomes,
        validate=validate,
    )


def bulk_search(
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
    """Fetch a whole result set in resumable, checkpointed pieces."""
    return default_client().bulk_search(
        result,
        query=query,
        fields=fields,
        threshold=threshold,
        partition_field=partition_field,
        checkpoint_dir=checkpoint_dir,
        concurrency=concurrency,
        resume=resume,
        validate=validate,
        on_partition=on_partition,
    )


def plan_partitions(
    result: str,
    *,
    query: str | None = None,
    fields: Sequence[str] | None = None,
    threshold: int = DEFAULT_THRESHOLD,
    partition_field: str | None = None,
    validate: bool = True,
) -> BulkPlan:
    """How bulk_search would split a query, without fetching any rows."""
    return default_client().plan_partitions(
        result,
        query=query,
        fields=fields,
        threshold=threshold,
        partition_field=partition_field,
        validate=validate,
    )


def filereport(
    accession: str | Sequence[str],
    *,
    result: str = "read_run",
    fields: Sequence[str] | None = None,
    limit: int | None = None,
    format: Format = "tsv",
    validate: bool = True,
) -> pl.DataFrame:
    """Everything ENA holds for an accession, including its file locations."""
    return default_client().filereport(
        accession,
        result=result,
        fields=fields,
        limit=limit,
        format=format,
        validate=validate,
    )


def related(
    accession: str,
    *,
    to: str = "read_run",
    fields: Sequence[str] | None = None,
    limit: int | None = None,
    validate: bool = True,
) -> pl.DataFrame:
    """Navigate from one accession to the objects related to it."""
    return default_client().related(accession, to=to, fields=fields, limit=limit, validate=validate)


def results() -> list[Result]:
    """Every result type ENA exposes."""
    return default_client().results()


def return_fields(result: str) -> list[Field]:
    """The fields a result type can return."""
    return default_client().return_fields(result)


def search_fields(result: str) -> list[Field]:
    """The fields a result type can be queried on."""
    return default_client().search_fields(result)


def refresh_schema(result: str | None = None) -> None:
    """Drop the cached schema so the next lookup reads it from ENA again."""
    default_client().schema.refresh(result)


__all__ = [
    "bulk_search",
    "count",
    "default_client",
    "filereport",
    "plan_partitions",
    "refresh_schema",
    "related",
    "results",
    "return_fields",
    "search",
    "search_fields",
]
