# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-mcp."""


def test_import_and_version() -> None:
    import ne.mcp

    assert ne.mcp.__version__ == "0.1.0"
