# Command line

Installing enaportal puts an `enaportal` command on the path. It is a thin
layer over the library, one subcommand per operation, and it is built to sit
in a pipeline.

| Command | Does |
|---|---|
| `search` | Run a Portal query |
| `count` | Count the records a Portal query matches |
| `bulk` | Fetch a large Portal query in resumable, checkpointed partitions |
| `filereport` | Everything ENA holds for accessions, including file locations |
| `related` | The objects related to an accession, such as a study's runs |
| `manifest` | Write file locations as input for aria2c, curl or nf-core/fetchngs |
| `fetch` | Records by accession as XML, EMBL or FASTA |
| `textsearch` | Free-text search over ENA |
| `fields` | The fields a result type returns, or can be searched on |
| `results` | Every result type ENA exposes |

`enaportal COMMAND --help` lists every option, and the
[command reference](../reference/cli.md) has them all on one page. Where the
script is not on the path, `python -m enaportal` works the same way.

## Output

Tables go to **stdout** as TSV with a header row, exactly as ENA writes it:
tab-separated and unquoted. It pipes into `cut`, `awk`, `sort` or another
enaportal call, and loads with any TSV reader. Records from `fetch` and
manifests from `manifest` go to stdout as they are.

Warnings, progress and errors go to **stderr**, each line prefixed
`enaportal:`, so they never end up in the data. `-q` hides warnings and
progress. `-o PATH` writes to a file instead of stdout.

```bash
enaportal count read_run --query 'tax_tree(4932) AND library_strategy="RNA-Seq"'
enaportal search read_run --query 'tax_tree(4932)' -f run_accession,read_count --limit 10
enaportal fields read_run --search
```

`-f` takes a comma-separated list, and can be repeated. Result types and field
names are checked against ENA's schema before anything is sent, as in the
library; `--no-validate` skips the check.

There is no JSON output. TSV is what ENA streams and what pipes; for anything
else, use the library.

## Pipelines

Commands that take accessions read them from stdin when given `-`. The input
can be a plain list, one or more per line, or the TSV another enaportal call
printed: the first column of each line is used, and a header line is skipped.
That lets one call feed the next:

```bash
# The XML of every run in a study, in one request.
enaportal related PRJEB1787 | enaportal fetch - > runs.xml

# The runs of the top text search hit, with their samples and files.
enaportal textsearch 'Tara oceans' --result read_study --limit 1 \
  | enaportal filereport - -f run_accession,sample_accession,fastq_ftp
```

A header is recognised by being all lower case, which no ENA accession is.

## Bulk downloads

`bulk` is the resumable download described in
[Bulk retrieval](bulk.md). Progress goes to stderr as partitions land.
Interrupt it with Ctrl-C and it says how to resume:

```text
enaportal: interrupted. Run it again without --restart to resume; partitions already fetched are kept.
```

Running the same command again carries on from the partitions already on
disk. `--restart` discards them instead.

```bash
enaportal bulk read_run --query 'tax_tree(4932)' -f run_accession,fastq_ftp > yeast.tsv
enaportal bulk read_run --query 'tax_tree(4932)' --dry-run
enaportal bulk read_run --query 'tax_tree(4932)' --checkpoint-dir yeast-runs/ -o yeast.tsv
```

`--dry-run` prints the plan as a table, with one row per partition giving its
start, end, count and query, and fetches nothing. `--checkpoint-dir` keeps the
partitions after the job succeeds; without it they live in the cache and are
removed once the job completes. `--threshold`, `--partition-field` and
`--concurrency` match the library's arguments.

## Manifests

`manifest` resolves accessions to files and writes a manifest for a
downloader. Given accessions it asks `filereport` for exactly the columns it
needs, checksums included:

```bash
enaportal manifest PRJEB1787 > files.txt
aria2c -i files.txt

enaportal manifest ERR315859 --format curl > download.sh
enaportal manifest PRJEB1787 --format nf-core --pipeline rnaseq > samplesheet.csv
enaportal manifest PRJEB1787 --format accessions > ids.txt
```

With `--table` it builds the manifest from a TSV that `search`, `bulk` or
`filereport` already wrote, and sends no requests at all:

```bash
enaportal search read_run --query 'tax_tree(4932)' -f run_accession,fastq_ftp,fastq_md5 \
  | enaportal manifest --table - --format nf-core > samplesheet.csv
```

Piping accessions into `manifest -` also works, but costs one `filereport`
request per accession. For a whole study, give the study accession, or use
`--table`.

`--source`, `--protocol` and `--directory` match the arguments of
[`to_manifest`](files.md#manifests).

## Exit status

| Status | Meaning |
|---|---|
| 0 | Success |
| 1 | ENA returned an error, or could not be reached |
| 2 | A usage error, including values the library refuses |
| 130 | Interrupted with Ctrl-C |
| 141 | The reader closed the pipe, as `head` does |

A script can tell a failed query from an interrupted run, and treat
`enaportal search ... | head` as the success it is.
