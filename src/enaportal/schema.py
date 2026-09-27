"""Introspection of ENA's result types and fields.

The schema is read from ENA at runtime and cached on disk. The snapshot shipped
inside the package is a last-resort offline fallback, never the source of truth:
freezing this metadata is how the predecessor library died.
"""

from __future__ import annotations

import difflib
import json
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Final

from enaportal._cache import JSONCache, default_cache_dir
from enaportal._http import ENAHTTPClient
from enaportal.errors import ENAError, ENAQueryError, ENASchemaError

DEFAULT_TTL_SECONDS: Final = 7 * 24 * 60 * 60

RESULTS_KEY: Final = "results"

# ENA omits "type" for some fields and puts the type word in "description"
# instead. Recovering it matters: read_run hides a date field that way, and M5
# chooses partition keys by type.
_TYPE_WORDS: Final = frozenset(
    {"text", "number", "date", "boolean", "latlon", "list", "taxonomy", "controlled value"}
)


@dataclass(frozen=True, slots=True)
class Result:
    """One ENA result type, as listed by /results."""

    result_id: str
    description: str
    primary_accession_type: str | None = None
    record_count: int | None = None
    last_updated: str | None = None

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> Result:
        """Build a Result from one /results entry."""
        return cls(
            result_id=str(raw["resultId"]),
            description=str(raw.get("description", "")),
            primary_accession_type=_optional_str(raw.get("primaryAccessionType")),
            record_count=_optional_int(raw.get("recordCount")),
            last_updated=_optional_str(raw.get("lastUpdated")),
        )


@dataclass(frozen=True, slots=True)
class Field:
    """One field of a result type, from /returnFields or /searchFields."""

    column_id: str
    description: str
    type: str | None = None

    @property
    def is_date(self) -> bool:
        """Whether the field can be used as a date range, which bulk retrieval partitions on."""
        return self.type == "date"

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> Field:
        """Build a Field from one /returnFields or /searchFields entry."""
        column_id = str(raw["columnId"])
        description = str(raw.get("description", "")).strip()
        declared = raw.get("type")
        if declared is None and description.lower() in _TYPE_WORDS:
            return cls(column_id, "", description.lower())
        return cls(column_id, description, _optional_str(declared))


class SchemaClient:
    """Reads ENA's schema, caching it on disk and degrading rather than failing.

    Lookups fall back in order: memory, fresh cache, ENA, stale cache, the
    packaged snapshot. Each fallback past ENA emits a warning.
    """

    def __init__(
        self,
        *,
        http: ENAHTTPClient | None = None,
        cache_dir: Path | str | None = None,
        ttl: float = DEFAULT_TTL_SECONDS,
        offline: bool = False,
    ) -> None:
        self._http = http if http is not None else ENAHTTPClient()
        self._owns_http = http is None
        directory = Path(cache_dir).expanduser() if cache_dir is not None else default_cache_dir()
        self.cache = JSONCache(directory / "schema", ttl)
        self.offline = offline
        self._memo: dict[str, list[dict[str, Any]]] = {}

    def close(self) -> None:
        """Close the HTTP client, if this instance created it."""
        if self._owns_http:
            self._http.close()

    def results(self) -> list[Result]:
        """Every result type ENA exposes."""
        return [Result.from_payload(row) for row in self._load(RESULTS_KEY, "results", {})]

    def result_ids(self) -> list[str]:
        """The result type identifiers, in the order ENA lists them."""
        return [result.result_id for result in self.results()]

    def return_fields(self, result: str) -> list[Field]:
        """The fields a result type can return."""
        self.validate_result(result)
        payload = self._load(f"return_fields.{result}", "returnFields", {"result": result})
        return [Field.from_payload(row) for row in payload]

    def search_fields(self, result: str) -> list[Field]:
        """The fields a result type can be queried on."""
        self.validate_result(result)
        payload = self._load(f"search_fields.{result}", "searchFields", {"result": result})
        return [Field.from_payload(row) for row in payload]

    def date_search_fields(self, result: str) -> list[Field]:
        """Searchable date fields, the candidates for a bulk partition key."""
        return [field for field in self.search_fields(result) if field.is_date]

    def link_fields(self, result: str) -> list[Field]:
        """The fields that point at another ENA object.

        Portal rows are denormalised, so these are what makes navigation a
        query rather than a request to an endpoint that does not exist.
        """
        return [
            field
            for field in self.return_fields(result)
            if field.column_id.endswith("_accession") or field.column_id == "tax_id"
        ]

    def refresh(self, result: str | None = None) -> None:
        """Discard cached schema and read it from ENA again.

        With no argument this drops everything, including the result list.
        """
        if result is None:
            self._memo.clear()
            self.cache.clear()
            return
        for key in (f"return_fields.{result}", f"search_fields.{result}"):
            self._memo.pop(key, None)
            self.cache.clear(key)

    def validate_result(self, result: str) -> None:
        """Raise ENAQueryError if this is not one of ENA's result types."""
        known = self.result_ids()
        if result not in known:
            raise ENAQueryError(_unknown_message("result type", result, known))

    def validate_return_fields(self, result: str, fields: Iterable[str]) -> None:
        """Raise ENAQueryError if any field cannot be returned by this result type."""
        self._validate_fields(result, fields, self.return_fields(result), "return field")

    def validate_search_fields(self, result: str, fields: Iterable[str]) -> None:
        """Raise ENAQueryError if any field cannot be queried on this result type."""
        self._validate_fields(result, fields, self.search_fields(result), "search field")

    def _validate_fields(
        self, result: str, requested: Iterable[str], known: Sequence[Field], label: str
    ) -> None:
        names = [field.column_id for field in known]
        allowed = set(names)
        unknown = [field for field in requested if field not in allowed]
        if unknown:
            raise ENAQueryError(
                "; ".join(
                    _unknown_message(f"{label} for {result}", name, names) for name in unknown
                )
            )

    def _load(self, key: str, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        memoised = self._memo.get(key)
        if memoised is not None:
            return memoised
        cached = self.cache.read(key)
        payload = cached if cached is not None else self._fetch(key, path, params)
        rows = _as_rows(key, payload)
        self._memo[key] = rows
        return rows

    def _fetch(self, key: str, path: str, params: dict[str, str]) -> Any:
        error: ENAError | None = None
        if not self.offline:
            try:
                payload = self._http.get_json(path, params={**params, "format": "json"})
            except ENAError as exc:
                error = exc
            else:
                self.cache.write(key, payload)
                return payload

        stale = self.cache.read(key, allow_stale=True)
        if stale is not None:
            warnings.warn(
                f"Using expired cached schema for {key!r}: {_reason(error, self.offline)}",
                stacklevel=4,
            )
            return stale

        snapshot = read_snapshot(key)
        if snapshot is not None:
            warnings.warn(
                f"Using the packaged schema snapshot for {key!r}, which may be out of date: "
                f"{_reason(error, self.offline)}",
                stacklevel=4,
            )
            return snapshot

        raise ENASchemaError(
            f"Could not read ENA's schema for {key!r} from the network, the cache "
            f"or the packaged snapshot: {_reason(error, self.offline)}"
        ) from error


def read_snapshot(key: str) -> Any | None:
    """Load a schema payload from the snapshot shipped with the package."""
    resource = resources.files("enaportal").joinpath("_snapshot").joinpath(f"{key}.json")
    try:
        return json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _as_rows(key: str, payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ENASchemaError(
            f"ENA returned an unexpected shape for {key!r}: {type(payload).__name__}"
        )
    return payload


def _reason(error: ENAError | None, offline: bool) -> str:
    if offline:
        return "the client is in offline mode"
    return str(error) if error is not None else "no live response"


def _unknown_message(label: str, value: str, known: Sequence[str]) -> str:
    message = f"Unknown {label}: {value!r}"
    close = difflib.get_close_matches(value, known, n=3)
    if close:
        return f"{message}. Did you mean {', '.join(repr(name) for name in close)}?"
    return message


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "Field",
    "Result",
    "SchemaClient",
    "read_snapshot",
]
