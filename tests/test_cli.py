"""Unit tests for the command line. Every response is mocked; nothing hits ENA."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from importlib.metadata import entry_points
from pathlib import Path

import httpx
import pytest
import respx

from enaportal import cli
from enaportal._http import BROWSER_BASE_URL, PORTAL_BASE_URL, ENAHTTPClient
from enaportal._version import __version__
from enaportal.browser import DEFAULT_TEXTSEARCH_LIMIT, BrowserClient
from enaportal.portal import PortalClient
from enaportal.schema import SchemaClient

SEARCH_URL = f"{PORTAL_BASE_URL}search"
COUNT_URL = f"{PORTAL_BASE_URL}count"
FILEREPORT_URL = f"{PORTAL_BASE_URL}filereport"
XML_URL = f"{BROWSER_BASE_URL}xml"
FASTA_URL = f"{BROWSER_BASE_URL}fasta"
TEXTSEARCH_URL = f"{BROWSER_BASE_URL}tsv/textsearch"
TEXTSEARCH_COUNT_URL = f"{BROWSER_BASE_URL}tsv/textsearch/count"

# study_title carries bare double quotes, which ENA does not escape.
TSV = 'run_accession\tstudy_title\nERR1\tA "quoted" title\nERR2\tPlain title\n'

RUN_FILES = (
    "run_accession\tfastq_ftp\tfastq_md5\tfastq_bytes\n"
    "ERR1\tftp.sra.ebi.ac.uk/vol1/fastq/ERR1/ERR1_1.fastq.gz;"
    "ftp.sra.ebi.ac.uk/vol1/fastq/ERR1/ERR1_2.fastq.gz\taaa;bbb\t10;20\n"
)

Run = Callable[..., tuple[int, str, str]]


@pytest.fixture
def portal(tmp_path: Path) -> Iterator[PortalClient]:
    """A client whose schema comes from the packaged snapshot, never the network."""
    schema = SchemaClient(cache_dir=tmp_path, offline=True)
    with pytest.warns(UserWarning, match="packaged schema snapshot"):
        schema.results()
        for result in ("read_run", "analysis"):
            schema.return_fields(result)
            schema.search_fields(result)
    with ENAHTTPClient(max_retries=0, rate_limit=None) as http:
        yield PortalClient(http=http, schema=schema)


@pytest.fixture
def browser() -> Iterator[BrowserClient]:
    with ENAHTTPClient(BROWSER_BASE_URL, max_retries=0, rate_limit=None) as http:
        yield BrowserClient(http=http)


@pytest.fixture
def run(
    portal: PortalClient,
    browser: BrowserClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Run:
    """Run the CLI against mocked clients, returning status, stdout and stderr."""
    monkeypatch.setattr(cli, "_portal", lambda: portal)
    monkeypatch.setattr(cli, "_browser", lambda: browser)

    def invoke(*argv: str, stdin: str | None = None) -> tuple[int, str, str]:
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin.encode())))
        status = cli.main(list(argv))
        captured = capsys.readouterr()
        return status, captured.out, captured.err

    return invoke


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["--version"])

    assert exited.value.code == 0
    assert capsys.readouterr().out.strip() == f"enaportal {__version__}"


@pytest.mark.parametrize(
    ("argv", "quiet"),
    [(["-q", "results"], True), (["results", "-q"], True), (["results"], False)],
)
def test_quiet_is_accepted_before_or_after_the_command(argv: list[str], quiet: bool) -> None:
    assert cli.build_parser().parse_args(argv).quiet is quiet


@respx.mock
def test_search_streams_enas_tsv_unchanged(run: Run) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))

    status, out, _ = run(
        "search", "read_run", "--query", "tax_tree(4932)", "-f", "run_accession,study_title"
    )

    assert status == 0
    assert out == TSV
    params = route.calls[0].request.url.params
    assert params["query"] == "tax_tree(4932)"
    assert params["fields"] == "run_accession,study_title"
    assert "limit" not in params


@respx.mock
def test_fields_can_be_repeated_or_comma_separated(run: Run) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))

    run("search", "read_run", "-f", "run_accession, study_title", "-f", "read_count")

    assert route.calls[0].request.url.params["fields"] == "run_accession,study_title,read_count"


@respx.mock
def test_search_writes_to_a_file_with_output(run: Run, tmp_path: Path) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))
    path = tmp_path / "runs.tsv"

    status, out, _ = run("search", "read_run", "--limit", "2", "-o", str(path))

    assert status == 0
    assert out == ""
    assert path.read_text() == TSV


@respx.mock
def test_count_prints_the_number(run: Run) -> None:
    respx.get(COUNT_URL).mock(return_value=httpx.Response(200, text="count\n45329\n"))

    assert run("count", "read_run", "--query", "tax_tree(4932)")[:2] == (0, "45329\n")


@respx.mock
def test_filereport_reads_accessions_from_stdin_past_a_header(run: Run) -> None:
    route = respx.get(FILEREPORT_URL).mock(return_value=httpx.Response(200, text=TSV))
    piped = "run_accession\tstudy_title\nERR1\tfirst\nERR2 ERR3\n\n"

    status, _, _ = run("filereport", "-", stdin=piped)

    assert status == 0
    sent = [call.request.url.params["accession"] for call in route.calls]
    assert sent == ["ERR1", "ERR2", "ERR3"]


@respx.mock
def test_filereport_stacks_the_rows_of_each_accession(run: Run) -> None:
    respx.get(FILEREPORT_URL).mock(return_value=httpx.Response(200, text=TSV))

    _, out, _ = run("filereport", "PRJEB1", "PRJEB2", "-f", "run_accession,study_title")

    assert out == TSV + TSV.split("\n", 1)[1]


@respx.mock
def test_related_asks_for_the_navigation_columns(run: Run, portal: PortalClient) -> None:
    route = respx.get(FILEREPORT_URL).mock(return_value=httpx.Response(200, text=TSV))

    run("related", "PRJEB1787")

    expected = [field.column_id for field in portal.schema.link_fields("read_run")]
    assert route.calls[0].request.url.params["fields"].split(",") == expected


def test_fields_lists_what_a_result_returns_or_searches(run: Run, portal: PortalClient) -> None:
    _, returned, _ = run("fields", "read_run")
    _, searched, _ = run("fields", "read_run", "--search")

    assert returned.splitlines()[0] == "column_id\ttype\tdescription"
    listed = [line.split("\t")[0] for line in searched.splitlines()[1:]]
    assert listed == [field.column_id for field in portal.search_fields("read_run")]
    assert returned != searched


def test_results_lists_every_result_type(run: Run, portal: PortalClient) -> None:
    status, out, _ = run("results")

    assert status == 0
    rows = [line.split("\t") for line in out.splitlines()]
    assert rows[0] == [
        "result_id",
        "description",
        "primary_accession_type",
        "record_count",
        "last_updated",
    ]
    assert [row[0] for row in rows[1:]] == [result.result_id for result in portal.results()]


@respx.mock
def test_manifest_asks_filereport_for_exactly_the_file_columns(run: Run) -> None:
    route = respx.get(FILEREPORT_URL).mock(return_value=httpx.Response(200, text=RUN_FILES))

    status, out, _ = run("manifest", "ERR1")

    assert status == 0
    fields = route.calls[0].request.url.params["fields"].split(",")
    assert fields[0] == "run_accession"
    assert {"fastq_ftp", "fastq_md5", "fastq_bytes", "submitted_ftp", "sra_md5"} <= set(fields)
    assert "generated_ftp" not in fields
    assert out.splitlines()[:3] == [
        "https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR1/ERR1_1.fastq.gz",
        "  out=ERR1_1.fastq.gz",
        "  checksum=md5=aaa",
    ]


@respx.mock
def test_manifest_for_one_source_asks_for_that_family_only(run: Run) -> None:
    route = respx.get(FILEREPORT_URL).mock(return_value=httpx.Response(200, text=RUN_FILES))

    run("manifest", "ERR1", "--source", "fastq", "--format", "accessions")

    assert route.calls[0].request.url.params["fields"] == (
        "run_accession,fastq_ftp,fastq_md5,fastq_bytes"
    )


@respx.mock
def test_manifest_for_analyses_uses_the_analysis_accession(run: Run) -> None:
    route = respx.get(FILEREPORT_URL).mock(
        return_value=httpx.Response(200, text="analysis_accession\tgenerated_ftp\nERZ1\tx/a.gz\n")
    )

    run("manifest", "ERZ1", "--result", "analysis")

    fields = route.calls[0].request.url.params["fields"].split(",")
    assert fields[0] == "analysis_accession"
    assert "generated_ftp" in fields
    assert "fastq_ftp" not in fields


def test_manifest_from_a_table_needs_no_request(run: Run, tmp_path: Path) -> None:
    table = tmp_path / "runs.tsv"
    table.write_text(RUN_FILES)

    status, out, _ = run("manifest", "--table", str(table), "--format", "nf-core")

    assert status == 0
    assert out.splitlines() == [
        "sample,fastq_1,fastq_2",
        "ERR1,https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR1/ERR1_1.fastq.gz,"
        "https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR1/ERR1_2.fastq.gz",
    ]


def test_manifest_reads_a_table_from_stdin(run: Run) -> None:
    _, out, _ = run("manifest", "--table", "-", "--format", "accessions", stdin=RUN_FILES)

    assert out == "ERR1\n"


@pytest.mark.parametrize("argv", [["manifest"], ["manifest", "ERR1", "--table", "x.tsv"]])
def test_manifest_takes_accessions_or_a_table(run: Run, argv: list[str]) -> None:
    status, _, err = run(*argv)

    assert status == cli.EXIT_USAGE
    assert "either accessions or --table" in err


@respx.mock
def test_fetch_streams_records_to_stdout(run: Run) -> None:
    body = ">ENA|A1|A1.1 one\nACGT\n>ENA|A2|A2.1 two\nACGT\n"
    route = respx.post(FASTA_URL).mock(return_value=httpx.Response(200, text=body))

    status, out, _ = run("fetch", "A1", "A2", "--format", "fasta", "--line-limit", "2")

    assert status == 0
    assert out == body
    assert json.loads(route.calls[0].request.content) == {
        "accessions": ["A1", "A2"],
        "lineLimit": 2,
    }


@respx.mock
def test_fetch_writes_a_file_whole_with_output(run: Run, tmp_path: Path) -> None:
    body = '<RUN_SET>\n<RUN accession="ERR1">\n</RUN>\n</RUN_SET>\n'
    respx.post(XML_URL).mock(return_value=httpx.Response(200, text=body))
    path = tmp_path / "runs.xml"

    run("fetch", "ERR1", "-o", str(path))

    assert path.read_text() == body
    assert list(tmp_path.iterdir()) == [path]


@respx.mock
def test_warnings_go_to_stderr_as_one_plain_line(run: Run) -> None:
    body = '<RUN_SET>\n<RUN accession="ERR1">\n</RUN>\n</RUN_SET>\n'
    respx.post(XML_URL).mock(return_value=httpx.Response(200, text=body))

    status, _, err = run("fetch", "ERR1", "ERR9")
    _, _, quiet = run("fetch", "ERR1", "ERR9", "-q")

    assert status == 0
    assert err.startswith("enaportal: warning: Only 1 of 2 accessions came back")
    assert len(err.splitlines()) == 1
    assert quiet == ""


@respx.mock
def test_textsearch_writes_unquoted_hits(run: Run) -> None:
    body = 'accession\tdescription\n"ERP1"\t"Draft of ""Candidatus"" X"\n'
    route = respx.get(TEXTSEARCH_URL).mock(return_value=httpx.Response(200, text=body))

    _, out, _ = run("textsearch", "Candidatus", "--result", "read_study")

    assert out == 'accession\tdescription\nERP1\tDraft of "Candidatus" X\n'
    assert route.calls[0].request.url.params["limit"] == str(DEFAULT_TEXTSEARCH_LIMIT)


@respx.mock
def test_textsearch_all_sends_no_limit(run: Run) -> None:
    route = respx.get(TEXTSEARCH_URL).mock(
        return_value=httpx.Response(200, text="accession\tdescription\n")
    )

    run("textsearch", "Tara", "--result", "read_study", "--all")

    assert "limit" not in route.calls[0].request.url.params


def test_textsearch_limit_zero_points_at_all(run: Run) -> None:
    status, _, err = run("textsearch", "Tara", "--result", "read_study", "--limit", "0")

    assert status == cli.EXIT_USAGE
    assert "use --all" in err


@respx.mock
def test_textsearch_count_prints_the_number(run: Run) -> None:
    respx.get(TEXTSEARCH_COUNT_URL).mock(return_value=httpx.Response(200, json={"count": "48"}))

    assert run("textsearch", "Tara", "--result", "read_study", "--count")[:2] == (0, "48\n")


@respx.mock
def test_bulk_dry_run_prints_the_plan_and_fetches_nothing(run: Run) -> None:
    respx.get(COUNT_URL).mock(return_value=httpx.Response(200, text="count\n3\n"))
    search = respx.get(SEARCH_URL)

    status, out, err = run("bulk", "read_run", "--query", "tax_tree(4932)", "--dry-run")

    assert status == 0
    assert out.splitlines() == ["partition\tstart\tend\tcount\tquery", "0\t\t\t3\ttax_tree(4932)"]
    assert err == "enaportal: 3 rows in 1 partition on first_public\n"
    assert not search.called


@respx.mock
def test_bulk_writes_the_rows_and_reports_progress(run: Run, tmp_path: Path) -> None:
    respx.get(COUNT_URL).mock(return_value=httpx.Response(200, text="count\n2\n"))
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=TSV))
    checkpoints = str(tmp_path / "job")

    status, out, err = run("bulk", "read_run", "--checkpoint-dir", checkpoints)
    _, again, quiet = run("bulk", "read_run", "--checkpoint-dir", checkpoints, "--restart", "-q")

    assert status == 0
    assert out == again == TSV
    assert err == "enaportal: fetched the whole query, 2 rows (1 partition done, 2 rows so far)\n"
    assert quiet == ""


def test_an_interrupted_bulk_run_says_how_to_resume(
    run: Run, portal: PortalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(portal, "bulk_search", interrupted)
    monkeypatch.setattr(portal, "count", interrupted)

    bulk_status, _, bulk_err = run("bulk", "read_run")
    count_status, _, count_err = run("count", "read_run")

    assert bulk_status == count_status == cli.EXIT_INTERRUPTED
    assert "without --restart to resume" in bulk_err
    assert count_err == "enaportal: interrupted\n"


@respx.mock
def test_a_closed_pipe_exits_quietly(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(COUNT_URL).mock(return_value=httpx.Response(200, text="count\n1\n"))
    silenced: list[bool] = []

    def closed(text: str, output: str | None) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(cli, "_emit_text", closed)
    monkeypatch.setattr(cli, "_silence_stdout", lambda: silenced.append(True))

    status, _, err = run("count", "read_run")

    assert status == cli.EXIT_BROKEN_PIPE
    assert err == ""
    assert silenced == [True]


def test_an_unknown_field_fails_locally_with_a_suggestion(run: Run) -> None:
    status, out, err = run("search", "read_run", "-f", "run_acession")

    assert status == cli.EXIT_ERROR
    assert out == ""
    assert err.startswith("enaportal: error: Unknown return field")
    assert "'run_accession'" in err


def test_a_value_the_library_refuses_is_a_usage_error(run: Run) -> None:
    status, _, err = run("bulk", "read_run", "--concurrency", "0")

    assert status == cli.EXIT_USAGE
    assert "concurrency must be at least 1" in err


def test_a_malformed_number_is_refused_by_the_parser(run: Run) -> None:
    with pytest.raises(SystemExit) as exited:
        run("search", "read_run", "--limit", "ten")

    assert exited.value.code == cli.EXIT_USAGE


def test_python_dash_m_runs_the_command_line() -> None:
    finished = subprocess.run(
        [sys.executable, "-m", "enaportal", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert finished.stdout.strip() == f"enaportal {__version__}"


def test_the_console_script_points_at_main() -> None:
    (script,) = entry_points(group="console_scripts", name="enaportal")

    assert script.load() is cli.main
