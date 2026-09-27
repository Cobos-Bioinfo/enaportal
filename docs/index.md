# enaportal

A typed Python client for the Portal and Browser APIs of the
[European Nucleotide Archive](https://www.ebi.ac.uk/ena/browser/home) (ENA).

!!! warning "Early development"

    Nothing is published yet. The first release, v0.1.0, is being prepared.
    Until then, install from source as described in
    [Getting started](quickstart.md#install).

The ENA Portal API can query 15 result types, from raw reads to assemblies,
samples, studies and taxa, on hundreds of fields: 195 return fields and 160
search fields on `read_run` alone. enaportal gives you that API from Python and
from a shell. It checks queries against ENA's own schema before sending them,
returns results as [Polars](https://pola.rs) DataFrames, and splits a query too
large for one download into pieces that survive an interruption.

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

## What it covers

- **Search and count** anything the Portal API exposes. Field names are
  checked locally, with suggestions when one is mistyped.
  See [Searching the Portal](guide/searching.md).
- **Fetch result sets of any size, resumably.** ENA offers no pagination, so
  enaportal partitions the query by date and saves each piece as it lands.
  See [Bulk retrieval](guide/bulk.md).
- **Resolve files** for a study, sample or run into URLs, checksums and sizes,
  and hand them to aria2c, curl or nf-core/fetchngs.
  See [Files and manifests](guide/files.md).
- **Fetch records** as XML, EMBL or FASTA, and search free text, through the
  Browser API. See [Records and text search](guide/browser.md).
- **Do all of it from a shell**, with TSV output that pipes.
  See [Command line](guide/cli.md).

## Why it exists

ENA has a capable public API and no maintained general-purpose Python client.
[pysradb](https://github.com/saketkc/pysradb) and
[ffq](https://github.com/pachterlab/ffq) are excellent at finding sequencing
data by accession, largely through NCBI, but neither exposes the Portal API as
something you can query on your own terms. [Choosing a tool](comparison.md)
sets out which to use when.

The one Python library that tried,
[enasearch](https://github.com/bebatut/enasearch), was last released in 2017,
and every endpoint it wraps now redirects to ENA's homepage. It froze ENA's
field metadata into files committed to its repository, so it could not survive
the API changing underneath it.

enaportal takes the opposite position. It reads the schema from ENA at runtime
and caches it, ships a snapshot only as an offline fallback, and runs a weekly
job that reports when ENA's schema moves.

## Design commitments

**Introspect, never freeze.** Result types and fields come from ENA's own
`/results`, `/returnFields` and `/searchFields`, cached on disk for a week.

**Resumable bulk retrieval.** The Portal API rejects `offset` and `sortFields`
and does not keep rows in a stable order, so a large query is one long
download that cannot be resumed. enaportal splits it into counted date ranges
instead and saves each one to disk.

**Library first.** The command line is a thin layer over the same public API.

**Polars, not pandas.** Faster at the row counts ENA returns, and a lighter
dependency. enaportal depends on `httpx` and `polars` and nothing else.

**Synchronous.** The expensive case, a query of hundreds of thousands of rows,
is a single streaming response that async would not speed up. Where requests
really can run side by side, as in a bulk fetch, a bounded thread pool runs
them internally.

**Typed.** The package ships `py.typed` and passes `mypy --strict`.

## Scope

In scope: the Portal API (search, count, bulk retrieval, file reports and
navigation between related objects) and the Browser API (records by accession,
and text search).

On files, enaportal resolves download URLs and checksums and writes manifests
for tools that do transfers well. It does not move bytes itself. Bulk and
Aspera transfer are out of scope, as is data submission, which
[ena-upload-cli](https://github.com/usegalaxy-eu/ena-upload-cli) covers.

enaportal is an independent project and is not affiliated with or endorsed by
EMBL-EBI.
