# Bulk retrieval

The Portal API has no pagination. This page explains why that matters, how
`bulk_search` fetches large result sets anyway, and what it cannot do.

## The problem

Most APIs that return large result sets let a client fetch them a page at a
time and pick up after a failure from the last page it received. The Portal API
offers no way to do that:

- **There is no cursor.** `offset` is rejected with HTTP 400 and the message
  `Unsupported param offset`, and so is `sortFields`.
- **`limit` caps, it does not page.** It returns the first *n* rows and there
  is no way to ask for the next *n*.
- **Row order is not stable.** Six identical requests for five rows returned
  four different sets, with repeats among them. Even a workaround such as
  "fetch everything sorted and skip what you have" has nothing to stand on.
- **There is no default limit.** A query without `limit` returns every matching
  row. ENA's documentation says the default is 100,000; the live API returned
  all 276,447 rows of the query below.

So a large query is one long HTTP response, and it either completes or is
lost. All yeast sequencing runs on `read_run`, `tax_tree(4932)`, is 276,447
rows and about 3 MB, and took between 20 and 35 seconds as a single response.
That is survivable. A query ten or a hundred times larger is minutes of
streaming, where a dropped connection, a timeout or a laptop going to sleep at
90% means starting again. ENA has also been seen to cut a very long response
short part way through, after answering with HTTP 200.

## The idea: split by query, not by position

A page is a slice of the result by **position**, which needs a stable order
that ENA does not have. A slice by **query** needs nothing from ENA beyond
what it already does. `first_public>=2020-01-01 AND first_public<=2021-01-01`
selects the same rows whatever order they come back in, and ranges that do not
overlap select rows that do not overlap. Cover the whole date range with such
slices and their union is the whole result, fetched in pieces that can each be
saved and skipped on a second run.

The size of each slice is known before fetching it, because `/count` accepts
the same query and answers quickly. That turns the question "how do I cut this
into pieces of at most 50,000 rows" into a search that costs a handful of
counts.

## How a plan is made

`plan_partitions` works out the pieces without fetching a single row, and
`bulk_search` calls it first:

```python
plan = enaportal.plan_partitions("read_run", query="tax_tree(4932)")
plan.partition_field  # 'first_public'
plan.total  # 276447
len(plan.partitions)  # 8
```

**1. Pick a partition field.** It has to be a searchable date field.
`first_public` is preferred, then `first_created`, then `last_updated`, and
failing those any other date field the result type has. Pass `partition_field`
to choose one yourself.

**2. Count the whole query.** If it is at or under the threshold, 50,000 rows
by default, the plan is a single partition and nothing else happens.

**3. Bisect the date range.** The range runs from 1982, when EMBL's data
library began, to two years past the current one, because records under
embargo carry a future `first_public`. Any range holding more than the
threshold is cut in half by days, and the halves are examined in turn until
every piece fits.

**4. Count only one half.** This is where the plan gets cheap. ENA reads
`f>=A AND f<=B` as the half-open range from `A` up to but not including `B`.
Two ranges that share a boundary therefore cover their parent exactly, with no
day in both and no day in neither, so the second half holds exactly the
parent's count minus the first half's. Only the first half costs a request.

Here is the plan above as the bisection found it on 27 September 2026. Every
counted range cost one `/count`; every subtracted one was free.

```text
[1982-01-01, 2028-01-01)  276,447  counted
├─ [1982-01-01, 2004-12-31)        0  counted, empty
└─ [2004-12-31, 2028-01-01)  276,447  subtracted
   ├─ [2004-12-31, 2016-07-01)   41,729  counted      partition 0
   └─ [2016-07-01, 2028-01-01)  234,718  subtracted
      ├─ [2016-07-01, 2022-04-01)  107,673  counted
      │  ├─ [2016-07-01, 2019-05-17)   53,092  counted
      │  │  ├─ [2016-07-01, 2017-12-08)  18,472  counted      partition 1
      │  │  └─ [2017-12-08, 2019-05-17)  34,620  subtracted   partition 2
      │  └─ [2019-05-17, 2022-04-01)   54,581  subtracted
      │     ├─ [2019-05-17, 2020-10-23)  25,419  counted      partition 3
      │     └─ [2020-10-23, 2022-04-01)  29,162  subtracted   partition 4
      └─ [2022-04-01, 2028-01-01)  127,045  subtracted
         ├─ [2022-04-01, 2025-02-14)   88,431  counted
         │  ├─ [2022-04-01, 2023-09-08)  48,241  counted      partition 5
         │  └─ [2023-09-08, 2025-02-14)  40,190  subtracted   partition 6
         └─ [2025-02-14, 2028-01-01)   38,614  subtracted     partition 7
```

Ten requests in all, including the count of the whole query, and 1.5 seconds.

**5. Account for every row.** A date range cannot reach a row whose date is
missing. The plan compares the count of the whole query with the count of the
full date range, and if they differ it adds a residual partition,
`(query) AND NOT (range)`. ENA's `NOT` is an exact complement, so the residual
is precisely the rows the ranges miss, and the partitions still add up to the
whole.

**6. Stop at one day.** A day is the finest range ENA can express, so a single
day holding more than the threshold cannot be split. It becomes one oversized
partition, with a warning, and is fetched as one request like any other.

Each `Partition` carries its `index`, its `query`, its planned `count`, and the
`start` and `end` of its range. `plan.oversized` lists any partition over the
threshold, and `plan.covered` is how many rows the partitions reach between
them, which is less than `plan.total` only when ENA holds matching rows that
nothing can address.

The query you pass is wrapped in brackets before a range is added to it. ENA's
`AND` binds tighter than `OR`, so adding a range to `a OR b` without them would
restrict only `b`. Unbracketed, one probe returned 8,596,605 rows where the
right answer was 59,553.

## Fetching and resuming

```python
frame = enaportal.bulk_search(
    "read_run",
    query="tax_tree(4932)",
    fields=["run_accession", "first_public", "read_count"],
    checkpoint_dir="yeast-runs/",
)
```

`bulk_search` fetches the partitions with a small thread pool, four at a time
by default, and streams each one to its own TSV file. A part file is written
under a temporary name and renamed into place only once it is complete, so a
part file that exists is a partition that finished. The plan is saved next to
the parts as `manifest.json`:

```text
yeast-runs/
├── manifest.json
├── part-0000_20041231-20160701.tsv
├── part-0001_20160701-20171208.tsv
├── ...
└── part-0007_20250214-20280101.tsv
```

**To resume, run the same call again.** It reads the stored plan rather than
making a new one, skips every partition whose file exists, and fetches the
rest. An interrupted run loses only the partitions that were in flight. Killed
halfway and restarted, the query above refetched nothing that had already
landed and returned all 276,447 rows.

The stored plan is matched to the job by its result type, query, fields,
partition field and threshold, and deliberately not by its counts. Counts move
as ENA grows, and a resume tomorrow has to recognise today's job rather than
bisect it into a different shape. Pointing a different job at a directory that
holds one raises `ENACheckpointError`. Pass `resume=False` to discard what is
there and start over.

When every partition is on disk, the parts are read back and stacked into one
frame, every column a string as with `search`.

**Where the parts go.** With `checkpoint_dir` the directory is yours: it is
kept after the job succeeds, and holds the raw TSV of every partition. Without
it the parts go to a directory under the [cache](searching.md#the-schema-cache)
named after the job, and are deleted once the job succeeds, so the next call
fetches current rows instead of replaying old ones. An interrupted run leaves
them in place either way, which is what makes the resume work.

**Progress.** `on_partition` is called on your thread as each partition lands:

```python
enaportal.bulk_search(
    "read_run",
    query="tax_tree(4932)",
    on_partition=lambda part: print(f"{part.stem}: {part.count} rows"),
)
```

**Speed.** Partitioning costs nothing even when nothing goes wrong. The query
above, fetched as eight partitions four at a time, took about 17 seconds
against 34.6 seconds for the same rows as one response.

**Rate limits.** ENA allows 50 requests per second from one address. The
bisection is a tight loop of fast `/count` calls, the easiest way to exceed
that by accident, so every request a client makes, from any thread, is spaced
to stay under half the limit. See [Errors, retries and limits](errors.md).

## Choosing a threshold

The threshold is the most rows a partition may hold. It changes how the work
is divided, never what comes back.

A lower threshold means more, smaller partitions: less to redo after an
interruption, more `/count` requests to plan, and more requests to fetch. A
higher one means fewer, longer requests. The default of 50,000 rows keeps each
request to seconds for typical field lists. If you ask for many wide fields,
such as descriptions or file lists, a lower threshold keeps each request short.

## Limits

**A bulk fetch is not a snapshot.** ENA keeps publishing while you fetch, and
a resumed job keeps the partitions it already has. A row published between two
runs may be missing from a partition fetched before it existed. Partitioning on
`first_public` at least keeps every row in exactly one partition, because a
record's first public date does not change.

**`last_updated` moves.** `assembly` has no other date field, so it is
partitioned on `last_updated`, with a warning. A record edited between an
interrupted run and its resume can move into a partition already fetched, and
be missed, or out of one, and appear twice.

**`taxon` cannot be partitioned.** It has no date field and no other field
with an order to split on. A large `taxon` query is fetched as a single
request, as `search` would, with a warning that it cannot resume.

**Oversized days are single requests.** A day that holds more rows than the
threshold is fetched in one piece, and restarts from the beginning of that day
if interrupted.

## From a shell

`enaportal bulk` is the same operation, writing TSV to stdout. Interrupt it and
run the same command again to resume:

```bash
enaportal bulk read_run --query 'tax_tree(4932)' -f run_accession,first_public > yeast.tsv
```

`--dry-run` prints the plan as a table and fetches nothing, and
`--checkpoint-dir` keeps the parts. See [Command line](cli.md).

## One large response instead

If you want the whole result in one go and can afford to rerun it, `search`
without a `limit` is simpler, and `search_to_file` streams it to disk without
holding it in memory. Neither can resume.
