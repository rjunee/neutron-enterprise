# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Parity guard: ``seeds.py``'s hard-coded signatures vs ``operational.yaml``.

2026-07-28 Argus P2: ``seeds.py``'s own docstring claims
``SEED_CLASS_SIGNATURES`` is "Transcribed from ... operational.yaml ...
verified against source at build time, lines ~66-260" — a normative prose
claim with a line-number citation and no test behind it
([[written-rules-disarm-review]]: a rule stated in a docstring disarms
review of itself, shifting the question from "is this right?" to "does the
code match the stated rationale?"). ``hygiene.py`` got the equivalent
AST-parity test in the same review round (``test_hygiene.py``); this is the
same fix applied to the second deliberate hard-coded transcription in the
package. Reads ``operational.yaml`` directly — a test-only file read, not an
import of ``ne.schema`` (which would violate this package's
dependency-free/no-cross-package-import posture, C5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ne.schema_bootstrap.seeds import PROVENANCE_SLOTS, SEED_CLASS_SIGNATURES


def _operational_yaml_path() -> Path:
    # tests/ -> packages/ne-schema-bootstrap -> packages/ -> ne-schema/...
    return (
        Path(__file__).resolve().parents[2]
        / "ne-schema"
        / "src"
        / "ne"
        / "schema"
        / "schemas"
        / "operational.yaml"
    )


def _load_operational_classes() -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    path = _operational_yaml_path()
    assert path.is_file(), f"operational.yaml not found at {path}"
    doc: dict[str, Any] = yaml.safe_load(path.read_text())

    raw_classes: dict[str, Any] = doc["classes"]
    provenance_slots = frozenset(raw_classes["OperationalEntity"]["slots"])

    classes: dict[str, frozenset[str]] = {}
    for name, spec in raw_classes.items():
        if name == "OperationalEntity" or spec.get("tree_root"):
            continue
        classes[name] = frozenset(spec.get("attributes", {}).keys())
    return classes, provenance_slots


def test_seed_class_signatures_match_operational_yaml() -> None:
    classes, provenance_slots = _load_operational_classes()

    # Non-vacuity control: the YAML parse actually found the 9 non-abstract
    # operational classes, not an empty/malformed document comparing
    # trivially.
    assert len(classes) == 9

    assert set(SEED_CLASS_SIGNATURES) == set(classes), (
        "seeds.py's class set has drifted from operational.yaml's non-abstract classes"
    )
    for name, attrs in classes.items():
        assert SEED_CLASS_SIGNATURES[name] == attrs, (
            f"seeds.py['{name}'] has drifted from operational.yaml's {name} attributes: "
            f"seeds.py={sorted(SEED_CLASS_SIGNATURES[name])} yaml={sorted(attrs)}"
        )
    assert provenance_slots == PROVENANCE_SLOTS, (
        "PROVENANCE_SLOTS has drifted from OperationalEntity's slots in operational.yaml"
    )
