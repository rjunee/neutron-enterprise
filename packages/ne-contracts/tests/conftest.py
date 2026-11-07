# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared test fixtures for the ne-contracts suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_ne_config_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite hermetic against an ambient ``NE_CONFIG_FILE`` env var.

    A developer or CI machine that exports ``NE_CONFIG_FILE`` must not poison the
    config-layering tests; individual tests set it explicitly when needed.
    """
    monkeypatch.delenv("NE_CONFIG_FILE", raising=False)
