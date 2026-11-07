# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AST guardian: no production code broad-catches ``NeContractsError`` (NE-CTXB-1).

WHY: the ne-contracts error family now spans operationally-raised GOVERNED
refusals — chiefly ``ContextBudgetExceededError``. A broad
``except NeContractsError`` in production code would silently swallow such a
governed refusal, converting a typed, audited "budget exceeded" into an invisible
degradation. Catching a SPECIFIC member is fine; catching the family base is not.
This rule is enforced here by an AST scan across all production source, not by a
docstring — and the detector is itself unit-tested (test_guardian_detects_broad_except)
so the guardian cannot silently rot into a no-op.
"""

from __future__ import annotations

import ast
from pathlib import Path

_FORBIDDEN_NAME = "NeContractsError"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _handler_references_forbidden(handler: ast.ExceptHandler) -> bool:
    """True if this except-handler catches the forbidden family base directly."""
    exc = handler.type
    if exc is None:
        return False
    candidates: list[ast.expr] = list(exc.elts) if isinstance(exc, ast.Tuple) else [exc]
    for node in candidates:
        if isinstance(node, ast.Name) and node.id == _FORBIDDEN_NAME:
            return True
        if isinstance(node, ast.Attribute) and node.attr == _FORBIDDEN_NAME:
            return True
    return False


def find_broad_catches(source: str) -> list[int]:
    """Return the 1-indexed line numbers of every broad ``except NeContractsError``.

    Covers ``except*`` too — both ``except`` and ``except*`` parse to
    ``ast.ExceptHandler`` nodes.
    """
    tree = ast.parse(source)
    return [
        h.lineno
        for h in ast.walk(tree)
        if isinstance(h, ast.ExceptHandler) and _handler_references_forbidden(h)
    ]


def _production_source_files() -> list[Path]:
    files: list[Path] = []
    for parent in ("packages", "tools"):
        for src_dir in sorted((_REPO_ROOT / parent).glob("*/src")):
            files.extend(sorted(src_dir.rglob("*.py")))
    return files


def test_no_broad_except_ne_contracts_error_in_production() -> None:
    violations: list[str] = []
    for path in _production_source_files():
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for lineno in find_broad_catches(source):
            violations.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")
    assert violations == [], (
        "production code must catch SPECIFIC ne-contracts errors, never the "
        "family base (a broad catch silently swallows governed operational "
        f"refusals like ContextBudgetExceededError). Broad catches found: {violations}"
    )


def test_guardian_scanned_something() -> None:
    # Sanity: the scan must actually reach source (guards against a bad root).
    assert len(_production_source_files()) > 10


def test_guardian_detects_broad_except() -> None:
    name_form = "try:\n    x()\nexcept NeContractsError:\n    pass\n"
    attr_form = "try:\n    x()\nexcept ne.contracts.NeContractsError:\n    pass\n"
    tuple_form = "try:\n    x()\nexcept (ValueError, NeContractsError):\n    pass\n"
    star_form = "try:\n    x()\nexcept* NeContractsError:\n    pass\n"

    assert find_broad_catches(name_form) == [3]
    assert find_broad_catches(attr_form) == [3]
    assert find_broad_catches(tuple_form) == [3]
    assert find_broad_catches(star_form) == [3]

    # A specific member is allowed — the detector must NOT flag it.
    ok_form = "try:\n    x()\nexcept ContextBudgetExceededError:\n    pass\n"
    assert find_broad_catches(ok_form) == []
    bare_form = "try:\n    x()\nexcept Exception:\n    pass\n"
    assert find_broad_catches(bare_form) == []
