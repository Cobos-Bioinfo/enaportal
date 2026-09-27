"""Unit tests for reading field names out of an ENA query string."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from enaportal._query import extract_field_names


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ('instrument_platform="ILLUMINA"', ["instrument_platform"]),
        ("tax_tree(4932)", []),
        ('tax_tree(4932) AND library_strategy="RNA-Seq"', ["library_strategy"]),
        ("first_public>=2020-01-01 AND first_public<=2021-01-01", ["first_public"]),
        ("read_count>1000000", ["read_count"]),
        ('base_count!="0"', ["base_count"]),
        ('country="United Kingdom" OR country="Spain"', ["country"]),
        ("", []),
        ('study_title="a=b" AND center_name="x"', ["study_title", "center_name"]),
        ('NOT instrument_platform="OXFORD_NANOPORE"', ["instrument_platform"]),
        ('INSTRUMENT_PLATFORM="ILLUMINA"', ["instrument_platform"]),
        ('a="1" AND b="2" AND a="3"', ["a", "b"]),
    ],
)
def test_extracts_compared_field_names(query: str, expected: list[str]) -> None:
    assert extract_field_names(query) == expected


def test_an_operator_inside_a_quoted_value_is_not_a_field() -> None:
    assert extract_field_names('description="depth>=10m"') == ["description"]


def test_a_function_argument_is_not_a_field() -> None:
    assert extract_field_names("geo_box(10,20,30,40) AND tax_eq(9606)") == []


_names = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True).filter(
    lambda name: name not in {"and", "or", "not"}
)
_literals = st.text(
    alphabet=st.characters(blacklist_characters='"\\', blacklist_categories=("Cs",)), max_size=30
)


@given(st.lists(st.tuples(_names, _literals), min_size=1, max_size=5))
def test_nothing_inside_a_quoted_literal_is_taken_for_a_field(
    clauses: list[tuple[str, str]],
) -> None:
    query = " AND ".join(f'{name}="{literal}"' for name, literal in clauses)

    assert extract_field_names(query) == list(dict.fromkeys(name for name, _ in clauses))
