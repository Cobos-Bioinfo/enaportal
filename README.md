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

## Scope

In scope: the Portal API (`search`, `count`, `filereport`, `links`) and the
Browser API (records by accession as XML, EMBL or FASTA).

Out of scope, deliberately: bulk FASTQ downloading, which
[enaBrowserTools](https://github.com/enasequence/enaBrowserTools) and
[nf-core/fetchngs](https://nf-co.re/fetchngs) already do well, and data
submission, which [ena-upload-cli](https://github.com/usegalaxy-eu/ena-upload-cli)
covers.

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
uv sync              # create the environment
uv run ruff check .  # lint
uv run pytest        # unit tests (no network)
uv run pytest -m live  # tests that hit the live ENA API
```

## Licence

MIT. See [LICENSE](LICENSE).

`enaportal` is an independent project and is not affiliated with or endorsed by
EMBL-EBI.
