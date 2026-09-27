# Choosing a tool

Several good tools fetch sequencing metadata and data from the public archives.
They answer different questions, and the right one depends on where you start.
This page is meant to help you pick, including when the answer is not
enaportal.

The short version: **pysradb and ffq start from an accession you already
have**, most often an NCBI or GEO one. **enaportal starts from a question** and
finds the accessions, across everything the ENA Portal API can query.

| | enaportal | pysradb | ffq |
|---|---|---|---|
| Starts from | a query, or an accession | an accession, or a search of a few fixed filters | an accession |
| Archives | ENA | NCBI SRA and GEO, with ENA for file URLs and search | SRA, ENA, DDBJ, GEO, ENCODE, BioSample |
| Record types | all 15 ENA result types: runs, samples, studies, analyses, assemblies, sequences, taxa and more | sequencing runs and their study, sample and experiment | sequencing runs and their parents |
| Query language | ENA's full query grammar, on any searchable field | a set of options: organism, strategy, platform, layout, dates and so on | none |
| Result size | any, with resumable bulk retrieval | 20 hits by default for a search | per accession |
| Output | Polars DataFrames, TSV | pandas DataFrames, TSV | JSON |
| File transfer | none; writes manifests for aria2c, curl and fetchngs | downloads | links for FTP, AWS, GCP and NCBI |
| Interface | Python library, and a command line | Python library, and a command line | command line |

## pysradb

[pysradb](https://github.com/saketkc/pysradb) is a mature, actively maintained
Python package and command line for the NCBI Sequence Read Archive and GEO.
Its metadata comes mainly from NCBI's E-utilities. It uses ENA to find FASTQ
URLs and as one of the backends of its search.

Its strengths are where enaportal has none. It converts between every kind of
accession across GEO and SRA, from a GSE to its GSMs to its SRRs and back, and
links studies to PubMed IDs, PMC articles and DOIs. It downloads files. And it
returns pandas DataFrames, if that is what the rest of your code uses.

Its ENA search covers `read_run` only, through a fixed set of options such as
organism, library strategy and publication date, and returns 20 hits unless
told otherwise.

**Use pysradb when** you start from GEO or NCBI identifiers, need to move
between them, or want publications linked to datasets.

## ffq

[ffq](https://github.com/pachterlab/ffq), from the Pachter lab, takes an
accession from almost any archive, including SRA, ENA, DDBJ, GEO, ENCODE and
BioSample, or a DOI, and returns the metadata for it and for everything below
it as JSON, following the links between archives on your behalf. It can
return download links on FTP, AWS, GCP or NCBI instead of metadata. It
deliberately stops at links and leaves the transfer to other tools. Its last
release was 0.3.1, in March 2024.

It has no search: you need the accessions first.

**Use ffq when** you have accessions from several archives, especially GEO,
and want the whole linked metadata tree in one call, or links to the cloud
copies of the data.

## enaportal

enaportal is a client for the ENA Portal and Browser APIs as ENA exposes them,
rather than a tool for one workflow.

- **Any question the Portal can answer.** All 15 result types, every
  searchable field, ENA's full query grammar, and any of the return fields. It
  reaches beyond sequencing runs to samples, analyses, assemblies, annotated
  sequences and taxa.
- **Checked before it is sent.** Field names are validated against ENA's
  current schema, read at runtime, with suggestions for a typo.
- **Result sets of any size.** The Portal API has no pagination, and a large
  query is one response that cannot be resumed. enaportal splits it into
  counted date ranges and saves each one. See
  [Bulk retrieval](guide/bulk.md).
- **A handoff, not a downloader.** It resolves files into URLs, checksums and
  sizes, and writes manifests for aria2c, curl, nf-core pipelines and
  nf-core/fetchngs.

The archives of the International Nucleotide Sequence Database Collaboration
exchange their data, so most runs submitted to NCBI's SRA are in ENA too, under
their `SRR` accessions, and enaportal can query them there. What it does not
have is anything that lives only at NCBI: GEO series and samples, and links to
publications.

**Use enaportal when** you are asking a question rather than looking up an
answer: every RNA-Seq run of a genus published since a date, every sample
from a country with a given attribute, every assembly of a taxon. Or when the
answer is too large for one download, or you want ENA's own fields as a
DataFrame.

## Other tools

- [**nf-core/fetchngs**](https://nf-co.re/fetchngs) is a Nextflow pipeline
  that downloads FASTQ files and metadata for a list of accessions from ENA,
  SRA, DDBJ or GEO, with retries and checksums, and writes a samplesheet for
  other nf-core pipelines. It is the right tool for downloading a whole
  project, and `to_manifest(..., "accessions")` writes its input.
- [**enaBrowserTools**](https://github.com/enasequence/enaBrowserTools) is
  ENA's own set of download scripts, including Aspera transfer. Use it for
  large transfers from ENA.
- [**ena-upload-cli**](https://github.com/usegalaxy-eu/ena-upload-cli)
  submits data to ENA, which enaportal does not do.
- [**enasearch**](https://github.com/bebatut/enasearch) was the earlier Python
  client for ENA's search. It was last released in 2017, and the endpoints it
  wraps have since been retired. It is not an option any more.

## Using them together

These tools compose. A few combinations that work well:

- Find runs with an enaportal query, then hand the accessions to fetchngs to
  download them:
  `enaportal search read_run --query '...' -f run_accession | enaportal manifest --table - --format accessions`.
- Start from a GEO series with pysradb or ffq to get its SRA project, then use
  enaportal to pull ENA's fields for its runs, or to find related samples.
- Resolve a study's files with enaportal and download them with aria2c, which
  verifies each file against ENA's checksum as it goes.
