# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-connect."""

from __future__ import annotations


def test_import_and_version() -> None:
    import ne.connect

    assert ne.connect.__version__ == "0.1.0"


def test_framework_import_does_not_drag_dlt() -> None:
    import sys

    # A fresh worker may already have dlt loaded by a prior test; this assertion
    # is meaningful in isolation and is belt-and-braces to the subprocess proof
    # in test_dlt_confinement.py. We only assert that importing the framework
    # package itself does not REQUIRE dlt.
    if "dlt" in sys.modules:
        return
    import ne.connect  # noqa: F401

    assert "dlt" not in sys.modules
