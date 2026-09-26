"""A small on-disk JSON cache for ENA's introspected schema.

Deliberately dependency-free and failure-tolerant: an unwritable cache
directory degrades to no caching rather than breaking the caller.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ENV_CACHE_DIR = "ENAPORTAL_CACHE_DIR"

_SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]+$")


def default_cache_dir() -> Path:
    """The platform cache directory, overridable with ENAPORTAL_CACHE_DIR."""
    override = os.environ.get(ENV_CACHE_DIR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "enaportal"


class JSONCache:
    """A directory of JSON files sharing one time-to-live, in seconds."""

    def __init__(self, directory: Path, ttl: float) -> None:
        self.directory = directory
        self.ttl = ttl

    def path_for(self, key: str) -> Path:
        """The file backing a cache key."""
        if not _SAFE_KEY.match(key):
            raise ValueError(f"unsafe cache key: {key!r}")
        return self.directory / f"{key}.json"

    def read(self, key: str, *, allow_stale: bool = False) -> Any | None:
        """Return the cached payload, or None if absent, expired or unreadable."""
        path = self.path_for(key)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(entry, dict) or "payload" not in entry:
            return None
        if not allow_stale and time.time() - float(entry.get("fetched_at", 0)) > self.ttl:
            return None
        return entry["payload"]

    def write(self, key: str, payload: Any) -> None:
        """Store a payload, silently doing nothing if the cache is unwritable."""
        path = self.path_for(key)
        entry = {"fetched_at": time.time(), "payload": payload}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Written through a temporary file so a crashed or concurrent run
            # can never leave a half-written cache entry behind.
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
            ) as handle:
                json.dump(entry, handle)
                temporary = Path(handle.name)
            os.replace(temporary, path)
        except OSError:
            return

    def age(self, key: str) -> float | None:
        """Seconds since the entry was written, or None if there is no entry."""
        try:
            entry = json.loads(self.path_for(key).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return time.time() - float(entry.get("fetched_at", 0))

    def clear(self, key: str | None = None) -> None:
        """Delete one entry, or every entry when key is None."""
        paths = [self.path_for(key)] if key else sorted(self.directory.glob("*.json"))
        for path in paths:
            path.unlink(missing_ok=True)
