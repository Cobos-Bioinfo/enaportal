"""The enaportal command: a thin layer over the library for shells and pipelines.

Tables go to stdout as ENA's own unquoted TSV, so output pipes into cut, awk or
another enaportal call. Warnings and progress go to stderr.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO, TextIO

import polars as pl

from enaportal._tsv import read_ena_tsv
from enaportal._version import __version__
from enaportal.browser import DEFAULT_TEXTSEARCH_LIMIT, RECORD_FORMATS, BrowserClient
from enaportal.bulk import DEFAULT_CONCURRENCY, DEFAULT_THRESHOLD, Partition
from enaportal.errors import ENAError
from enaportal.files import AUTO, SOURCE_ORDER, to_manifest
from enaportal.portal import PortalClient

PROG = "enaportal"

EXIT_ERROR = 1
EXIT_USAGE = 2
# What a shell reports for a process killed by SIGINT or SIGPIPE, so a script
# can tell an interrupted or cut-off run from a failed one.
EXIT_INTERRUPTED = 130
EXIT_BROKEN_PIPE = 141

MANIFEST_FORMATS = ("aria2c", "curl", "nf-core", "accessions")

# A header piped in from another enaportal call, such as run_accession. No ENA
# accession is all lower case, so this cannot swallow a real one.
_HEADER = re.compile(r"[a-z_]+")

_RESUME_HINT = (
    "interrupted. Run it again without --restart to resume; partitions already fetched are kept."
)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit status."""
    args = build_parser().parse_args(argv)
    with warnings.catch_warnings():
        if args.quiet:
            warnings.simplefilter("ignore")
        else:
            warnings.showwarning = _show_warning
        try:
            args.handler(args)
        except KeyboardInterrupt:
            _say(args.interrupted)
            return EXIT_INTERRUPTED
        except BrokenPipeError:
            _silence_stdout()
            return EXIT_BROKEN_PIPE
        except ENAError as exc:
            _say(f"error: {exc}")
            return EXIT_ERROR
        except ValueError as exc:
            _say(f"error: {exc}")
            return EXIT_USAGE
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The argument parser, exposed so documentation can render its help."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Query the ENA Portal and Browser APIs. Tables are written as TSV.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-q", "--quiet", action="store_true", help="hide warnings and progress")
    parser.set_defaults(interrupted="interrupted")

    # Also accepted after the subcommand. SUPPRESS keeps the subcommand from
    # resetting a --quiet given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)

    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("-o", "--output", metavar="PATH", help="write to PATH instead of stdout")

    checked = argparse.ArgumentParser(add_help=False)
    checked.add_argument(
        "--no-validate",
        dest="validate",
        action="store_false",
        help="send result and field names without checking them against ENA's schema",
    )

    returned = argparse.ArgumentParser(add_help=False)
    returned.add_argument(
        "-f",
        "--fields",
        type=_field_list,
        action="extend",
        metavar="FIELD[,FIELD]",
        help="fields to return; repeat, or separate with commas",
    )

    scoped = argparse.ArgumentParser(add_help=False)
    scoped.add_argument("--query", help="an ENA advanced search query")
    scoped.add_argument("--data-portal", help="restrict to one data portal, such as pathogen")
    scoped.add_argument(
        "--include-metagenomes",
        action="store_true",
        default=None,
        help="include metagenome records, which ENA leaves out of some results",
    )

    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(
        name: str,
        handler: Callable[[argparse.Namespace], None],
        summary: str,
        *parents: argparse.ArgumentParser,
    ) -> argparse.ArgumentParser:
        sub = commands.add_parser(
            name, help=summary, description=summary, parents=[common, *parents]
        )
        sub.set_defaults(handler=handler)
        return sub

    search = command("search", _search, "Run a Portal query.", scoped, returned, checked, output)
    search.add_argument("result", help="a result type, such as read_run")
    search.add_argument(
        "--limit",
        type=_non_negative,
        help="at most this many rows. ENA returns every match when omitted, as one "
        "download that cannot resume; use bulk for large result sets",
    )

    count = command("count", _count, "Count the records a Portal query matches.", scoped, checked)
    count.add_argument("result", help="a result type, such as read_run")

    bulk = command(
        "bulk",
        _bulk,
        "Fetch a large Portal query in resumable, checkpointed partitions.",
        returned,
        checked,
        output,
    )
    bulk.add_argument("result", help="a result type, such as read_run")
    bulk.add_argument("--query", help="an ENA advanced search query")
    bulk.add_argument(
        "--threshold",
        type=_non_negative,
        default=DEFAULT_THRESHOLD,
        help=f"most rows per partition (default {DEFAULT_THRESHOLD:,})",
    )
    bulk.add_argument(
        "--partition-field", help="date field to split on (default first_public where it exists)"
    )
    bulk.add_argument(
        "--checkpoint-dir",
        metavar="DIR",
        help="keep partitions here, even after success. By default they go under the "
        "cache directory and are removed once the job completes",
    )
    bulk.add_argument(
        "--concurrency",
        type=_non_negative,
        default=DEFAULT_CONCURRENCY,
        help=f"partitions fetched at once (default {DEFAULT_CONCURRENCY})",
    )
    bulk.add_argument(
        "--restart", action="store_true", help="discard partitions from an earlier run"
    )
    bulk.add_argument(
        "--dry-run",
        action="store_true",
        help="print the partition plan as TSV and fetch nothing",
    )
    bulk.set_defaults(interrupted=_RESUME_HINT)

    filereport = command(
        "filereport",
        _filereport,
        "Everything ENA holds for accessions, including file locations.",
        returned,
        checked,
        output,
    )
    filereport.add_argument(
        "accessions",
        nargs="+",
        metavar="ACCESSION",
        help="study, sample, experiment or run accessions, or - to read them from stdin",
    )
    filereport.add_argument("--result", default="read_run", help="result type (default read_run)")
    filereport.add_argument("--limit", type=_non_negative, help="at most this many rows each")

    related = command(
        "related",
        _related,
        "The objects related to an accession, such as a study's runs.",
        returned,
        checked,
        output,
    )
    related.add_argument("accession", help="an accession, primary or secondary")
    related.add_argument("--to", default="read_run", help="result type (default read_run)")
    related.add_argument("--limit", type=_non_negative, help="at most this many rows")

    manifest = command(
        "manifest",
        _manifest,
        "Write file locations as input for aria2c, curl or nf-core/fetchngs.",
        output,
    )
    manifest.add_argument(
        "accessions", nargs="*", metavar="ACCESSION", help="accessions, or - to read from stdin"
    )
    manifest.add_argument(
        "--table",
        metavar="PATH",
        help="build from a TSV that search, bulk or filereport wrote, or - for stdin, "
        "instead of looking accessions up",
    )
    manifest.add_argument(
        "--format", choices=MANIFEST_FORMATS, default="aria2c", help="(default aria2c)"
    )
    manifest.add_argument("--result", default="read_run", help="result type (default read_run)")
    manifest.add_argument(
        "--source",
        choices=(AUTO, *SOURCE_ORDER),
        default=AUTO,
        help="which files; auto takes generated FASTQ and falls back per run",
    )
    manifest.add_argument("--protocol", choices=("https", "ftp"), default="https")
    manifest.add_argument("--directory", metavar="DIR", help="where the downloader should save")
    manifest.add_argument(
        "--pipeline", choices=("rnaseq",), help="add the columns this nf-core pipeline requires"
    )

    fetch = command(
        "fetch",
        _fetch,
        "Records by accession as XML, EMBL or FASTA, from the Browser API.",
        output,
    )
    fetch.add_argument(
        "accessions",
        nargs="+",
        metavar="ACCESSION",
        help="accessions of one data type, or - to read them from stdin",
    )
    fetch.add_argument("--format", choices=RECORD_FORMATS, default="xml", help="(default xml)")
    fetch.add_argument("--annotation-only", action="store_true", help="EMBL without the sequence")
    fetch.add_argument(
        "--line-limit", type=_non_negative, metavar="N", help="cut each record after N lines"
    )

    textsearch = command(
        "textsearch", _textsearch, "Free-text search over ENA, from the Browser API.", output
    )
    textsearch.add_argument("query", help="words to search for")
    textsearch.add_argument("--result", required=True, help="a result type, such as read_study")
    textsearch.add_argument(
        "--limit",
        type=_non_negative,
        default=DEFAULT_TEXTSEARCH_LIMIT,
        help=f"at most this many hits (default {DEFAULT_TEXTSEARCH_LIMIT})",
    )
    textsearch.add_argument("--all", action="store_true", help="every hit, however many")
    textsearch.add_argument("--offset", type=_non_negative, help="skip this many hits first")
    textsearch.add_argument("--count", action="store_true", help="print only how many match")

    fields = command("fields", _fields, "The fields a result type returns.", output)
    fields.add_argument("result", help="a result type, such as read_run")
    fields.add_argument(
        "--search", action="store_true", help="list the fields a query can use instead"
    )

    command("results", _results, "Every result type ENA exposes.", output)
    return parser


def _search(args: argparse.Namespace) -> None:
    with _portal() as client:
        client.search_to_file(
            _destination(args.output),
            args.result,
            query=args.query,
            fields=args.fields,
            limit=args.limit,
            data_portal=args.data_portal,
            include_metagenomes=args.include_metagenomes,
            validate=args.validate,
        )


def _count(args: argparse.Namespace) -> None:
    with _portal() as client:
        total = client.count(
            args.result,
            query=args.query,
            data_portal=args.data_portal,
            include_metagenomes=args.include_metagenomes,
            validate=args.validate,
        )
    _emit_text(f"{total}\n", None)


def _bulk(args: argparse.Namespace) -> None:
    with _portal() as client:
        if args.dry_run:
            plan = client.plan_partitions(
                args.result,
                query=args.query,
                fields=args.fields,
                threshold=args.threshold,
                partition_field=args.partition_field,
                validate=args.validate,
            )
            if not args.quiet:
                _say(
                    f"{plan.total:,} rows in {_plural(len(plan.partitions), 'partition')} on "
                    f"{plan.partition_field or 'no field, as one unresumable request'}"
                )
            _emit(_plan_table(plan.partitions), args.output)
            return
        frame = client.bulk_search(
            args.result,
            query=args.query,
            fields=args.fields,
            threshold=args.threshold,
            partition_field=args.partition_field,
            checkpoint_dir=args.checkpoint_dir,
            concurrency=args.concurrency,
            resume=not args.restart,
            validate=args.validate,
            on_partition=None if args.quiet else _progress(),
        )
    _emit(frame, args.output)


def _filereport(args: argparse.Namespace) -> None:
    with _portal() as client:
        frame = client.filereport(
            _accessions(args.accessions),
            result=args.result,
            fields=args.fields,
            limit=args.limit,
            validate=args.validate,
        )
    _emit(frame, args.output)


def _related(args: argparse.Namespace) -> None:
    with _portal() as client:
        frame = client.related(
            args.accession, to=args.to, fields=args.fields, limit=args.limit, validate=args.validate
        )
    _emit(frame, args.output)


def _manifest(args: argparse.Namespace) -> None:
    if bool(args.accessions) == bool(args.table):
        raise ValueError("give either accessions or --table, not both and not neither")
    if args.table:
        frame = read_ena_tsv(sys.stdin.buffer if args.table == "-" else Path(args.table))
    else:
        with _portal() as client:
            frame = client.filereport(
                _accessions(args.accessions),
                result=args.result,
                fields=_manifest_fields(client, args.result, args.source),
            )
    text = to_manifest(
        frame,
        args.format,
        source=args.source,
        protocol=args.protocol,
        directory=args.directory,
        pipeline=args.pipeline,
    )
    _emit_text(text, args.output)


def _fetch(args: argparse.Namespace) -> None:
    with _browser() as client:
        client.fetch_to_file(
            _destination(args.output),
            _accessions(args.accessions),
            format=args.format,
            annotation_only=args.annotation_only,
            line_limit=args.line_limit,
        )


def _textsearch(args: argparse.Namespace) -> None:
    if args.limit == 0 and not args.all:
        raise ValueError("--limit must be at least 1; use --all for every hit")
    with _browser() as client:
        if args.count:
            total = client.textsearch_count(args.query, result=args.result)
            _emit_text(f"{total}\n", args.output)
            return
        frame = client.textsearch(
            args.query,
            result=args.result,
            limit=None if args.all else args.limit,
            offset=args.offset,
        )
    _emit(frame, args.output)


def _fields(args: argparse.Namespace) -> None:
    with _portal() as client:
        listed = (
            client.search_fields(args.result) if args.search else client.return_fields(args.result)
        )
    _emit(
        pl.DataFrame(
            {
                "column_id": [field.column_id for field in listed],
                "type": [field.type for field in listed],
                "description": [field.description for field in listed],
            },
            schema={"column_id": pl.String, "type": pl.String, "description": pl.String},
        ),
        args.output,
    )


def _results(args: argparse.Namespace) -> None:
    with _portal() as client:
        listed = client.results()
    _emit(
        pl.DataFrame(
            {
                "result_id": [result.result_id for result in listed],
                "description": [result.description for result in listed],
                "primary_accession_type": [result.primary_accession_type for result in listed],
                "record_count": [result.record_count for result in listed],
                "last_updated": [result.last_updated for result in listed],
            },
            schema={
                "result_id": pl.String,
                "description": pl.String,
                "primary_accession_type": pl.String,
                "record_count": pl.Int64,
                "last_updated": pl.String,
            },
        ),
        args.output,
    )


def _portal() -> PortalClient:
    return PortalClient()


def _browser() -> BrowserClient:
    return BrowserClient()


def _manifest_fields(client: PortalClient, result: str, source: str) -> list[str]:
    """The accession column and file columns a manifest needs, and nothing else.

    Asked for explicitly because filereport's default columns vary by result
    type, and a manifest without checksums would lose aria2c's verification.
    """
    client.schema.validate_result(result)
    available = {field.column_id for field in client.return_fields(result)}
    primary = next(
        (entry.primary_accession_type for entry in client.results() if entry.result_id == result),
        None,
    )
    accession = (primary or "accession").split(",")[0]
    sources = SOURCE_ORDER if source == AUTO else (source,)
    wanted = [
        accession,
        *(f"{name}_{part}" for name in sources for part in ("ftp", "md5", "bytes")),
    ]
    return [name for name in wanted if name in available]


def _plan_table(partitions: Sequence[Partition]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "partition": [part.index for part in partitions],
            "start": [part.start.isoformat() if part.start else None for part in partitions],
            "end": [part.end.isoformat() if part.end else None for part in partitions],
            "count": [part.count for part in partitions],
            "query": [part.query for part in partitions],
        },
        schema={
            "partition": pl.Int64,
            "start": pl.String,
            "end": pl.String,
            "count": pl.Int64,
            "query": pl.String,
        },
    )


def _progress() -> Callable[[Partition], None]:
    landed = 0
    rows = 0

    def report(part: Partition) -> None:
        nonlocal landed, rows
        landed += 1
        rows += part.count
        if part.start is not None and part.end is not None:
            span = f"{part.start} to {part.end}"
        else:
            span = "the rows no date range reaches" if part.index else "the whole query"
        _say(
            f"fetched {span}, {part.count:,} rows "
            f"({_plural(landed, 'partition')} done, {rows:,} rows so far)"
        )

    return report


def _accessions(values: Sequence[str]) -> list[str]:
    """Accessions as given, or read from stdin when the only one is '-'.

    Stdin may be a plain list or a TSV another enaportal call printed: the first
    column of each line is used and a header line is skipped.
    """
    if list(values) != ["-"]:
        return list(values)
    accessions: list[str] = []
    for number, line in enumerate(sys.stdin):
        tokens = line.split("\t", 1)[0].split()
        if number == 0 and len(tokens) == 1 and _HEADER.fullmatch(tokens[0]):
            continue
        accessions.extend(tokens)
    return accessions


def _destination(output: str | None) -> str | BinaryIO:
    return sys.stdout.buffer if output in (None, "-") else output


def _emit(frame: pl.DataFrame, output: str | None) -> None:
    # A frame with no columns means no rows at all, and Polars would still
    # write a blank line for it.
    if not frame.width:
        _emit_text("", output)
        return
    # Quoting stays off: ENA's TSV is unquoted and its free text carries bare
    # double quotes, so this gives back what ENA sent and what read_ena_tsv reads.
    frame.write_csv(_destination(output), separator="\t", quote_style="never")


def _emit_text(text: str, output: str | None) -> None:
    destination = _destination(output)
    if isinstance(destination, str):
        Path(destination).write_text(text, encoding="utf-8")
    else:
        destination.write(text.encode("utf-8"))
        destination.flush()


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _field_list(value: str) -> list[str]:
    return [name.strip() for name in value.split(",") if name.strip()]


def _non_negative(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {value!r}") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {value}")
    return number


def _say(message: str) -> None:
    print(f"{PROG}: {message}", file=sys.stderr)


def _show_warning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: TextIO | None = None,
    line: str | None = None,
) -> None:
    _say(f"warning: {message}")


def _silence_stdout() -> None:
    """Point stdout at devnull once the reader has gone, as `| head` does.

    Otherwise the interpreter's own flush at exit hits the closed pipe again
    and prints a second, noisier error.
    """
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError):
        return


__all__ = ["build_parser", "main"]
