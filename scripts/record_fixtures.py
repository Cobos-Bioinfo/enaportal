"""Record real ENA responses for the offline test suite to replay.

The offline tests feed these through the real parsers, so what they check
against is what ENA actually sends rather than a mock written from memory. Each
replay also asserts the library still sends the request recorded here, so a
change on either side shows up as a failing test that says to re-record.

Bodies are stored one line per array element, which keeps a re-recording's diff
readable: it is the record of what ENA changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from enaportal._http import BROWSER_BASE_URL, DEFAULT_TIMEOUT, PORTAL_BASE_URL, USER_AGENT

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

BASES = {"portal": PORTAL_BASE_URL, "browser": BROWSER_BASE_URL}

FILE_FIELDS = ",".join(
    f"{source}_{part}" for source in ("fastq", "submitted") for part in ("ftp", "md5", "bytes")
)


@dataclass(frozen=True)
class Request:
    """One request, spelled exactly as the library sends it."""

    api: str
    method: str
    path: str
    params: dict[str, str] = field(default_factory=dict)
    json: dict[str, Any] | None = None


RECORDINGS: dict[str, Request] = {
    "portal_count": Request(
        "portal", "GET", "count", {"result": "read_run", "query": "tax_tree(4932)"}
    ),
    # SRP265601's title quotes a species name, and ENA does not escape it.
    "portal_search_tsv": Request(
        "portal",
        "GET",
        "search",
        {
            "result": "read_run",
            "query": "secondary_study_accession=SRP265601",
            "fields": "run_accession,study_title,fastq_ftp,fastq_md5,fastq_bytes",
            "limit": "3",
            "format": "tsv",
        },
    ),
    "portal_search_json": Request(
        "portal",
        "GET",
        "search",
        {
            "result": "read_run",
            "query": "secondary_study_accession=SRP265601",
            "fields": "run_accession,study_title,fastq_ftp,fastq_md5,fastq_bytes",
            "limit": "3",
            "format": "json",
        },
    ),
    "portal_filereport_paired": Request(
        "portal",
        "GET",
        "filereport",
        {
            "accession": "ERR315859",
            "result": "read_run",
            "format": "tsv",
            "fields": f"run_accession,{FILE_FIELDS}",
        },
    ),
    "portal_filereport_wrong_result": Request(
        "portal",
        "GET",
        "filereport",
        {"accession": "ERR164407", "result": "sample", "format": "tsv"},
    ),
    "portal_search_offset": Request(
        "portal", "GET", "search", {"result": "read_run", "offset": "10", "format": "tsv"}
    ),
    "portal_not_found": Request("portal", "GET", "no_such_endpoint"),
    # A query error that ENA reports as a server fault.
    "portal_count_unknown_field": Request(
        "portal", "GET", "count", {"result": "read_run", "query": "not_a_field=1"}
    ),
    "browser_xml_runs": Request(
        "browser", "POST", "xml", json={"accessions": ["ERR164407", "ERR164408"]}
    ),
    "browser_xml_taxon": Request("browser", "POST", "xml", json={"accessions": ["9606"]}),
    "browser_xml_sample": Request("browser", "POST", "xml", json={"accessions": ["SAMEA1571379"]}),
    "browser_xml_dropped": Request(
        "browser", "POST", "xml", json={"accessions": ["ERR164407", "ERR99999999"]}
    ),
    "browser_xml_not_found": Request(
        "browser", "POST", "xml", json={"accessions": ["ERR99999999"]}
    ),
    "browser_xml_mixed_types": Request(
        "browser", "POST", "xml", json={"accessions": ["PRJEB1787", "ERR164407"]}
    ),
    "browser_embl": Request("browser", "POST", "embl", json={"accessions": ["A00145"]}),
    "browser_embl_annotation_only": Request(
        "browser", "POST", "embl", json={"accessions": ["A00145"], "annotationOnly": True}
    ),
    "browser_embl_wrong_format": Request(
        "browser", "POST", "embl", json={"accessions": ["PRJEB1787"]}
    ),
    "browser_fasta": Request("browser", "POST", "fasta", json={"accessions": ["A00145", "A00146"]}),
    "browser_textsearch": Request(
        "browser",
        "GET",
        "tsv/textsearch",
        {"query": "Candidatus Phytoplasma", "result": "read_study", "limit": "20"},
    ),
    "browser_textsearch_count": Request(
        "browser", "GET", "tsv/textsearch/count", {"query": "Tara oceans", "result": "read_study"}
    ),
    "browser_textsearch_bad_result": Request(
        "browser",
        "GET",
        "tsv/textsearch",
        {"query": "Tara oceans", "result": "nonsense", "limit": "100"},
    ),
}


def record(client: httpx.Client, request: Request) -> dict[str, Any]:
    """Send one request to ENA and describe the exchange as a fixture."""
    response = client.request(
        request.method,
        BASES[request.api] + request.path,
        params=request.params or None,
        json=request.json,
    )
    return {
        "api": request.api,
        "method": request.method,
        "path": request.path,
        "params": request.params,
        "json": request.json,
        "status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "body": response.text.split("\n"),
    }


def main(argv: list[str] | None = None) -> int:
    """Record every fixture, or the ones named, and report which changed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="record only these (default all)")
    parser.add_argument("--directory", type=Path, default=FIXTURE_DIR)
    args = parser.parse_args(argv)

    unknown = sorted(set(args.names) - set(RECORDINGS))
    if unknown:
        parser.error(f"no such recording: {', '.join(unknown)}")
    args.directory.mkdir(parents=True, exist_ok=True)

    changed = []
    with httpx.Client(timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        for name in args.names or sorted(RECORDINGS):
            text = json.dumps(record(client, RECORDINGS[name]), indent=1) + "\n"
            path = args.directory / f"{name}.json"
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                changed.append(name)
            path.write_text(text, encoding="utf-8")
    print("\n".join(changed) if changed else "no fixture changed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
