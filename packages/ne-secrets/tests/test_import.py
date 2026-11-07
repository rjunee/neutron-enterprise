# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-secrets."""


def test_import_and_version() -> None:
    import ne.secrets

    assert ne.secrets.__version__ == "0.1.0"
