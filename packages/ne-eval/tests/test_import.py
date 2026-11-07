# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-eval."""

from __future__ import annotations

import ne.eval


def test_import_and_version() -> None:
    assert ne.eval.__version__ == "0.1.0"


def test_public_all_importable() -> None:
    for name in ne.eval.__all__:
        assert hasattr(ne.eval, name), f"missing public export {name!r}"
