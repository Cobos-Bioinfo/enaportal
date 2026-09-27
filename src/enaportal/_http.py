"""HTTP transport for both ENA APIs: timeouts, bounded retries, and body sniffing.

ENA reports some query failures as plain text with HTTP 200, so response bodies
are inspected here rather than trusted by callers.
"""

from __future__ import annotations

import html
import json
import random
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from itertools import chain
from types import TracebackType
from typing import Any, Literal

import httpx

from enaportal._version import __version__
from enaportal.errors import (
    ENAConnectionError,
    ENAHTTPError,
    ENANotFoundError,
    ENAQueryError,
    ENARateLimitError,
    ENATimeoutError,
)

PORTAL_BASE_URL = "https://www.ebi.ac.uk/ena/portal/api/"
BROWSER_BASE_URL = "https://www.ebi.ac.uk/ena/browser/api/"

USER_AGENT = f"enaportal/{__version__} (+https://github.com/Cobos-Bioinfo/enaportal)"

# A 276k-row read_run search is a single 35 second streaming response, so the
# read timeout has to be generous while connect stays short.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)

DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 0.5

# ENA documents 50 requests per second across the discovery and retrieval APIs,
# rejecting the excess with HTTP 429. M5 must keep its thread pool under this.
# https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access.html
RATE_LIMIT_PER_SECOND = 50

# The limit is measured per second, so waiting out a whole one clears it. ENA
# documents no Retry-After, but honour it if it ever appears.
RATE_LIMIT_MIN_BACKOFF = 1.0
MAX_RETRY_AFTER = 60.0

# Half the documented budget. The limit is per source address and this client
# cannot see what else is using it, so spending all of it invites a 429 caused
# by somebody else's traffic.
DEFAULT_RATE_LIMIT = RATE_LIMIT_PER_SECOND / 2

ParamValue = str | int | float | bool | None
Params = Mapping[str, ParamValue]

ResponseShape = Literal["json", "tsv", "text"]

# ENA has no error schema, so plain-text rejections are recognised by how they
# open. Anchored and followed by a separator, which keeps them from matching a
# TSV header of snake_case column ids.
_ERROR_OPENINGS = re.compile(
    r"^(?:"
    r"unsupported|unrecogni[sz]ed|unknown|invalid|illegal|missing|"
    r"cannot|could not|no such|not a valid|"
    r"bad request|error|exception|internal server error"
    r")\b[\s:,]",
    re.IGNORECASE,
)

# The Browser API's text search wraps its HTTP 200 rejections in a bare
# <error> element rather than answering in the format that was asked for.
_ERROR_ELEMENT = re.compile(r"^<error>(.*)</error>$", re.DOTALL)

# The Browser API's error bodies are Spring Boot ErrorDetails serialised to
# match the request: XML on /xml, key=value lines on /embl and /fasta, JSON on
# a batch POST. JSON is parsed; these cover the other two.
_ERROR_MESSAGE = (
    re.compile(r"<message>(.*?)</message>", re.DOTALL),
    re.compile(r"^message=(.*)$", re.MULTILINE),
)


def sniff_text_error(first_line: str, shape: ResponseShape) -> str | None:
    """Return the message if this HTTP 200 body is really an ENA rejection.

    JSON can be checked structurally. For TSV and free text there is nothing to
    check against, so this falls back to the observed shape of ENA's messages
    and is deliberately conservative: a missed rejection surfaces later as a
    parse error, while a false positive would reject valid data.
    """
    stripped = first_line.strip()
    if not stripped:
        return None
    element = _ERROR_ELEMENT.match(stripped)
    if element is not None:
        return html.unescape(element.group(1).strip()) or stripped
    if shape == "json":
        return None if stripped[0] in "[{" else stripped
    return stripped if _ERROR_OPENINGS.match(stripped) else None


class RateLimiter:
    """Spaces request starts so no one second window can exceed a given rate.

    A token bucket sized to the rate would still allow a full bucket followed
    by a refilled one, twice the rate across a window straddling the two, so
    this spaces requests evenly instead. Shared by every thread of one client,
    which is what keeps the bisect loop and the partition fetches inside a
    single budget rather than two.
    """

    def __init__(self, rate: float, *, sleep: Callable[[float], None] = time.sleep) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self._interval = 1.0 / rate
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        """Block until the caller may send, then claim the slot."""
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self._interval
        delay = start - now
        if delay > 0:
            self._sleep(delay)


class ENAHTTPClient:
    """An httpx client configured for ENA.

    Retries connection failures and 5xx with exponential backoff, never retries
    4xx, turns ENA's HTTP 200 text rejections into ENAQueryError, and holds the
    whole client to a request rate ENA will accept.
    """

    def __init__(
        self,
        base_url: str = PORTAL_BASE_URL,
        *,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        user_agent: str = USER_AGENT,
        rate_limit: float | None = DEFAULT_RATE_LIMIT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self.base_url = base_url
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.limiter = RateLimiter(rate_limit, sleep=sleep) if rate_limit else None
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        )

    def __enter__(self) -> ENAHTTPClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    def get_text(
        self, path: str, *, params: Params | None = None, shape: ResponseShape = "text"
    ) -> str:
        """GET a path and return the body, raising if ENA rejected the query."""
        return self.request_text("GET", path, params=params, shape=shape)

    def post_text(self, path: str, *, data: Params, shape: ResponseShape = "text") -> str:
        """POST a form-encoded query and return the body.

        Long queries exceed what ENA accepts in a URL, which is the only reason
        this exists alongside get_text.
        """
        return self.request_text("POST", path, data=data, shape=shape)

    def get_json(self, path: str, *, params: Params | None = None) -> Any:
        """GET a path and parse the body as JSON."""
        request = self._build_request("GET", path, params=params)
        body = self._read(request, shape="json")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ENAQueryError(
                f"ENA returned a body that is not valid JSON: {exc}",
                url=str(request.url),
                body=body[:500],
            ) from exc

    def request_text(
        self,
        method: str,
        path: str,
        *,
        params: Params | None = None,
        data: Params | None = None,
        shape: ResponseShape = "text",
    ) -> str:
        """Send a request and return the whole body as text."""
        return self._read(self._build_request(method, path, params=params, data=data), shape=shape)

    @contextmanager
    def stream_lines(
        self,
        path: str,
        *,
        method: str = "GET",
        params: Params | None = None,
        data: Params | None = None,
        json_body: Any = None,
        shape: ResponseShape = "tsv",
    ) -> Iterator[Iterator[str]]:
        """Stream a response line by line without holding it all in memory.

        The first line is read eagerly so that an HTTP 200 rejection raises here
        rather than reaching a TSV parser as a bogus header.
        """
        request = self._build_request(method, path, params=params, data=data, json_body=json_body)
        response = self._send(request, stream=True)
        try:
            lines = _guarded(response.iter_lines(), request)
            first = next(lines, None)
            if first is None:
                yield iter(())
                return
            message = sniff_text_error(first, shape)
            if message is not None:
                raise ENAQueryError(message, url=str(request.url), body=message)
            yield chain([first], lines)
        finally:
            response.close()

    def _build_request(
        self,
        method: str,
        path: str,
        *,
        params: Params | None = None,
        data: Params | None = None,
        json_body: Any = None,
    ) -> httpx.Request:
        return self._client.build_request(
            method,
            path,
            params=_clean(params),
            data=_clean(data),
            json=json_body,
        )

    def _read(self, request: httpx.Request, *, shape: ResponseShape) -> str:
        response = self._send(request, stream=False)
        body = response.text
        message = sniff_text_error(body.split("\n", 1)[0], shape)
        if message is not None:
            raise ENAQueryError(message, url=str(request.url), body=body[:500])
        return body

    def _send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
        last_error: ENAHTTPError | None = None
        delay = 0.0
        for attempt in range(self.max_retries + 1):
            if attempt:
                self._sleep(delay)
            if self.limiter is not None:
                self.limiter.acquire()
            try:
                response = self._client.send(request, stream=stream)
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
                # A read timeout means ENA accepted the request and is still
                # working. Retrying only adds load, so surface it instead.
                raise ENATimeoutError(f"Request to {request.url} timed out: {exc}") from exc
            except httpx.TransportError as exc:
                last_error = ENAConnectionError(
                    f"Could not reach {request.url}: {exc}", url=str(request.url)
                )
                delay = self._backoff(attempt)
                continue

            if response.status_code < 400:
                return response

            body = _drain(response)
            if response.status_code == 429:
                # The one 4xx worth retrying: ENA is throttling, not refusing.
                last_error = ENARateLimitError(
                    f"ENA rate limit reached for {request.url}. The documented limit is "
                    f"{RATE_LIMIT_PER_SECOND} requests per second.",
                    status_code=429,
                    url=str(request.url),
                    body=body,
                )
                delay = max(self._backoff(attempt), _retry_after(response.headers))
                continue
            if response.status_code >= 500:
                last_error = ENAHTTPError(
                    f"ENA returned HTTP {response.status_code} for {request.url}",
                    status_code=response.status_code,
                    url=str(request.url),
                    body=body,
                )
                delay = self._backoff(attempt)
                continue
            raise _client_error(response, request, body)

        assert last_error is not None
        raise last_error

    def _backoff(self, attempt: int) -> float:
        # Jittered because M5 fetches partitions concurrently and lockstep
        # backoff would re-synchronise every worker onto ENA.
        return self.backoff_factor * (2.0**attempt) * random.uniform(0.5, 1.0)


def _retry_after(headers: httpx.Headers) -> float:
    """Seconds to wait after a 429, from Retry-After if ENA sent one.

    Falls back to a one second floor, and caps whatever is advertised so an
    absurd value cannot stall the caller.
    """
    value = headers.get("Retry-After")
    if value is None:
        return RATE_LIMIT_MIN_BACKOFF
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return RATE_LIMIT_MIN_BACKOFF
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(seconds, RATE_LIMIT_MIN_BACKOFF), MAX_RETRY_AFTER)


def _client_error(
    response: httpx.Response, request: httpx.Request, body: str
) -> ENAQueryError | ENAHTTPError:
    """Pick the exception for a 4xx. ENA uses 400 for a malformed query."""
    detail = _error_message(body)
    if response.status_code in (400, 422):
        return ENAQueryError(
            detail or body.strip() or f"ENA rejected the query with HTTP {response.status_code}",
            url=str(request.url),
            body=body,
        )
    error = ENANotFoundError if response.status_code == 404 else ENAHTTPError
    message = f"ENA returned HTTP {response.status_code} for {request.url}"
    return error(
        f"{message}: {detail}" if detail else message,
        status_code=response.status_code,
        url=str(request.url),
        body=body,
    )


def _error_message(body: str) -> str | None:
    """ENA's own explanation out of a structured error body, if it gave one."""
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str):
            return None
        return message.strip() or None
    for pattern in _ERROR_MESSAGE:
        match = pattern.search(stripped)
        if match is not None and match.group(1).strip():
            return html.unescape(match.group(1).strip())
    return None


def _guarded(lines: Iterator[str], request: httpx.Request) -> Iterator[str]:
    """Raise a failure part way through a streamed body as an ENAError.

    ENA can abort a response after sending some of it, and httpx only notices
    while the caller is iterating, long after _send has returned. It is not
    retried: the lines already read have been handed on.
    """
    read = 0
    try:
        for line in lines:
            yield line
            read += 1
    except httpx.TimeoutException as exc:
        raise ENATimeoutError(
            f"Response from {request.url} stalled after {read} lines: {exc}"
        ) from exc
    except httpx.TransportError as exc:
        raise ENAConnectionError(
            f"ENA cut the response from {request.url} short after {read} lines: {exc}",
            url=str(request.url),
        ) from exc


def _drain(response: httpx.Response) -> str:
    """Read and close a failed response so the connection can be reused."""
    try:
        response.read()
        return response.text[:500]
    finally:
        response.close()


def _clean(params: Params | None) -> dict[str, str] | None:
    """Drop None values and stringify the rest, so callers can pass optionals."""
    if params is None:
        return None
    return {k: str(v) for k, v in params.items() if v is not None}
