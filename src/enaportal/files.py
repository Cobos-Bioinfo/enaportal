"""File locations and download manifests.

enaportal resolves URLs and checksums and hands them to a downloader that
already does transfers well. It does not move bytes itself.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Sequence
from typing import Final, Literal

import polars as pl

from enaportal.errors import ENAQueryError

Protocol = Literal["https", "ftp"]
ManifestFormat = Literal["aria2c", "curl", "nf-core", "accessions"]

# nf-core samplesheets vary by downstream pipeline, which is why fetchngs has
# its own --nf_core_pipeline flag. Only rnaseq's extra column is known here.
SamplesheetPipeline = Literal["rnaseq"]

AUTO: Final = "auto"

# ENA groups file columns by prefix: fastq_ftp, fastq_md5, fastq_bytes and so
# on. read_run carries fastq, submitted, sra and bam; analysis carries
# generated and submitted. The order is the fallback order for source="auto":
# generated files first, then whatever the submitter sent.
SOURCE_ORDER: Final = ("fastq", "generated", "submitted", "sra", "bam")

_ACCESSION_COLUMNS: Final = (
    "accession",
    "run_accession",
    "analysis_accession",
    "experiment_accession",
    "sample_accession",
    "study_accession",
)

_FILE_COLUMNS: Final = ("accession", "source", "file_index", "filename", "url", "md5", "bytes")


def file_urls(
    frame: pl.DataFrame,
    *,
    source: str = AUTO,
    protocol: Protocol = "https",
) -> pl.DataFrame:
    """One row per file, with a usable URL, its checksum and its size.

    ENA packs a row's files into single cells separated by semicolons, keeps
    the md5 and byte counts positionally parallel to the paths, and serves
    paths with no URL scheme. This unpacks all three and prepends the scheme.

    With source="auto" each row falls back through fastq, generated, submitted,
    sra and bam, which is what makes runs that have no generated FASTQ come out
    with their submitted files instead.
    """
    if frame.is_empty():
        return _empty()

    accession = _accession_column(frame)
    available = [name for name in _wanted(source) if f"{name}_ftp" in frame.columns]
    if not available:
        raise ENAQueryError(
            f"No file columns in this frame for source {source!r}. "
            f"Ask for {_suggest(source)} in the search or filereport fields."
        )

    indexed = frame.with_row_index("_row").with_columns(
        _source=pl.coalesce(
            [pl.when(_populated(f"{name}_ftp")).then(pl.lit(name)) for name in available]
        )
    )
    parts = [_unpack(indexed, name, accession, protocol) for name in available]
    populated = [part for part in parts if not part.is_empty()]
    if not populated:
        return _empty()
    return (
        pl.concat(populated, how="vertical")
        .sort("_row", "file_index")
        .drop("_row")
        .select(_FILE_COLUMNS)
    )


def to_manifest(
    frame: pl.DataFrame,
    fmt: ManifestFormat = "aria2c",
    *,
    source: str = AUTO,
    protocol: Protocol = "https",
    directory: str | None = None,
    pipeline: SamplesheetPipeline | None = None,
) -> str:
    """Render file locations as input for a tool that does the transfer.

    Accepts either a search or filereport frame, which it resolves first, or a
    frame already returned by file_urls.

    The formats close two different seams. aria2c, curl and nf-core hand over
    resolved URLs, so the transfer happens outside any pipeline. accessions
    hands the whole job to nf-core/fetchngs instead, which does retries, Aspera
    and metadata harmonisation better than a URL list can.

    pipeline applies to nf-core only, and mirrors fetchngs' --nf_core_pipeline:
    rnaseq needs a strandedness column and rejects a samplesheet without one.
    """
    if fmt == "accessions":
        return _accessions(frame)

    resolved = (
        frame
        if {"url", "filename"} <= set(frame.columns)
        else file_urls(frame, source=source, protocol=protocol)
    )
    if fmt == "aria2c":
        return _aria2c(resolved, directory)
    if fmt == "curl":
        return _curl(resolved, directory)
    if fmt == "nf-core":
        return _nf_core(resolved, pipeline)
    raise ValueError(f"Unknown manifest format: {fmt!r}. Use aria2c, curl, nf-core or accessions.")


def _unpack(frame: pl.DataFrame, name: str, accession: str, protocol: Protocol) -> pl.DataFrame:
    part = frame.filter(pl.col("_source") == name)
    if part.is_empty():
        return _empty(with_row=True)

    part = part.with_columns(_path=pl.col(f"{name}_ftp").str.split(";"))
    part = part.with_columns(file_index=pl.int_ranges(pl.col("_path").list.len()))
    # empty_as_null=False so a row with no files disappears instead of
    # becoming a phantom file with a null URL.
    part = part.explode("_path", "file_index", empty_as_null=False)
    return part.select(
        pl.col(accession).alias("accession"),
        pl.lit(name).alias("source"),
        pl.col("file_index").cast(pl.Int64),
        pl.col("_path").str.split("/").list.last().alias("filename"),
        (pl.lit(f"{protocol}://") + pl.col("_path")).alias("url"),
        _positional(frame, f"{name}_md5").alias("md5"),
        _positional(frame, f"{name}_bytes").cast(pl.Int64, strict=False).alias("bytes"),
        pl.col("_row"),
    )


def _positional(frame: pl.DataFrame, column: str) -> pl.Expr:
    """The value lining up with this file's position in the semicolon list.

    Missing entirely, or shorter than the path list, both give null rather than
    a value belonging to a different file.
    """
    if column not in frame.columns:
        return pl.lit(None, dtype=pl.String)
    return pl.col(column).str.split(";").list.get(pl.col("file_index"), null_on_oob=True)


def _populated(column: str) -> pl.Expr:
    return pl.col(column).is_not_null() & (pl.col(column).str.strip_chars() != "")


def _accession_column(frame: pl.DataFrame) -> str:
    for candidate in _ACCESSION_COLUMNS:
        if candidate in frame.columns:
            return candidate
    raise ENAQueryError(
        "No accession column in this frame. Include one of "
        f"{', '.join(_ACCESSION_COLUMNS)} in the requested fields."
    )


def _wanted(source: str) -> Sequence[str]:
    if source == AUTO:
        return SOURCE_ORDER
    if source not in SOURCE_ORDER:
        raise ValueError(f"Unknown file source: {source!r}. Use {AUTO} or one of {SOURCE_ORDER}.")
    return (source,)


def _suggest(source: str) -> str:
    names = SOURCE_ORDER if source == AUTO else (source,)
    return ", ".join(f"{name}_ftp" for name in names)


def _empty(*, with_row: bool = False) -> pl.DataFrame:
    schema: dict[str, pl.DataType] = {
        "accession": pl.String(),
        "source": pl.String(),
        "file_index": pl.Int64(),
        "filename": pl.String(),
        "url": pl.String(),
        "md5": pl.String(),
        "bytes": pl.Int64(),
    }
    if with_row:
        schema["_row"] = pl.UInt32()
    return pl.DataFrame(schema=schema)


def _aria2c(frame: pl.DataFrame, directory: str | None) -> str:
    lines: list[str] = []
    for row in frame.iter_rows(named=True):
        lines.append(row["url"])
        lines.append(f"  out={row['filename']}")
        if directory:
            lines.append(f"  dir={directory}")
        if row["md5"]:
            lines.append(f"  checksum=md5={row['md5']}")
    return _joined(lines)


def _curl(frame: pl.DataFrame, directory: str | None) -> str:
    lines = ["#!/bin/sh", "set -eu"]
    for row in frame.iter_rows(named=True):
        target = f"{directory}/{row['filename']}" if directory else row["filename"]
        if row["md5"]:
            lines.append(f"# md5 {row['md5']}")
        lines.append(
            f"curl -fL -C - --create-dirs -o {shlex.quote(target)} {shlex.quote(row['url'])}"
        )
    return _joined(lines)


def _nf_core(frame: pl.DataFrame, pipeline: SamplesheetPipeline | None) -> str:
    if pipeline is not None and pipeline != "rnaseq":
        raise ValueError(
            f"Unknown samplesheet pipeline: {pipeline!r}. Only 'rnaseq' has a known extra "
            "column; leave pipeline out for the neutral sample,fastq_1,fastq_2 header."
        )
    # rnaseq requires strandedness and rejects a sheet without it. "auto" makes
    # it infer by subsampling, which beats guessing on the archive's behalf.
    extra = ["auto"] if pipeline == "rnaseq" else []
    header = ["sample", "fastq_1", "fastq_2", *(["strandedness"] if extra else [])]

    lines = [",".join(header)]
    for accession, files in _by_accession(frame):
        first, second = _mates(files)
        lines.append(",".join([accession, first, second, *extra]))
    return _joined(lines)


def _accessions(frame: pl.DataFrame) -> str:
    """One accession per line, which is what fetchngs takes as --input.

    Deduplicated, because a resolved frame has one row per file and a paired
    run would otherwise appear twice.
    """
    if frame.is_empty():
        return ""
    column = _accession_column(frame)
    values = frame[column].drop_nulls().unique(maintain_order=True).to_list()
    return _joined([str(value) for value in values if str(value)])


def _by_accession(frame: pl.DataFrame) -> Iterable[tuple[str, list[dict[str, object]]]]:
    """Rows grouped by accession, keeping the order the frame arrived in."""
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in frame.iter_rows(named=True):
        grouped.setdefault(str(row["accession"]), []).append(row)
    return grouped.items()


def _mates(files: Sequence[dict[str, object]]) -> tuple[str, str]:
    """The two URLs for a paired run, or the single one and an empty string.

    A paired ENA run often carries a third unpaired file alongside _1 and _2,
    so the numbered pair wins whenever it is present.
    """
    first = _with_suffix(files, "_1.")
    second = _with_suffix(files, "_2.")
    if first and second:
        return first, second
    return str(files[0]["url"]), ""


def _with_suffix(files: Sequence[dict[str, object]], marker: str) -> str:
    for row in files:
        if marker in str(row["filename"]):
            return str(row["url"])
    return ""


def _joined(lines: Sequence[str]) -> str:
    return "\n".join(lines) + "\n" if lines else ""


__all__ = [
    "SOURCE_ORDER",
    "ManifestFormat",
    "Protocol",
    "SamplesheetPipeline",
    "file_urls",
    "to_manifest",
]
