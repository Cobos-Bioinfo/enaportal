"""Where streamed output goes: a path, or a binary file the caller already holds."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, TypeAlias


class BinaryWriter(Protocol):
    """Anything with a binary write, such as sys.stdout.buffer or a gzip.open handle."""

    def write(self, data: bytes, /) -> object: ...


Destination: TypeAlias = Path | str | BinaryWriter


__all__ = ["BinaryWriter", "Destination"]
