# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-flywheel."""


def test_import_and_version() -> None:
    import ne.flywheel

    assert ne.flywheel.__version__ == "0.5.0"
