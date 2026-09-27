"""Refresh the offline schema snapshot shipped inside the package.

Run by hand to refresh it, and weekly with --check by the schema-drift workflow,
which turns the report into an issue. The snapshot is a fallback for working
offline, not a source of truth, so drift is expected over time; the report says
which of it touches something enaportal reads.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from enaportal._http import ENAHTTPClient
from enaportal.errors import ENAError
from enaportal.schema import RESULTS_KEY, Field

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "src" / "enaportal" / "_snapshot"

# These move every day. Comparing them would report drift on every run and
# bury the changes that matter, which is what a plain equality check did.
VOLATILE_KEYS: Final = frozenset({"recordCount", "lastUpdated"})

# A whole report has to fit in one GitHub issue body, which is capped at 65,536
# characters, and a key that changed in bulk is better summarised than listed.
MAX_LINES_PER_KEY: Final = 25
MAX_REPORT_CHARS: Final = 60_000


@dataclass(frozen=True)
class Change:
    """One difference between the snapshot and the live schema.

    ``why`` is set when the change touches something enaportal reads, and says
    what, so the report can list those first.
    """

    key: str
    name: str
    what: str
    why: str = ""


def fetch_all(http: ENAHTTPClient) -> dict[str, Any]:
    """Read every result type and its two field lists from ENA."""
    results = http.get_json("results", params={"format": "json"})
    payloads: dict[str, Any] = {RESULTS_KEY: results}
    for raw in results if isinstance(results, list) else []:
        # A result without an id cannot be followed, and diff() reports it.
        result = raw.get("resultId") if isinstance(raw, dict) else None
        if result is None:
            continue
        for key, path in (("return_fields", "returnFields"), ("search_fields", "searchFields")):
            payloads[f"{key}.{result}"] = http.get_json(
                path, params={"result": result, "format": "json"}
            )
    return payloads


def read_snapshot_dir(directory: Path) -> dict[str, Any]:
    """Load every payload in a snapshot directory, keyed like fetch_all()."""
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in directory.glob("*.json")}


def write(payloads: dict[str, Any], directory: Path) -> list[str]:
    """Write payloads as one compact JSON file per key. Returns the changed keys."""
    directory.mkdir(parents=True, exist_ok=True)
    changed = []
    for key, payload in sorted(payloads.items()):
        path = directory / f"{key}.json"
        text = json.dumps(payload, indent=1, sort_keys=True) + "\n"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            changed.append(key)
        path.write_text(text, encoding="utf-8")
    for stale in sorted(directory.glob("*.json")):
        if stale.stem not in payloads:
            stale.unlink()
            changed.append(f"{stale.stem} (removed)")
    return changed


def diff(snapshot: Mapping[str, Any], live: Mapping[str, Any]) -> list[Change]:
    """Every structural difference between two schemas, ignoring counts and order."""
    changes: list[Change] = []
    for key in sorted(set(snapshot) | set(live)):
        if key not in live:
            changes.append(Change(key, "", "no longer served"))
        elif key not in snapshot:
            size = len(live[key]) if isinstance(live[key], list) else 0
            changes.append(Change(key, "", f"new, {_plural(size, 'entry', 'entries')}"))
        else:
            changes.extend(_diff_payload(key, snapshot[key], live[key]))
    changes.extend(_new_types(snapshot, live))
    return changes


def render(changes: list[Change]) -> str:
    """A Markdown report of the changes, sized to fit in a GitHub issue."""
    lines = [
        "# ENA schema drift",
        "",
        f"ENA's live schema differs from the snapshot in `src/enaportal/_snapshot` "
        f"in {_plural(len(changes), 'place', 'places')}. Record counts "
        "and update times are ignored, because they change every day.",
    ]
    flagged = [change for change in changes if change.why]
    if flagged:
        lines += ["", "## Check these first", "", "They touch something enaportal reads.", ""]
        lines += _bullets(f"{_line(change, located=True)} *{change.why}.*" for change in flagged)
    lines += ["", "## Every change"]
    for key in sorted({change.key for change in changes}):
        lines += ["", f"### `{key}`", ""]
        lines += _bullets(_line(change) for change in changes if change.key == key)
    lines += [
        "",
        "## What to do",
        "",
        "1. Run `uv run python scripts/update_snapshot.py` and review the diff.",
        "2. Check each change listed first against the code that reads it.",
        "3. Open a PR with the refreshed snapshot. This issue closes itself on the "
        "first scheduled run after it merges.",
    ]
    text = "\n".join(lines) + "\n"
    if len(text) <= MAX_REPORT_CHARS:
        return text
    note = "\n\nThe report was cut to fit here. Run the check locally for all of it.\n"
    return text[: MAX_REPORT_CHARS - len(note)].rsplit("\n", 1)[0] + note


def main(argv: list[str] | None = None) -> int:
    """Fetch the live schema and write it to the snapshot directory, or check it.

    With --check, exits 1 and prints a Markdown report when the schema has
    drifted, and exits 2 when ENA could not be read, so the two stay apart.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=SNAPSHOT_DIR)
    parser.add_argument(
        "--check",
        action="store_true",
        help="print a drift report and exit 1 if the snapshot is out of date, without writing",
    )
    args = parser.parse_args(argv)

    try:
        with ENAHTTPClient() as http:
            payloads = fetch_all(http)
    except ENAError as exc:
        print(f"Could not read the live schema: {exc}", file=sys.stderr)
        return 2

    if args.check:
        changes = diff(read_snapshot_dir(args.directory), payloads)
        if changes:
            print(render(changes), end="")
            return 1
        print("Snapshot matches the live schema.")
        return 0

    changed = write(payloads, args.directory)
    print(f"Wrote {len(payloads)} files to {args.directory}, {len(changed)} changed.")
    for key in changed:
        print(f"  {key}")
    return 0


def _diff_payload(key: str, old: Any, new: Any) -> list[Change]:
    id_key = "resultId" if key == RESULTS_KEY else "columnId"
    new_rows, problem = _index(new, id_key)
    if problem:
        return [Change(key, "", problem, "The payload no longer parses")]
    old_rows, _ = _index(old, id_key)

    changes = []
    for name in sorted(set(old_rows) | set(new_rows)):
        before, after = old_rows.get(name), new_rows.get(name)
        if before is None:
            what = f"added{_type_note(key, after)}"
        elif after is None:
            what = "removed"
        else:
            parts = [
                f"{attr} {_show(before.get(attr))} -> {_show(after.get(attr))}"
                for attr in sorted((set(before) | set(after)) - VOLATILE_KEYS)
                if before.get(attr) != after.get(attr)
            ]
            if not parts:
                continue
            what = "; ".join(parts)
        changes.append(Change(key, name, what, _why(key, before, after)))
    return changes


def _why(key: str, before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> str:
    if key == RESULTS_KEY:
        if before is None:
            return "A result type the offline fallback does not know"
        if after is None:
            return "A result type ENA no longer serves"
        if before.get("primaryAccessionType") != after.get("primaryAccessionType"):
            return "The accession column `enaportal manifest` asks for"
        return ""
    old_type = Field.from_payload(before).type if before is not None else None
    new_type = Field.from_payload(after).type if after is not None else None
    if after is not None and new_type is None:
        return "Its type cannot be recovered, from `type` or from its description"
    if key.startswith("search_fields.") and old_type != new_type and "date" in (old_type, new_type):
        return "A date search field, which bulk partitions on"
    return ""


def _new_types(snapshot: Mapping[str, Any], live: Mapping[str, Any]) -> list[Change]:
    known = {name for name, _ in _field_types(snapshot)}
    first_seen: dict[str, str] = {}
    for name, where in _field_types(live):
        if name not in known:
            first_seen.setdefault(name, where)
    return [
        Change(
            "field types",
            name,
            f"first used by {where}",
            "A type never seen before, which `Field.is_date` and the CLI may misread",
        )
        for name, where in sorted(first_seen.items())
    ]


def _field_types(payloads: Mapping[str, Any]) -> Iterable[tuple[str, str]]:
    for key in sorted(payloads):
        if key == RESULTS_KEY or not isinstance(payloads[key], list):
            continue
        for raw in payloads[key]:
            if isinstance(raw, dict) and "columnId" in raw:
                field = Field.from_payload(raw)
                if field.type is not None:
                    yield field.type, f"`{field.column_id}` in `{key}`"


def _index(payload: Any, id_key: str) -> tuple[dict[str, dict[str, Any]], str]:
    if not isinstance(payload, list):
        return {}, f"is a {type(payload).__name__}, not a list"
    entries = [raw for raw in payload if isinstance(raw, dict) and id_key in raw]
    rows = {str(raw[id_key]): raw for raw in entries}
    if len(entries) < len(payload):
        return rows, f"{len(payload) - len(entries)} of {len(payload)} entries have no `{id_key}`"
    return rows, ""


def _type_note(key: str, raw: Mapping[str, Any] | None) -> str:
    if key == RESULTS_KEY or raw is None:
        return ""
    field_type = Field.from_payload(raw).type
    return f", {field_type}" if field_type else ""


def _show(value: Any) -> str:
    return "absent" if value is None else f"`{value}`"


def _line(change: Change, *, located: bool = False) -> str:
    subject = f"`{change.name}`" if change.name else ""
    if located:
        subject = f"`{change.key}` {subject}".rstrip()
    return (
        f"{subject}: {change.what}." if subject else f"{change.what[0].upper()}{change.what[1:]}."
    )


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _bullets(lines: Iterable[str]) -> list[str]:
    listed = list(lines)
    shown = [f"- {line}" for line in listed[:MAX_LINES_PER_KEY]]
    if len(listed) > MAX_LINES_PER_KEY:
        shown.append(f"- and {len(listed) - MAX_LINES_PER_KEY} more")
    return shown


if __name__ == "__main__":
    sys.exit(main())
