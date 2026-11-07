# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-contracts."""

import importlib.metadata


def test_import_and_version() -> None:
    import ne.contracts

    assert ne.contracts.__version__ == "1.29.0"
    assert ne.contracts.__version__ == importlib.metadata.version("ne-contracts")
