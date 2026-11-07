# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ABI vendor-manifest guardian (NE-085-spike).

Asserts the ABI lift is pinned to one reviewed commit with the mandated
keep/rewrite/drop triage, and that every file marked ``lifted`` exists at its NE
path and carries the ABI attribution (source repo + pin sha) — the
attribution invariant, enforced in CI.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parents[1]
_VENDOR_MANIFEST = _PACKAGE_DIR / "vendor" / "MANIFEST.toml"
_ABI_PIN = "a3e15eff03902091f23acfd9edbc95dc9695183f"

_MANIFEST = tomllib.loads(_VENDOR_MANIFEST.read_text(encoding="utf-8"))

# The six mandated lift targets (SPEC row + teardown §4), matched by basename.
_MANDATED = (
    "ApacheJenaTDB2.py",
    "TripleStoreService.py",
    "oxigraph_server.py",
    "validate_bfo_ontology.py",
    "templatablesparqlquery",
    "generic_test.py",  # the dual-backend conformance-suite pattern crib
)
_VALID_STATUS = {
    "lifted",
    "pattern-lifted",
    "planned-085a",
    "planned-085b",
    "planned-085c",
    "dropped",
}


def test_vendor_manifest_parses_and_pins() -> None:
    abi = _MANIFEST["abi"]
    assert re.fullmatch(r"[0-9a-f]{40}", abi["pinned_commit"])
    assert abi["pinned_commit"] == _ABI_PIN
    assert abi["reviewed_against"] == "4b0d12f3"
    assert abi["license_line"] == "MIT License, Copyright (C) 2025 Naas.ai"


def test_triage_covers_all_mandated_targets() -> None:
    entries = _MANIFEST["lift"]
    for entry in entries:
        assert entry["status"] in _VALID_STATUS, entry
    for mandated in _MANDATED:
        matches = [e for e in entries if mandated in e["source_path"]]
        assert matches, f"no triage entry for mandated target {mandated}"
        assert all(m["status"] in _VALID_STATUS for m in matches)


def test_lifted_files_exist_and_carry_attribution() -> None:
    lifted = [e for e in _MANIFEST["lift"] if e["status"] == "lifted"]
    assert lifted, "spike must have at least one lifted file"
    for entry in lifted:
        assert entry["ne_path"], entry
        target = _PACKAGE_DIR / entry["ne_path"]
        assert target.is_file(), f"lifted file missing: {entry['ne_path']}"
        text = target.read_text(encoding="utf-8")
        assert "jupyter-naas/abi" in text
        assert _ABI_PIN in text


def test_newly_lifted_085a_targets_flipped() -> None:
    # NE-085a lifts the two triple-store targets (Fuseki adapter + hash-diff sync).
    by_source = {e["source_path"]: e for e in _MANIFEST["lift"]}
    fuseki = next(e for s, e in by_source.items() if s.endswith("ApacheJenaTDB2.py"))
    sync = next(e for s, e in by_source.items() if s.endswith("TripleStoreService.py"))
    assert fuseki["status"] == "lifted"
    assert fuseki["ne_path"] == "src/ne/brain_ontology/store/fuseki.py"
    assert sync["status"] == "lifted"
    assert sync["ne_path"] == "src/ne/brain_ontology/store/sync.py"
