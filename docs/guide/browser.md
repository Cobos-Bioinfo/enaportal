# Records and text search

The Portal API returns tables of fields. The Browser API returns the records
themselves, and offers a free-text search the Portal cannot do. enaportal wraps
both in `BrowserClient`, with `fetch`, `textsearch` and `textsearch_count` also
available as module-level functions.

## Fetching records

```python
enaportal.fetch("PRJEB1787")  # one <PROJECT_SET> document
enaportal.fetch(["ERR164407", "ERR164408"])  # one request, one <RUN_SET>
enaportal.fetch("A00145", format="fasta")
```

Records come back as text, exactly as ENA sends them. Which formats a record
has depends on what it is:

| Format | Records |
|---|---|
| `xml`, the default | studies and projects, samples, experiments, runs, analyses, taxa |
| `embl` | sequences, and WGS and TSA sets |
| `fasta` | sequences, and whole assemblies |

Asking for a format a record does not have raises `ENAQueryError` with ENA's
explanation.

**Many accessions, one request.** A list of accessions goes out as a single
batch request, so fetching the XML of a study's 249 runs costs one request
rather than 249. ENA accepts up to 10,000 accessions per request; a longer
list is split into batches, and XML batches are merged into one document with
a single root.

**One kind of record per call.** ENA requires every accession in a call to be
of the same data type, and rejects a mix:

```text
ENAQueryError: All accessions must be of the same data type as the first
accession, which was PROJECT.
```

**Primary and secondary accessions are not interchangeable here.** The Portal
treats `PRJEB1787` and `ERP001736` as the same study. The Browser returns a
`PROJECT_SET` for the first and a `STUDY_SET` for the second, because they are
two different records.

**Missing records are reported.** ENA silently leaves out any accession in a
batch that it cannot find and still answers with success. enaportal counts the
records as they stream past and warns when fewer come back than were asked
for:

```text
UserWarning: Only 1 of 2 accessions came back from ENA, which leaves out
accessions it cannot find without saying which.
```

If none come back it raises `ENANotFoundError`. Repeated accessions are sent
once, since ENA would return a record once per mention and a duplicate could
hide a missing one.

**Options.** `annotation_only=True` returns EMBL records without their
sequence. `line_limit` cuts each EMBL or FASTA record after that many lines;
it applies to every record, not to the response as a whole.

## Large records

A record has no size limit. The FASTA of a genome assembly is the whole
genome: a yeast assembly is megabytes, a human one gigabytes. Stream anything
large to disk with `fetch_to_file`:

```python
from enaportal import BrowserClient

with BrowserClient() as browser:
    browser.fetch_to_file("yeast.fasta", "GCA_000146045.2", format="fasta")
```

The file is written under a temporary name and only moved into place once the
download is complete. A truncated FASTA looks exactly like a complete one, so a
failed download leaves no file at all rather than a partial one. It returns the
number of records written.

`fetch_to_file` also accepts a binary file that is already open, such as
`sys.stdout.buffer`, and writes records to it as they arrive. Nothing can be
taken back from a stream, so the all-or-nothing guarantee only applies to a
path.

## Parsing records

enaportal returns records as text and leaves parsing to the tool you already
use. The standard library reads the XML:

```python
import xml.etree.ElementTree as ET

runs = ET.fromstring(enaportal.fetch(["ERR164407", "ERR164408"]))
[run.get("accession") for run in runs]  # ['ERR164407', 'ERR164408']
```

and [Biopython](https://biopython.org) reads EMBL and FASTA:

```python
import io

from Bio import SeqIO

records = list(SeqIO.parse(io.StringIO(enaportal.fetch("A00145", format="embl")), "embl"))
```

## Text search

A Portal query needs a field for every term. Text search matches words
anywhere in a record, and returns accessions with a short description:

```python
enaportal.textsearch("Tara oceans", result="read_study", limit=3)
```

```text
shape: (3, 2)
┌───────────┬─────────────────────────────────────────────────────┐
│ accession ┆ description                                         │
│ ---       ┆ ---                                                 │
│ str       ┆ str                                                 │
╞═══════════╪═════════════════════════════════════════════════════╡
│ ERP004109 ┆ Tara-oceans samples barcoding and shotgun sequenci… │
│ ERP009009 ┆ Tara Oceans Ocean Microbiome project                │
│ ERP167472 ┆ Single cell rDNA 16S V4V5 and 18S V9 metabarcoding… │
└───────────┴─────────────────────────────────────────────────────┘
```

`result` is required and takes a Portal result type, such as `read_study`,
`sample` or `sequence`. Pass the accessions on to `fetch`, `filereport` or
`related` for more.

It returns the first 100 hits unless told otherwise. `limit=None` returns every
hit, and `offset` skips hits, so text search, unlike the Portal, can be paged.
One difference from the Portal is a trap: here `limit=0` means **no** hits
rather than all of them, so enaportal refuses it.

`textsearch_count` says how many hits a search has without fetching them:

```python
enaportal.textsearch_count("Tara oceans", result="read_study")  # 48
```

Text search reports a bad query with HTTP 200 and an error in the body, and
enaportal raises that as `ENAQueryError` like any other rejected query. ENA
has been seen to cut a very long text search response short, which raises
`ENAConnectionError`; page with `limit` and `offset` rather than asking for a
million hits at once.

## From a shell

```bash
enaportal fetch PRJEB1787 > study.xml
enaportal related PRJEB1787 | enaportal fetch - > runs.xml
enaportal fetch GCA_000146045.2 --format fasta -o yeast.fasta
enaportal textsearch 'Tara oceans' --result read_study --limit 20
enaportal textsearch 'Tara oceans' --result read_study --count
```

See [Command line](cli.md).
