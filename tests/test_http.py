"""Unit tests for the HTTP layer. Every response is mocked; nothing hits ENA."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from enaportal._http import (
    MAX_RETRY_AFTER,
    PORTAL_BASE_URL,
    RATE_LIMIT_MIN_BACKOFF,
    ENAHTTPClient,
    sniff_text_error,
)
from enaportal.errors import (
    ENAConnectionError,
    ENAHTTPError,
    ENAQueryError,
    ENARateLimitError,
    ENATimeoutError,
)

SEARCH_URL = f"{PORTAL_BASE_URL}search"
COUNT_URL = f"{PORTAL_BASE_URL}count"


@pytest.fixture
def slept() -> list[float]:
    return []


@pytest.fixture
def client(slept: list[float]) -> Iterator[ENAHTTPClient]:
    with ENAHTTPClient(backoff_factor=0.0, sleep=slept.append) as client:
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
        ENAHTTPClient(max_retries=2, backoff_factor=0.0, sleep=slept.append) as client,
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
        ENAHTTPClient(max_retries=2, backoff_factor=0.0, sleep=slept.append) as client,
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
        ENAHTTPClient(max_retries=0, sleep=slept.append) as client,
        pytest.raises(ENAHTTPError),
    ):
        client.get_text("count")


@respx.mock
def test_http_400_becomes_a_query_error(client: ENAHTTPClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(400, text="sortFields is not supported"))

    with pytest.raises(ENAQueryError, match="sortFields"):
        client.get_text("search")


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
        ENAHTTPClient(max_retries=1, backoff_factor=0.0, sleep=slept.append) as client,
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
    ],
)
def test_sniffer_passes_real_payloads(body: str) -> None:
    assert sniff_text_error(body, "tsv") is None
