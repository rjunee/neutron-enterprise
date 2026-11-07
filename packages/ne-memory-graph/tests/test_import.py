# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Importing ne.memory_graph pulls NO graphiti_core/neo4j/litellm/openai (C3/C4)."""

from __future__ import annotations

import subprocess
import sys


def test_import_pulls_no_sidecar_or_provider_sdk() -> None:
    """Checked in a CLEAN subprocess so an unrelated test that imports ne.gateway
    can't pollute the assertion (the ne-serve test_import precedent)."""
    code = (
        "import ne.memory_graph, sys; "
        "banned = [m for m in sys.modules "
        "if m.split('.')[0] in ('graphiti_core','graphiti','neo4j','litellm','openai','posthog')]; "
        "print(sorted(banned))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout
