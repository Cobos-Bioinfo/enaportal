"""The exception hierarchy. Everything this library raises derives from ENAError."""

from __future__ import annotations


class ENAError(Exception):
    """Base class for every error raised by enaportal."""


class ENAHTTPError(ENAError):
    """A request failed with a non-success HTTP status, or the connection never
    produced one."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.body = body


class ENAConnectionError(ENAHTTPError):
    """The request never reached ENA, or the connection dropped, after retries."""


class ENARateLimitError(ENAHTTPError):
    """ENA returned HTTP 429 and the request still failed after backing off."""


class ENAQueryError(ENAError):
    """ENA rejected the query.

    Raised both for HTTP 400 and for the plain-text rejections ENA serves with
    HTTP 200, so callers have one type to catch for "the query was wrong".
    """

    def __init__(self, message: str, *, url: str | None = None, body: str = "") -> None:
        super().__init__(message)
        self.url = url
        self.body = body


class ENATimeoutError(ENAError):
    """A request exceeded its timeout."""


class ENASchemaError(ENAError):
    """ENA's schema could not be read from the network, the cache or the snapshot."""


class ENACheckpointError(ENAError):
    """A bulk checkpoint directory does not belong to the job being resumed."""


__all__ = [
    "ENACheckpointError",
    "ENAConnectionError",
    "ENAError",
    "ENAHTTPError",
    "ENAQueryError",
    "ENARateLimitError",
    "ENASchemaError",
    "ENATimeoutError",
]
