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
| M1 HTTP layer | **next** |
| M2 Introspection and cache | not started |
| M3 search and count | not started |
| M4 filereport, links, manifests | not started |
| M5 Resumable bulk retrieval | not started |
| M6 Browser API | not started |
| M7 CLI | not started |
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

**Install order in all docs: `uv`, then conda-forge, then source.**

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

## Verified API facts

Probed live 2026-09-26. Recheck before assuming any still hold, but do not
re-probe as a matter of course.

| Fact | Value |
|---|---|
| Portal base | `https://www.ebi.ac.uk/ena/portal/api/` |
| Browser base | `https://www.ebi.ac.uk/ena/browser/api/` |
| Result types | 15, from `/results` |
| `read_run` return fields | 195, from `/returnFields?result=read_run` |
| `read_run` search fields | 160, from `/searchFields?result=read_run` |
| Field metadata shape | `{columnId, description, type}`, type in `text` \| `number` \| `date` |
| `offset` | **Rejected**, GET and POST, body `Unsupported param offset` |
| `sortFields` | **Rejected**, HTTP 400 |
| `limit=0` | Returns everything in one response. `tax_tree(4932)` read_run: 276,447 rows, 3.1 MB, 34.6 s |
| `/count` | Cheap, accepts the full query grammar including date ranges |
| OpenAPI spec | None. `/v3/api-docs`, `/v2/api-docs`, `/swagger.json` all 404 |
| Rate-limit headers | None returned |
| Retired endpoints | `data/warehouse/search`, `data/view`, `data/warehouse/filereport` all 301 to the browser homepage |

**The two facts that shape the architecture:** no `offset` and no `sortFields`
means there is no cursor, so a large result set cannot be resumed by any
built-in mechanism. M5 exists to solve that. And ENA returns some errors as
plain text with **HTTP 200**, so status-code checking alone is insufficient;
body sniffing belongs in the HTTP layer (M1), not bolted on later.

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

### M4. filereport, links, and manifests
*Depends on: M3.*

- `filereport()`: the accession-oriented endpoint most users actually want.
  First-class ergonomics, this is the common path.
- `links()`: cross-references between studies, samples, runs, analyses.
- **Tier 1 URL resolution:** expose `fastq_ftp`, `fastq_md5`, `fastq_bytes`,
  `submitted_ftp`, `sra_ftp` cleanly, with a helper to choose a source and
  handle runs where generated FASTQs do not exist and only submitted files do.
- **Tier 2 manifest export:** `to_manifest(fmt=...)` emitting an aria2c input
  file, a curl script, or an nf-core/fetchngs-compatible samplesheet. This is
  the handoff to real downloaders and it is what makes tier 3 optional.

*Done when:* a search result can be turned into a manifest that aria2c accepts
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
4. On resume, skip partitions already checkpointed.

Fallback when no usable partition key exists: a single unresumable fetch with a
loud warning. Never fail outright.

*Done when:* a 276k-row query can be killed mid-run and resumed without
refetching completed partitions, proven by a test that does exactly that.

### M6. Browser API
*Depends on: M1.*

Module `enaportal/browser.py`. Records by accession as XML, EMBL or FASTA.
Smaller surface than the Portal side, kept separate.

### M7. CLI
*Depends on: M3, M4, M6.*

Thin `argparse` layer over the public API. Subcommands mirror the library:
`search`, `count`, `filereport`, `links`, `fields`, `results`. TSV to stdout by
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

- **2026-09-26, M0.** Repo, licence, uv project, ruff, `mypy --strict`, pytest
  with the `live` marker, CI across Python 3.10 to 3.13, build check. CI green
  on first push. Removed the `[project.scripts]` entry point from
  `pyproject.toml` because it pointed at an unwritten `enaportal.cli:main`;
  M7 restores it.
