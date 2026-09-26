"""The ENA Portal API: search and count.

Queries are validated against the introspected schema before they are sent, so
a mistyped field fails locally with a useful message instead of coming back as
an ENA error page.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import polars as pl

from enaportal._http import DEFAULT_TIMEOUT, PORTAL_BASE_URL, ENAHTTPClient, Params, ResponseShape
from enaportal._query import extract_field_names
from enaportal._tsv import read_ena_tsv
from enaportal.bulk import DEFAULT_CONCURRENCY, DEFAULT_THRESHOLD, BulkPlan, Partition
from enaportal.bulk import bulk_search as _bulk_search
from enaportal.bulk import plan_partitions as _plan_partitions
from enaportal.errors import ENAQueryError
from enaportal.schema import DEFAULT_TTL_SECONDS, Field, Result, SchemaClient

Format = Literal["tsv", "json"]

# Tomcat rejects very long request lines, and a query naming thousands of
# accessions passes that easily, so long requests go out as a POST form.
MAX_GET_LENGTH = 1500


class PortalClient:
    """A configured client for the Portal API.

    Holds the HTTP connection pool and the schema cache, so reuse one instance
    rather than calling the module-level helpers in a loop.
    """

    def __init__(
        self,
        *,
        http: ENAHTTPClient | None = None,
        schema: SchemaClient | None = None,
        timeout: float | None = None,
        cache_dir: Path | str | None = None,
        ttl: float = DEFAULT_TTL_SECONDS,
        offline: bool = False,
    ) -> None:
        self._http = (
            http
            if http is not None
            else ENAHTTPClient(
                PORTAL_BASE_URL, timeout=timeout if timeout is not None else DEFAULT_TIMEOUT
            )
        )
        self._owns_http = http is None
        self.schema = (
            schema
            if schema is not None
            else SchemaClient(http=self._http, cache_dir=cache_dir, ttl=ttl, offline=offline)
        )

    def __enter__(self) -> PortalClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP client, if this instance created it."""
        if self._owns_http:
            self._http.close()

    def results(self) -> list[Result]:
        """Every result type ENA exposes."""
        return self.schema.results()

    def return_fields(self, result: str) -> list[Field]:
        """The fields a result type can return."""
        return self.schema.return_fields(result)

    def search_fields(self, result: str) -> list[Field]:
        """The fields a result type can be queried on."""
        return self.schema.search_fields(result)

    def count(
        self,
        result: str,
        *,
        query: str | None = None,
        data_portal: str | None = None,
        include_metagenomes: bool | None = None,
        validate: bool = True,
    ) -> int:
        """How many records match, without fetching any of them.

        Cheap enough to call freely, which is what makes M5's partitioning work.
        """
        params = self._params(
            result,
            query=query,
            data_portal=data_portal,
            include_metagenomes=include_metagenomes,
            validate=validate,
        )
        return _parse_count(self._request("count", params, shape="text"))

    def search(
        self,
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
        """Run a query and return the rows as a Polars DataFrame.

        Every column comes back as a string. ENA packs multiple values into one
        cell with semicolons, so inferring types would give the same field a
        different dtype from one query to the next; cast explicitly instead.

        ENA applies no default limit: omitting it returns the whole result set,
        exactly like limit=0. For a large query that is one long download with
        no way to resume it, so pass an explicit limit unless you do want all
        of it.
        """
        params = self._params(
            result,
            query=query,
            data_portal=data_portal,
            include_metagenomes=include_metagenomes,
            validate=validate,
        )
        if fields:
            if validate:
                self.schema.validate_return_fields(result, fields)
            params["fields"] = ",".join(fields)
        if limit is not None:
            params["limit"] = limit
        params["format"] = format

        if format == "json":
            return _frame_from_records(self._request_json("search", params))
        return self._read_tsv("search", params)

    def search_to_file(
        self,
        path: Path | str,
        result: str,
        *,
        query: str | None = None,
        fields: Sequence[str] | None = None,
        limit: int | None = None,
        data_portal: str | None = None,
        include_metagenomes: bool | None = None,
        validate: bool = True,
    ) -> int:
        """Stream a query straight to a TSV file, returning the rows written.

        Memory stays flat however large the result set is, which is what makes
        it the right shape for a bulk checkpoint. TSV only: it is the format
        ENA streams, and the one that can be concatenated afterwards.
        """
        params = self._params(
            result,
            query=query,
            data_portal=data_portal,
            include_metagenomes=include_metagenomes,
            validate=validate,
        )
        if fields:
            if validate:
                self.schema.validate_return_fields(result, fields)
            params["fields"] = ",".join(fields)
        if limit is not None:
            params["limit"] = limit
        params["format"] = "tsv"

        written = 0
        with Path(path).open("wb") as handle:
            for line in self._stream("search", params):
                handle.write(line.encode("utf-8"))
                handle.write(b"\n")
                written += 1
        return max(written - 1, 0)

    def bulk_search(
        self,
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

        ENA has no cursor, so this partitions the query by date range instead,
        counts each range before fetching it and writes every part to disk. A
        run that is killed resumes without refetching what already landed.
        """
        return _bulk_search(
            self,
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
        self,
        result: str,
        *,
        query: str | None = None,
        fields: Sequence[str] | None = None,
        threshold: int = DEFAULT_THRESHOLD,
        partition_field: str | None = None,
        validate: bool = True,
    ) -> BulkPlan:
        """How bulk_search would split this query, without fetching any rows."""
        return _plan_partitions(
            self,
            result,
            query=query,
            fields=fields,
            threshold=threshold,
            partition_field=partition_field,
            validate=validate,
        )

    def filereport(
        self,
        accession: str | Sequence[str],
        *,
        result: str = "read_run",
        fields: Sequence[str] | None = None,
        limit: int | None = None,
        format: Format = "tsv",
        validate: bool = True,
    ) -> pl.DataFrame:
        """Everything ENA holds for an accession, including its file locations.

        The accession may name any level ENA can map to the result type: a
        study, experiment, sample or run accession all work for read_run, in
        either the primary or the secondary form.

        ENA accepts exactly one accession per request and answers a
        comma-separated list with zero rows rather than an error, so a sequence
        is sent as one request each and the frames are stacked.
        """
        accessions = [accession] if isinstance(accession, str) else list(accession)
        if not accessions:
            return pl.DataFrame()
        if validate:
            self.schema.validate_result(result)
            if fields:
                self.schema.validate_return_fields(result, fields)

        frames = [self._one_filereport(one, result, fields, limit, format) for one in accessions]
        populated = [frame for frame in frames if frame.width]
        if not populated:
            return pl.DataFrame()
        return pl.concat(populated, how="diagonal_relaxed")

    def related(
        self,
        accession: str,
        *,
        to: str = "read_run",
        fields: Sequence[str] | None = None,
        limit: int | None = None,
        validate: bool = True,
    ) -> pl.DataFrame:
        """Navigate from one accession to the objects related to it.

        There is no links endpoint on the Portal API. There does not need to be:
        rows are denormalised, so one filereport call on a study accession
        already returns its runs carrying their experiment and sample. With no
        fields given this returns just the accession columns, which is the
        navigation table rather than all 195 of them.
        """
        chosen = list(fields) if fields else [f.column_id for f in self.schema.link_fields(to)]
        return self.filereport(accession, result=to, fields=chosen, limit=limit, validate=validate)

    def _one_filereport(
        self,
        accession: str,
        result: str,
        fields: Sequence[str] | None,
        limit: int | None,
        format: Format,
    ) -> pl.DataFrame:
        params: dict[str, Any] = {"accession": accession, "result": result, "format": format}
        if fields:
            params["fields"] = ",".join(fields)
        if limit is not None:
            params["limit"] = limit
        if format == "json":
            return _frame_from_records(self._request_json("filereport", params))
        return self._read_tsv("filereport", params)

    def _params(
        self,
        result: str,
        *,
        query: str | None,
        data_portal: str | None,
        include_metagenomes: bool | None,
        validate: bool,
    ) -> dict[str, Any]:
        if validate:
            self.schema.validate_result(result)
            if query:
                self.schema.validate_search_fields(result, extract_field_names(query))
        params: dict[str, Any] = {"result": result}
        if query:
            params["query"] = query
        if data_portal is not None:
            params["dataPortal"] = data_portal
        if include_metagenomes is not None:
            params["includeMetagenomes"] = "true" if include_metagenomes else "false"
        return params

    def _read_tsv(self, path: str, params: Params) -> pl.DataFrame:
        buffer = io.BytesIO()
        for line in self._stream(path, params):
            buffer.write(line.encode("utf-8"))
            buffer.write(b"\n")
        if not buffer.getbuffer().nbytes:
            return pl.DataFrame()
        buffer.seek(0)
        return read_ena_tsv(buffer)

    def _stream(self, path: str, params: Params) -> Iterator[str]:
        if _needs_post(params):
            with self._http.stream_lines(path, method="POST", data=params, shape="tsv") as lines:
                yield from lines
        else:
            with self._http.stream_lines(path, params=params, shape="tsv") as lines:
                yield from lines

    def _request(self, path: str, params: Params, *, shape: ResponseShape) -> str:
        if _needs_post(params):
            return self._http.request_text("POST", path, data=params, shape=shape)
        return self._http.request_text("GET", path, params=params, shape=shape)

    def _request_json(self, path: str, params: Params) -> Any:
        if _needs_post(params):
            return _parse_json(self._http.request_text("POST", path, data=params, shape="json"))
        return self._http.get_json(path, params=params)


def _parse_count(body: str) -> int:
    """Read the number out of a /count response.

    ENA serves it as a one-column TSV with a "count" header rather than a bare
    number, so the header has to be skipped.
    """
    lines = [line for line in body.splitlines() if line.strip()]
    if lines and lines[0].strip().lower() == "count":
        lines = lines[1:]
    try:
        return int(lines[0].strip())
    except (IndexError, ValueError):
        raise ENAQueryError(f"ENA returned a non-numeric count: {body[:200]!r}") from None


def _needs_post(params: Params) -> bool:
    """Whether the encoded parameters are too long to send in a request line."""
    encoded = sum(len(str(key)) + len(str(value)) + 2 for key, value in params.items())
    return encoded > MAX_GET_LENGTH


def _frame_from_records(records: Any) -> pl.DataFrame:
    if not isinstance(records, list) or not records:
        return pl.DataFrame()
    columns = list(records[0])
    return pl.DataFrame(
        {name: [_as_text(row.get(name)) for row in records] for name in columns},
        schema={name: pl.String for name in columns},
    )


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def _parse_json(body: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ENAQueryError(f"ENA returned a body that is not valid JSON: {exc}") from exc


__all__ = ["MAX_GET_LENGTH", "Format", "PortalClient"]
