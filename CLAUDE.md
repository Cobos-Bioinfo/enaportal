# CLAUDE.md

Instructions for agents and contributors working in this repo.

## What this is

`enaportal`, a typed Python client for the ENA Portal and Browser APIs.
Read [PLAN.md](PLAN.md) first: it holds the milestones, the decisions already
made and their rationale, and a table of verified API facts. Do not re-derive
those facts by probing the API; they are written down.

## Writing style

No emojis. No em dashes. Plain prose in every README, docstring, comment,
commit message and CLI string.

## Comments

Code should be self-explanatory. Comments explain **why**, never **what**.

- A short docstring on each public function, class and module. One or two
  lines, saying what it is for and anything surprising about it.
- Inline comments only where the reason for the code is not visible in the
  code: an ENA quirk, a workaround, a non-obvious tradeoff.
- Never write a comment that restates the line below it.
- Never write a comment explaining another comment.
- No banner comments, no section dividers, no commented-out code.

```python
# Bad
# Loop over the fields
# (the fields are the ones we validated above)
for field in fields:
    ...

# Good
# ENA rejects unknown fields with HTTP 200 and a plain-text body, so validate
# locally before sending.
for field in fields:
    ...
```

## Conventions

- Python 3.10 is the floor. Use `from __future__ import annotations`.
- Typed throughout, `mypy --strict` must pass. `py.typed` is shipped.
- Public API is sync. Do not add async twins; see the decision in PLAN.md.
- Tabular output is Polars, not pandas.
- Line length 100, enforced by ruff.
- Private modules are underscore-prefixed (`_http.py`).

## Tests

- `uv run pytest` must never touch the network. Mock with `respx`.
- Tests that hit the live API are marked `@pytest.mark.live` and are excluded
  from the default run.
- Test the query-building and partitioning logic hardest. That is where the
  bugs will be.
- Real ENA responses live in `tests/fixtures`, recorded by
  `scripts/record_fixtures.py` and replayed through `tests/recorded.py`.
  Re-record them; never edit one by hand.
- Invariants over generated inputs use Hypothesis, checked against an oracle
  rather than a restatement of the code.
- CI enforces a 90% line and branch coverage floor, set in `pyproject.toml`.
- Examples in the README and `docs/` are checked by `tests/test_docs.py`
  against the real signatures and parser. Show only real output in them.

## Commits and branches

- Work on a branch, merge to `main` by PR. Never commit straight to `main`.
- Conventional commit prefixes: `feat`, `fix`, `docs`, `test`, `build`, `ci`,
  `chore`, `refactor`.
- Small, logical commits. One concern per commit. Do not squash an entire
  milestone into one commit.
- Do not commit or push unless asked.

## Commands

```bash
uv sync                  # environment
uv run ruff check .      # lint
uv run ruff format .     # format
uv run mypy              # types
uv run pytest            # offline tests
uv run pytest --cov      # offline tests with the coverage floor
uv run pytest -m live    # live API tests
uv run python scripts/update_snapshot.py --check  # report schema drift against ENA
uv run python scripts/update_snapshot.py  # refresh the offline schema snapshot
uv run python scripts/record_fixtures.py  # re-record the replayed ENA responses
uv run --group docs mkdocs serve           # preview the documentation site
uv run --group docs mkdocs build --strict  # build it as CI does
uv build                 # distributions
```

## Things to leave alone

- Bulk FASTQ downloading and data submission are out of scope. See PLAN.md.
- Do not commit ENA's field metadata as a frozen source of truth. The cached
  snapshot under `src/enaportal/` is a fallback for offline use only, and the
  schema-drift workflow keeps it honest. Freezing this metadata is exactly how
  the predecessor library died.
