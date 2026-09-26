"""Module-level helpers over one lazily created PortalClient.

Convenient for a script or a REPL. Anything doing repeated work should hold its
own PortalClient so the connection pool and the schema cache are reused.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

import polars as pl

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
    "count",
    "default_client",
    "refresh_schema",
    "results",
    "return_fields",
    "search",
    "search_fields",
]
