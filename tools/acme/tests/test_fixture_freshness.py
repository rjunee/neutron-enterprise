# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The CI staleness gate: committed fixtures are byte-reproducible from the
generator at the recorded seed (mirrors ne-schema's test_generated_freshness.py).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ne.acme.generator import ARTIFACTS, check
from ne.acme.pools import DEFAULT_SEED

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "acme-v1"


def test_recorded_fixtures_match_regeneration() -> None:
    assert check(FIXTURES, seed=DEFAULT_SEED) == []


def test_manifest_hashes_match_artifact_bytes() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
    for name in ARTIFACTS:
        if name == "manifest.json":
            continue
        actual = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
        assert manifest["sha256"][name] == actual, f"manifest hash mismatch for {name}"
