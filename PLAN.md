# enaportal implementation plan

**Audience: Claude agents.** This file plus `CLAUDE.md` is the resume anchor for
a cold session. Read both before touching code. Facts recorded here were
verified against the live API; do not re-derive them by probing.

Timebox: **three weeks part-time**, from 2026-09-26. If the box is threatened,
apply the cut order at the bottom. Do not extend the box. This library is
infrastructure for a wider portfolio, not its centrepiece.

---

## Current status

| Milestone | State |
|---|---|
| M0 Scaffolding | done 2026-09-26 |
| M1 HTTP layer | done 2026-09-26 |
| M2 Introspection and cache | done 2026-09-26 |
| M3 search and count | done 2026-09-26 |
| M4 filereport, related, manifests | done 2026-09-26 |
| M5 Resumable bulk retrieval | done 2026-09-26 |
| M6 Browser API | done 2026-09-27 |
| M7 CLI | **next** |
| M8 Test suite | not started |
| M9 Schema-drift workflow | not started |
| M10 Documentation | not started |
| M11 Release | not started |

Append to the status log at the bottom when a milestone closes.

---

## Hard constraints

Violating any of these is a bug, not a judgement call.

1. **Public API is sync.** No async twins. Rationale below.
2. **Tabular output is Polars.** Never pandas.
3. **`mypy --strict` passes.** `py.typed` is shipped.
4. **Python 3.10 floor.** `from __future__ import annotations` everywhere.
5. **`uv run pytest` never touches the network.** Live tests are marked
   `@pytest.mark.live` and excluded from the default run.
6. **ENA's field metadata is never the committed source of truth.** A snapshot
   may exist as an offline fallback only. Freezing this metadata is precisely
   how the predecessor library died.
7. **No commits straight to `main`.** Branch, PR, merge.

---

## Decisions already made

Do not re-litigate these. If you think one is wrong, say so to the user rather
than quietly building the alternative.

**Sync-only public API on `httpx`.** Async helps only with many independent
requests in flight. The expensive case here is the opposite: one search for
276k rows is a single 34-second streaming response, which async does not speed
up. An async twin doubles the public surface and the test burden. `httpx` was
chosen so the twin can be added in v2 without a rewrite. Where parallelism is
genuinely needed (M5 partition fetch, many `filereport` calls) use a bounded
thread pool internally.

**Polars over pandas.** Faster at the row counts ENA returns, lighter
dependency, consistent with the user's EukaHub project.

**Introspect, never freeze.** `/results`, `/returnFields` and `/searchFields`
are read from ENA at runtime and cached on disk with a TTL. M9 diffs the live
schema against a committed snapshot and opens an issue on drift. This is the
project's central design commitment and the answer to "why will this not rot
like enasearch did".

**conda-forge, not Bioconda.** User constraint: Bioconda is blocked at some
institutions. Accepted tradeoff: no automatic BioContainers image. If a
container is later needed (a Nextflow pipeline would want one), publish to
GHCR or add a Bioconda recipe depending on the conda-forge build.

**Retry HTTP 429, and nothing else in the 4xx range.** ENA
[documents](https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access.html)
a limit of 50 requests per second and rejects the excess with 429. A 429 is ENA
throttling, not refusing, so it is the one client error worth retrying. The
window is measured per second, so backoff has a one second floor; no
`Retry-After` is documented but one is honoured and capped if it ever appears.
`ENARateLimitError` is raised once retries are exhausted, so a caller can tell
throttling apart from a real failure.

**Install order in all docs: `uv`, then conda-forge, then source.**

**`links()` is dropped. Navigation is a query, not an endpoint.** Decided
2026-09-26 after probing. M4 assumed a Portal `links` endpoint; there is none,
it 404s. Nothing is lost, because Portal rows are denormalised: every
`read_run` row already carries `experiment_accession`, `sample_accession`,
`secondary_sample_accession`, `study_accession`, `secondary_study_accession`,
`submission_accession` and `tax_id`, so
`filereport?accession=PRJEB1787&result=read_run` returns a study's runs with
their experiment and sample in one call. M4 ships a thin `related()` helper
over that instead. The milestone's own acceptance criterion never mentioned
links, which suggests it was a nice-to-have rather than a requirement.

Rejected alternatives. The Browser API does have
`/{format}/links/{study|sample|taxon}?accession=&result=`, but it returned
882 KB of XML where the Portal returns a few KB of TSV, it gives records rather
than a table, its parameters are undocumented, and it belongs to M6. The xref
service is a different feature, links out to external databases, and is
deferred to v0.2 as `xrefs()`.

The helper is not called `links()` because ENA uses that word for the external
cross references. Reusing it would send users looking in the wrong place.

**Downloading is tiered, not binary.** Decided 2026-09-27 after the user
challenged the original blanket exclusion. The original "no downloads" position
was too absolute.

| Tier | Scope | Verdict |
|---|---|---|
| 1. URL resolution | `fastq_ftp`, `fastq_md5`, `fastq_bytes`, `submitted_ftp`, `sra_ftp`; pick the right source | **v1, in M4** |
| 2. Manifest export | aria2c input file, curl script, nf-core/fetchngs samplesheet | **v1, in M4** |
| 3. Modest file transfer | HTTP with resume and MD5 verification, tens of files | **v0.2, after release** |
| 4. Bulk transfer | Aspera, Globus, HPC-aware, multi-TB | **never** |

Reasoning: getting the URLs is trivial, moving bytes reliably is not. Tier 3
done honestly costs 3 to 4 days of a roughly 15 day budget, spent on the least
differentiated feature in the library, while M5 is unbuilt. Tier 2 closes the
workflow seam for almost nothing by composing with tools that already do
transfers well. Precedent: `ffq` (Pachter lab) deliberately stops at tier 2 and
documents it; `pysradb` implements tier 3.

---

## Reference documentation

ENA has no OpenAPI spec, so these pages are the reference. Read them before
guessing, but trust the table below over them: several statements on these
pages are already contradicted by the live API, and each contradiction is
recorded as its own row.

| Page | URL |
|---|---|
| Programmatic access, index and rate limits | <https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access.html> |
| File reports, the `filereport` endpoint | <https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/file-reports.html> |
| Advanced search, the query grammar | <https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/advanced-search.html> |
| Browser API | <https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/browser-api.html> |
| Cross references | <https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/cross-reference.html> |
| Taxon API | <https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/taxon-api.html> |
| File download, FTP layout and Aspera | <https://ena-docs.readthedocs.io/en/latest/retrieval/file-download.html> |
| Portal API reference | <https://www.ebi.ac.uk/ena/portal/api/doc>, a 302 to a Google Doc that is not machine readable |
| Browser API reference | <https://www.ebi.ac.uk/ena/browser/api/doc> |

**Where the docs are wrong.** They say `filereport` accepts only `read_run` and
`analysis`, and that `limit` defaults to 100,000. Neither holds. Assume any
unverified claim on these pages may be stale.

---

## Verified API facts

Probed live 2026-09-26, the Browser API rows 2026-09-27. Recheck before
assuming any still hold, but do not re-probe as a matter of course.

| Fact | Value |
|---|---|
| Portal base | `https://www.ebi.ac.uk/ena/portal/api/` |
| Browser base | `https://www.ebi.ac.uk/ena/browser/api/` |
| Result types | 15, from `/results` |
| `read_run` return fields | 195, from `/returnFields?result=read_run` |
| `read_run` search fields | 160, from `/searchFields?result=read_run` |
| `/results` shape | `{resultId, description, primaryAccessionType, recordCount, lastUpdated}`, `recordCount` is a string |
| Field metadata shape | `{columnId, description, type}` |
| Field `type` values | `text`, `number`, `date`, `boolean`, `latlon`, `list`, `taxonomy`, `controlled value`, `indexed`. Wider than first recorded |
| Missing field `type` | **`type` is absent on 247 of 2845 fields** (40 of 195 `read_run` return fields). The type word sits in `description` instead, e.g. `{"columnId": "run_date", "description": "date"}`. Every untyped description is one of `text`, `number`, `latlon`, `boolean`, `date` |
| `/count` body | A one-column TSV with a `count` header, not a bare number |
| `filereport` accessions | **One per request.** A comma-separated list returns zero rows with HTTP 200 and no error, and a repeated `accession` parameter silently uses the first. Loop, never join |
| `filereport` accession level | Any level ENA can map to the result: study, experiment, sample or run all work for `read_run`, in either the primary or the secondary form |
| `filereport` `limit` | Accepted, and behaves as on `search` |
| File source families | Columns come in families `{prefix}_ftp`, `_md5`, `_bytes`, `_aspera`, `_galaxy`. `read_run` has `fastq`, `submitted`, `sra`, `bam`; `analysis` has `generated`, `submitted` |
| HTTPS on file paths | Works. `https://ftp.sra.ebi.ac.uk/vol1/...` serves the same path as FTP |
| nf-core/rnaseq samplesheet | Requires four columns, `sample,fastq_1,fastq_2,strandedness`, and rejects a sheet without the last. `auto` is a valid value and makes rnaseq infer it |
| nf-core/fetchngs input | A plain list of accessions, one per line. Accepts run, experiment, sample, study, GEO and BioSample identifiers |
| Result ordering | **Not guaranteed and not reproducible.** Six identical `limit=5` requests returned four different row sets, with repeats among them. Consistent with load balancing across backends that disagree, so instability cannot be asserted in a test, only relied on never |
| `read_run` date search fields | `first_created`, `first_public`, `last_updated` |
| `offset` | **Rejected**, GET and POST, body `Unsupported param offset` |
| `sortFields` | **Rejected**, HTTP 400 |
| `limit=0` | Returns everything in one response. `tax_tree(4932)` read_run: 276,447 rows, 3.1 MB, 34.6 s |
| `/count` | Cheap, accepts the full query grammar including date ranges |
| OpenAPI spec | None for the Portal. `/v3/api-docs`, `/v2/api-docs`, `/swagger.json` all 404. The Browser API **does** have one, see below |
| Rate limit | **50 requests per second**, documented, across the discovery and retrieval APIs. Excess is rejected with HTTP 429 |
| Rate-limit headers | None returned, and no documented `Retry-After` |
| Retired endpoints | `data/warehouse/search`, `data/view`, `data/warehouse/filereport` all 301 to the browser homepage |
| Omitting `limit` | Returns **everything**, exactly like `limit=0`. The docs claim a default of 100,000; the live API returned all 276,447 rows, 3.2 MB, in 20 s. There is no safe default, so an unbounded `search()` is a footgun that M5 exists to replace |
| `filereport` | `filereport?accession=&result=&fields=&format=`. **Not** limited to `read_run` and `analysis` as the docs claim: `result=sample` returns sample metadata |
| Accession and result mismatch | HTTP 400, plain text, and the body lists the accepted accession regexes for that result, e.g. `sample [ ^(SAME[A]?[0-9]{6,})\|(SAM[ND][0-9]{8})$ ]`. Let ENA report this rather than hardcoding prefix tables |
| Portal 404 body | JSON, Spring Boot shape `{timestamp, status, error, path}`. Different from the plain-text rejections served with 200 |
| `/links` on the Portal | **Does not exist.** HTTP 404. Cross references come from the Browser API or the separate xref service, see M4 |
| Multi-value file fields | `;` separated and positionally parallel across `fastq_ftp`, `fastq_md5` and `fastq_bytes`. A single-file run has no separator, and `submitted_ftp` or `sra_ftp` may be empty |
| File paths | Carry **no URL scheme**: `ftp.sra.ebi.ac.uk/vol1/fastq/ERR100/090/ERR10003190/ERR10003190_1.fastq.gz`. Prepend `ftp://` or `https://` |
| FTP directory layout | Inconsistent. Older runs are flat, `ERR164/ERR164407`, newer ones are sharded, `ERR100/090/ERR10003190`. **Never construct a path**, always use the field |
| Submitted files | Keep the submitter's own filenames under `/vol1/run/`, unrelated to the run accession |
| FTP roots | Reads and analyses `ftp://ftp.sra.ebi.ac.uk/vol1/`, assembled and annotated sequences `ftp://ftp.ebi.ac.uk/pub/databases/ena/` |
| xref service | `https://www.ebi.ac.uk/ena/xref/rest/{tsv,json}/...`, and it **does** support `offset` and `limit`. Pagination exists there but not on the Portal |
| Browser `links` | `/{format}/links/{study\|sample\|taxon}?accession=&result=`. Both parameters are required and neither is documented; omitting one gives an opaque Spring Boot 400. Returned 882 KB of XML for one study. ENA's own OpenAPI spec says it "does not produce up to date ENA data" |
| Primary and secondary accessions | One object has both forms, e.g. study `PRJEB1787` and `ERP001736`. The Portal exposes both as separate columns, so anything taking an accession must accept either |
| Date ranges are **half-open** | `f>=A AND f<=B` selects `A <= f < B`. Probed 2026-09-26: 2020 whole year 15,070, `[Jan1,Jun30)` 7,905, `[Jul1,Dec31)` 7,089, which do not sum; `[Jan1,Jul1)` 7,981 and `[Jul1,Jan1)` 7,089 do. This is a gift, not a trap: adjacent partitions sharing a boundary tile a range exactly, with no day arithmetic |
| Date operators | `<` and `<=` are the same operator, and so are `>` and `>=`. `f=2020-06-30` returns **0** even for rows displaying that exact value, so equality on a date is useless |
| Finest date range | One day, `[D, D+1)`. `f>=D AND f<=D` is empty, not a single day |
| `AND` binds tighter than `OR` | Appending a range to `a OR b` restricts only `b`. Unparenthesised, one probe returned 8,596,605 rows where the bracketed form returned 59,553. **Always parenthesise a caller's query before composing onto it** |
| `NOT` | An exact set complement, including rows the inner clause cannot reach. `q AND NOT (range)` plus `q AND range` equals `q`, verified at 261,377 + 15,070 = 276,447 |
| Date search field coverage | Every result type has one except **`taxon`**, which has 13 search fields and no date, no orderable number. `assembly` has only `last_updated` |
| Browser OpenAPI | `browser/api/v3/api-docs`, OpenAPI 3, version 1.1. Lists far more than records and text search: `livelist`, `changelog`, `versions`, `summary`, `gff3`, `ebisearch` |
| Browser batch fetch | `POST /{xml,embl,fasta}` with a JSON body `{"accessions": [...]}` and options such as `annotationOnly` and `lineLimit`. Documented ceiling of 10,000 accessions per request. A comma-joined GET path, `/xml/A,B`, also works |
| Browser unknown accessions | **Silently dropped from a batch**: HTTP 200 without them. A batch with none found, or a single unknown accession, is a 404. A repeated accession is returned once per mention |
| Browser data types | One per request. Mixing is a 400: `All accessions must be of the same data type as the first accession, which was PROJECT.` |
| Browser errors | Real status codes, unlike the Portal: 400 for a malformed accession or a format the record lacks, 404 for an unknown one. The body is Spring Boot ErrorDetails serialised to match the request: XML on `/xml`, `key=value` lines on `/embl` and `/fasta`, JSON on a batch POST. Its `message` is the useful part. GET `/fasta/{run}` names the wrong format in it, "Format embl is not available"; POST names the right one |
| Browser XML layout | One `<X_SET>` root per response, one child per record. Some types open with an XML declaration (taxon, sample) and some do not (run, study, project). Indentation is inconsistent, sample XML puts nested tags at column 0, and a taxon nests `<taxon>` elements inside its `<lineage>`. Count records by parsing, never by line shape |
| Primary and secondary in the Browser | **Not interchangeable.** `PRJEB1787` returns a `PROJECT_SET`, its secondary `ERP001736` a `STUDY_SET` |
| Browser record size | Unbounded. `/fasta/GCA_000146045.2` streams a whole yeast assembly; a human one would be gigabytes |
| Browser `lineLimit` | Applies **per record**, not per response. `annotationOnly=true` drops the `SQ` block and keeps the `//` terminator |
| Text search | `/{tsv,xml,embl,fasta}/textsearch?query=&result=&limit=&offset=`. `query` is a **query parameter**; the path form `/xml/textsearch/{query}` 404s. `result`, a Portal result name, or an EBI Search `domain` is required. `offset` works |
| Text search TSV | Two columns, `accession` and `description`, **quoted** CSV style with inner quotes doubled. Every Portal TSV is unquoted |
| Text search `limit` | Omitted returns every hit. **`limit=0` returns none**, the opposite of the Portal |
| Text search errors | HTTP 200 with a body of `<error>message</error>`, whatever format was asked for |
| Text search count | `/{format}/textsearch/count?query=&result=` returns JSON `{"count": "48"}`, a string. The format segment matters: `xml` rejects `result=sequence`, `tsv` accepts every result |
| Streams can be cut short | `/tsv/textsearch?query=Saccharomyces&result=sequence`, 1,248,884 hits, aborted at the same byte, 220,686, with and without `limit=5000`: HTTP 200 and then a truncated chunked body. httpx raises `RemoteProtocolError` from inside the line iterator, after the request has returned |
| Denormalised rows | A `read_run` row carries `experiment_accession`, `sample_accession`, `secondary_sample_accession`, `study_accession`, `secondary_study_accession`, `submission_accession`, `tax_id`. `analysis` adds `related_analysis_accession`, `sample` adds `related_sample_accession`. Navigation needs no extra endpoint |

**The three facts that shape the architecture:** no `offset` and no
`sortFields` means there is no cursor, so a large result set cannot be resumed
by any built-in mechanism. M5 exists to solve that. Unstable ordering closes the
last workaround: you cannot page, diff or reproducibly sample on top of `limit`
alone, so M5 must partition by query range and never by row position. And ENA
returns some errors as plain text with **HTTP 200**, so status-code checking
alone is insufficient; body sniffing belongs in the HTTP layer (M1), not bolted
on later.

**The fact that made M5 work:** date ranges are half-open. Partitions that
share a boundary tile exactly, so one child of a bisection can be counted and
the other taken as the difference. That is what keeps the plan cheap.

---

## Milestones

Each is a branch merged to `main` by PR with green CI. Dependencies are listed
because they determine what can be parallelised and what cannot.

### M1. HTTP layer
*Depends on: nothing. Next up.*

Module `enaportal/_http.py`. A wrapper over `httpx.Client` providing:
- Configurable timeouts and a descriptive User-Agent identifying the library
  and version.
- Bounded retries with exponential backoff on 5xx and connection errors. No
  retry on 4xx.
- Error hierarchy: `ENAError` base, then `ENAHTTPError`, `ENAQueryError`,
  `ENATimeoutError`.
- **Text-error detection.** ENA returns messages like `Unsupported param
  offset` with HTTP 200. Sniff the body and raise `ENAQueryError` rather than
  handing garbage to a TSV parser.
- Streaming response support, needed by M3 and M5.

*Done when:* `respx`-backed unit tests cover retry, no-retry-on-4xx, timeout,
and the HTTP-200-text-error path.

### M2. Introspection and cache
*Depends on: M1.*

Module `enaportal/schema.py`. `results()`, `return_fields(result)`,
`search_fields(result)` returning typed dataclasses built from the
`{columnId, description, type}` shape. On-disk JSON cache in the platform cache
directory with a TTL and an explicit `refresh()`. Falls back to a committed
snapshot when offline, so the library degrades instead of failing.

*Done when:* works fully offline from cache; one `-m live` test confirms the
live shapes still match.

### M3. search and count
*Depends on: M1, M2.*

`search()` returning a Polars DataFrame, and `count()`. Validates `result` and
all field names against M2 **before sending**, so a typo fails locally with a
useful message instead of returning an ENA error page. Supports TSV and JSON
response formats, `limit`, and field selection.

*Done when:* the three-line README example runs against the live API.

### M4. filereport, related, and manifests
*Depends on: M3.*

- `filereport()`: the accession-oriented endpoint most users actually want.
  First-class ergonomics, this is the common path.
- `related()`: navigation between studies, samples, experiments, runs and
  analyses. A thin wrapper over a single `filereport` call, not a new endpoint,
  because the rows are already denormalised. Must accept either accession form,
  primary or secondary. Deliberately **not** named `links()`. See the decision
  above; `links()` is dropped and is not to be reintroduced.
- **Tier 1 URL resolution:** expose `fastq_ftp`, `fastq_md5`, `fastq_bytes`,
  `submitted_ftp`, `sra_ftp` cleanly, with a helper to choose a source and
  handle runs where generated FASTQs do not exist and only submitted files do.
- **Tier 2 manifest export:** `to_manifest(fmt=...)` emitting an aria2c input
  file, a curl script, an nf-core samplesheet of URLs, or a plain accession
  list for `nf-core/fetchngs --input`. This is the handoff to real downloaders
  and it is what makes tier 3 optional.

*Done when:* `related()` turns a study accession into its runs in one call,
and a search result can be turned into a manifest that aria2c accepts
and that fetchngs parses, proven by fixture tests on the emitted text.

### M5. Resumable bulk retrieval
*Depends on: M3. **This is the milestone the project stands on.** Cut anything
else before cutting this.*

For a query whose `/count` exceeds a threshold:
1. Choose a partition field, preferring a `date`-typed search field.
   `first_public` is the natural default.
2. Bisect the range using `/count` until every partition is under the
   threshold. `/count` is cheap so this costs little.
3. Fetch partitions with bounded concurrency, writing each to a checkpoint.
   The pool must stay under ENA's 50 requests per second. Bisection makes this
   easy to breach by accident, because `/count` is fast and the bisect loop is
   tight, so budget the whole client and not just the fetch stage.
4. On resume, skip partitions already checkpointed.

Fallback when no usable partition key exists: a single unresumable fetch with a
loud warning. Never fail outright.

*Done when:* a 276k-row query can be killed mid-run and resumed without
refetching completed partitions, proven by a test that does exactly that.

### M6. Browser API
*Depends on: M1.*

Module `enaportal/browser.py`. Records by accession as XML, EMBL or FASTA.
Smaller surface than the Portal side, kept separate.

Paths are all `/{format}/...` under `https://www.ebi.ac.uk/ena/browser/api/`:
`/{format}/{accession}`, `/{format}/textsearch?query=`, `/{format}/search`
and `/{format}/links/{study|sample|taxon}`. XML covers study, sample, run,
experiment, analysis and taxon; EMBL covers sequences, WGS and TSA sets; FASTA
covers sequences and assemblies.

Shipped as `fetch()`, `fetch_to_file()`, `textsearch()` and
`textsearch_count()`. `links` and `search` are deliberately not wrapped; see
the status log.

### M7. CLI
*Depends on: M3, M4, M6.*

Thin `argparse` layer over the public API. Subcommands mirror the library:
`search`, `count`, `filereport`, `related`, `fields`, `results`, and from M6
`fetch` and `textsearch`. TSV to stdout by
default so it pipes. **Re-add the `[project.scripts]` entry point**, which was
removed in M0 because the module did not exist and a broken console script is
worse than none.

### M8. Test suite
*Depends on: M1 to M7.*

Recorded HTTP fixtures for the offline suite, a small set of `-m live` contract
tests. Concentrate coverage on query building and partitioning, which is where
the bugs will be. Transport plumbing needs less.

### M9. Schema-drift workflow
*Depends on: M2.*

Weekly scheduled job: fetch the live schema, diff against the committed
snapshot, open an issue on change. This is the maintenance story made concrete
and it is what keeps long-term maintenance cost near zero.

### M10. Documentation
*Depends on: M1 to M7.*

mkdocs-material. Quickstart, a page explaining the missing-pagination problem
and how partitioning solves it, API reference, and honest positioning against
`pysradb` and `ffq` so users pick the right tool.

### M11. Release
*Depends on: everything.*

PyPI via GitHub Actions trusted publishing on a version tag, then a conda-forge
recipe through `staged-recipes`. Tag `v0.1.0`.

---

## v0.2, after release

- **Tier 3 downloader.** `download()` over HTTP with resume (range requests),
  per-file MD5 verification against `fastq_md5`, retry with backoff, bounded
  parallelism. Scoped and documented as suitable for tens of files. README
  points at enaBrowserTools and nf-core/fetchngs for serious transfers.
- **`xrefs()` over the cross reference service.** External database links for
  an accession, from
  `https://www.ebi.ac.uk/ena/xref/rest/{tsv,json}/search?accession=`, returning
  rows such as EuropePMC and MGnify. Genuinely useful but additive: a third
  base URL, a third client, and `offset` pagination the Portal does not have.
  Perhaps half a day, which is not worth spending while M5 is unbuilt.
- **M12 taxon-driven discovery.** A helper wrapping `tax_tree()` with filters,
  carried over from the archived `enatrieve-tx`. Deferred on user instruction:
  the general client has to be right first.
- **M13 hosted query builder.** A static page using the introspected field
  metadata to build Portal queries, for people who do not use a CLI. This, not
  an academic paper, is what would make the project reachable by a
  non-computational audience.

---

## Cut order

If the three-week box is threatened, cut in this order:

1. M10 documentation, down to a good README only
2. M6 Browser API
3. M7 CLI, down to `search` and `filereport` subcommands only
4. M4 tier 2 manifests

**Never cut M5.** The library's value is concentrated in introspection, local
validation, and resumable bulk retrieval. Without M5 this collapses into a thin
requests wrapper and most of its justification goes with it.

---

## Explicitly rejected

Do not add these, and do not suggest them as improvements.

| Rejected | Why |
|---|---|
| Async public API | See decisions |
| A `links()` endpoint wrapper | No such Portal endpoint, and navigation is already in the denormalised rows. See decisions |
| pandas | See constraints |
| Bulk or Aspera transfer | Tier 4, out permanently |
| Data submission | `ena-upload-cli` owns it |
| A JOSS paper for v1 | JOSS desk-rejects thin API wrappers; revisit only if real adoption appears |
| Pitching this as "modernising enasearch" | enasearch's endpoints no longer exist and it has ~59 downloads/month. The honest and stronger pitch is the missing general-purpose ENA Portal client |

---

## Predecessor

Replaces [`enatrieve-tx`](https://github.com/Cobos-Bioinfo/enatrieve-tx),
archived 2026-09-26. That tool answered one question (RNA-Seq runs for a taxon,
as TSV). `enaportal` subsumes it once M12 lands.

---

## Status log

Append one entry per closed milestone: date, what shipped, and anything
surprising that a later session would otherwise rediscover the hard way.

- **2026-09-27, M6.** `browser.py` with `BrowserClient.fetch()`,
  `fetch_to_file()`, `textsearch()` and `textsearch_count()`, and the first,
  third and fourth as module-level helpers. Records come back as text; parsing
  XML or EMBL is left to whatever tool the caller already uses.
  Unlike the Portal, the Browser API publishes an OpenAPI spec, and it lists far
  more than M6 planned for. Scope was held to what the milestone named. Not
  wrapped: `links`, which ENA's own spec says does not produce up to date data
  and which `related()` already covers; `/{format}/search`, a Portal query that
  returns records, whose FASTA form takes only two result types; and
  `livelist`, `changelog`, `versions`, `summary`, `gff3` and `ebisearch`, which
  nothing in the plan asked for.
  The trap is M4's again, silent loss. A batch drops accessions ENA cannot find
  and still answers 200. Looping is not the fix this time, because one request
  per accession turns 10,000 records into 400 seconds at the rate limit. So
  `fetch` counts records as they stream past, warns when fewer come back than
  were asked for, and raises `ENANotFoundError` when none do. XML is counted
  with a pull parser, not by line shape, because the layout varies by record
  type and a taxon nests `<taxon>` inside its own lineage. EMBL and FASTA are
  counted where a record starts, not at its `//`, so `line_limit` truncation
  still counts. Repeats are dropped before sending: ENA returns a record once
  per mention, and a duplicate would hide a missing one.
  Over 10,000 accessions go out in batches, and XML batches are merged into one
  document, since each is its own `<X_SET>` and concatenation would give several
  roots. The merge runs only when there is more than one batch, so the common
  case passes ENA's bytes through untouched and a layout change can break only
  the rare case, loudly. `fetch_to_file` writes to a temporary file and renames
  it into place, because a truncated FASTA looks exactly like a complete one.
  Two holes in M1 surfaced and were fixed there. A body cut short mid-stream
  escaped as a raw `httpx.RemoteProtocolError`, raised from inside the line
  iterator long after `_send` returned; `search_to_file` and bulk partitions
  were exposed to it too. It now raises `ENAConnectionError`, unretried, since
  lines have already been handed on. And 4xx bodies were reported verbatim,
  which for the Browser meant a whole ErrorDetails document in three
  serialisations, so the `message` is now extracted. 404 became
  `ENANotFoundError`, a subclass of `ENAHTTPError`, so code that caught the old
  type still works.
  Text search differs from the Portal in three ways that would each have
  bitten: its TSV is quoted, `limit=0` means none rather than all, and its
  rejections are `<error>` elements served with HTTP 200. `textsearch` defaults
  to 100 hits and refuses 0.

- **2026-09-26, M5.** `bulk.py` with `plan_partitions()` and `bulk_search()` on
  `PortalClient`, over `_checkpoint.py` for the on-disk state and a new
  `search_to_file()` that streams a query to a file without building a frame.
  A 276,447 row query plans into 8 partitions in 1.2 s, fetches in 17 s at
  concurrency 4, and survives being killed: re-running it refetched nothing
  that had landed and returned 276,447 unique run accessions. That is faster
  than the 34.6 s recorded for the same query as one unresumable response, so
  partitioning costs nothing even when nothing goes wrong.
  The discovery that made it cheap: **ENA date ranges are half-open.**
  `f>=A AND f<=B` means `A <= f < B`. The first probe looked like data loss,
  a year of 15,070 splitting into halves of 7,905 and 7,089, and it took a
  boundary-day query returning 0 to see that both bounds behave as `<`. It
  turns the design around. Partitions sharing a boundary tile a range exactly,
  so a bisection can count one child and take the other as the difference,
  halving the requests a plan costs. Had the range been closed, every boundary
  day would have been fetched twice and every split would have needed day
  arithmetic to avoid it.
  The trap that nearly shipped: **`AND` binds tighter than `OR`.** Composing a
  range onto `a OR b` restricts only `b`. One probe returned 8,596,605 rows
  unparenthesised where the bracketed form returned 59,553, so `range_query`
  wraps the caller's query and a test pins it.
  Two silent failures are now loud instead. Rows a date range cannot reach
  would simply be missing, so the plan compares `/count` of the query against
  `/count` of the range; `NOT` turned out to be an exact set complement, so
  the difference is fetched as a residual partition rather than only reported.
  And a single day is the finest range ENA can express, so a day holding more
  than the threshold is emitted oversized with a warning rather than bisected
  forever.
  Checkpoint state is the part files themselves, not a field in the manifest:
  each is written to a temporary file and renamed into place only once
  complete, so presence means finished and no lock is needed across the
  thread pool. The manifest stores the plan and is keyed by job identity with
  the counts deliberately excluded, because counts move as ENA grows and a
  resume has to recognise yesterday's job rather than re-bisect it into a
  different shape.
  The default checkpoint directory is removed on success and an explicit one
  is kept. Keeping both would have made `bulk_search` a silent cache of stale
  rows, which is the opposite of what this library is for.
  Rate limiting moved into `_http.py` rather than the fetch stage, as the
  milestone required: the bisect loop is a tight run of cheap `/count` calls
  and is the easiest way to breach 50 requests per second by accident. The
  limiter spaces requests instead of using a token bucket, because a full
  bucket plus a refill puts twice the rate into a window straddling the two.
  It defaults to half the documented limit, since the budget is per source
  address and the client cannot see what else is sharing it.
  `taxon` is the only result type with no date search field, and it has no
  orderable number either, so the plan's documented fallback of one
  unresumable fetch with a loud warning is genuinely the right answer rather
  than a placeholder.

- **2026-09-26, M4.** `filereport()` and `related()` on `PortalClient`, plus
  `files.py` with `file_urls()` for tier 1 and `to_manifest()` for tier 2 in
  aria2c, curl and nf-core samplesheet form.
  The trap: `filereport` takes **one** accession. A comma-separated list comes
  back as HTTP 200 with zero rows and no error, and a repeated parameter uses
  only the first, so a sequence is sent as one request each and stacked.
  Joining would have silently lost data. Rewriting multi-accession into a
  `search` OR query was rejected: it only works when every accession is of the
  result's own type, and filereport's whole value is accepting any level.
  File handling generalised better than the plan assumed. Sources are families
  keyed by prefix, so `bam` and `generated` fell out for free alongside
  `fastq`, `submitted` and `sra`. `source="auto"` falls back per row, which is
  what makes runs with no generated FASTQ resolve to their submitted files.
  `md5` and `bytes` are read positionally, and a short or missing list yields
  null rather than another file's checksum.
  Four manifest formats, not three. "fetchngs-compatible samplesheet" in the
  plan described two different artefacts: fetchngs' **input**, a plain
  accession list, and its **output**, a samplesheet of URLs for a downstream
  pipeline. They close different seams, so both ship. nf-core/rnaseq turned
  out to require a fourth `strandedness` column and rejects a sheet without
  one, so `pipeline="rnaseq"` adds it set to `auto`, mirroring fetchngs' own
  `--nf_core_pipeline`. A three-column sheet alone would not have worked with
  the most likely downstream target.
  Acceptance was by fixture tests on the emitted text as the plan specified,
  since neither aria2c nor nextflow is installed here. A live test closes the
  gap fixtures cannot: it resolves a real run and checks the URL returns 200
  with a `content-length` equal to `fastq_bytes`.
  Also removed the ordering test added in M3. Six identical requests returned
  four different row sets **with repeats**, so the instability is real but not
  reproducible and cannot be asserted. The fact is documentation, not a test.

- **2026-09-26, M3.** `portal.py` with `PortalClient.search()` returning a
  Polars DataFrame and `count()`. Result types, return fields and query field
  names are all validated against M2 before anything is sent, with `difflib`
  suggestions on a typo. Module-level `search`/`count`/`results` helpers in
  `_api.py` over one lazily created client.
  Three things worth knowing. `/count` returns a one-column TSV with a `count`
  header, not the bare number the name suggests. Search results are **not
  stably ordered**, which is now a recorded fact and a live test: it rules out
  comparing rows between two runs and reinforces M5's range partitioning. And
  every column is returned as a Polars string on purpose, because ENA packs
  multiple values into one cell with semicolons and inferred dtypes would
  otherwise change from query to query.
  Also fixed an M0 hole: `pytest` was not excluding `live` at all. The marker
  was registered but `addopts` had no `-m 'not live'`, so the constraint was
  documentation only. `tests/conftest.py` now also refuses sockets in unmarked
  tests, so the offline guarantee is enforced rather than remembered.

- **2026-09-26, M2.** `schema.py` with `results()`, `return_fields()`,
  `search_fields()` and typed `Result`/`Field` dataclasses, over a TTL'd JSON
  cache in the platform cache directory (`_cache.py`, no new dependency).
  Lookups fall back memory, fresh cache, ENA, stale cache, packaged snapshot,
  warning on each step past ENA. `scripts/update_snapshot.py` regenerates the
  404 KB snapshot of all 15 result types and has a `--check` mode for M9.
  The surprise: ENA **omits** `type` on 247 fields and puts the type word in
  `description` instead. Taking the recorded shape at face value would have
  silently dropped a date field, which M5 partitions on, so `Field.from_payload`
  recovers it. The recorded set of type values was also too narrow.

- **2026-09-26, M1.** `_http.py` over `httpx.Client`: retries with jittered
  exponential backoff on connection errors and 5xx, never on 4xx, streaming
  line reads, and body sniffing for ENA's HTTP 200 text rejections. HTTP 400
  raises `ENAQueryError` rather than `ENAHTTPError`, so callers catch one type
  for "the query was wrong" however ENA chose to report it. Added
  `ENAConnectionError` and `ENASchemaError` to the hierarchy in the plan.
  Read timeouts are deliberately **not** retried: a slow response means ENA is
  still working, and retrying a 35 second query only adds load.
  HTTP 429 **is** retried, as the single exception to no-retry-on-4xx, once
  ENA's documented 50 requests per second limit came to light. The original
  "no retry on 4xx" line would have made M5's concurrent partition fetch fail
  hard on a transient throttle.

- **2026-09-26, M0.** Repo, licence, uv project, ruff, `mypy --strict`, pytest
  with the `live` marker, CI across Python 3.10 to 3.13, build check. CI green
  on first push. Removed the `[project.scripts]` entry point from
  `pyproject.toml` because it pointed at an unwritten `enaportal.cli:main`;
  M7 restores it.
