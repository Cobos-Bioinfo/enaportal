"""Unit tests for the HTTP layer. Every response is mocked; nothing hits ENA."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import httpx
import pytest
import respx

from enaportal._http import (
    MAX_RETRY_AFTER,
    PORTAL_BASE_URL,
    RATE_LIMIT_MIN_BACKOFF,
    ENAHTTPClient,
    RateLimiter,
    ResponseShape,
    sniff_text_error,
)
from enaportal.errors import (
    ENAConnectionError,
    ENAHTTPError,
    ENANotFoundError,
    ENAQueryError,
    ENARateLimitError,
    ENATimeoutError,
)

SEARCH_URL = f"{PORTAL_BASE_URL}search"
COUNT_URL = f"{PORTAL_BASE_URL}count"


@pytest.fixture
def slept() -> list[float]:
    return []


def _unthrottled(*, max_retries: int, sleep: Callable[[float], None]) -> ENAHTTPClient:
    """A client whose only sleeps are backoff, so `slept` measures just that."""
    return ENAHTTPClient(max_retries=max_retries, backoff_factor=0.0, rate_limit=None, sleep=sleep)


@pytest.fixture
def client(slept: list[float]) -> Iterator[ENAHTTPClient]:
    """Throttling off, so `slept` records backoff delays and nothing else."""
    with ENAHTTPClient(backoff_factor=0.0, rate_limit=None, sleep=slept.append) as client:
        yield client


@respx.mock
def test_get_text_returns_body(client: ENAHTTPClient) -> None:
    route = respx.get(COUNT_URL).mock(return_value=httpx.Response(200, text="276447"))

    assert client.get_text("count", params={"result": "read_run"}) == "276447"
    assert route.call_count == 1


@respx.mock
def test_user_agent_identifies_the_library(client: ENAHTTPClient) -> None:
    route = respx.get(COUNT_URL).mock(return_value=httpx.Response(200, text="1"))

    client.get_text("count")

    user_agent = route.calls[0].request.headers["user-agent"]
    assert user_agent.startswith("enaportal/")


@respx.mock
def test_none_params_are_dropped(client: ENAHTTPClient) -> None:
    route = respx.get(COUNT_URL).mock(return_value=httpx.Response(200, text="1"))

    client.get_text("count", params={"result": "read_run", "query": None, "limit": 0})

    assert dict(route.calls[0].request.url.params) == {"result": "read_run", "limit": "0"}


@respx.mock
def test_retries_5xx_then_succeeds(client: ENAHTTPClient, slept: list[float]) -> None:
    route = respx.get(COUNT_URL).mock(
        side_effect=[
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(500, text="oops"),
            httpx.Response(200, text="42"),
        ]
    )

    assert client.get_text("count") == "42"
    assert route.call_count == 3
    assert len(slept) == 2


@respx.mock
def test_gives_up_after_max_retries(slept: list[float]) -> None:
    route = respx.get(COUNT_URL).mock(return_value=httpx.Response(502, text="bad gateway"))

    with (
        _unthrottled(max_retries=2, sleep=slept.append) as client,
        pytest.raises(ENAHTTPError) as caught,
    ):
        client.get_text("count")

    assert route.call_count == 3
    assert caught.value.status_code == 502


@respx.mock
def test_does_not_retry_4xx(client: ENAHTTPClient) -> None:
    route = respx.get(COUNT_URL).mock(return_value=httpx.Response(404, text="not found"))

    with pytest.raises(ENAHTTPError) as caught:
        client.get_text("count")

    assert route.call_count == 1
    assert caught.value.status_code == 404


@respx.mock
def test_retries_429_then_succeeds(client: ENAHTTPClient, slept: list[float]) -> None:
    route = respx.get(COUNT_URL).mock(
        side_effect=[
            httpx.Response(429, text="Too Many Requests"),
            httpx.Response(200, text="42"),
        ]
    )

    assert client.get_text("count") == "42"
    assert route.call_count == 2


@respx.mock
def test_429_waits_at_least_the_rate_limit_window(
    client: ENAHTTPClient, slept: list[float]
) -> None:
    respx.get(COUNT_URL).mock(
        side_effect=[httpx.Response(429, text="Too Many Requests"), httpx.Response(200, text="1")]
    )

    client.get_text("count")

    assert slept == [RATE_LIMIT_MIN_BACKOFF]


@respx.mock
def test_429_honours_a_numeric_retry_after(client: ENAHTTPClient, slept: list[float]) -> None:
    respx.get(COUNT_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(200, text="1"),
        ]
    )

    client.get_text("count")

    assert slept == [5.0]


@respx.mock
def test_429_caps_an_absurd_retry_after(client: ENAHTTPClient, slept: list[float]) -> None:
    respx.get(COUNT_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "99999"}),
            httpx.Response(200, text="1"),
        ]
    )

    client.get_text("count")

    assert slept == [MAX_RETRY_AFTER]


@respx.mock
def test_429_ignores_an_unparseable_retry_after(client: ENAHTTPClient, slept: list[float]) -> None:
    respx.get(COUNT_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "soon"}),
            httpx.Response(200, text="1"),
        ]
    )

    client.get_text("count")

    assert slept == [RATE_LIMIT_MIN_BACKOFF]


@respx.mock
def test_persistent_429_raises_a_rate_limit_error(slept: list[float]) -> None:
    route = respx.get(COUNT_URL).mock(return_value=httpx.Response(429, text="Too Many Requests"))

    with (
        _unthrottled(max_retries=2, sleep=slept.append) as client,
        pytest.raises(ENARateLimitError) as caught,
    ):
        client.get_text("count")

    assert route.call_count == 3
    assert caught.value.status_code == 429
    assert "50 requests per second" in str(caught.value)


@respx.mock
def test_a_rate_limit_error_is_still_an_http_error(slept: list[float]) -> None:
    respx.get(COUNT_URL).mock(return_value=httpx.Response(429))

    with (
        _unthrottled(max_retries=0, sleep=slept.append) as client,
        pytest.raises(ENAHTTPError),
    ):
        client.get_text("count")


@respx.mock
def test_http_400_becomes_a_query_error(client: ENAHTTPClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(400, text="sortFields is not supported"))

    with pytest.raises(ENAQueryError, match="sortFields"):
        client.get_text("search")


@respx.mock
def test_http_404_is_a_not_found_error(client: ENAHTTPClient) -> None:
    body = '{"timestamp": 1, "status": 404, "error": "Not Found", "path": "/ena/portal/api/x"}'
    respx.get(COUNT_URL).mock(return_value=httpx.Response(404, text=body))

    with pytest.raises(ENANotFoundError) as caught:
        client.get_text("count")

    assert caught.value.status_code == 404
    assert isinstance(caught.value, ENAHTTPError)


@respx.mock
def test_http_404_carries_enas_message_when_it_sent_one(client: ENAHTTPClient) -> None:
    body = '{"status": 404, "message": "Failed to get response from SRA API. Response code 404"}'
    respx.get(COUNT_URL).mock(return_value=httpx.Response(404, text=body))

    with pytest.raises(ENANotFoundError, match="Failed to get response from SRA API"):
        client.get_text("count")


# The Browser API serialises one error to match the format that was asked for.
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            '<?xml version="1.0" encoding="UTF-8"?>\n<ErrorDetails>\n  <status>400</status>\n'
            "  <message>Invalid result type &apos;nope&apos;.</message>\n</ErrorDetails>\n",
            "Invalid result type 'nope'.",
        ),
        (
            "timestamp=1\nstatus=400\nerror=Bad Request\n"
            "message=Format embl is not available for record PRJEB1787\npath=/x\n",
            "Format embl is not available for record PRJEB1787",
        ),
        (
            '{"status": 400, "error": "Bad Request", "message": "All accessions must match"}',
            "All accessions must match",
        ),
    ],
)
@respx.mock
def test_http_400_reports_enas_own_message(client: ENAHTTPClient, body: str, message: str) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(400, text=body))

    with pytest.raises(ENAQueryError) as caught:
        client.get_text("search")

    assert str(caught.value) == message
    assert caught.value.body == body


@respx.mock
def test_retries_connection_errors(client: ENAHTTPClient, slept: list[float]) -> None:
    route = respx.get(COUNT_URL).mock(
        side_effect=[
            httpx.ConnectError("no route to host"),
            httpx.Response(200, text="7"),
        ]
    )

    assert client.get_text("count") == "7"
    assert route.call_count == 2
    assert len(slept) == 1


@respx.mock
def test_connection_error_after_retries(slept: list[float]) -> None:
    respx.get(COUNT_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    with (
        _unthrottled(max_retries=1, sleep=slept.append) as client,
        pytest.raises(ENAConnectionError),
    ):
        client.get_text("count")


@respx.mock
def test_read_timeout_is_not_retried(client: ENAHTTPClient) -> None:
    route = respx.get(SEARCH_URL).mock(side_effect=httpx.ReadTimeout("too slow"))

    with pytest.raises(ENATimeoutError):
        client.get_text("search")

    assert route.call_count == 1


@respx.mock
def test_http_200_text_error_raises_query_error(client: ENAHTTPClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text="Unsupported param offset"))

    with pytest.raises(ENAQueryError, match="Unsupported param offset"):
        client.get_text("search", shape="tsv")


@respx.mock
def test_http_200_text_error_is_caught_while_streaming(client: ENAHTTPClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text="Unsupported param offset"))

    with (
        pytest.raises(ENAQueryError, match="Unsupported param offset"),
        client.stream_lines("search") as lines,
    ):
        list(lines)


@respx.mock
def test_stream_lines_yields_every_line(client: ENAHTTPClient) -> None:
    body = "run_accession\tfastq_ftp\nERR1\tftp/1.fastq.gz\nERR2\tftp/2.fastq.gz\n"
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=body))

    with client.stream_lines("search") as lines:
        rows = list(lines)

    assert rows == ["run_accession\tfastq_ftp", "ERR1\tftp/1.fastq.gz", "ERR2\tftp/2.fastq.gz"]


@respx.mock
def test_stream_lines_handles_an_empty_body(client: ENAHTTPClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=""))

    with client.stream_lines("search") as lines:
        assert list(lines) == []


@respx.mock
def test_get_json_parses_the_body(client: ENAHTTPClient) -> None:
    respx.get(f"{PORTAL_BASE_URL}results").mock(
        return_value=httpx.Response(200, json=[{"resultId": "read_run"}])
    )

    assert client.get_json("results") == [{"resultId": "read_run"}]


@respx.mock
def test_get_json_rejects_a_text_error_body(client: ENAHTTPClient) -> None:
    respx.get(f"{PORTAL_BASE_URL}returnFields").mock(
        return_value=httpx.Response(200, text="Invalid result type")
    )

    with pytest.raises(ENAQueryError, match="Invalid result type"):
        client.get_json("returnFields", params={"result": "nope"})


@respx.mock
def test_post_sends_a_form_body(client: ENAHTTPClient) -> None:
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text="run_accession\n"))

    client.post_text("search", data={"result": "read_run", "limit": 0}, shape="tsv")

    assert route.calls[0].request.content == b"result=read_run&limit=0"


class _CutShort(httpx.SyncByteStream):
    """A body that stops part way, the way ENA aborts a long text search."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __iter__(self) -> Iterator[bytes]:
        yield b"accession\tdescription\nERR1\tone\n"
        raise self.error


@respx.mock
def test_a_body_cut_short_raises_a_connection_error(client: ENAHTTPClient) -> None:
    error = httpx.RemoteProtocolError("peer closed connection without sending complete body")
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, stream=_CutShort(error)))

    with (
        pytest.raises(ENAConnectionError, match="short after 2 lines"),
        client.stream_lines("search") as lines,
    ):
        list(lines)


@respx.mock
def test_a_body_that_stalls_raises_a_timeout(client: ENAHTTPClient) -> None:
    error = httpx.ReadTimeout("timed out")
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, stream=_CutShort(error)))

    with (
        pytest.raises(ENATimeoutError, match="stalled after 2 lines"),
        client.stream_lines("search") as lines,
    ):
        list(lines)


def test_negative_retries_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        ENAHTTPClient(max_retries=-1)


@pytest.mark.parametrize(
    "body",
    [
        "Unsupported param offset",
        "Invalid result type: nope",
        "unknown field: run_acession",
        "Cannot parse query",
        "Error: something went wrong",
    ],
)
def test_sniffer_flags_ena_rejections(body: str) -> None:
    assert sniff_text_error(body, "tsv") == body


@pytest.mark.parametrize(
    "body",
    [
        "run_accession\tfastq_ftp",
        "276447",
        "",
        "invalid_reason\tstatus",
        ">ENA|A00145|A00145.1 description",
        "ID   A00145; SV 1; linear; DNA;",
        "<RUN_SET>",
        '<?xml version="1.0" encoding="UTF-8"?>',
    ],
)
def test_sniffer_passes_real_payloads(body: str) -> None:
    assert sniff_text_error(body, "tsv") is None


@pytest.mark.parametrize("shape", ["tsv", "text", "json"])
def test_sniffer_unwraps_the_text_search_error_element(shape: ResponseShape) -> None:
    body = "<error>Invalid result type &apos;nonsense&apos;.</error>"

    assert sniff_text_error(body, shape) == "Invalid result type 'nonsense'."


def test_the_limiter_does_not_delay_a_lone_request() -> None:
    slept: list[float] = []
    limiter = RateLimiter(10.0, sleep=slept.append)

    limiter.acquire()

    assert slept == []


def test_the_limiter_claims_one_slot_per_interval() -> None:
    """The sleep here is a no-op, so each call asks to wait one interval more."""
    slept: list[float] = []
    limiter = RateLimiter(50.0, sleep=slept.append)

    for _ in range(4):
        limiter.acquire()

    assert len(slept) == 3
    assert slept == sorted(slept)
    assert slept[-1] == pytest.approx(3 * 0.02, abs=0.005)


def test_the_limiter_holds_real_time_to_the_rate() -> None:
    """A token bucket would allow a full bucket plus a refill; spacing does not."""
    limiter = RateLimiter(200.0)

    started = time.monotonic()
    for _ in range(10):
        limiter.acquire()
    elapsed = time.monotonic() - started

    assert elapsed >= 9 / 200.0


def test_the_limiter_rejects_a_rate_of_zero() -> None:
    with pytest.raises(ValueError, match="rate must be positive"):
        RateLimiter(0.0)


@respx.mock
def test_every_request_passes_through_the_limiter() -> None:
    """Counting during a bisect and fetching partitions share one budget."""
    slept: list[float] = []
    respx.get(COUNT_URL).mock(return_value=httpx.Response(200, text="1"))

    with ENAHTTPClient(rate_limit=50.0, sleep=slept.append) as client:
        for _ in range(3):
            client.get_text("count")

    assert len(slept) == 2


@respx.mock
def test_a_retry_is_throttled_like_any_other_request() -> None:
    slept: list[float] = []
    respx.get(COUNT_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, text="1")],
    )

    with ENAHTTPClient(max_retries=1, backoff_factor=0.0, rate_limit=50.0, sleep=slept.append) as (
        client
    ):
        assert client.get_text("count") == "1"

    # The backoff of 0.0, then the limiter holding the retry off the wire.
    assert len(slept) == 2


def test_rate_limiting_can_be_turned_off() -> None:
    with ENAHTTPClient(rate_limit=None) as client:
        assert client.limiter is None
