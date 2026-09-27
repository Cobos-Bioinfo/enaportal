# enaportal

A typed Python client for the [ENA](https://www.ebi.ac.uk/ena/browser/home)
Portal and Browser APIs.

> **Status: early development.** Nothing is published yet. The first release is
> tracked in [PLAN.md](PLAN.md).

## Why this exists

The European Nucleotide Archive exposes a capable public API: 15 result types
(reads, assemblies, sequences, samples, studies, analyses, taxon and more), with
195 return fields and 160 searchable fields on `read_run` alone. There is no
maintained general-purpose Python client for it.

The closest things are `pysradb` and `ffq`, both of which are excellent but are
*"find sequencing data"* tools built around accessions. Neither gives you the
Portal API as a queryable interface.

The one library that tried, [`enasearch`](https://github.com/bebatut/enasearch),
was last released in 2017 and every endpoint it wraps now redirects to ENA's
homepage. It broke partly because it froze ENA's field metadata into pickle
files committed to the repo, so the library could not survive the API changing
underneath it.

`enaportal` takes the opposite approach: it reads ENA's schema from ENA at
runtime and caches it, and a scheduled CI job tells us when that schema moves.

## Design commitments

- **Introspect, never freeze.** Result types and field lists come from
  `/results`, `/returnFields` and `/searchFields`, cached on disk with a TTL.
- **Resumable bulk retrieval.** The Portal API has no pagination: `offset` and
  `sortFields` are both rejected, so a large query is one long non-resumable
  response. `enaportal` splits big queries into counted date partitions and
  checkpoints each one, so a killed run picks up where it stopped.
- **Library first, CLI second.** The CLI is a thin layer over the public API.
- **Polars, not pandas.** Faster on the row counts ENA returns, and lighter.
- **Typed throughout**, with `py.typed` shipped.

## Usage

```python
import enaportal

enaportal.count("read_run", query='tax_tree(4932) AND library_strategy="RNA-Seq"')

frame = enaportal.search(
    "read_run",
    query='tax_tree(4932) AND library_strategy="RNA-Seq"',
    fields=["run_accession", "fastq_ftp", "read_count"],
    limit=100,
)
```

ENA applies no default limit, so leaving `limit` out returns the whole result
set in one long download with no way to resume it. Use `bulk_search` instead
for anything large.

`search` returns a Polars DataFrame. Every column is a string: ENA packs
multiple values into one cell with semicolons, so inferring types would give
the same field a different dtype from one query to the next. Cast explicitly
when you want numbers.

Result types and field names are checked against ENA's own schema before the
request goes out, so a typo fails locally and tells you what you probably
meant:

```
ENAQueryError: Unknown return field for read_run: 'run_acession'.
Did you mean 'run_accession', 'study_accession', 'submission_accession'?
```

The schema is browsable for the same reason:

```python
enaportal.results()  # the 15 result types
enaportal.return_fields("read_run")  # the 195 fields read_run can return
enaportal.search_fields("read_run")  # the 160 it can be queried on
```

It is read from ENA once and cached on disk, so only the first call costs a
request. Set `ENAPORTAL_CACHE_DIR` to move the cache, or call
`enaportal.refresh_schema()` to re-read it now. If ENA is unreachable the
library falls back to the cache and then to a snapshot shipped in the package,
warning each time, so it degrades instead of failing.

## Bulk retrieval

The Portal API has no cursor. `offset` and `sortFields` are both rejected and
the row order is not stable, so a large result set cannot be paged, and a
download that breaks at 90% has to start again.

`bulk_search` splits the query instead. It bisects a date range with `/count`
until every piece is under a threshold, fetches the pieces with bounded
concurrency, and writes each one to disk as it lands:

```python
frame = enaportal.bulk_search(
    "read_run",
    query="tax_tree(4932)",
    fields=["run_accession", "first_public", "read_count"],
)
```

That query is 276,447 rows. It plans into 8 partitions in about a second,
because `/count` is cheap, and fetches them in around 17 seconds. Kill it
halfway and run it again: the completed partitions are already on disk, so it
only fetches what is missing.

To see the split before committing to it:

```python
plan = enaportal.plan_partitions("read_run", query="tax_tree(4932)")
plan.total  # 276447
plan.partitions  # 8 of them, each with its own count, date range and query
plan.oversized  # partitions a single day could not be split below
```

Checkpoints live under the platform cache directory, keyed by the job, and are
removed once the job finishes so the next call fetches current rows. Pass
`checkpoint_dir` to put them somewhere you choose and keep them, which also
leaves you the raw per-partition TSVs:

```python
enaportal.bulk_search(
    "read_run",
    query="tax_tree(4932)",
    checkpoint_dir="yeast-runs/",
    threshold=20_000,
    concurrency=4,
    on_partition=lambda part: print(part.stem, part.count),
)
```

The partition key is a searchable date field, `first_public` by default
because it never moves. Two cases are handled out loud rather than silently:
a result type with no date field at all (only `taxon`) falls back to a single
unresumable fetch with a warning, and rows a date range cannot reach are
counted and fetched as a residual partition.

For a single query too large to hold in memory, `search_to_file` streams
straight to disk:

```python
with PortalClient() as ena:
    rows = ena.search_to_file("runs.tsv", "read_run", query="tax_tree(4932)")
```

## Files

`filereport` is the accession-oriented path, and it accepts any level ENA can
map to the result type, in either accession form:

```python
runs = enaportal.filereport("PRJEB1787", fields=["run_accession", "fastq_ftp", "fastq_md5"])
```

`related` navigates between objects. There is no links endpoint on the Portal
API and none is needed, because the rows already carry their parents:

```python
enaportal.related("PRJEB1787")  # the study's runs, with sample and experiment
enaportal.related("SAMEA2620995", to="analysis")
```

ENA packs a row's files into one cell separated by semicolons, keeps the
checksums and sizes positionally parallel, and serves paths with no URL
scheme. `file_urls` unpacks all of that into one row per file:

```python
enaportal.file_urls(runs)
# accession  source  file_index  filename              url  md5  bytes
```

It falls back per row through `fastq`, `generated`, `submitted`, `sra` and
`bam`, so runs that have no generated FASTQ resolve to their submitted files
instead. Pass `source="submitted"` to pin one.

`to_manifest` hands the result to a tool that does transfers properly:

```python
enaportal.to_manifest(runs, "aria2c", directory="/data")  # aria2c -i
enaportal.to_manifest(runs, "curl")  # a resumable sh script
enaportal.to_manifest(runs, "nf-core")  # sample,fastq_1,fastq_2
```

## Records and text search

The Browser API returns records rather than tables: XML for studies, samples,
experiments, runs, analyses and taxa, and EMBL or FASTA for sequences.

```python
enaportal.fetch("PRJEB1787")  # one <PROJECT_SET> document
enaportal.fetch(["ERR164407", "ERR164408"])  # one request, one <RUN_SET>
enaportal.fetch("A00145", format="fasta")
```

ENA silently leaves out any accession it cannot find, so `fetch` counts the
records that come back, warns when some are missing and raises
`ENANotFoundError` when all are. Whole assemblies come back as FASTA too, so
stream anything large to disk. `fetch_to_file` only puts the file in place once
it is complete:

```python
from enaportal import BrowserClient

with BrowserClient() as browser:
    browser.fetch_to_file("yeast.fasta", "GCA_000146045.2", format="fasta")
```

Text search matches free text across every field, which a Portal query cannot:

```python
enaportal.textsearch("Tara oceans", result="read_study")  # accession, description
enaportal.textsearch_count("Tara oceans", result="read_study")
```

It returns the first 100 hits unless told otherwise. Here `limit=0` means none
rather than all, the opposite of the Portal, so it is refused; pass
`limit=None` for every hit.

## Command line

The same library as a command. Tables go to stdout as ENA's own TSV, so they
pipe:

```bash
enaportal count read_run --query 'tax_tree(4932) AND library_strategy="RNA-Seq"'
enaportal search read_run --query 'tax_tree(4932)' -f run_accession,read_count --limit 10
enaportal related PRJEB1787 | cut -f1
enaportal fetch PRJEB1787 > study.xml
enaportal textsearch 'Tara oceans' --result read_study
enaportal fields read_run --search
```

Commands that take accessions read them from stdin given `-`, including the
TSV another call printed, header and all. This fetches the XML of all 249 runs
in a study in one request:

```bash
enaportal related PRJEB1787 | enaportal fetch - > runs.xml
```

`manifest` turns accessions into input for a downloader, or builds it straight
from a table that already carries the file columns, with no further requests:

```bash
enaportal manifest PRJEB1787 > files.txt  # every file in the study, checksums included
aria2c -i files.txt

enaportal search read_run --query 'tax_tree(4932)' -f run_accession,fastq_ftp,fastq_md5 \
  | enaportal manifest --table - --format nf-core > samplesheet.csv
```

`bulk` is the resumable download. Interrupt it and run the same command again,
and it carries on from the partitions already on disk:

```bash
enaportal bulk read_run --query 'tax_tree(4932)' -f run_accession,fastq_ftp > yeast.tsv
enaportal bulk read_run --query 'tax_tree(4932)' --dry-run  # the plan, nothing fetched
```

Warnings and progress go to stderr, and `-q` hides them. `enaportal COMMAND
--help` lists every option, and `python -m enaportal` works where the script is
not on the path.

## Holding a client

For repeated work, hold a client so the connection pool and schema cache are
reused:

```python
from enaportal import PortalClient

with PortalClient() as ena:
    for taxon in (4932, 9606):
        print(taxon, ena.count("read_run", query=f"tax_tree({taxon})"))
```

ENA allows 50 requests per second and rejects the excess with HTTP 429.
`enaportal` spaces its requests to stay under half of that, across every
thread of one client, and waits out a 429 if one arrives anyway. A loop like
the one above needs no throttling of its own, and neither does a bulk fetch.

## Scope

In scope: the Portal API (`search`, `count`, `bulk_search`, `filereport`,
`related`) and the Browser API (records by accession as XML, EMBL or FASTA).

On files: `enaportal` resolves download URLs and checksums, and exports
manifests that `aria2c`, `curl` or
[nf-core/fetchngs](https://nf-co.re/fetchngs) can consume. Getting the URLs is
the easy part; moving terabytes reliably is a different problem, and
[enaBrowserTools](https://github.com/enasequence/enaBrowserTools) already
solves it. A modest HTTP downloader with resume and MD5 verification is planned
for v0.2.

Out of scope, deliberately: bulk and Aspera transfer, and data submission,
which [ena-upload-cli](https://github.com/usegalaxy-eu/ena-upload-cli) covers.

## Installation

Not yet published. Once released:

```bash
# uv (recommended)
uv add enaportal

# conda / mamba, from conda-forge
conda install -c conda-forge enaportal

# from source
git clone https://github.com/Cobos-Bioinfo/enaportal.git
cd enaportal
uv sync
```

## Development

```bash
uv sync                                 # create the environment
uv run ruff check . && uv run mypy      # lint and types
uv run pytest                           # unit tests, never touch the network
uv run pytest --cov                     # the same, held to the coverage floor
uv run pytest -m live                   # tests that hit the live ENA API
uv run python scripts/update_snapshot.py  # refresh the offline schema snapshot
uv run python scripts/update_snapshot.py --check  # report schema drift, write nothing
uv run python scripts/record_fixtures.py  # re-record the ENA responses tests replay
```

Every Monday the schema-drift workflow compares ENA's live schema with the
snapshot and runs the live tests. Each kind of failure is kept as one open
issue, labelled `schema-drift` or `live-tests`, which is updated when the
finding changes and closed by the first clean run. Record counts and update
times are not drift. To resolve a drift issue, refresh the snapshot, check the
changes the issue lists first, and open a PR. GitHub pauses scheduled workflows
in a repository with no activity for 60 days; re-enable it from the Actions tab.

## Licence

MIT. See [LICENSE](LICENSE).

`enaportal` is an independent project and is not affiliated with or endorsed by
EMBL-EBI.
