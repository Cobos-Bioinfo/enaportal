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
  response. `enaportal` splits big queries into counted partitions and
  checkpoints them.
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
set. That is one long download with no way to resume it, which is the problem
resumable retrieval exists to solve. Pass an explicit `limit` until it lands.

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

For repeated work, hold a client so the connection pool and schema cache are
reused:

```python
from enaportal import PortalClient

with PortalClient() as ena:
    for taxon in (4932, 9606):
        print(taxon, ena.count("read_run", query=f"tax_tree({taxon})"))
```

ENA allows 50 requests per second and rejects the excess with HTTP 429.
`enaportal` waits out a 429 and retries it, so a loop like the one above does
not need its own throttling.

## Scope

In scope: the Portal API (`search`, `count`, `filereport`, `links`) and the
Browser API (records by accession as XML, EMBL or FASTA).

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
uv run pytest -m live                   # tests that hit the live ENA API
uv run python scripts/update_snapshot.py  # refresh the offline schema snapshot
```

## Licence

MIT. See [LICENSE](LICENSE).

`enaportal` is an independent project and is not affiliated with or endorsed by
EMBL-EBI.
