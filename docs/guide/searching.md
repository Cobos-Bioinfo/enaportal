# Searching the Portal

The Portal API answers questions of the form "which records of this type match
this query, and what are these fields for each of them". enaportal wraps it as
`search`, `count` and `search_to_file`, and reads the schema that says which
questions are possible.

## Result types

A result type is the kind of record a query returns. ENA lists 15:

```python
[result.result_id for result in enaportal.results()]
# ['analysis', 'assembly', 'coding', 'noncoding', 'sequence', 'read_experiment',
#  'read_run', 'sample', 'analysis_study', 'read_study', 'study', 'taxon',
#  'tls_set', 'tsa_set', 'wgs_set']
```

Each is a `Result` with a `description`, the `primary_accession_type` its rows
are keyed by, and ENA's `record_count` and `last_updated`.

## Fields

Every result type has two lists of fields. **Return fields** are the columns a
row can carry. **Search fields** are the ones a query can test. They overlap
but are not the same: `fastq_ftp` can be returned but not searched on.

```python
enaportal.return_fields("read_run")  # 195 fields
enaportal.search_fields("read_run")  # 160 fields
```

Each is a `Field` with a `column_id`, a `description` and a `type`, one of
`text`, `number`, `date`, `boolean`, `latlon`, `list`, `taxonomy`,
`controlled value` or `indexed`. ENA leaves `type` out for some fields and puts
the type word in the description instead; enaportal moves it back, so
`field.type` is right either way. `field.is_date` picks out the date fields,
which are what [bulk retrieval](bulk.md) partitions on.

From a shell, `enaportal fields read_run` lists the return fields as a table
and `enaportal fields read_run --search` the search fields.

## Writing a query

Queries use ENA's
[advanced search grammar](https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/advanced-search.html):
comparisons joined by `AND`, `OR` and `NOT`, with text values in double quotes
and functions such as `tax_tree`.

```python
enaportal.count("read_run", query='tax_tree(4932) AND library_strategy="RNA-Seq"')
enaportal.count("read_run", query="tax_eq(4932)")  # the taxon itself, no descendants
enaportal.count("read_study", query='study_title="*ocean*"')  # * is a wildcard
```

Four behaviours of the grammar catch people out. Each was confirmed against the
live API.

**`AND` binds tighter than `OR`.** `a OR b AND c` means `a OR (b AND c)`, so
restricting a disjunction needs brackets. Without them, one query that should
have matched 59,553 rows matched 8,596,605. Of these two queries, the first
applies the date to WGS runs only, and the second to both strategies:

```text
library_strategy="RNA-Seq" OR library_strategy="WGS" AND first_public>=2020-01-01
(library_strategy="RNA-Seq" OR library_strategy="WGS") AND first_public>=2020-01-01
```

**Date ranges are half-open.** On a date, `<=` behaves as `<` and `>` behaves
as `>=`, so `f>=A AND f<=B` selects dates from `A` up to but **not including**
`B`. To select the whole of 2020, end the range on the first day of 2021. The
first of these queries is all of 2020; the second misses 31 December:

```text
first_public>=2020-01-01 AND first_public<=2021-01-01
first_public>=2020-01-01 AND first_public<=2020-12-31
```

**Equality on a date matches nothing.** `first_public=2020-06-30` returns no
rows, even for records showing exactly that date. Use a one-day range instead:
`first_public>=2020-06-30 AND first_public<=2020-07-01`.

**`NOT` is an exact complement.** `q AND NOT (r)` and `q AND r` together give
exactly `q`, including rows where the field in `r` has no value.

## Validation

Before a request goes out, enaportal checks the result type, every requested
return field, and every field the query compares against a value, all against
ENA's own schema. A typo fails locally, with the closest matches:

```text
ENAQueryError: Unknown search field for read_run: 'libary_strategy'.
Did you mean 'library_strategy', 'library_source', 'library_prep_date'?
```

The check is deliberately permissive about the query. It only reads field
names that sit in front of a comparison operator, ignores anything inside
quotes, and does not look inside function calls such as `tax_tree(4932)`.
Anything it cannot read is sent as it is, and if ENA rejects it, ENA's own
message comes back as the same `ENAQueryError`. Wrongly rejecting a query ENA
would have accepted is worse than sending one it will refuse.

Pass `validate=False` to skip the checks, for example to use a field ENA has
added since the schema was cached.

## search

```python
frame = enaportal.search(
    "read_run",
    query='tax_tree(4932) AND library_strategy="RNA-Seq"',
    fields=["run_accession", "sample_accession", "read_count", "base_count"],
    limit=1000,
)
```

`fields` chooses the columns. Without it ENA returns a default set, usually
just the accession and a description.

`limit` caps the number of rows. **ENA has no default limit**: without one, or
with `limit=0`, it returns every matching row in one response. For small
result sets that is what you want. For large ones it is a long download that
starts again from nothing if it breaks, which is what
[bulk retrieval](bulk.md) is for.

The frame has one row per record and **every column is a string**. ENA packs
several values into one cell with semicolons (`fastq_ftp` holds both files of a
paired run), and the type Polars would infer for a column would change from
one query to the next. Cast what you need:

```python
import polars as pl

frame = frame.with_columns(pl.col("read_count", "base_count").cast(pl.Int64))
```

Row order is **not stable**. Six identical requests for five rows once
returned four different sets of rows. Do not rely on order, and do not expect
`limit` to return the same rows twice.

`format="json"` asks ENA for JSON instead of TSV and gives the same frame. TSV
is the default because it streams.

`data_portal` restricts the query to one of ENA's data portals, such as
`pathogen`, and `include_metagenomes=True` includes metagenome records that
some result types leave out by default.

A query that names thousands of accessions is too long for a URL, and ENA's
server rejects it. enaportal notices and sends long queries as a POST form
instead, with the same result.

## count

`count` takes the same result type, query, `data_portal` and
`include_metagenomes`, and returns an integer. It is cheap, so call it freely
before deciding how to fetch.

## search_to_file

`search_to_file` streams a query straight to a TSV file and returns the number
of rows written. Memory stays flat however large the result is:

```python
from enaportal import PortalClient

with PortalClient() as ena:
    rows = ena.search_to_file(
        "yeast-runs.tsv", "read_run", query="tax_tree(4932)", fields=["run_accession"]
    )
```

It also accepts a binary file that is already open, such as `sys.stdout.buffer`
or a `gzip.open(path, "wb")` handle, and leaves it open. The file holds ENA's
own TSV, unquoted, with a header row.

A single call is still one response. It saves memory, not progress: if it
breaks, run it again from the start, or use [bulk retrieval](bulk.md).

## The schema cache

Result types and fields are read from ENA once and cached on disk for a week,
so only the first lookup costs a request. The cache lives under the platform's
cache directory:

| Platform | Default |
|---|---|
| Linux | `$XDG_CACHE_HOME/enaportal`, or `~/.cache/enaportal` |
| macOS | `~/Library/Caches/enaportal` |
| Windows | `%LOCALAPPDATA%\enaportal` |

Set `ENAPORTAL_CACHE_DIR` to move it, or pass `cache_dir` and `ttl` (in
seconds) to `PortalClient`.

To read the schema again now, for example after ENA adds a field:

```python
enaportal.refresh_schema()  # everything
enaportal.refresh_schema("read_run")  # only read_run's fields
```

If ENA cannot be reached, a lookup falls back to the cache even if it has
expired, and then to a snapshot shipped inside the package, with a warning at
each step. Only when all three fail does it raise `ENASchemaError`. The
library degrades instead of failing, but the snapshot is a fallback, never the
source of truth: it is as old as the release you installed.

`PortalClient(offline=True)` never asks ENA for the schema and goes straight to
the cache and the snapshot. It affects schema lookups only; searches still go
to ENA.
