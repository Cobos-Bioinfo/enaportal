"""Real ENA exchanges, recorded by scripts/record_fixtures.py, for replay with respx."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import respx

from enaportal._http import BROWSER_BASE_URL, PORTAL_BASE_URL

FIXTURES = Path(__file__).parent / "fixtures"

_BASES = {"portal": PORTAL_BASE_URL, "browser": BROWSER_BASE_URL}


@dataclass(frozen=True)
class Recording:
    """One request sent to ENA and exactly what came back."""

    name: str
    api: str
    method: str
    path: str
    params: dict[str, str]
    json: dict[str, Any] | None
    status: int
    content_type: str
    body: str

    @property
    def url(self) -> str:
        return _BASES[self.api] + self.path

    def mock(self) -> respx.Route:
        """Serve the recorded response to a request like the recorded one."""
        pattern: dict[str, Any] = {"method": self.method, "url": self.url}
        if self.params:
            pattern["params"] = self.params
        if self.json is not None:
            pattern["json"] = self.json
        return respx.route(**pattern).mock(
            return_value=httpx.Response(
                self.status,
                headers={"content-type": self.content_type},
                content=self.body.encode("utf-8"),
            )
        )

    def assert_sent_by(self, route: respx.Route) -> None:
        """Fail if the library no longer sends the request this was recorded from.

        A response only proves something about the request that produced it, so
        a replay against a different request would be testing nothing real.
        """
        request = route.calls.last.request
        hint = f"{self.name}: the library's request changed; re-record the fixture"
        assert dict(request.url.params) == self.params, hint
        if self.json is not None:
            assert json.loads(request.content) == self.json, hint


def load(name: str) -> Recording:
    """Read one recording from tests/fixtures."""
    raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return Recording(
        name=name,
        api=raw["api"],
        method=raw["method"],
        path=raw["path"],
        params=raw["params"],
        json=raw["json"],
        status=raw["status"],
        content_type=raw["content_type"],
        body="\n".join(raw["body"]),
    )
