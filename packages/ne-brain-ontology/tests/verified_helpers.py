# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared, non-collected helpers for the NE-085c verified-answer tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.brain_ontology import PropertyGraphLift
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.brain_ontology.quarantine import SqliteTripleQuarantine
from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.brain_ontology.store.protocols import DatasetSpec
from ne.contracts.types import Principal

TESTS_DATA = Path(__file__).resolve().parent / "data"
FIXTURES_DIR = TESTS_DATA / "verified_templates"
FREEFORM_DIR = TESTS_DATA / "verified_templates_freeform"
COUNT_DIR = TESTS_DATA / "verified_templates_count"

_CONFIG_HASH = "a" * 64


class NullSink:
    def emit(self, event: Any) -> None:
        return None


class StubExpander:
    """A deny-nothing expander: labels = {principal.id} union groups (mirrors trimming)."""

    def expand(self, principal: Principal | str) -> frozenset[str]:
        if isinstance(principal, Principal):
            return frozenset({principal.id, *principal.groups})
        return frozenset({principal})


class RaisingExpander:
    """An expander that always raises — proves deny-by-erroring propagation (C7)."""

    def expand(self, principal: Principal | str) -> frozenset[str]:
        raise RuntimeError("expander backend down")


class EmptyExpander:
    """Returns an empty set — principal_labels must still union {principal.id}."""

    def expand(self, principal: Principal | str) -> frozenset[str]:
        return frozenset()


class RecordingStore:
    """Wraps a gateway, counting sparql_scoped calls (proves 'no store touch')."""

    def __init__(self, inner: ValidatingTripleStore) -> None:
        self._inner = inner
        self.scoped_calls = 0

    async def sparql_scoped(
        self, query: str, *, dataset: DatasetSpec
    ) -> Sequence[Mapping[str, str]]:
        self.scoped_calls += 1
        return await self._inner.sparql_scoped(query, dataset=dataset)

    def ontology_version(self) -> Any:
        return self._inner.ontology_version()


def make_gateway() -> ValidatingTripleStore:
    settings = OntologySettings(include_ontology_graph=False)
    return ValidatingTripleStore(
        OxigraphBackend(),
        build_validator(settings),
        SqliteTripleQuarantine(),
        NullSink(),
        settings=settings,
        config_hash=_CONFIG_HASH,
        compiled_shapes=load_compiled_shapes(),
    )


def account(key: str, acl: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "entity_class": "Account",
        "display_name": "Acct",
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
        "name": f"{key} Corp",
    }


async def seed_accounts(gateway: ValidatingTripleStore, nodes: Sequence[dict[str, Any]]) -> None:
    mem = InMemoryGraphStore()
    await mem.upsert_nodes(list(nodes))
    await PropertyGraphLift(mem, gateway).run()


def account_named(key: str, acl: list[str], name: str) -> dict[str, Any]:
    """An Account node with an explicit ``name`` (the count fixtures group by name)."""
    node = account(key, acl)
    node["name"] = name
    return node


def person_node(key: str, acl: list[str]) -> dict[str, Any]:
    """A Person node (an Order's required PLACED_BY target) — shape from the lift tests."""
    return {
        "key": key,
        "entity_class": "Person",
        "display_name": "Ada",
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
        "given_name": "Ada",
        "family_name": "Lovelace",
        "email": "ada@example.org",
    }


def order_node(key: str, acl: list[str]) -> dict[str, Any]:
    """An Order node — needs a canonical ``ORD-[0-9]{4,}`` number to clear SHACL."""
    digits = "".join(ch for ch in key if ch.isdigit()) or "1"
    return {
        "key": key,
        "entity_class": "Order",
        "display_name": "Order",
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
        "order_number": f"ORD-{int(digits):04d}",
        "status": "placed",
        "currency": "USD",
        "total_amount": "100.5",
        "placed_at": "2026-01-02T00:00:00+00:00",
    }


def edge(kind: str, key: str, from_key: str, to_key: str, acl: list[str]) -> dict[str, Any]:
    """A property-graph edge (Order REQUIRES an ACCOUNT + a PLACED_BY or it quarantines)."""
    return {
        "key": key,
        "edge_kind": kind,
        "from_key": from_key,
        "to_key": to_key,
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "config_hash": "c",
    }


async def seed_graph(
    gateway: ValidatingTripleStore,
    nodes: Sequence[dict[str, Any]],
    edges: Sequence[dict[str, Any]],
) -> None:
    """Upsert nodes + edges and lift them, asserting a CLEAN load (no quarantine).

    A quarantined element would silently drop a triple the count tests depend on, so
    this fails loud on ``report.quarantined`` — the count fixtures must land whole.
    """
    mem = InMemoryGraphStore()
    if nodes:
        await mem.upsert_nodes(list(nodes))
    if edges:
        await mem.upsert_edges(list(edges))
    report = await PropertyGraphLift(mem, gateway).run()
    assert report.quarantined == (), f"seed_graph quarantined {report.quarantined!r}"


def principal(pid: str, groups: tuple[str, ...] = ()) -> Principal:
    return Principal(id=pid, groups=groups)
