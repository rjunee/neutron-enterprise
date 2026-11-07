# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-runtime."""

from __future__ import annotations

import ne.runtime


def test_import_and_version() -> None:
    assert ne.runtime.__version__ == "0.1.0"


def test_public_all_importable() -> None:
    for name in ne.runtime.__all__:
        assert hasattr(ne.runtime, name), f"dangling __all__ export {name!r}"


def test_dead_write_actions_factory_stays_deleted() -> None:
    # NE-SCOPE-SEAM: create_write_actions was retired as a dead, wrong-shaped
    # factory (zero call sites); build_action_bindings (see its docstring in
    # action_tools.py) is the blessed NE-050 composition seam. Pin the deletion
    # against silent reintroduction.
    assert "create_write_actions" not in ne.runtime.__all__
    assert not hasattr(ne.runtime, "create_write_actions")
