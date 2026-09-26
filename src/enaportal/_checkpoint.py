"""A directory of atomically written part files plus a JSON manifest.

What makes a bulk fetch resumable. A part is written to a temporary file and
renamed into place only once it is complete, so the presence of the file is
proof that the partition finished. A run killed halfway leaves no half-written
part behind for the next one to trust.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

MANIFEST_NAME: Final = "manifest.json"

PART_SUFFIX: Final = ".tsv"


class Checkpoint:
    """The on-disk state of one bulk job."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory).expanduser()

    @property
    def manifest_path(self) -> Path:
        """The file holding the plan this directory was filled against."""
        return self.directory / MANIFEST_NAME

    def path_for(self, stem: str) -> Path:
        """Where one partition's rows live."""
        return self.directory / f"{stem}{PART_SUFFIX}"

    def is_done(self, stem: str) -> bool:
        """Whether this partition has already been fetched in full."""
        return self.path_for(stem).exists()

    def read_manifest(self) -> dict[str, Any] | None:
        """The stored plan, or None if this directory holds no job."""
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def write_manifest(self, payload: dict[str, Any]) -> None:
        """Record the plan, so a later run resumes it instead of re-planning."""
        self.directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.manifest_path, json.dumps(payload, indent=2, sort_keys=True))

    def write_part(self, stem: str, fill: Callable[[Path], int]) -> int:
        """Run fill against a temporary file and publish it as this partition.

        Returns whatever fill counted. A failure leaves the directory exactly
        as it was, which is what lets the next run retry only this partition.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = _reserve(self.directory, stem)
        try:
            written = fill(temporary)
            os.replace(temporary, self.path_for(stem))
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return written

    def clear(self) -> None:
        """Delete the manifest and every part, leaving the directory in place."""
        self.manifest_path.unlink(missing_ok=True)
        for path in self.directory.glob(f"*{PART_SUFFIX}"):
            path.unlink(missing_ok=True)

    def discard(self) -> None:
        """Clear the job and remove the directory if nothing else is in it."""
        self.clear()
        try:
            self.directory.rmdir()
        except OSError:
            return


def _reserve(directory: Path, stem: str) -> Path:
    with tempfile.NamedTemporaryFile(
        dir=directory, prefix=f"{stem}.", suffix=".tmp", delete=False
    ) as handle:
        return Path(handle.name)


def _atomic_write(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    os.replace(temporary, path)


__all__ = ["MANIFEST_NAME", "PART_SUFFIX", "Checkpoint"]
