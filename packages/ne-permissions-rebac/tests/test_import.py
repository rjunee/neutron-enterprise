# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test — every module of the package imports cleanly."""

from __future__ import annotations

import importlib


def test_import_and_version() -> None:
    import ne.permissions_rebac

    assert ne.permissions_rebac.__version__ == "0.1.0"


def test_all_modules_import() -> None:
    for mod in (
        "ne.permissions_rebac._client",
        "ne.permissions_rebac.bootstrap",
        "ne.permissions_rebac.config",
        "ne.permissions_rebac.errors",
        "ne.permissions_rebac.gc",
        "ne.permissions_rebac.ports",
        "ne.permissions_rebac.resolver",
        "ne.permissions_rebac.share",
        "ne.permissions_rebac.sovereignty",
        "ne.permissions_rebac.testing.fakes",
    ):
        assert importlib.import_module(mod) is not None
