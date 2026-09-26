"""Shared test configuration.

The offline guarantee is a project constraint, so it is enforced here rather
than left to whoever remembers to add a respx mock.
"""

from __future__ import annotations

import socket
from typing import Any, NoReturn

import pytest


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any unmarked test that opens a socket fail loudly."""
    if request.node.get_closest_marker("live"):
        return

    def refuse(*args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError(
            "this test tried to reach the network; mock it with respx or mark it live"
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
