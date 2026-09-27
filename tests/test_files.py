"""Unit tests for file URL resolution and manifest export.

Pure frame-to-text functions, so nothing here needs the network.
"""

from __future__ import annotations

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from enaportal.errors import ENAQueryError
from enaportal.files import file_urls, to_manifest

PAIRED = "ftp.sra.ebi.ac.uk/vol1/fastq/ERR100/090/ERR10003190/ERR10003190"


def frame(**columns: list[str]) -> pl.DataFrame:
    return pl.DataFrame(columns)


@pytest.fixture
def runs() -> pl.DataFrame:
    """A paired run, a single-ended run, and one with only submitted files."""
    return frame(
        run_accession=["ERR1", "ERR2", "ERR3"],
        fastq_ftp=[f"{PAIRED}_1.fastq.gz;{PAIRED}_2.fastq.gz", "host/b.fastq.gz", ""],
        fastq_md5=["aaa;bbb", "ccc", ""],
        fastq_bytes=["10;20", "30", ""],
        submitted_ftp=["", "", "host/original.bam"],
        submitted_md5=["", "", "ddd"],
        submitted_bytes=["", "", "40"],
    )


def test_semicolon_fields_become_one_row_per_file(runs: pl.DataFrame) -> None:
    urls = file_urls(runs)

    assert urls.height == 4
    assert urls["accession"].to_list() == ["ERR1", "ERR1", "ERR2", "ERR3"]


def test_md5_and_bytes_stay_aligned_with_their_file(runs: pl.DataFrame) -> None:
    urls = file_urls(runs)

    assert urls["md5"].to_list() == ["aaa", "bbb", "ccc", "ddd"]
    assert urls["bytes"].to_list() == [10, 20, 30, 40]


def test_bytes_is_numeric(runs: pl.DataFrame) -> None:
    assert file_urls(runs).schema["bytes"] == pl.Int64


def test_a_scheme_is_prepended(runs: pl.DataFrame) -> None:
    assert file_urls(runs)["url"][0] == f"https://{PAIRED}_1.fastq.gz"


def test_ftp_protocol_is_available(runs: pl.DataFrame) -> None:
    assert file_urls(runs, protocol="ftp")["url"][0] == f"ftp://{PAIRED}_1.fastq.gz"


def test_auto_falls_back_to_submitted_when_no_fastq_exists(runs: pl.DataFrame) -> None:
    urls = file_urls(runs)

    assert urls.filter(pl.col("accession") == "ERR3")["source"].to_list() == ["submitted"]


def test_auto_prefers_fastq_when_both_exist() -> None:
    both = frame(
        run_accession=["ERR1"],
        fastq_ftp=["host/a.fastq.gz"],
        fastq_md5=["aaa"],
        fastq_bytes=["1"],
        submitted_ftp=["host/a.bam"],
        submitted_md5=["bbb"],
        submitted_bytes=["2"],
    )

    assert file_urls(both)["source"].to_list() == ["fastq"]


def test_an_explicit_source_ignores_the_fallback(runs: pl.DataFrame) -> None:
    urls = file_urls(runs, source="submitted")

    assert urls["accession"].to_list() == ["ERR3"]


def test_file_index_distinguishes_the_mates(runs: pl.DataFrame) -> None:
    urls = file_urls(runs).filter(pl.col("accession") == "ERR1")

    assert urls["file_index"].to_list() == [0, 1]


def test_filename_is_the_basename(runs: pl.DataFrame) -> None:
    assert file_urls(runs)["filename"][0] == "ERR10003190_1.fastq.gz"


def test_a_missing_md5_column_gives_null_not_a_wrong_value() -> None:
    without = frame(run_accession=["ERR1"], fastq_ftp=["a.gz;b.gz"], fastq_bytes=["1;2"])

    urls = file_urls(without)

    assert urls["md5"].to_list() == [None, None]
    assert urls["bytes"].to_list() == [1, 2]


def test_a_short_md5_list_does_not_borrow_another_files_checksum() -> None:
    ragged = frame(run_accession=["ERR1"], fastq_ftp=["a.gz;b.gz"], fastq_md5=["onlyone"])

    assert file_urls(ragged)["md5"].to_list() == ["onlyone", None]


def test_analysis_frames_use_the_generated_source() -> None:
    analyses = frame(
        analysis_accession=["ERZ1"],
        generated_ftp=["host/x.vcf.gz"],
        generated_md5=["eee"],
        generated_bytes=["5"],
    )

    urls = file_urls(analyses)

    assert urls["source"].to_list() == ["generated"]
    assert urls["accession"].to_list() == ["ERZ1"]


def test_rows_with_no_files_are_dropped() -> None:
    nothing = frame(run_accession=["ERR1"], fastq_ftp=[""], fastq_md5=[""], fastq_bytes=[""])

    assert file_urls(nothing).is_empty()


def test_an_empty_frame_gives_an_empty_result() -> None:
    assert file_urls(pl.DataFrame()).is_empty()


def test_a_frame_without_file_columns_says_what_to_ask_for() -> None:
    with pytest.raises(ENAQueryError, match="fastq_ftp"):
        file_urls(frame(run_accession=["ERR1"]))


def test_a_frame_without_an_accession_says_so() -> None:
    with pytest.raises(ENAQueryError, match="accession column"):
        file_urls(frame(fastq_ftp=["a.gz"]))


def test_an_unknown_source_is_rejected(runs: pl.DataFrame) -> None:
    with pytest.raises(ValueError, match="Unknown file source"):
        file_urls(runs, source="cram")


def test_aria2c_manifest_matches_its_input_format(runs: pl.DataFrame) -> None:
    assert to_manifest(runs, "aria2c", directory="/data") == (
        f"https://{PAIRED}_1.fastq.gz\n"
        "  out=ERR10003190_1.fastq.gz\n"
        "  dir=/data\n"
        "  checksum=md5=aaa\n"
        f"https://{PAIRED}_2.fastq.gz\n"
        "  out=ERR10003190_2.fastq.gz\n"
        "  dir=/data\n"
        "  checksum=md5=bbb\n"
        "https://host/b.fastq.gz\n"
        "  out=b.fastq.gz\n"
        "  dir=/data\n"
        "  checksum=md5=ccc\n"
        "https://host/original.bam\n"
        "  out=original.bam\n"
        "  dir=/data\n"
        "  checksum=md5=ddd\n"
    )


def test_aria2c_omits_the_directory_when_not_given(runs: pl.DataFrame) -> None:
    assert "dir=" not in to_manifest(runs, "aria2c")


def test_aria2c_omits_the_checksum_when_ena_has_no_md5() -> None:
    no_md5 = frame(run_accession=["ERR1"], fastq_ftp=["host/a.gz"])

    assert "checksum" not in to_manifest(no_md5, "aria2c")


def test_aria2c_options_are_indented() -> None:
    """aria2c reads an unindented line as another URI, so this is load bearing."""
    one = frame(run_accession=["ERR1"], fastq_ftp=["host/a.gz"], fastq_md5=["aaa"])

    body = to_manifest(one, "aria2c").splitlines()

    assert body[0].startswith("https://")
    assert all(line.startswith("  ") for line in body[1:])


def test_curl_script_resumes_and_quotes(runs: pl.DataFrame) -> None:
    script = to_manifest(runs, "curl")

    assert script.startswith("#!/bin/sh\nset -eu\n")
    assert "curl -fL -C - --create-dirs -o ERR10003190_1.fastq.gz" in script


def test_curl_quotes_a_hostile_filename() -> None:
    """Submitted files keep the submitter's own name, which is not sanitised."""
    nasty = frame(run_accession=["ERR1"], submitted_ftp=["host/a b$(id).gz"])

    script = to_manifest(nasty, "curl")

    assert "'a b$(id).gz'" in script
    assert "$(id)" not in script.replace("'a b$(id).gz'", "").replace(
        "'https://host/a b$(id).gz'", ""
    )


def test_nf_core_samplesheet_pairs_the_mates(runs: pl.DataFrame) -> None:
    assert to_manifest(runs, "nf-core") == (
        "sample,fastq_1,fastq_2\n"
        f"ERR1,https://{PAIRED}_1.fastq.gz,https://{PAIRED}_2.fastq.gz\n"
        "ERR2,https://host/b.fastq.gz,\n"
        "ERR3,https://host/original.bam,\n"
    )


def test_nf_core_prefers_the_numbered_pair_over_an_unpaired_extra() -> None:
    """ENA often lists an unpaired file alongside _1 and _2 for a paired run."""
    three = frame(
        run_accession=["ERR1"],
        fastq_ftp=["host/x.fastq.gz;host/x_1.fastq.gz;host/x_2.fastq.gz"],
        fastq_md5=["a;b;c"],
    )

    assert to_manifest(three, "nf-core") == (
        "sample,fastq_1,fastq_2\nERR1,https://host/x_1.fastq.gz,https://host/x_2.fastq.gz\n"
    )


def test_a_manifest_accepts_an_already_resolved_frame(runs: pl.DataFrame) -> None:
    assert to_manifest(file_urls(runs), "aria2c") == to_manifest(runs, "aria2c")


def test_nf_core_rnaseq_adds_the_strandedness_column(runs: pl.DataFrame) -> None:
    """rnaseq requires four columns and rejects a samplesheet with three."""
    sheet = to_manifest(runs, "nf-core", pipeline="rnaseq")

    assert sheet.splitlines()[0] == "sample,fastq_1,fastq_2,strandedness"
    assert all(line.endswith(",auto") for line in sheet.splitlines()[1:])


def test_nf_core_leaves_strandedness_out_by_default(runs: pl.DataFrame) -> None:
    assert "strandedness" not in to_manifest(runs, "nf-core")


def test_an_unknown_pipeline_is_rejected(runs: pl.DataFrame) -> None:
    with pytest.raises(ValueError, match="Unknown samplesheet pipeline"):
        to_manifest(runs, "nf-core", pipeline="sarek")  # type: ignore[arg-type]


def test_accessions_manifest_is_one_per_line(runs: pl.DataFrame) -> None:
    assert to_manifest(runs, "accessions") == "ERR1\nERR2\nERR3\n"


def test_accessions_deduplicates_a_resolved_frame(runs: pl.DataFrame) -> None:
    """A paired run is two rows once resolved, but still one accession."""
    assert to_manifest(file_urls(runs), "accessions") == "ERR1\nERR2\nERR3\n"


def test_accessions_needs_no_file_columns() -> None:
    """The point of this format is handing discovery off before resolving files."""
    assert to_manifest(frame(run_accession=["ERR9", "ERR8"]), "accessions") == "ERR9\nERR8\n"


def test_accessions_on_an_empty_frame() -> None:
    assert to_manifest(pl.DataFrame(), "accessions") == ""


def test_an_unknown_format_is_rejected(runs: pl.DataFrame) -> None:
    with pytest.raises(ValueError, match="Unknown manifest format"):
        to_manifest(runs, "wget")  # type: ignore[arg-type]


def test_an_empty_frame_gives_an_empty_manifest() -> None:
    assert to_manifest(pl.DataFrame(), "aria2c") == ""


_segments = st.from_regex(r"[A-Za-z0-9_.]{1,10}", fullmatch=True)
_digests = st.from_regex(r"[0-9a-f]{4}", fullmatch=True)

Run = tuple[str, list[str], list[str], list[int]]


@st.composite
def _runs(draw: st.DrawFn) -> list[Run]:
    """Runs whose checksum and size lists may be shorter or longer than their paths."""
    runs = []
    for index in range(draw(st.integers(1, 6))):
        paths = [
            f"ftp.sra.ebi.ac.uk/vol1/{index}/{draw(_segments)}"
            for _ in range(draw(st.integers(0, 4)))
        ]
        digests = draw(st.lists(_digests, max_size=len(paths) + 1))
        sizes = draw(st.lists(st.integers(0, 10**12), max_size=len(paths) + 1))
        runs.append((f"ERR{index}", paths, digests, sizes))
    return runs


@given(_runs())
def test_every_file_keeps_its_own_checksum_and_size(runs: list[Run]) -> None:
    table = frame(
        run_accession=[accession for accession, _, _, _ in runs],
        fastq_ftp=[";".join(paths) for _, paths, _, _ in runs],
        fastq_md5=[";".join(digests) for _, _, digests, _ in runs],
        fastq_bytes=[";".join(map(str, sizes)) for _, _, _, sizes in runs],
    )

    files = file_urls(table, source="fastq")

    assert files.select("accession", "file_index", "url", "md5", "bytes").rows() == [
        (
            accession,
            position,
            f"https://{path}",
            digests[position] if position < len(digests) else None,
            sizes[position] if position < len(sizes) else None,
        )
        for accession, paths, digests, sizes in runs
        for position, path in enumerate(paths)
    ]
