"""The schema-drift check in scripts/update_snapshot.py.

The weekly workflow turns this report into an issue, so a false positive files
an issue every week and a false negative hides the drift M9 exists to catch.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st

from enaportal._http import PORTAL_BASE_URL

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "update_snapshot.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("update_snapshot", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses looks its module up by name while building the class.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


drift = _load()

SNAPSHOT = drift.read_snapshot_dir(drift.SNAPSHOT_DIR)


@pytest.fixture
def live() -> dict[str, Any]:
    return copy.deepcopy(SNAPSHOT)


def field(payloads: dict[str, Any], key: str, column: str) -> dict[str, Any]:
    return next(raw for raw in payloads[key] if raw["columnId"] == column)


def flagged(changes: list[Any]) -> list[tuple[str, str]]:
    return [(change.key, change.name) for change in changes if change.why]


def test_the_snapshot_matches_itself() -> None:
    assert drift.diff(SNAPSHOT, SNAPSHOT) == []


def test_record_counts_and_update_times_are_not_drift(live: dict[str, Any]) -> None:
    """The plain equality check this replaced reported drift the day after a refresh."""
    for raw in live["results"]:
        raw["recordCount"] = str(int(raw["recordCount"]) + 1)
        raw["lastUpdated"] = "2031-01-01 00:00"

    assert drift.diff(SNAPSHOT, live) == []


def test_order_is_not_drift(live: dict[str, Any]) -> None:
    for payload in live.values():
        payload.reverse()

    assert drift.diff(SNAPSHOT, live) == []


def test_a_date_search_field_that_changes_type_is_flagged(live: dict[str, Any]) -> None:
    field(live, "search_fields.read_run", "first_public")["type"] = "text"

    changes = drift.diff(SNAPSHOT, live)

    assert flagged(changes) == [("search_fields.read_run", "first_public")]
    assert changes[0].what == "type `date` -> `text`"
    assert "bulk partitions on" in changes[0].why


def test_a_new_and_a_removed_date_search_field_are_flagged(live: dict[str, Any]) -> None:
    live["search_fields.read_run"].remove(field(live, "search_fields.read_run", "first_public"))
    live["search_fields.read_run"].append(
        {"columnId": "first_seen", "description": "Seen", "type": "date"}
    )

    changes = drift.diff(SNAPSHOT, live)

    assert [(change.name, change.what) for change in changes] == [
        ("first_public", "removed"),
        ("first_seen", "added, date"),
    ]
    assert all("bulk partitions on" in change.why for change in changes)


def test_a_return_field_changing_type_is_listed_but_not_flagged(live: dict[str, Any]) -> None:
    """Bulk partitions on search fields only."""
    field(live, "return_fields.read_run", "first_public")["type"] = "text"

    changes = drift.diff(SNAPSHOT, live)

    assert [(change.key, change.name) for change in changes] == [
        ("return_fields.read_run", "first_public")
    ]
    assert flagged(changes) == []


def test_a_type_moved_into_the_description_is_listed_but_not_flagged(
    live: dict[str, Any],
) -> None:
    """ENA already does this for 247 fields, and Field recovers the type."""
    raw = field(live, "search_fields.read_run", "first_public")
    del raw["type"]
    raw["description"] = "date"

    changes = drift.diff(SNAPSHOT, live)

    assert len(changes) == 1
    assert "type `date` -> absent" in changes[0].what
    assert flagged(changes) == []


def test_a_field_whose_type_cannot_be_recovered_is_flagged(live: dict[str, Any]) -> None:
    del field(live, "return_fields.sample", "sample_accession")["type"]

    changes = drift.diff(SNAPSHOT, live)

    assert flagged(changes) == [("return_fields.sample", "sample_accession")]
    assert "cannot be recovered" in changes[0].why


def test_a_new_type_is_flagged_once_where_first_seen(live: dict[str, Any]) -> None:
    field(live, "search_fields.read_run", "first_public")["type"] = "datetime"
    field(live, "search_fields.sample", "first_public")["type"] = "datetime"

    changes = [change for change in drift.diff(SNAPSHOT, live) if change.key == "field types"]

    assert [(change.name, change.what) for change in changes] == [
        ("datetime", "first used by `first_public` in `search_fields.read_run`")
    ]
    assert changes[0].why


def test_a_new_result_type_is_flagged_and_its_fields_summarised(live: dict[str, Any]) -> None:
    live["results"].append({"resultId": "spectra", "description": "Spectra"})
    live["return_fields.spectra"] = [{"columnId": "accession", "description": "text"}]
    live["search_fields.spectra"] = []

    changes = drift.diff(SNAPSHOT, live)

    assert [(change.key, change.name, change.what) for change in changes] == [
        ("results", "spectra", "added"),
        ("return_fields.spectra", "", "new, 1 entry"),
        ("search_fields.spectra", "", "new, 0 entries"),
    ]
    assert flagged(changes) == [("results", "spectra")]


def test_a_removed_result_type_is_flagged_once(live: dict[str, Any]) -> None:
    live["results"] = [raw for raw in live["results"] if raw["resultId"] != "taxon"]
    del live["return_fields.taxon"], live["search_fields.taxon"]

    changes = drift.diff(SNAPSHOT, live)

    assert [(change.key, change.what) for change in changes] == [
        ("results", "removed"),
        ("return_fields.taxon", "no longer served"),
        ("search_fields.taxon", "no longer served"),
    ]
    assert flagged(changes) == [("results", "taxon")]


def test_a_new_primary_accession_type_is_flagged(live: dict[str, Any]) -> None:
    """The manifest subcommand asks filereport for this column."""
    run = next(raw for raw in live["results"] if raw["resultId"] == "read_run")
    run["primaryAccessionType"] = "accession"

    assert flagged(drift.diff(SNAPSHOT, live)) == [("results", "read_run")]


def test_a_new_description_on_a_result_is_listed_but_not_flagged(live: dict[str, Any]) -> None:
    live["results"][0]["description"] = "Something else"

    changes = drift.diff(SNAPSHOT, live)

    assert len(changes) == 1
    assert flagged(changes) == []


@pytest.mark.parametrize(
    ("payload", "problem"),
    [
        ({"error": "down"}, "is a dict, not a list"),
        ([{"columnId": "a"}, {"name": "b"}, "c"], "2 of 3 entries have no `columnId`"),
    ],
)
def test_a_payload_that_no_longer_parses_is_flagged(
    live: dict[str, Any], payload: Any, problem: str
) -> None:
    live["search_fields.read_run"] = payload

    assert [(change.key, change.what, change.why) for change in drift.diff(SNAPSHOT, live)] == [
        ("search_fields.read_run", problem, "The payload no longer parses")
    ]


def test_the_report_lists_flagged_changes_first(live: dict[str, Any]) -> None:
    field(live, "search_fields.read_run", "first_public")["type"] = "text"
    live["results"][0]["description"] = "Something else"

    report = drift.render(drift.diff(SNAPSHOT, live))

    first, every = report.split("## Every change")
    assert "`search_fields.read_run` `first_public`: type `date` -> `text`." in first
    assert "Something else" not in first
    assert "### `results`" in every
    assert "### `search_fields.read_run`" in every
    assert "in 2 places" in report


def test_a_report_without_flagged_changes_has_no_list_of_them(live: dict[str, Any]) -> None:
    live["results"][0]["description"] = "Something else"

    report = drift.render(drift.diff(SNAPSHOT, live))

    assert "Check these first" not in report
    assert "in 1 place." in report


def test_a_key_that_changed_in_bulk_is_summarised(live: dict[str, Any]) -> None:
    for raw in live["return_fields.read_run"]:
        raw["description"] += "."

    report = drift.render(drift.diff(SNAPSHOT, live))

    listed = len(live["return_fields.read_run"])
    assert f"- and {listed - drift.MAX_LINES_PER_KEY} more" in report


def test_the_report_fits_in_an_issue(live: dict[str, Any]) -> None:
    for key, payload in live.items():
        for raw in payload:
            raw["description"] = "A much longer description than before. " * 5 + key

    report = drift.render(drift.diff(SNAPSHOT, live))

    assert len(report) <= drift.MAX_REPORT_CHARS
    assert report.endswith("Run the check locally for all of it.\n")


_ids = st.sampled_from(["a", "b", "c", "d"])
_fields = st.dictionaries(
    _ids,
    st.fixed_dictionaries(
        {"description": st.sampled_from(["", "date", "Run date"])},
        optional={"type": st.sampled_from(["text", "date", "number", "datetime"])},
    ),
    max_size=4,
)
_results = st.dictionaries(
    _ids,
    st.fixed_dictionaries(
        {
            "description": st.sampled_from(["Runs", "Samples"]),
            "recordCount": st.sampled_from(["1", "2"]),
            "lastUpdated": st.sampled_from(["2026-01-01 00:00", "2026-02-01 00:00"]),
        },
        optional={"primaryAccessionType": st.sampled_from(["run_accession", "accession"])},
    ),
    max_size=3,
)
_schemas = st.fixed_dictionaries(
    {"results": _results},
    optional={"return_fields.a": _fields, "search_fields.a": _fields, "search_fields.b": _fields},
)

Schema = dict[str, dict[str, dict[str, str]]]


def payloads(schema: Schema) -> dict[str, list[dict[str, str]]]:
    return {
        key: [
            {"resultId" if key == "results" else "columnId": name, **attrs}
            for name, attrs in rows.items()
        ]
        for key, rows in schema.items()
    }


def stable(attrs: dict[str, str] | None) -> dict[str, str] | None:
    if attrs is None:
        return None
    return {attr: value for attr, value in attrs.items() if attr not in drift.VOLATILE_KEYS}


@given(_schemas, _schemas)
def test_every_record_that_differs_is_reported_and_no_other(old: Schema, new: Schema) -> None:
    expected = {(key, "") for key in old.keys() ^ new.keys()} | {
        (key, name)
        for key in old.keys() & new.keys()
        for name in old[key].keys() | new[key].keys()
        if stable(old[key].get(name)) != stable(new[key].get(name))
    }

    changes = drift.diff(payloads(old), payloads(new))

    assert {(c.key, c.name) for c in changes if c.key != "field types"} == expected


@given(_schemas, st.randoms(use_true_random=False))
def test_counts_times_and_order_alone_are_never_drift(schema: Schema, random: Any) -> None:
    moved = payloads(schema)
    for payload in moved.values():
        random.shuffle(payload)
    for raw in moved["results"]:
        raw["recordCount"] = str(int(raw["recordCount"]) + 1)
        raw["lastUpdated"] = "2031-01-01 00:00"

    assert drift.diff(payloads(schema), moved) == []


MINIMAL = {
    "results": [{"resultId": "read_run", "description": "Raw reads", "recordCount": "1"}],
    "return_fields.read_run": [{"columnId": "run_accession", "description": "text"}],
    "search_fields.read_run": [{"columnId": "first_public", "description": "date"}],
}


def serve(schema: dict[str, Any]) -> None:
    respx.get(f"{PORTAL_BASE_URL}results").respond(json=schema["results"])
    for key, path in (("return_fields", "returnFields"), ("search_fields", "searchFields")):
        respx.get(f"{PORTAL_BASE_URL}{path}", params={"result": "read_run"}).respond(
            json=schema[f"{key}.read_run"]
        )


def snapshot_dir(tmp_path: Path, schema: dict[str, Any]) -> Path:
    for key, payload in schema.items():
        (tmp_path / f"{key}.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


@respx.mock
def test_check_passes_when_nothing_but_counts_moved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    moved = copy.deepcopy(MINIMAL)
    moved["results"][0]["recordCount"] = "2"
    serve(moved)

    status = drift.main(["--check", "--directory", str(snapshot_dir(tmp_path, MINIMAL))])

    assert status == 0
    assert capsys.readouterr().out == "Snapshot matches the live schema.\n"


@respx.mock
def test_check_prints_the_report_and_exits_one_on_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    moved = copy.deepcopy(MINIMAL)
    moved["search_fields.read_run"][0]["description"] = "number"
    serve(moved)

    status = drift.main(["--check", "--directory", str(snapshot_dir(tmp_path, MINIMAL))])

    assert status == 1
    report = capsys.readouterr().out
    assert report.startswith("# ENA schema drift\n")
    assert "`first_public`: description `date` -> `number`." in report


@respx.mock
def test_check_leaves_the_snapshot_alone(tmp_path: Path) -> None:
    moved = copy.deepcopy(MINIMAL)
    moved["search_fields.read_run"] = []
    serve(moved)
    directory = snapshot_dir(tmp_path, MINIMAL)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}

    drift.main(["--check", "--directory", str(directory)])

    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


@respx.mock
def test_check_exits_two_when_ena_cannot_be_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Kept apart from drift, so an outage fails the job instead of filing an issue."""
    respx.get(f"{PORTAL_BASE_URL}results").mock(return_value=httpx.Response(404))

    status = drift.main(["--check", "--directory", str(snapshot_dir(tmp_path, MINIMAL))])

    assert status == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Could not read the live schema")


@respx.mock
def test_a_result_without_an_id_is_reported_rather_than_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = copy.deepcopy(MINIMAL)
    broken["results"].append({"description": "Unnamed"})
    serve(broken)

    status = drift.main(["--check", "--directory", str(snapshot_dir(tmp_path, MINIMAL))])

    assert status == 1
    assert "1 of 2 entries have no `resultId`" in capsys.readouterr().out


@respx.mock
def test_without_check_the_snapshot_is_rewritten(tmp_path: Path) -> None:
    serve(MINIMAL)
    directory = snapshot_dir(tmp_path, {**MINIMAL, "search_fields.gone": []})

    assert drift.main(["--directory", str(directory)]) == 0

    assert drift.read_snapshot_dir(directory) == MINIMAL
