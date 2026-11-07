# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The server and client CSV exporters neutralise the same characters.

Governance evidence leaves through the server-authored audit export and through
client-built exports such as Approvals. Both carry attacker-reachable text and
are opened in spreadsheets, so their formula-prefix classifications must not
drift. This guard reads both owning definitions and fails loud if either source
definition is absent or empty; it does not restate an unbound third list.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from ne.api.routers.audit import _FORMULA_PREFIXES

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CSV_TS = _REPO_ROOT / "packages" / "ne-console" / "src" / "primitives" / "csv.ts"

# Anchored on the exported name: a rename or restructure fails loud instead of
# silently comparing against an unrelated or stale array literal.
_TS_DECLARATION = re.compile(
    r"export const FORMULA_PREFIXES\s*=\s*\[(?P<items>.*?)\]\s*as const;",
    re.DOTALL,
)


def _console_formula_prefixes() -> tuple[str, ...]:
    assert _CSV_TS.is_file(), f"console CSV module not found at {_CSV_TS}"
    match = _TS_DECLARATION.search(_CSV_TS.read_text(encoding="utf-8"))
    assert match is not None, (
        "could not locate `export const FORMULA_PREFIXES = [...] as const;` in "
        f"{_CSV_TS}; update this guard if the owning declaration changes"
    )
    items = [chunk.strip() for chunk in match.group("items").split(",") if chunk.strip()]
    assert items, f"FORMULA_PREFIXES in {_CSV_TS} parsed as empty"

    decoded: list[str] = []
    for item in items:
        value = ast.literal_eval(item)
        assert isinstance(value, str) and len(value) == 1, (
            f"expected single-character string literals in FORMULA_PREFIXES, got {item!r}"
        )
        decoded.append(value)
    return tuple(decoded)


def test_console_and_server_neutralise_the_same_formula_prefixes() -> None:
    assert set(_console_formula_prefixes()) == set(_FORMULA_PREFIXES)


def test_both_definitions_are_nonempty_and_cover_the_known_openers() -> None:
    """Set equality alone is satisfied by two empty definitions."""
    known = {"=", "+", "-", "@", "\t", "\r"}
    assert set(_console_formula_prefixes()) >= known
    assert set(_FORMULA_PREFIXES) >= known


@pytest.mark.parametrize("prefix", sorted(set(_FORMULA_PREFIXES)))
def test_every_server_prefix_is_load_bearing(prefix: str) -> None:
    from ne.api.routers.audit import _csv_safe_cell

    assert _csv_safe_cell(f"{prefix}1+1") == f"'{prefix}1+1"
    # Bidirectional control: the same character away from the leading position
    # is ordinary text and must remain byte-identical.
    assert _csv_safe_cell(f"a{prefix}1") == f"a{prefix}1"
