# Development

enaportal is developed on [GitHub](https://github.com/Cobos-Bioinfo/enaportal).
Bug reports and pull requests are welcome.

## Setting up

```bash
git clone https://github.com/Cobos-Bioinfo/enaportal.git
cd enaportal
uv sync
```

[uv](https://docs.astral.sh/uv/) creates the environment with the development
tools. Python 3.10 is the oldest version supported, and CI tests 3.10 to 3.13.

## Checks

```bash
uv run ruff check .       # lint
uv run ruff format .      # format
uv run mypy               # types, in strict mode
uv run pytest             # tests, which never touch the network
uv run pytest --cov       # the same, held to the coverage floor
uv run pytest -m live     # tests against the live ENA API
```

CI runs all but the live tests on every pull request.

## Tests

The default test run is offline, and a test that opens a socket fails. HTTP is
mocked with [respx](https://lundberg.github.io/respx/). Tests that need the
live API are marked `live` and excluded unless asked for with `-m live`.

Real ENA responses live in `tests/fixtures`, recorded by
`scripts/record_fixtures.py` and replayed by `tests/recorded.py`. Every replay
also checks that the library still sends the request the fixture was recorded
from, so a stale fixture fails instead of vouching for a request that no longer
exists. Re-record fixtures rather than editing them:

```bash
uv run python scripts/record_fixtures.py
```

Invariants over generated inputs, such as the bisection behind
[bulk retrieval](guide/bulk.md), are tested with
[Hypothesis](https://hypothesis.readthedocs.io) against an independent oracle.
Line and branch coverage must stay at or above 90%.

The documentation's code is tested too: every Python example must parse and
use only names and arguments that exist, and every command line example must
be accepted by the command's parser.

## Schema drift

enaportal reads ENA's schema at runtime. A snapshot of it ships in the package
only as an offline fallback, and a scheduled workflow keeps it honest. Every
Monday it compares ENA's live schema with the snapshot and runs the live tests.
Each kind of failure is kept as one open issue, labelled `schema-drift` or
`live-tests`, which is updated when the finding changes and closed by the first
clean run. Record counts and update times are not drift.

```bash
uv run python scripts/update_snapshot.py --check  # report drift, write nothing
uv run python scripts/update_snapshot.py          # refresh the snapshot
```

To resolve a drift issue, refresh the snapshot, check the changes the issue
lists first, since those are the ones enaportal reads, and open a pull request.
GitHub pauses scheduled workflows in a repository with no activity for 60 days;
re-enable it from the Actions tab.

## Documentation

The site is built with [MkDocs](https://www.mkdocs.org) and
[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/), and the
API reference is generated from docstrings by
[mkdocstrings](https://mkdocstrings.github.io). To preview it:

```bash
uv run --group docs mkdocs serve
```

Pull requests build it in strict mode, so a broken link fails the build, and
every merge to `main` publishes it.
