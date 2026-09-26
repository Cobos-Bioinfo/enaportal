"""Reading ENA's TSV, which is not quite anybody else's TSV."""

from __future__ import annotations

from pathlib import Path
from typing import IO

import polars as pl


def read_ena_tsv(source: IO[bytes] | Path) -> pl.DataFrame:
    """Parse an ENA TSV body, every column as a string.

    ENA does not quote its TSV and free-text fields such as study_title carry
    bare double quotes, so quote parsing has to be off entirely. Types are not
    inferred either: ENA packs several values into one cell with semicolons, so
    the same field would otherwise change dtype from one query to the next.
    """
    return pl.read_csv(
        source,
        separator="\t",
        has_header=True,
        quote_char=None,
        infer_schema_length=0,
        truncate_ragged_lines=True,
    )


__all__ = ["read_ena_tsv"]
