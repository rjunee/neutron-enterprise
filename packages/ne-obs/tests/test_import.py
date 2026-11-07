# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-obs."""


def test_import_and_version() -> None:
    import ne.obs

    assert ne.obs.__version__ == "0.1.0"
