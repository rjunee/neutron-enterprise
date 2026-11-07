# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""P1-5 guardian: the universal C3/C4 chokepoint contracts cover EVERY ne.* package.

The mcp-sdk/openfga/psycopg/t2-sidecar/memory-graph-sidecar contracts hand-enumerate
``source_modules``. They were authored before ``ne.worker``/``ne.permissions_rebac``/
``ne.memory_graph`` existed, so a newly-added package can be SILENTLY uncovered — a
future ``import psycopg`` / ``import openfga_sdk`` in the new package would merge with
``lint-imports`` GREEN, bypassing the C3 chokepoint + C4 out-of-process gates with NO
CI signal.

This guardian parses ``.importlinter`` and structurally forbids that: every UNIVERSAL
chokepoint contract's ``source_modules`` MUST equal the FULL ``root_packages`` set, and
``root_packages`` MUST equal the ne.* packages discovered on disk. It also asserts every
contract section is explicitly classified universal-vs-scoped, so a NEW contract can
never be silently unclassified. The guardian FAILS until a new package is added to
``root_packages`` AND every universal contract (non-vacuous in both directions).
"""

from __future__ import annotations

import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# --- contract classification (read each contract's INTENT before editing) ----------
#
# UNIVERSAL chokepoint contracts: every ne.* package MUST route through the one
# designated chokepoint module, so `source_modules` must list ALL root packages. House
# style INCLUDES the chokepoint owner itself in source_modules — `ignore_imports` carves
# ONLY the single designated chokepoint-module edge (e.g. ne.mcp._sdk -> mcp), so even
# the owner package, when imported OUTSIDE that module, is still gated. A new ne.*
# package added to root_packages MUST be added to every contract below.
UNIVERSAL_CHOKEPOINT_CONTRACTS = frozenset(
    {
        "c3-provider-sdk-chokepoint",  # provider SDKs / litellm only via ne.gateway._litellm
        "c3-mcp-sdk-chokepoint",  # mcp SDK only via ne.mcp._sdk
        "c3-openfga-sdk-chokepoint",  # openfga SDK only via ne.permissions_rebac._client
        "c3-psycopg-chokepoint",  # psycopg only via the _pg chokepoints
        "t2-sidecars-out-of-process",  # C4: splink/duckdb/igraph/lightrag/pipmaster sidecars
        "memory-graph-sidecar-out-of-process",  # C4: graphiti/neo4j/posthog closure
    }
)

# SCOPED contracts: each legitimately constrains ONE named source package (NOT the full
# set) — its `source_modules` is deliberately a single package, so it is EXCLUDED from
# the full-coverage assertion. A new scoped contract must be classified here with intent.
SCOPED_CONTRACTS = frozenset(
    {
        "ne-api-decoration-free",  # decoration-free ne.api graph (enqueue by NAME only)
        "ne-net-leaf",  # stdlib-only leaf: ne.net
        "ne-chat-interface-lane",  # interface adapter: ne.chat (no heavy-plane imports)
        "ne-envbuild-leaf",  # filelock-only leaf: ne.envbuild
        "brain-ontology-lane",  # T3 additive lane: ne.brain_ontology
        "runtime-verified-lane",  # C3: ne.runtime never imports ne.brain_ontology (Protocol seam)
        "ne162-gateway-redactor-protocol-seam",  # seam: Redactor via ne.contracts, not ne.govern
    }
)


def _load() -> configparser.ConfigParser:
    # Contract section names contain ':', so restrict delimiters to bare '=' only.
    cp = configparser.ConfigParser(delimiters=("=",))
    read = cp.read(REPO_ROOT / ".importlinter", encoding="utf-8")
    assert read, f".importlinter not found under {REPO_ROOT}"
    return cp


def _modules(value: str) -> set[str]:
    return {line.strip() for line in value.strip().splitlines() if line.strip()}


def _discover_ne_packages() -> set[str]:
    discovered: set[str] = set()
    for pattern in ("packages/*/src/ne/*", "tools/*/src/ne/*"):
        for p in REPO_ROOT.glob(pattern):
            if p.is_dir() and (p / "__init__.py").exists():
                discovered.add("ne." + p.name)
    return discovered


def _contract_names(cp: configparser.ConfigParser) -> set[str]:
    prefix = "importlinter:contract:"
    return {s.removeprefix(prefix) for s in cp.sections() if s.startswith(prefix)}


def test_root_packages_match_workspace_ne_packages() -> None:
    cp = _load()
    declared = _modules(cp["importlinter"]["root_packages"])
    discovered = _discover_ne_packages()
    missing = discovered - declared
    extra = declared - discovered
    assert not missing and not extra, (
        f".importlinter root_packages is out of sync with the ne.* packages on disk: "
        f"missing (add to root_packages AND every universal chokepoint contract) "
        f"{sorted(missing)}; stale (remove) {sorted(extra)}."
    )


def test_every_contract_is_explicitly_classified() -> None:
    cp = _load()
    actual = _contract_names(cp)
    classified = UNIVERSAL_CHOKEPOINT_CONTRACTS | SCOPED_CONTRACTS
    unclassified = actual - classified
    stale = classified - actual
    assert not unclassified and not stale, (
        f"a new/removed .importlinter contract must be classified in this guardian: "
        f"unclassified (add to UNIVERSAL_CHOKEPOINT_CONTRACTS for full-coverage "
        f"source_modules, or SCOPED_CONTRACTS with its intent) {sorted(unclassified)}; "
        f"stale classification entries (remove) {sorted(stale)}."
    )


def test_universal_chokepoint_contracts_cover_every_package() -> None:
    cp = _load()
    root = _modules(cp["importlinter"]["root_packages"])
    for name in sorted(UNIVERSAL_CHOKEPOINT_CONTRACTS):
        section = f"importlinter:contract:{name}"
        sources = _modules(cp[section]["source_modules"])
        missing = root - sources
        extra = sources - root
        assert not missing and not extra, (
            f"universal chokepoint contract {name!r} must list ALL root packages in "
            f"source_modules: missing {sorted(missing)}; extra {sorted(extra)}. "
            f"A new ne.* package is added here so its imports still route through the "
            f"chokepoint; if a package legitimately does NOT belong, reclassify the "
            f"contract as scoped (do NOT silently drop coverage)."
        )
