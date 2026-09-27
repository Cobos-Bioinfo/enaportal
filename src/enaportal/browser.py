"""The ENA Browser API: records by accession as XML, EMBL or FASTA, and text search.

Kept apart from the Portal client because it answers a different question. The
Portal returns tables of fields; the Browser returns the records themselves.
"""

from __future__ import annotations

import io
import os
import re
import warnings
from collections.abc import Iterable, Iterator, Sequence
from contextlib import ExitStack
from itertools import chain
from pathlib import Path
from types import TracebackType
from typing import Any, Literal
from xml.etree.ElementTree import Element, ParseError, XMLPullParser

import polars as pl

from enaportal._checkpoint import _reserve
from enaportal._http import BROWSER_BASE_URL, DEFAULT_TIMEOUT, ENAHTTPClient
from enaportal._tsv import read_ena_tsv
from enaportal.errors import ENANotFoundError, ENAQueryError

RecordFormat = Literal["xml", "embl", "fasta"]

RECORD_FORMATS: tuple[RecordFormat, ...] = ("xml", "embl", "fasta")

# ENA's documented ceiling for one request to the batch endpoints.
MAX_ACCESSIONS_PER_REQUEST = 10_000

# With no limit ENA streams every hit, and a broad query matches over a million
# records, so the default is a page rather than the lot.
DEFAULT_TEXTSEARCH_LIMIT = 100

# Counted where a record starts rather than where it ends, so a record cut
# short by line_limit still counts.
_RECORD_START = {"embl": "ID   ", "fasta": ">"}

_XML_ROOT_OPEN = re.compile(r"^\s*<[A-Za-z_][\w.-]*>\s*$")
_XML_ROOT_CLOSE = re.compile(r"^\s*</[A-Za-z_][\w.-]*>\s*$")

_UNMERGEABLE = (
    "ENA returned XML laid out in a way enaportal cannot join across batches of "
    f"{MAX_ACCESSIONS_PER_REQUEST} accessions. Fetch fewer accessions per call."
)


class BrowserClient:
    """A configured client for the Browser API.

    Holds its own connection pool, so reuse one instance for repeated work.
    """

    def __init__(self, *, http: ENAHTTPClient | None = None, timeout: float | None = None) -> None:
        self._http = (
            http
            if http is not None
            else ENAHTTPClient(
                BROWSER_BASE_URL, timeout=timeout if timeout is not None else DEFAULT_TIMEOUT
            )
        )
        self._owns_http = http is None

    def __enter__(self) -> BrowserClient:
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

    def fetch(
        self,
        accession: str | Sequence[str],
        *,
        format: RecordFormat = "xml",
        annotation_only: bool = False,
        line_limit: int | None = None,
    ) -> str:
        """Records for one or more accessions, as XML, EMBL or FASTA text.

        XML covers studies, samples, experiments, runs, analyses and taxa. EMBL
        and FASTA cover sequences, and FASTA whole assemblies too, which run to
        gigabytes; use fetch_to_file for those.

        All accessions in one call must be of the same data type, which ENA
        enforces. ENA leaves out accessions it cannot find without saying so,
        so fewer records than accessions raises a warning, and none at all
        raises ENANotFoundError.
        """
        accessions = _distinct(accession)
        if not accessions:
            return ""
        options = _options(format, annotation_only=annotation_only, line_limit=line_limit)
        counter = _RecordCounter(format)
        lines: list[str] = []
        for line in self._lines(accessions, format, options):
            counter.feed(line)
            lines.append(line)
        _warn_if_short(len(accessions), counter)
        return "\n".join(lines) + "\n" if lines else ""

    def fetch_to_file(
        self,
        path: Path | str,
        accession: str | Sequence[str],
        *,
        format: RecordFormat = "xml",
        annotation_only: bool = False,
        line_limit: int | None = None,
    ) -> int:
        """Stream records straight to a file, returning how many were written.

        Memory stays flat whatever the size, and the file only appears once it
        is complete, so a failure part way never leaves a truncated record that
        looks whole. Otherwise behaves exactly like fetch.
        """
        accessions = _distinct(accession)
        options = _options(format, annotation_only=annotation_only, line_limit=line_limit)
        counter = _RecordCounter(format)
        target = Path(path)
        temporary = _reserve(target.parent, target.name)
        try:
            with temporary.open("wb") as handle:
                for line in self._lines(accessions, format, options) if accessions else ():
                    counter.feed(line)
                    handle.write(line.encode("utf-8"))
                    handle.write(b"\n")
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        _warn_if_short(len(accessions), counter)
        return counter.records

    def textsearch(
        self,
        query: str,
        *,
        result: str,
        limit: int | None = DEFAULT_TEXTSEARCH_LIMIT,
        offset: int | None = None,
    ) -> pl.DataFrame:
        """Free-text search, returning matching accessions and their descriptions.

        This is what the Portal cannot do, since its queries need a field for
        every term. result takes a Portal result type such as read_study or
        sequence. Pass the accessions on to fetch or filereport for more.

        Unlike the Portal, limit=0 here returns nothing rather than everything,
        so it is rejected. Pass None for every hit, bearing in mind that ENA
        has been seen to cut a very long text search response short.
        """
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1, or None for every hit")
        if offset is not None and offset < 0:
            raise ValueError("offset must not be negative")
        params = {"query": query, "result": result, "limit": limit, "offset": offset}
        buffer = io.BytesIO()
        with self._http.stream_lines("tsv/textsearch", params=params, shape="tsv") as lines:
            for line in lines:
                buffer.write(line.encode("utf-8"))
                buffer.write(b"\n")
        if not buffer.getbuffer().nbytes:
            return pl.DataFrame()
        buffer.seek(0)
        return read_ena_tsv(buffer, quoted=True)

    def textsearch_count(self, query: str, *, result: str) -> int:
        """How many records a text search matches, without fetching any."""
        payload = self._http.get_json(
            "tsv/textsearch/count", params={"query": query, "result": result}
        )
        try:
            return int(payload["count"])
        except (KeyError, TypeError, ValueError):
            raise ENAQueryError(f"ENA returned an unexpected count: {payload!r}") from None

    def _lines(
        self, accessions: list[str], format: RecordFormat, options: dict[str, Any]
    ) -> Iterator[str]:
        chunks = self._chunks(accessions, format, options)
        if format == "xml" and len(accessions) > MAX_ACCESSIONS_PER_REQUEST:
            return _merge_xml(chunks)
        return chain.from_iterable(chunks)

    def _chunks(
        self, accessions: list[str], format: RecordFormat, options: dict[str, Any]
    ) -> Iterator[Iterator[str]]:
        missing: ENANotFoundError | None = None
        answered = False
        for start in range(0, len(accessions), MAX_ACCESSIONS_PER_REQUEST):
            batch = accessions[start : start + MAX_ACCESSIONS_PER_REQUEST]
            body = {"accessions": batch, **options}
            with ExitStack() as stack:
                try:
                    lines = stack.enter_context(
                        self._http.stream_lines(format, method="POST", json_body=body, shape="text")
                    )
                except ENANotFoundError as exc:
                    # ENA answers a batch it found nothing of with a 404, and one
                    # it found anything of with a 200 that silently omits the rest.
                    missing = exc
                    continue
                answered = True
                yield lines
        if not answered:
            assert missing is not None
            what = accessions[0] if len(accessions) == 1 else f"any of {len(accessions)} accessions"
            raise ENANotFoundError(
                f"ENA has no {format} record for {what}",
                status_code=404,
                url=missing.url,
                body=missing.body,
            ) from missing


class _RecordCounter:
    """Counts records as their lines stream past, to notice any ENA dropped.

    A safety net rather than a validator: XML it cannot follow is passed on
    unchanged and simply stops being counted.
    """

    def __init__(self, format: RecordFormat) -> None:
        self.records = 0
        self.countable = True
        self._start = _RECORD_START.get(format)
        self._parser: XMLPullParser[Element] | None = (
            XMLPullParser(events=("start", "end")) if format == "xml" else None
        )
        self._depth = 0

    def feed(self, line: str) -> None:
        """Take the next line of the response."""
        if not self.countable:
            return
        if self._parser is None:
            if self._start is not None and line.startswith(self._start):
                self.records += 1
            return
        try:
            self._parser.feed(line + "\n")
            events = list(self._parser.read_events())
        except ParseError:
            self.countable = False
            return
        for event in events:
            if event[0] == "start":
                self._depth += 1
                if self._depth == 2:
                    self.records += 1
                continue
            self._depth -= 1
            element = event[-1]
            # Records are only counted, so each is dropped once it closes
            # rather than holding a whole batch's tree in memory.
            if self._depth == 1 and isinstance(element, Element):
                element.clear()


def _merge_xml(chunks: Iterable[Iterator[str]]) -> Iterator[str]:
    """Join the XML of several batches into one document.

    Each batch is its own <X_SET> document, and concatenating them would give
    several roots, which no parser accepts. So the first batch's root is kept
    and every later batch contributes only the records inside its own.
    """
    closing: str | None = None
    for lines in chunks:
        prologue = _xml_prologue(lines)
        if prologue is None:
            continue
        if closing is None:
            yield from prologue
        held: str | None = None
        for line in lines:
            if not line.strip():
                continue
            if held is not None:
                yield held
            held = line
        if held is None or not _XML_ROOT_CLOSE.match(held):
            raise ENAQueryError(_UNMERGEABLE)
        closing = held
    if closing is not None:
        yield closing


def _xml_prologue(lines: Iterator[str]) -> list[str] | None:
    """Read one batch up to its root start tag, or None if the batch is empty."""
    prologue: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        prologue.append(line)
        if _XML_ROOT_OPEN.match(line):
            return prologue
        if len(prologue) > 1 or not line.lstrip().startswith("<?xml"):
            raise ENAQueryError(_UNMERGEABLE)
    if prologue:
        raise ENAQueryError(_UNMERGEABLE)
    return None


def _warn_if_short(requested: int, counter: _RecordCounter) -> None:
    if counter.countable and counter.records < requested:
        warnings.warn(
            f"Only {counter.records} of {requested} accessions came back from ENA, which "
            "leaves out accessions it cannot find without saying which.",
            stacklevel=3,
        )


def _options(
    format: RecordFormat, *, annotation_only: bool, line_limit: int | None
) -> dict[str, Any]:
    if format not in RECORD_FORMATS:
        raise ValueError(f"format must be one of {', '.join(RECORD_FORMATS)}, not {format!r}")
    if annotation_only and format != "embl":
        raise ValueError("annotation_only applies to EMBL records only")
    if line_limit is not None:
        if format == "xml":
            raise ValueError("line_limit applies to EMBL and FASTA records only")
        if line_limit < 1:
            raise ValueError("line_limit must be at least 1, or None for whole records")
    options: dict[str, Any] = {}
    if annotation_only:
        options["annotationOnly"] = True
    if line_limit is not None:
        options["lineLimit"] = line_limit
    return options


def _distinct(accession: str | Sequence[str]) -> list[str]:
    # ENA returns a record once per mention, so a repeat would both duplicate
    # the record and mask a missing one from the count.
    accessions = [accession] if isinstance(accession, str) else list(accession)
    return list(dict.fromkeys(one.strip() for one in accessions if one.strip()))


__all__ = [
    "DEFAULT_TEXTSEARCH_LIMIT",
    "MAX_ACCESSIONS_PER_REQUEST",
    "RECORD_FORMATS",
    "BrowserClient",
    "RecordFormat",
]
