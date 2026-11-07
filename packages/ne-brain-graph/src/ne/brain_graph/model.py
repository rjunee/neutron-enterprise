# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Frozen Tier-2 graph model types + deterministic identity helpers.

These are the caller-facing return shapes of :class:`ne.brain_graph.read.EntityGraph`
and the internal construction types. The no-leak invariant lives here: an
:class:`EntityNode`/:class:`EntityEdge` NEVER carries the ``acl`` label set or the
governance hashes — the read layer strips them before constructing these models,
so an ACL label can never leak into a returned payload.

Identity is deterministic UUIDv5 (the ``point_id_for`` discipline,
``ne.brain.qdrant`` qdrant.py:57-59): the same source record always maps to the
same node ``key`` forever, so re-sync is a property overwrite, never a duplicate,
and the keys are stable IRIs for the NE-085 RDF lift.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

_ENTITY_NAMESPACE = uuid.NAMESPACE_URL
_EDGE_NAMESPACE = uuid.NAMESPACE_URL


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class EntityKey(_Frozen):
    """A principal-agnostic handle for a node: (LinkML class, deterministic key)."""

    entity_class: str
    key: str


class EntityNode(_Frozen):
    """A trimmed entity node. ``properties`` NEVER contains ``acl``/hash fields."""

    key: str
    entity_class: str
    display_name: str
    source_system: str
    source_ref: str
    #: "source" = non-LLM-extracted (connector backbone OR ne:er ER-derived —
    #: SAME_AS edges legitimately carry it); "extracted"/"confirmed" = the
    #: ne:llm pathway (contracts ADR-0014 semantics). The read layer sets this
    #: EXPLICITLY from the stored element (fail-closed for ne:llm elements) —
    #: never rely on the default when mapping store rows (design §5.2
    #: correction: these frozen models are NOT extra="forbid", so a
    #: misspelled key silently defaults toward the trusted-sounding value).
    assurance: str = "source"
    properties: Mapping[str, object] = {}


class EntityEdge(_Frozen):
    """A trimmed relation. ACL/governance fields are stripped before construction."""

    key: str
    edge_kind: str
    from_key: str
    to_key: str
    source_system: str
    source_ref: str
    #: "source" = non-LLM-extracted (connector backbone OR ne:er ER-derived —
    #: SAME_AS edges legitimately carry it); "extracted"/"confirmed" = the
    #: ne:llm pathway (contracts ADR-0014 semantics). The read layer sets this
    #: EXPLICITLY from the stored element (fail-closed for ne:llm elements) —
    #: never rely on the default when mapping store rows (design §5.2
    #: correction: these frozen models are NOT extra="forbid", so a
    #: misspelled key silently defaults toward the trusted-sounding value).
    assurance: str = "source"


class Subgraph(_Frozen):
    """A trimmed set of nodes + edges (neighbors / subgraph results)."""

    nodes: tuple[EntityNode, ...] = ()
    edges: tuple[EntityEdge, ...] = ()


class GraphPathRecord(_Frozen):
    """An ordered trimmed path: ``nodes[i] -(edges[i])-> nodes[i+1]``."""

    nodes: tuple[EntityNode, ...] = ()
    edges: tuple[EntityEdge, ...] = ()


def entity_key_for(source_system: str, entity_class: str, source_id: str, record_id: str) -> str:
    """Deterministic node key (UUIDv5) — stable across runs and processes."""
    name = f"ne-entity:{source_system}:{entity_class}:{source_id}:{record_id}"
    return str(uuid.uuid5(_ENTITY_NAMESPACE, name))


def edge_key_for(edge_kind: str, from_key: str, to_key: str, source_ref: str) -> str:
    """Deterministic edge key (UUIDv5) over (kind, endpoints, asserting ref)."""
    name = f"ne-edge:{edge_kind}:{from_key}:{to_key}:{source_ref}"
    return str(uuid.uuid5(_EDGE_NAMESPACE, name))


def node_source_ref(source_id: str, record_id: str) -> str:
    """The T1 citation-equal source-of-record pointer (tier2-spec §4.3)."""
    return f"{source_id}:{record_id}"
