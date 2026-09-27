"""The module-level helpers must stay exact mirrors of the client methods they wrap.

Each helper restates its method's signature by hand, which is where drift would
creep in: a new parameter added to a method and forgotten in its helper, or a
default changed in one place only.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest

from enaportal import _api
from enaportal.browser import BrowserClient
from enaportal.portal import PortalClient

MIRRORS: dict[str, Callable[..., Any]] = {
    "search": PortalClient.search,
    "count": PortalClient.count,
    "bulk_search": PortalClient.bulk_search,
    "plan_partitions": PortalClient.plan_partitions,
    "filereport": PortalClient.filereport,
    "related": PortalClient.related,
    "results": PortalClient.results,
    "return_fields": PortalClient.return_fields,
    "search_fields": PortalClient.search_fields,
    "fetch": BrowserClient.fetch,
    "textsearch": BrowserClient.textsearch,
    "textsearch_count": BrowserClient.textsearch_count,
}


class Recorder:
    """Stands in for a client and remembers the one call made on it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Callable[..., None]:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


def _parameters(function: Callable[..., Any]) -> list[tuple[str, Any, Any]]:
    signature = inspect.signature(function)
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in signature.parameters.values()
        if parameter.name != "self"
    ]


def test_every_helper_is_checked() -> None:
    covered = {*MIRRORS, "default_client", "refresh_schema"}

    assert set(_api.__all__) == covered


@pytest.mark.parametrize("name", sorted(MIRRORS))
def test_a_helper_takes_exactly_what_its_method_takes(name: str) -> None:
    assert _parameters(getattr(_api, name)) == _parameters(MIRRORS[name])


@pytest.mark.parametrize("name", sorted(MIRRORS))
def test_a_helper_passes_every_argument_through_unchanged(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Recorder()
    monkeypatch.setattr(_api, "_client", client)
    monkeypatch.setattr(_api, "_browser", client)
    sentinels = {parameter: object() for parameter, _, _ in _parameters(MIRRORS[name])}

    getattr(_api, name)(**sentinels)

    ((called, args, kwargs),) = client.calls
    bound = inspect.signature(MIRRORS[name]).bind(None, *args, **kwargs)
    assert called == name
    assert {key: value for key, value in bound.arguments.items() if key != "self"} == sentinels
