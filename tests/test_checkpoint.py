"""Unit tests for the on-disk checkpoint. Nothing here touches the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from enaportal._checkpoint import MANIFEST_NAME, Checkpoint


def test_a_part_is_absent_until_it_is_written(tmp_path: Path) -> None:
    checkpoint = Checkpoint(tmp_path / "job")

    assert not checkpoint.is_done("part-0000_all")

    checkpoint.write_part("part-0000_all", lambda path: _fill(path, "run_accession\nERR1\n"))

    assert checkpoint.is_done("part-0000_all")
    assert checkpoint.path_for("part-0000_all").read_text() == "run_accession\nERR1\n"


def test_write_part_returns_what_fill_counted(tmp_path: Path) -> None:
    checkpoint = Checkpoint(tmp_path)

    assert checkpoint.write_part("part-0000_all", lambda path: _fill(path, "a\n1\n2\n")) == 2


def test_a_failed_part_leaves_nothing_behind(tmp_path: Path) -> None:
    """The whole point: an interrupted partition must not look finished."""
    checkpoint = Checkpoint(tmp_path)

    def half_write(path: Path) -> int:
        path.write_text("run_accession\nERR1\n")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        checkpoint.write_part("part-0000_all", half_write)

    assert not checkpoint.is_done("part-0000_all")
    assert list(tmp_path.iterdir()) == []


def test_a_completed_part_survives_a_later_failure(tmp_path: Path) -> None:
    checkpoint = Checkpoint(tmp_path)
    checkpoint.write_part("part-0000_all", lambda path: _fill(path, "a\n1\n"))

    with pytest.raises(RuntimeError):
        checkpoint.write_part("part-0001_rest", _explode)

    assert checkpoint.is_done("part-0000_all")
    assert not checkpoint.is_done("part-0001_rest")


def test_the_manifest_round_trips(tmp_path: Path) -> None:
    checkpoint = Checkpoint(tmp_path)

    assert checkpoint.read_manifest() is None

    checkpoint.write_manifest({"job_key": "abc", "partitions": []})

    assert checkpoint.read_manifest() == {"job_key": "abc", "partitions": []}
    assert (tmp_path / MANIFEST_NAME).exists()


def test_an_unreadable_manifest_reads_as_absent(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_NAME).write_text("{not json")

    assert Checkpoint(tmp_path).read_manifest() is None


def test_clear_removes_the_job_but_keeps_the_directory(tmp_path: Path) -> None:
    checkpoint = Checkpoint(tmp_path)
    checkpoint.write_manifest({"job_key": "abc"})
    checkpoint.write_part("part-0000_all", lambda path: _fill(path, "a\n"))

    checkpoint.clear()

    assert tmp_path.exists()
    assert checkpoint.read_manifest() is None
    assert not checkpoint.is_done("part-0000_all")


def test_discard_removes_the_directory_too(tmp_path: Path) -> None:
    directory = tmp_path / "job"
    checkpoint = Checkpoint(directory)
    checkpoint.write_manifest({"job_key": "abc"})
    checkpoint.write_part("part-0000_all", lambda path: _fill(path, "a\n"))

    checkpoint.discard()

    assert not directory.exists()


def test_discard_leaves_a_directory_holding_anything_else(tmp_path: Path) -> None:
    checkpoint = Checkpoint(tmp_path)
    checkpoint.write_manifest({"job_key": "abc"})
    (tmp_path / "notes.txt").write_text("mine")

    checkpoint.discard()

    assert tmp_path.exists()
    assert (tmp_path / "notes.txt").read_text() == "mine"


def _fill(path: Path, text: str) -> int:
    path.write_text(text)
    return max(len(text.splitlines()) - 1, 0)


def _explode(path: Path) -> int:
    raise RuntimeError("ENA went away")
