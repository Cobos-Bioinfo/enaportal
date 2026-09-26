"""Unit tests for the on-disk schema cache."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from enaportal._cache import ENV_CACHE_DIR, JSONCache, default_cache_dir


def test_round_trips_a_payload(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path, ttl=60)

    cache.write("results", [{"resultId": "read_run"}])

    assert cache.read("results") == [{"resultId": "read_run"}]


def test_missing_entry_reads_as_none(tmp_path: Path) -> None:
    assert JSONCache(tmp_path, ttl=60).read("results") is None


def test_expired_entry_reads_as_none(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path, ttl=0.0)
    cache.write("results", [1])
    time.sleep(0.01)

    assert cache.read("results") is None


def test_expired_entry_is_still_available_as_stale(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path, ttl=0.0)
    cache.write("results", [1])
    time.sleep(0.01)

    assert cache.read("results", allow_stale=True) == [1]


def test_corrupt_entry_reads_as_none(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path, ttl=60)
    cache.path_for("results").write_text("{ not json", encoding="utf-8")

    assert cache.read("results") is None


def test_write_is_atomic_and_leaves_no_temporary_files(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path, ttl=60)

    cache.write("results", [1])

    assert list(tmp_path.glob("*.tmp")) == []
    assert json.loads(cache.path_for("results").read_text(encoding="utf-8"))["payload"] == [1]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_cache_is_not_an_error(tmp_path: Path) -> None:
    unwritable = tmp_path / "read-only"
    unwritable.mkdir(mode=0o500)

    cache = JSONCache(unwritable / "schema", ttl=60)
    cache.write("results", [1])

    assert cache.read("results") is None


def test_clear_removes_one_entry(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path, ttl=60)
    cache.write("results", [1])
    cache.write("return_fields.read_run", [2])

    cache.clear("results")

    assert cache.read("results") is None
    assert cache.read("return_fields.read_run") == [2]


def test_clear_removes_everything(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path, ttl=60)
    cache.write("results", [1])
    cache.write("return_fields.read_run", [2])

    cache.clear()

    assert list(tmp_path.glob("*.json")) == []


def test_keys_cannot_escape_the_cache_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe cache key"):
        JSONCache(tmp_path, ttl=60).path_for("../../etc/passwd")


def test_env_var_overrides_the_cache_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_CACHE_DIR, str(tmp_path / "elsewhere"))

    assert default_cache_dir() == tmp_path / "elsewhere"


def test_default_cache_directory_is_namespaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_CACHE_DIR, raising=False)

    assert default_cache_dir().name == "enaportal"
