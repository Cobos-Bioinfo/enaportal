# Getting started

## Install

enaportal needs Python 3.10 or later.

!!! note "Not released yet"

    enaportal is not on PyPI or conda-forge yet. Until the first release, use
    the source install.

=== "uv"

    ```bash
    uv add enaportal
    ```

=== "conda-forge"

    ```bash
    conda install -c conda-forge enaportal
    ```

=== "Source"

    ```bash
    uv add git+https://github.com/Cobos-Bioinfo/enaportal
    ```

Installing puts an `enaportal` command on the path as well as the library.

## Count before you fetch

A Portal query names a **result type**, such as `read_run` for sequencing runs
or `sample` for samples, and optionally a **query** in ENA's
[advanced search grammar](https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/advanced-search.html).
Counting is cheap, so it is the right first step:

```python
import enaportal

enaportal.count("read_run", query="tax_tree(4932)")  # 276447
enaportal.count("read_run", query='tax_tree(4932) AND library_strategy="RNA-Seq"')  # 45329
```

`tax_tree(4932)` matches *Saccharomyces cerevisiae* and every taxon below it.
Counts are as of September 2026; ENA grows every day.

## Find the fields

Every result type has its own fields, some to return and some to query on.
enaportal reads them from ENA, so they are always the current ones:

```python
enaportal.results()  # the 15 result types
enaportal.return_fields("read_run")  # the fields a read_run row can carry
enaportal.search_fields("read_run")  # the fields a read_run query can test

[field.column_id for field in enaportal.search_fields("read_run") if field.is_date]
# ['first_created', 'first_public', 'last_updated']
```

## Search

`search` returns a Polars DataFrame:

```python
frame = enaportal.search(
    "read_run",
    query='tax_tree(4932) AND library_strategy="RNA-Seq"',
    fields=["run_accession", "fastq_ftp", "read_count"],
    limit=3,
)
```

```text
shape: (3, 3)
┌───────────────┬────────────────────────────────────────────────┬────────────┐
│ run_accession ┆ fastq_ftp                                      ┆ read_count │
│ ---           ┆ ---                                            ┆ ---        │
│ str           ┆ str                                            ┆ str        │
╞═══════════════╪════════════════════════════════════════════════╪════════════╡
│ ERR10125414   ┆ ftp.sra.ebi.ac.uk/vol1/fastq/ERR101/014/ERR10… ┆ 2688732    │
│ ERR10125417   ┆ ftp.sra.ebi.ac.uk/vol1/fastq/ERR101/017/ERR10… ┆ 2960149    │
│ ERR11184487   ┆ ftp.sra.ebi.ac.uk/vol1/fastq/ERR111/087/ERR11… ┆ 111249086  │
└───────────────┴────────────────────────────────────────────────┴────────────┘
```

Three things to know straight away:

- **Always pass `limit`** unless you want every row. ENA has no default limit,
  and a query without one downloads the whole result set in one response that
  cannot be resumed. For large result sets use `bulk_search`, below.
- **Every column is a string.** ENA packs several values into one cell with
  semicolons, so an inferred type would change from query to query. Cast what
  you need: `frame.with_columns(pl.col("read_count").cast(pl.Int64))`.
- **Row order is not stable.** The same query can return rows in a different
  order, or a different subset under a `limit`, each time it runs.

A typo fails before anything is sent, with suggestions:

```python
enaportal.search("read_run", fields=["run_acession"], limit=1)
```

```text
ENAQueryError: Unknown return field for read_run: 'run_acession'.
Did you mean 'run_accession', 'study_accession', 'submission_accession'?
```

## Everything for a study

`related` turns a study, sample or experiment accession into the objects below
it, with the accessions that link them:

```python
runs = enaportal.related("PRJEB1787")  # the study's 249 runs
```

`filereport` returns any fields for the same kind of lookup, including where
the files are. `file_urls` unpacks those into one row per file, and
`to_manifest` writes them out for a downloader:

```python
runs = enaportal.filereport(
    "ERR315859", fields=["run_accession", "fastq_ftp", "fastq_md5", "fastq_bytes"]
)
enaportal.file_urls(runs)  # accession, source, file_index, filename, url, md5, bytes
print(enaportal.to_manifest(runs, "aria2c", directory="/data"))
```

```text
https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR315/ERR315859/ERR315859_1.fastq.gz
  out=ERR315859_1.fastq.gz
  dir=/data
  checksum=md5=5bed3016d7a3f0cc82426ae96f93a86c
https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR315/ERR315859/ERR315859_2.fastq.gz
  out=ERR315859_2.fastq.gz
  dir=/data
  checksum=md5=3842517e58daefeb7473176e74d93344
```

## Large result sets

`bulk_search` fetches a result set of any size in pieces, saving each one as
it lands, so an interrupted run carries on where it stopped:

```python
frame = enaportal.bulk_search(
    "read_run",
    query="tax_tree(4932)",
    fields=["run_accession", "first_public", "read_count"],
)
```

That is 276,447 rows, planned into 8 partitions in about a second and fetched
in under 20. [Bulk retrieval](guide/bulk.md) explains why ENA needs this and
how it works.

## Records

The Browser API returns the records themselves: XML for studies, samples,
experiments, runs, analyses and taxa, and EMBL or FASTA for sequences.

```python
xml = enaportal.fetch("PRJEB1787")
fasta = enaportal.fetch("A00145", format="fasta")
```

## From a shell

The same operations are subcommands, and tables go to stdout as TSV:

```bash
enaportal count read_run --query 'tax_tree(4932)'
enaportal search read_run --query 'tax_tree(4932)' -f run_accession,read_count --limit 10
enaportal related PRJEB1787 | enaportal fetch - > runs.xml
```

See [Command line](guide/cli.md).

## Hold a client for repeated work

The module-level functions share one client, created on first use. For a loop
or a long script, hold your own so its connections and schema cache are
reused and closed when you are done:

```python
from enaportal import PortalClient

with PortalClient() as ena:
    for taxon in (4932, 9606):
        print(taxon, ena.count("read_run", query=f"tax_tree({taxon})"))
```

The Portal functions are methods of `PortalClient` with the same arguments,
and `fetch`, `textsearch` and `textsearch_count` are methods of
`BrowserClient`. `refresh_schema()` becomes `ena.schema.refresh()`.
