# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Parity guard: the bundle's ephemeral (resolved-secrets) runtime dir that
``ne pilot`` excludes from the sealed bundle must NEVER drift back into an
object backup. ne-backup's excluded-top-dirs must be a superset of pilotcfg's
``_EPHEMERAL_SUBDIRS``.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from ne.backup.tarsafe import archive_dir

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PILOTCFG = _REPO_ROOT / "tools" / "ne-cli" / "src" / "ne" / "cli" / "pilotcfg.py"


def _ephemeral_subdirs_from_pilotcfg() -> set[str]:
    tree = ast.parse(_PILOTCFG.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_EPHEMERAL_SUBDIRS":
                    value = node.value
                    # The assignment is `frozenset({...})` / `set({...})` — unwrap
                    # the call to literal-eval the inner set.
                    if isinstance(value, ast.Call) and value.args:
                        value = value.args[0]
                    return set(ast.literal_eval(value))
    raise AssertionError("could not find _EPHEMERAL_SUBDIRS in pilotcfg.py")


def _backup_excluded_top_dirs() -> set[str]:
    default = inspect.signature(archive_dir).parameters["exclude_top_dirs"].default
    return set(default)


def test_ne_cli_pilotcfg_present() -> None:
    assert _PILOTCFG.is_file()


def test_backup_excludes_are_superset_of_ephemeral() -> None:
    ephemeral = _ephemeral_subdirs_from_pilotcfg()
    excluded = _backup_excluded_top_dirs()
    assert ephemeral, "pilotcfg._EPHEMERAL_SUBDIRS is unexpectedly empty"
    assert ephemeral <= excluded, (
        f"ne-backup must exclude every ephemeral bundle dir; missing: {ephemeral - excluded}"
    )
