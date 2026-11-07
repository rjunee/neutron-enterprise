# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-brain."""

from __future__ import annotations

import subprocess
import sys


def test_import_and_version() -> None:
    import ne.brain

    assert ne.brain.__version__ == "0.1.0"


def test_pgvector_is_lazy_and_unexported() -> None:
    # The pgvector ChunkStore alternate (NE-091) is an OPTIONAL [pgvector] extra:
    # importing ne.brain alone must NEVER pull in ne.brain.pgvector or psycopg, so
    # a Qdrant-only deployment ships/imports nothing pgvector-specific (the
    # ne-govern test_cedar_is_lazy_and_unexported mechanism, verbatim).
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ne.brain, sys; "
            "raise SystemExit(1 if ('psycopg' in sys.modules or "
            "'ne.brain.pgvector' in sys.modules) else 0)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
