"""The ENA Portal API: search and count.

Queries are validated against the introspected schema before they are sent, so
a mistyped field fails locally with a useful message instead of coming back as
an ENA error page.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import polars as pl

from enaportal._http import DEFAULT_TIMEOUT, PORTAL_BASE_URL, ENAHTTPClient, Params, ResponseShape
from enaportal._query import extract_field_names
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
        return _read_ena_tsv(buffer)

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


def _read_ena_tsv(source: io.BytesIO) -> pl.DataFrame:
    # ENA does not quote its TSV, and free-text fields such as study_title
    # contain bare double quotes, so quote parsing has to be off entirely.
    return pl.read_csv(
        source,
        separator="\t",
        has_header=True,
        quote_char=None,
        infer_schema_length=0,
        truncate_ragged_lines=True,
    )


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
