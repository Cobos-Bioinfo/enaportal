"""Refresh the offline schema snapshot shipped inside the package.

Run by hand and by the schema-drift workflow. The snapshot is a fallback for
working offline, not a source of truth, so a diff here is expected over time and
is exactly what the drift job reports on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from enaportal._http import ENAHTTPClient
from enaportal.schema import Result

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "src" / "enaportal" / "_snapshot"


def fetch_all(http: ENAHTTPClient) -> dict[str, Any]:
    """Read every result type and its two field lists from ENA."""
    payloads: dict[str, Any] = {"results": http.get_json("results", params={"format": "json"})}
    for raw in payloads["results"]:
        result = Result.from_payload(raw).result_id
        for key, path in (("return_fields", "returnFields"), ("search_fields", "searchFields")):
            payloads[f"{key}.{result}"] = http.get_json(
                path, params={"result": result, "format": "json"}
            )
    return payloads


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


def main(argv: list[str] | None = None) -> int:
    """Fetch the live schema and write it to the snapshot directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=SNAPSHOT_DIR)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the snapshot would change, without writing it",
    )
    args = parser.parse_args(argv)

    with ENAHTTPClient() as http:
        payloads = fetch_all(http)

    if args.check:
        current = {
            p.stem: json.loads(p.read_text(encoding="utf-8")) for p in args.directory.glob("*.json")
        }
        drift = sorted(set(payloads) ^ set(current)) + sorted(
            key for key in set(payloads) & set(current) if payloads[key] != current[key]
        )
        if drift:
            print("Schema drift in: " + ", ".join(drift))
            return 1
        print("Snapshot matches the live schema.")
        return 0

    changed = write(payloads, args.directory)
    print(f"Wrote {len(payloads)} files to {args.directory}, {len(changed)} changed.")
    for key in changed:
        print(f"  {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
