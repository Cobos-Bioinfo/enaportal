# Files and manifests

Most people come to ENA for files: the reads behind a study, the analyses
behind a sample. enaportal finds them, resolves them into URLs with checksums
and sizes, and writes them out for a tool that does transfers properly. It
does not download them itself.

## filereport

`filereport` is the accession-first route into the Portal API. Give it any
accession ENA can map to the result type, and it returns the matching rows:

```python
runs = enaportal.filereport(
    "PRJEB1787", fields=["run_accession", "fastq_ftp", "fastq_md5", "fastq_bytes"]
)
```

For `read_run`, the default, the accession can be a study, experiment, sample
or run, in either its primary or its secondary form: `PRJEB1787` and
`ERP001736` name the same study and return the same runs. `result` switches to
another type, such as `analysis`, and works for more than the documentation
suggests: `result="sample"` returns sample metadata.

A list of accessions is accepted, and sent as **one request per accession**.
ENA takes exactly one per request: a comma-separated list comes back with zero
rows and no error, and a repeated parameter silently uses only the first. The
frames are stacked, so a long list costs one request each at the client's rate
limit. When every accession is of the result's own type, such as a list of
runs for `read_run`, a `search` with an `OR` query does it in one request.

## related

`related` navigates from one object to the objects of another type connected to
it:

```python
enaportal.related("PRJEB1787")  # the study's 249 runs
enaportal.related("SAMEA2620995", to="analysis")  # the analyses of a sample
```

The first returns one row per run with eight columns: `run_accession`,
`experiment_accession`, `sample_accession`, `secondary_sample_accession`,
`secondary_study_accession`, `study_accession`, `submission_accession` and
`tax_id`.

There is no links endpoint on the Portal API, and none is needed. Portal rows
are denormalised: every `read_run` row already carries its experiment, sample,
study and submission accessions, and its taxon. So `related` is one
`filereport` call that asks for exactly those columns. Pass `fields` to choose
other columns instead.

It is not called `links` on purpose: in ENA that word means cross references
to external databases, a different service.

## File URLs

ENA's file columns are awkward to use directly. A row's files share one cell,
separated by semicolons, with their checksums and sizes in parallel cells in
the same order. Paths have no URL scheme. And a run may have generated FASTQ
files, only the files its submitter sent, or both.

`file_urls` unpacks all of that into one row per file. For a paired run:

```python
runs = enaportal.filereport(
    "ERR315859", fields=["run_accession", "fastq_ftp", "fastq_md5", "fastq_bytes"]
)
files = enaportal.file_urls(runs)  # two rows, one per mate
files.row(0, named=True)
```

```text
{'accession': 'ERR315859',
 'source': 'fastq',
 'file_index': 0,
 'filename': 'ERR315859_1.fastq.gz',
 'url': 'https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR315/ERR315859/ERR315859_1.fastq.gz',
 'md5': '5bed3016d7a3f0cc82426ae96f93a86c',
 'bytes': 3277487044}
```

`bytes` is an integer column and `file_index` the file's position in the row;
every other column is a string.

A checksum or size that is missing, or a list shorter than the list of paths,
gives null rather than another file's value.

**Sources.** File columns come in families named by prefix: `fastq_ftp`,
`fastq_md5` and `fastq_bytes`, then `submitted_*`, `sra_*` and `bam_*` on runs,
and `generated_*` and `submitted_*` on analyses. With `source="auto"`, the
default, each row takes the first family it has files in, in the order
`fastq`, `generated`, `submitted`, `sra`, `bam`. That is what makes a run with
no generated FASTQ come out with its submitted files instead of nothing. Pass
`source="submitted"` or another family to insist on one.

The frame you pass needs an accession column and the `_ftp` column of at least
one family, plus the `_md5` and `_bytes` columns if you want checksums and
sizes. Ask for them in `fields`.

**Protocol.** URLs use HTTPS by default, which serves the same paths as FTP.
`protocol="ftp"` gives `ftp://` URLs.

Never build a file path from an accession. ENA's directory layout differs
between older and newer runs, and submitted files keep the submitter's own
names. Always take the path from the field.

## Manifests

`to_manifest` renders files as input for a downloader. It takes a frame from
`search` or `filereport`, resolving it with `file_urls` first, or a frame
`file_urls` already returned, and accepts the same `source` and `protocol`.
The examples below use the paired run from the previous section.

### aria2c

An [aria2c](https://aria2.github.io/) input file, with each file's checksum so
aria2c verifies what it downloads:

```python
open("files.txt", "w").write(enaportal.to_manifest(runs, "aria2c", directory="/data"))
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

```bash
aria2c -i files.txt -j 4
```

### curl

A shell script that needs nothing but curl. Each download resumes if the
script is run again, and the checksum is left as a comment:

```text
#!/bin/sh
set -eu
# md5 5bed3016d7a3f0cc82426ae96f93a86c
curl -fL -C - --create-dirs -o ERR315859_1.fastq.gz https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR315/ERR315859/ERR315859_1.fastq.gz
# md5 3842517e58daefeb7473176e74d93344
curl -fL -C - --create-dirs -o ERR315859_2.fastq.gz https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR315/ERR315859/ERR315859_2.fastq.gz
```

### nf-core samplesheet

A samplesheet of URLs for an nf-core pipeline, one row per run, with the two
mates of a paired run in `fastq_1` and `fastq_2`:

```text
sample,fastq_1,fastq_2
ERR315859,https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR315/ERR315859/ERR315859_1.fastq.gz,https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR315/ERR315859/ERR315859_2.fastq.gz
```

[nf-core/rnaseq](https://nf-co.re/rnaseq) also requires a `strandedness`
column and rejects a samplesheet without one. `pipeline="rnaseq"` adds it, set
to `auto` so that the pipeline infers it:

```python
enaportal.to_manifest(runs, "nf-core", pipeline="rnaseq")
```

```text
sample,fastq_1,fastq_2,strandedness
ERR315859,https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR315/ERR315859/ERR315859_1.fastq.gz,https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR315/ERR315859/ERR315859_2.fastq.gz,auto
```

When a paired run also carries an unpaired file, the numbered `_1` and `_2`
files are used.

### Accession list

One accession per line, deduplicated, which is what
[nf-core/fetchngs](https://nf-co.re/fetchngs) takes as `--input`:

```text
ERR315859
```

This one hands over the whole job rather than a list of URLs. fetchngs does
the downloading, the retries and the metadata itself, and is the better choice
for anything large.

## Which to use

| You want | Use |
|---|---|
| Tens of files, verified, from a laptop or a server | `aria2c` |
| No dependencies beyond a shell | `curl` |
| URLs to feed an nf-core pipeline directly | `nf-core` |
| A reproducible, pipeline-managed download of a whole project | `accessions`, with fetchngs |
| Terabytes, Aspera, or an HPC transfer node | [enaBrowserTools](https://github.com/enasequence/enaBrowserTools), or fetchngs |

## From a shell

`enaportal manifest` asks `filereport` for exactly the columns a manifest
needs, checksums included, and writes the manifest to stdout:

```bash
enaportal manifest PRJEB1787 > files.txt
aria2c -i files.txt
```

With `--table` it builds the manifest from a TSV that another command already
wrote, with no further requests:

```bash
enaportal search read_run --query 'tax_tree(4932)' -f run_accession,fastq_ftp,fastq_md5 \
  | enaportal manifest --table - --format nf-core > samplesheet.csv
```

## Why no downloader

Resolving URLs is simple. Moving large files reliably is not: resuming,
retrying, verifying and saturating a link without overwhelming the server, on
every platform, is a project of its own, and aria2c, fetchngs and
enaBrowserTools already do it well. A modest HTTP downloader with resume and
checksum verification, meant for tens of files, is planned for v0.2. Bulk and
Aspera transfer will stay out of scope.
