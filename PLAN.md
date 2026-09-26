# enaportal implementation plan

Written 2026-09-26. Timeboxed to **three weeks part-time**. If a milestone
threatens that budget, cut scope rather than extend the box: this library is
infrastructure for the wider portfolio, not the centrepiece.

## Goal

Ship a typed, maintained Python client for the ENA Portal API that a
bioinformatician can install with `uv add enaportal` and use without reading
ENA's documentation first.

## Non-goals for v1

| Not doing | Why |
|---|---|
| Bulk FASTQ downloading | `enaBrowserTools` and `nf-core/fetchngs` own this |
| Data submission | `ena-upload-cli` owns this |
| Async public API | See "Decisions" below |
| Taxon-driven discovery helpers | Deferred to stretch, see M12 |
| A JOSS paper | Revisit only if the library gains real users |

---

## Decisions already made

**Sync-only public API, built on `httpx`.** Async only helps with many
independent requests in flight. Our expensive case is the opposite: one search
for 276k rows is a single 34-second streaming response, which async does not
speed up at all. An async twin would double the public surface and the test
burden. Building on `httpx` means the twin can be added in v2 without a
rewrite. Where we genuinely need parallelism (partitioned bulk fetch, many
`filereport` calls) we use a bounded thread pool internally.

**Polars, not pandas.** Faster on the row counts ENA returns, a lighter
dependency, and consistent with EukaHub.

**Introspect, never freeze.** `enasearch` died in part because it committed
ENA's field metadata to the repo as pickle files. We read `/results`,
`/returnFields` and `/searchFields` from ENA at runtime and cache them on disk
with a TTL. A scheduled CI job diffs the live schema against a committed
snapshot and opens an issue when ENA moves. This is the single most important
design commitment in the project and the answer to "why will yours not rot?".

**conda-forge, not Bioconda.** User preference: Bioconda is blocked at some
institutions. Tradeoff to accept: Bioconda would have given a BioContainers
image for free, which a future Nextflow pipeline would want. If that becomes
necessary, publish our own image to GHCR (cheap) or add a Bioconda recipe later
that simply depends on the conda-forge build.

**Install priority: `uv` first, then conda-forge, then source.** Documented in
that order in the README.

---

## Verified API facts

Probed against the live API on 2026-09-26. These drive the design, so recheck
them before assuming any still hold.

| Fact | Value |
|---|---|
| Portal base | `https://www.ebi.ac.uk/ena/portal/api/` |
| Browser base | `https://www.ebi.ac.uk/ena/browser/api/` |
| Result types | 15 (`/results`) |
| `read_run` return fields | 195 (`/returnFields?result=read_run`) |
| `read_run` search fields | 160 (`/searchFields?result=read_run`) |
| Field metadata shape | `{columnId, description, type}` where type is `text`, `number` or `date` |
| `offset` param | **Rejected** on both GET and POST: `"Unsupported param offset"` |
| `sortFields` param | **Rejected**, HTTP 400 |
| `limit=0` | Returns everything in one response. `tax_tree(4932)` read_run: 276,447 rows, 3.1 MB, 34.6 s |
| `/count` | Cheap, and accepts the same query grammar including date ranges |
| OpenAPI spec | None published (`/v3/api-docs`, `/swagger.json` both 404) |
| Rate-limit headers | None returned |
| Retired endpoints | `data/warehouse/search`, `data/view`, `data/warehouse/filereport` all 301 to the browser homepage |

The absence of `offset` and `sortFields` is the central engineering problem:
there is no cursor, so a large result set cannot be resumed. M5 solves this.

---

## Milestones

Each milestone is a branch merged into `main` via PR, with tests and green CI.

### Week 1: a working client

**M0. Scaffolding** *(done)*
Repo, licence, `.gitignore`, uv project, ruff, mypy strict, pytest with a
`live` marker, CI with a 3.10 to 3.13 matrix, build check.

**M1. HTTP layer**
`enaportal._http`: a configurable `httpx.Client` wrapper with timeouts, a
descriptive User-Agent, bounded retries with exponential backoff on 5xx and
connection errors, and a typed error hierarchy (`ENAError`,
`ENAHTTPError`, `ENAQueryError`, `ENATimeoutError`). ENA returns plain-text
errors with HTTP 200 in some cases (for example `"Unsupported param offset"`),
so response-body sniffing is part of this layer, not an afterthought.
*Done when:* unit tests with `respx` cover retry, timeout, and text-error paths.

**M2. Introspection and cache**
`enaportal.schema`: `results()`, `return_fields(result)`, `search_fields(result)`
returning typed dataclasses. On-disk JSON cache under the platform cache dir
with a TTL and an explicit `refresh()`. Offline fallback to a committed
snapshot so the library degrades rather than fails.
*Done when:* works offline from cache; `-m live` test confirms the shapes.

**M3. `search()` and `count()`**
The core call, returning a Polars DataFrame. Validates `result` and field names
against M2 before sending, so a typo fails locally with a useful message
instead of returning an ENA error page. Supports TSV and JSON response formats,
`limit`, and field selection.
*Done when:* a user can run a real query in three lines, per the README example.

### Week 2: the hard parts

**M4. `filereport()` and `links()`**
The accession-oriented endpoints. `filereport` is what most users actually want
(give me the files for this study), so it gets first-class ergonomics.

**M5. Resumable bulk retrieval**
The interesting problem. Given a query whose `/count` exceeds a threshold:
1. Pick a partition field, preferring a `date`-typed search field
   (`first_public` is the natural default).
2. Bisect the range using `/count` until every partition is under the
   threshold. `/count` is cheap, so this costs little.
3. Fetch partitions with bounded concurrency, writing each to a checkpoint.
4. On resume, skip partitions already checkpointed.
*Done when:* a 276k-row query can be interrupted and resumed without refetching
completed partitions, proven by a test that kills it mid-run.

**M6. Browser API**
`enaportal.browser`: fetch records by accession as XML, EMBL or FASTA. Smaller
surface than the Portal side, kept in its own module.

**M7. CLI**
A thin layer over the public API using `argparse`. Subcommands mirroring the
library: `search`, `count`, `filereport`, `links`, `fields`, `results`. Writes
TSV to stdout by default so it pipes. Re-add the `[project.scripts]` entry
point, removed in M0 because the module did not exist yet.

### Week 3: make it survivable

**M8. Test suite**
Recorded HTTP fixtures for the offline suite, a small set of `-m live` tests
for contract checks. Target meaningful coverage of the query-building and
partitioning logic, which is where the bugs will be.

**M9. Schema-drift workflow**
Scheduled weekly job: fetch the live schema, diff against the committed
snapshot, open an issue on change. This is the maintenance story made concrete.

**M10. Documentation**
A docs site (mkdocs-material) with a quickstart, a page on the pagination
problem and how partitioning solves it, and an API reference. Plus honest
positioning against `pysradb` and `ffq` so users pick the right tool.

**M11. Release**
PyPI via GitHub Actions trusted publishing on a version tag, then a conda-forge
recipe via `staged-recipes`. Tag `v0.1.0`.

---

## Stretch, only if the box allows

**M12. Taxon-driven discovery.** A helper wrapping `tax_tree()` with filters,
carried over from `enatrieve-tx`, which this project replaces. Explicitly
deferred: the general client has to be right first.

**M13. Hosted query builder.** A small static page that uses the introspected
field metadata to build Portal queries, for people who do not use a CLI. This,
not a paper, is what would make the project shareable to a non-computational
audience.

---

## Risks

| Risk | Mitigation |
|---|---|
| Scope creep past three weeks | Milestones are cuttable from M10 down; M12 and M13 are explicitly out |
| ENA changes the API under us | M9 exists precisely for this |
| Nobody adopts it | Accepted going in. The project is justified by being correct and maintained, not by download counts |
| Partitioning has no good key for some queries | Fall back to a single unresumable fetch with a clear warning, rather than failing |
| Abandonment | A client library with a visible abandonment date is worse than none. M9 keeps maintenance cost near zero |

## Predecessor

This replaces [`enatrieve-tx`](https://github.com/Cobos-Bioinfo/enatrieve-tx),
archived 2026-09-26. That tool answered one question (RNA-Seq runs for a taxon,
as TSV); `enaportal` subsumes it as a single call once M12 lands.
