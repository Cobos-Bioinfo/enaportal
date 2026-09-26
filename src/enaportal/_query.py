"""Reading field names back out of an ENA query string.

Used to reject typos locally. Deliberately incomplete: anything this cannot
parse yields no field name, because wrongly rejecting a query ENA would have
accepted is worse than sending one it will not.
"""

from __future__ import annotations

import re
from typing import Final

_QUOTED: Final = re.compile(r'"(?:[^"\\]|\\.)*"')
_COMPARISON: Final = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*(?:>=|<=|!=|=|>|<)")
_BOOLEAN_KEYWORDS: Final = frozenset({"and", "or", "not"})


def extract_field_names(query: str) -> list[str]:
    """The field names a query compares against a value, in order of first use.

    Quoted literals are blanked first so that an operator inside a string value
    cannot be mistaken for a comparison. Function calls such as tax_tree(4932)
    name a function rather than a field and are ignored.
    """
    names: list[str] = []
    for match in _COMPARISON.finditer(_QUOTED.sub('""', query)):
        name = match.group(1).lower()
        if name not in _BOOLEAN_KEYWORDS and name not in names:
            names.append(name)
    return names
