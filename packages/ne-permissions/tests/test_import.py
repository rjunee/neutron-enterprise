# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-permissions."""


def test_import_and_version() -> None:
    import ne.permissions

    assert ne.permissions.__version__ == "0.1.0"
