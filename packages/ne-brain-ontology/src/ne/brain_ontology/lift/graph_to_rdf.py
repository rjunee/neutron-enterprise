# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic Tier-2 property-graph -> validated RDF lift (NE-085b).

Reads the merged Tier-2 AGE entity graph through an INJECTED
:class:`GraphScanSource` (the package-local structural twin of the T2 platform
batch-read capability — ``AgeGraphStore``/``InMemoryGraphStore`` both satisfy it;
NO ``ne.brain_graph`` import anywhere under ``src/``, enforced by the
brain-ontology-lane import contract, C3) and mints RDF triples typed against the
LinkML/CCO-aligned ``neop:*`` classes. Every triple is:

  * DENY-BY-DEFAULT typed: a node whose ``entity_class`` has no LinkML mapping is
    NOT lifted (counted), a node property not on the entity's contract is skipped,
    an unknown edge kind is skipped — untyped data has no business in the tier.
  * ACL-partitioned into per-label named graphs (``acl.graph_iri_for_label``):
    node triples into a graph per node ACL label; edge/IdentityAssertion triples
    into the graphs for the CONJUNCTION ``edge.acl ∩ from.acl ∩ to.acl`` (T2's
    hop rule — this can only UNDER-return vs T2, never over-return; the sanctioned
    direction). An element with no/empty ACL, or an edge with an empty
    intersection, is skipped + counted (fail-closed).
  * pushed through the fail-closed SHACL gateway
    (:meth:`ValidatingTripleStore.insert_triples_into`) — never a direct backend
    write. A non-conforming batch is quarantined WHOLE (its id recorded, the valid
    siblings still land); a validator error propagates as a denial. Each element's
    validation payload also carries its desired-union two-hop CBD ``context_triples``
    (NE-085e — see :meth:`PropertyGraphLift.run`), so an entity with a SHACL-required
    outgoing edge (``sh:minCount>=1``, e.g. ``Order.account``/``placed_by``,
    ``Ticket.belongs_to``) lands on a fresh initial load instead of being quarantined
    whole for an edge that simply hadn't landed yet.

SAME_AS is reified as a validated ``nea:IdentityAssertion`` resource (``owl:sameAs``
is deliberately NOT asserted — the generated closed shapes would quarantine it and
no runtime reasoner consumes it). IRIs are minted from the deterministic T2
UUIDv5 keys, so an UNCHANGED re-lift is idempotent by RDF set semantics and
citations join back to T2.

MUTATION-AWARE via :meth:`PropertyGraphLift.reconcile` (NE-085d). :meth:`run` is the
APPEND-ONLY initial-load path (per-element quarantine granularity — a valid sibling
still lands when a neighbour is quarantined); :meth:`reconcile` is the RECONCILING
path safe to re-run over CHANGED T2 data. Both validate each element/graph together
with a desired-union two-hop CBD context (NE-085d/NE-085e) — sourced from the DESIRED
T2 mapping, never the stale store — so a cross-graph ``sh:minCount``/``sh:class``
(a required edge, or an edge target's ``rdf:type`` living in a different per-ACL
graph) is satisfiable from data that is part of the SAME load. ``run`` remains
APPEND-ONLY and NOT mutation-aware: it still validates each element's OWN existing
store CBD (not the desired union) for anything already landed, so a changed
single-valued slot on a re-run still quarantines by ``sh:maxCount`` (by design — see
``test_append_run_still_quarantines_changed_slot``); ``reconcile`` is the only
mutation-safe path. ``reconcile`` computes the COMPLETE desired per-ACL-named-graph
triple partition from a full T2 scan and, per graph, does a VALIDATE-THEN-ATOMIC-SWAP
through the fail-closed gateway (:meth:`ValidatingTripleStore.replace_acl_graph`): it
validates the graph's full recomputed content (plus the desired-union two-hop CBD
context) and only on conformance atomically REPLACES the graph via the backend's
whole-named-graph ``replace_graph_turtle``, purges vacated graphs, and skips
byte-identical graphs (a true no-op on an unchanged re-lift). This closes BOTH
previously-tracked failure modes: (1) CORRECTNESS — a changed single-valued slot
reconciles because the rebuilt graph holds ONLY the new value (no ``sh:maxCount``
conflict); (2) C7 ACCESS CONTROL — an ACL change (label ``alpha`` → ``beta``)
vacates/rebuilds the old ``alpha`` graph WITHOUT the entity, closing the over-return.
Fail-closed is PRESERVED: validation runs before any destructive op (no
delete-then-validate window), and a violation quarantines the graph while leaving its
PRIOR content intact. A landed element whose desired neighbour independently
quarantines (e.g. an Order lands while its Account sibling has a bad literal) is the
documented per-element-granularity partial state, backstopped by the nightly
:meth:`ValidatingTripleStore.validate_full_graph`.

Recurring lift is now SAFE (reconciling): schedule :meth:`reconcile`, NOT :meth:`run`
— ``run`` remains the append-only initial-load path with per-element quarantine
granularity and MUST NOT be wired recurring over mutating T2 data. The swap is atomic
PER graph (not across graphs); a crash mid-reconcile leaves each graph individually
valid and a re-run converges (nightly :meth:`ValidatingTripleStore.validate_full_graph`
is the global backstop).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict
from rdflib import URIRef
from rdflib.namespace import SH, XSD

from ne.contracts.source_authority import SourceAuthorityStamp
from ne.schema import EDGE_DEFS, ENTITY_CLASSES, ENTITY_CONTRACTS

from ..acl import graph_iri_for_label
from ..gateway import DeclaredLiteral, TripleObject, TripleQuarantinedError, ValidatingTripleStore
from ..shapes import load_compiled_shapes

__all__ = ["GraphScanSource", "LiftReport", "PropertyGraphLift"]

# -- T2 property-name constants (the map_record output shape) ------------------
NODE_KEY = "key"
ENTITY_CLASS = "entity_class"
ACL = "acl"
SOURCE_SYSTEM = "source_system"
SOURCE_REF = "source_ref"
SOURCE_STATUS = "source_status"
SOURCE_AUTHORITY_STAMPS = "source_authority_stamps"
EDGE_KIND = "edge_kind"
FROM_KEY = "from_key"
TO_KEY = "to_key"
#: T2 er.merge constants (cross-checked against ne.brain_graph.er.merge in tests).
SAME_AS_EDGE_KIND = "SAME_AS"
ER_SOURCE_SYSTEM = "ne:er"

#: Node property keys never lifted as operational slots (platform provenance /
#: governance / structural). A contract field in this set (source_system/
#: source_ref) is provenance, not an operational attribute.
GOVERNANCE_SKIP = frozenset(
    {
        "acl",
        "content_hash",
        "config_hash",
        "schema_version",
        "synced_at",
        "display_name",
        "key",
        "entity_class",
        "source_system",
        "source_ref",
        "canonical_key",
        "source_status",
        "source_authority_stamps",
    }
)

# -- IRI minting + namespaces (deterministic from the T2 UUIDv5 keys) ----------
NEOP = "https://neutron.enterprise/schema/operational/"
NEA = "https://neutron.enterprise/schema/alignment/"
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

_EDGE_DEF_BY_KIND = {d.edge_kind: d for d in EDGE_DEFS}

#: Schemes that mark a plain-``str`` triple object as an IRI (a reconcile CBD seed) —
#: mirrors the gateway's ``_IRI_SCHEMES``. A :class:`DeclaredLiteral` is never a seed.
_CONTEXT_IRI_SCHEMES = ("http://", "https://", "urn:")


def entity_iri(key: str) -> str:
    """Stable IRI for a T2 node key — a re-lift is idempotent; citations join back."""
    return f"urn:ne:entity:{key}"


def sameas_iri(edge_key: str) -> str:
    """Stable IRI for the IdentityAssertion reifying a SAME_AS edge."""
    return f"urn:ne:sameas:{edge_key}"


def _lexical(value: Any) -> str:
    """RDF lexical form. ``bool`` -> XSD ``true``/``false`` (``str(True)='True'``
    would FAIL ``sh:datatype xsd:boolean``); everything else ``str(value)`` (mapper
    props are already ``model_dump(mode='json')`` lexical). The gateway's datatype
    map re-types the literal on insert.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@lru_cache(maxsize=1)
def _slot_literal_datatypes() -> Mapping[str, str | None]:
    """Map each property-shape ``sh:path`` predicate → its declared literal datatype.

    ``None`` = a plain (``xsd:string``/RDF 1.1) literal; a datatype IRI string =
    that typed literal (``xsd:integer``, ``xsd:boolean``, ``xsd:dateTime`` …). A
    predicate in this map is a LITERAL slot, so the lift emits a
    :class:`DeclaredLiteral` for it — schema-authoritative, never subject to the
    gateway's IRI-vs-literal heuristic (FIX-1: an IRI-shaped ``xsd:string`` value
    like ``Document.uri`` lands as a Literal, not a URIRef).

    Derived from the SAME compiled per-write shapes the validator enforces, so lift
    typing can never drift from what quarantines. A shape with ``sh:datatype`` maps
    to that datatype (``xsd:string`` → ``None``); one with only ``sh:nodeKind
    sh:Literal`` maps to ``None``; anything else (IRI-ranged relationships) is
    skipped. Conflicting declared datatypes for one predicate degrade to ``None``
    (untyped literal — still fail-closed at validation), mirroring the gateway's
    defensive datatype-map conflict policy.
    """
    graph = load_compiled_shapes().per_write_graph()
    mapping: dict[str, str | None] = {}
    conflicting: set[str] = set()
    for prop_shape, _p, path in graph.triples((None, SH.path, None)):
        if not isinstance(path, URIRef):
            continue
        datatype = graph.value(prop_shape, SH.datatype)
        if isinstance(datatype, URIRef):
            declared: str | None = None if datatype == XSD.string else str(datatype)
        elif graph.value(prop_shape, SH.nodeKind) == SH.Literal:
            declared = None
        else:
            continue
        key = str(path)
        if key in conflicting:
            continue
        if key in mapping and mapping[key] != declared:
            del mapping[key]
            conflicting.add(key)
            continue
        mapping[key] = declared
    return mapping


def _slot_object(predicate: str, value: Any) -> TripleObject:
    """A schema-authoritative :class:`DeclaredLiteral` for a known literal slot, else
    a plain ``str`` (the heuristic fallback for a schema-signal-less predicate)."""
    datatypes = _slot_literal_datatypes()
    if predicate in datatypes:
        return DeclaredLiteral(_lexical(value), datatypes[predicate])
    return _lexical(value)


@runtime_checkable
class GraphScanSource(Protocol):
    """The narrow, non-principal batch-read seam the lift consumes (T2 twin).

    Injected at the composition root under platform identity; ``AgeGraphStore`` and
    ``InMemoryGraphStore`` both satisfy it structurally (the LightRAG projector
    precedent). NOT a runtime ``ne.brain_graph`` import (C3).
    """

    async def scan_nodes(self, entity_class: str | None = None) -> Sequence[Mapping[str, Any]]: ...

    async def scan_edges(self) -> Sequence[Mapping[str, Any]]: ...


class GraphAuthorityStampSink(Protocol):
    """Retain exact contributor stamps beside the immutable lifted subjects."""

    async def retain(
        self,
        subjects: tuple[str, ...],
        stamps: tuple[SourceAuthorityStamp, ...],
        graphs: tuple[str, ...],
    ) -> None: ...


class LiftReport(BaseModel):
    """Frozen outcome of one :meth:`PropertyGraphLift.run` or :meth:`reconcile`."""

    model_config = ConfigDict(frozen=True)

    nodes_lifted: int = 0
    edges_lifted: int = 0
    sameas_lifted: int = 0
    nodes_skipped_unmapped: int = 0
    edges_skipped_unmapped: int = 0
    elements_skipped_no_acl: int = 0
    edges_skipped_empty_intersection: int = 0
    elements_skipped_unknown_authority: int = 0
    quarantined: tuple[str, ...] = ()
    #: reconcile() only — per-ACL-graph swap tallies (0 for the append run() path).
    graphs_rebuilt: int = 0
    graphs_deleted: int = 0
    graphs_unchanged: int = 0


@dataclass(frozen=True)
class _MappedElement:
    """One deterministically-mapped T2 element: its minted triples + target graphs.

    The shared output of :meth:`PropertyGraphLift._map_elements`, consumed by both the
    append :meth:`~PropertyGraphLift.run` (push per element) and the reconciling
    :meth:`~PropertyGraphLift.reconcile` (accumulate the desired per-graph partition).
    """

    kind: Literal["node", "edge", "sameas"]
    triples: list[tuple[str, str, TripleObject]]
    graphs: tuple[str, ...]
    authority_stamps: tuple[SourceAuthorityStamp, ...] = ()


def _identity_key(triple: tuple[str, str, TripleObject]) -> tuple[str, str, object]:
    """Set-identity key for a lift triple (a :class:`DeclaredLiteral` keys by its
    lexical+datatype, a plain ``str`` object by itself) — the reconcile dedupe key."""
    subject, predicate, obj = triple
    okey: object = ("lit", obj.lexical, obj.datatype) if isinstance(obj, DeclaredLiteral) else obj
    return (subject, predicate, okey)


class PropertyGraphLift:
    """Lifts the injected T2 graph into the validated RDF knowledge graph.

    :meth:`run` is the APPEND-ONLY initial-load path (per-element quarantine
    granularity). :meth:`reconcile` is the MUTATION-AWARE path (NE-085d): a per-ACL
    named-graph validate-then-atomic-swap safe to re-run over CHANGED T2 data.
    """

    def __init__(
        self,
        source: GraphScanSource,
        store: ValidatingTripleStore,
        *,
        require_source_authority: bool = False,
        authority_sink: GraphAuthorityStampSink | None = None,
    ) -> None:
        self._source = source
        self._store = store
        self._require_source_authority = require_source_authority
        self._authority_sink = authority_sink

    def _authority_stamps(
        self, element: Mapping[str, Any]
    ) -> tuple[SourceAuthorityStamp, ...] | None:
        if element.get(SOURCE_STATUS) == "no_source":
            return ()
        raw = element.get(SOURCE_AUTHORITY_STAMPS)
        if not raw:
            return None if self._require_source_authority else ()
        if not isinstance(raw, list | tuple):
            return None
        try:
            stamps = tuple(SourceAuthorityStamp.model_validate(item) for item in raw)
        except (TypeError, ValueError):
            return None
        if not stamps or len(stamps) != len(set(stamps)):
            return None
        return stamps

    def _map_elements(
        self, nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]
    ) -> tuple[list[_MappedElement], int, int, int, int, int]:
        """Deterministically map T2 nodes+edges → mapped elements + the four skip counts.

        The SHARED deny-by-default mapping loop (extracted from ``run`` verbatim so its
        ordering/skip/count semantics are unchanged): nodes sorted by key FIRST (so an
        edge's endpoints exist), then edges sorted by key; the node-ACL index is built
        over ALL nodes first for the edge intersection rule. Returns ``(mapped,
        nodes_skipped_unmapped, edges_skipped_unmapped, elements_skipped_no_acl,
        edges_skipped_empty_intersection)``.
        """
        # Node ACLs indexed by key — the edge intersection rule needs both endpoints.
        node_acl: dict[str, frozenset[str]] = {}
        for node in nodes:
            key = node.get(NODE_KEY)
            acl = node.get(ACL)
            if key is not None and acl:
                node_acl[str(key)] = frozenset(str(label) for label in acl)

        mapped: list[_MappedElement] = []
        nodes_skipped_unmapped = 0
        edges_skipped_unmapped = 0
        elements_skipped_no_acl = 0
        edges_skipped_empty_intersection = 0
        elements_skipped_unknown_authority = 0

        for node in sorted(nodes, key=lambda n: str(n.get(NODE_KEY, ""))):
            entity_class = node.get(ENTITY_CLASS)
            if entity_class not in ENTITY_CLASSES:
                nodes_skipped_unmapped += 1
                continue
            acl = node.get(ACL)
            if not acl:
                elements_skipped_no_acl += 1
                continue
            stamps = self._authority_stamps(node)
            if stamps is None:
                elements_skipped_unknown_authority += 1
                continue
            node_triples = self._node_triples(node, str(entity_class))
            graphs = tuple(graph_iri_for_label(label) for label in sorted({str(x) for x in acl}))
            mapped.append(
                _MappedElement(
                    kind="node", triples=node_triples, graphs=graphs, authority_stamps=stamps
                )
            )

        for edge in sorted(edges, key=lambda e: str(e.get(NODE_KEY, ""))):
            kind = edge.get(EDGE_KIND)
            is_sameas = kind == SAME_AS_EDGE_KIND
            edge_def = None if is_sameas else _EDGE_DEF_BY_KIND.get(str(kind))
            if not is_sameas and edge_def is None:
                edges_skipped_unmapped += 1
                continue
            acl = edge.get(ACL)
            if not acl:
                elements_skipped_no_acl += 1
                continue
            stamps = self._authority_stamps(edge)
            if stamps is None:
                elements_skipped_unknown_authority += 1
                continue
            from_key = str(edge.get(FROM_KEY, ""))
            to_key = str(edge.get(TO_KEY, ""))
            intersection = (
                frozenset(str(x) for x in acl)
                & node_acl.get(from_key, frozenset())
                & node_acl.get(to_key, frozenset())
            )
            if not intersection:
                edges_skipped_empty_intersection += 1
                continue
            el_kind: Literal["node", "edge", "sameas"]
            edge_triples: list[tuple[str, str, TripleObject]]
            if is_sameas:
                edge_triples = self._sameas_triples(edge, from_key, to_key)
                el_kind = "sameas"
            else:
                assert edge_def is not None
                edge_triples = [
                    (entity_iri(from_key), NEOP + edge_def.slot_name, entity_iri(to_key))
                ]
                el_kind = "edge"
            graphs = tuple(graph_iri_for_label(label) for label in sorted(intersection))
            mapped.append(
                _MappedElement(
                    kind=el_kind,
                    triples=edge_triples,
                    graphs=graphs,
                    authority_stamps=stamps,
                )
            )

        return (
            mapped,
            nodes_skipped_unmapped,
            edges_skipped_unmapped,
            elements_skipped_no_acl,
            edges_skipped_empty_intersection,
            elements_skipped_unknown_authority,
        )

    async def run(self) -> LiftReport:
        """APPEND-ONLY initial load: push each mapped element through the gateway.

        Per-element quarantine granularity is preserved — a quarantined element records
        its id and continues; valid siblings still land. NOT mutation-aware: use
        :meth:`reconcile` for a re-lift over CHANGED T2 data (the stale-store CBD is
        still what ``_push`` validates against, so a changed single-valued slot still
        quarantines by design — see :meth:`reconcile`).

        Each element validates TOGETHER with its desired-union two-hop CBD context
        (NE-085e): a node's outgoing REQUIRED edges (hop 1) and their targets'
        ``rdf:type`` (hop 2) are added to the validation payload even though they
        land in a DIFFERENT ACL-graph partition than the node itself, so a fresh
        initial load lands an entity like ``Order`` (``account``/``placed_by``) or
        ``Ticket`` (``belongs_to``) whose contract has a ``sh:minCount>=1`` edge. A
        genuinely-missing required edge is simply absent from the desired union, so
        ``sh:minCount`` still fires and the element still quarantines (fail-closed
        unchanged). The context is validation-payload-only — never written.

        The append path also ALREADY validates each delta against the STORE's own
        CBD (:meth:`ValidatingTripleStore._cbd_context`, unconditional). Once an
        element's subject/IRI-object has ALREADY landed earlier in THIS SAME ``run``
        call, that data is what the store CBD independently supplies for it — so the
        caller context here excludes any triple that (a) shares a subject with the
        CURRENT element's own seed set (the store CBD queries EXACTLY those seeds)
        and (b) has ALREADY been written. Without this, an already-landed sibling's
        literal properties would appear TWICE — once via the store's own
        (potentially backend-canonicalized, e.g. ``xsd:decimal`` trailing-zero
        stripping) copy and once via this fresh caller-serialized copy — and a
        merely-cosmetic lexical difference between the two would spuriously trip
        ``sh:maxCount``. Data NOT yet landed (the true reason this context exists —
        a sibling required edge, or its target's type) is never excluded.
        """
        nodes = list(await self._source.scan_nodes())
        edges = list(await self._source.scan_edges())
        (
            mapped,
            nodes_skipped_unmapped,
            edges_skipped_unmapped,
            elements_skipped_no_acl,
            edges_skipped_empty_intersection,
            elements_skipped_unknown_authority,
        ) = self._map_elements(nodes, edges)
        by_subject = self._desired_by_subject(mapped)

        nodes_lifted = edges_lifted = sameas_lifted = 0
        quarantined: list[str] = []
        landed: set[tuple[str, str, object]] = set()
        for element in mapped:
            raw_context = self._desired_context(element.triples, by_subject)
            seeds = self._element_seeds(element.triples)
            context = [
                triple
                for triple in raw_context
                if not (triple[0] in seeds and _identity_key(triple) in landed)
            ]
            if self._authority_sink is not None and element.authority_stamps:
                await self._authority_sink.retain(
                    tuple(sorted({triple[0] for triple in element.triples})),
                    element.authority_stamps,
                    element.graphs,
                )
            if await self._push(element.triples, element.graphs, quarantined, context):
                landed.update(_identity_key(triple) for triple in element.triples)
                if element.kind == "node":
                    nodes_lifted += 1
                elif element.kind == "edge":
                    edges_lifted += 1
                else:
                    sameas_lifted += 1

        return LiftReport(
            nodes_lifted=nodes_lifted,
            edges_lifted=edges_lifted,
            sameas_lifted=sameas_lifted,
            nodes_skipped_unmapped=nodes_skipped_unmapped,
            edges_skipped_unmapped=edges_skipped_unmapped,
            elements_skipped_no_acl=elements_skipped_no_acl,
            edges_skipped_empty_intersection=edges_skipped_empty_intersection,
            elements_skipped_unknown_authority=elements_skipped_unknown_authority,
            quarantined=tuple(quarantined),
        )

    async def reconcile(self) -> LiftReport:
        """MUTATION-AWARE re-lift: per-ACL-graph VALIDATE-THEN-ATOMIC-SWAP (NE-085d).

        Computes the COMPLETE desired per-ACL-named-graph triple partition from a full
        T2 scan, then rebuilds EVERY desired graph and purges every VACATED graph via
        :meth:`ValidatingTripleStore.replace_acl_graph` (fail-closed: a non-conforming
        graph is quarantined and its PRIOR content left intact). A byte-identical graph
        is skipped (true no-op). Both 085b failure modes are closed — a changed
        single-valued slot reconciles to the new value only, and an ACL move vacates the
        old graph (over-return closed). Safe to schedule recurring; ``run`` is not.
        """
        nodes = list(await self._source.scan_nodes())
        edges = list(await self._source.scan_edges())
        (
            mapped,
            nodes_skipped_unmapped,
            edges_skipped_unmapped,
            elements_skipped_no_acl,
            edges_skipped_empty_intersection,
            elements_skipped_unknown_authority,
        ) = self._map_elements(nodes, edges)

        # Desired per-graph partition (element order, deduped per graph by identity)
        # + a by-subject index over the FULL desired union (the CBD context source —
        # NEVER the stale store, which is exactly what caused failure mode 1).
        desired: dict[str, list[tuple[str, str, TripleObject]]] = {}
        per_graph_seen: dict[str, set[tuple[str, str, object]]] = {}
        for element in mapped:
            for triple in element.triples:
                idkey = _identity_key(triple)
                for graph_iri in element.graphs:
                    bucket = desired.setdefault(graph_iri, [])
                    seen = per_graph_seen.setdefault(graph_iri, set())
                    if idkey not in seen:
                        seen.add(idkey)
                        bucket.append(triple)
        by_subject = self._desired_by_subject(mapped)

        # Target set unions DESIRED graphs with the store's EXISTING acl graphs — the
        # union is what surfaces VACATED graphs (a label whose last element left T2).
        target_graphs = sorted(set(desired) | set(await self._store.acl_graphs()))

        # Retain contributor truth before any graph swap. A failed retention may
        # over-retain an undisclosed subject, but can never publish an unstamped
        # source-bearing graph.
        if self._authority_sink is not None:
            for element in mapped:
                if element.authority_stamps:
                    await self._authority_sink.retain(
                        tuple(sorted({triple[0] for triple in element.triples})),
                        element.authority_stamps,
                        element.graphs,
                    )

        graphs_rebuilt = graphs_deleted = graphs_unchanged = 0
        quarantined: list[str] = []
        failed: set[str] = set()
        for graph_iri in target_graphs:
            graph_triples = desired.get(graph_iri, [])
            context = self._desired_context(graph_triples, by_subject) if graph_triples else []
            try:
                outcome = await self._store.replace_acl_graph(
                    graph_triples, graph_iri=graph_iri, context_triples=context
                )
            except TripleQuarantinedError as exc:
                quarantined.append(exc.quarantine_id)
                failed.add(graph_iri)
                continue
            if outcome == "replaced":
                graphs_rebuilt += 1
            elif outcome == "deleted":
                graphs_deleted += 1
            else:
                graphs_unchanged += 1

        # An element is lifted iff NONE of its target graphs was quarantined.
        nodes_lifted = edges_lifted = sameas_lifted = 0
        for element in mapped:
            if any(graph_iri in failed for graph_iri in element.graphs):
                continue
            if element.kind == "node":
                nodes_lifted += 1
            elif element.kind == "edge":
                edges_lifted += 1
            else:
                sameas_lifted += 1

        return LiftReport(
            nodes_lifted=nodes_lifted,
            edges_lifted=edges_lifted,
            sameas_lifted=sameas_lifted,
            nodes_skipped_unmapped=nodes_skipped_unmapped,
            edges_skipped_unmapped=edges_skipped_unmapped,
            elements_skipped_no_acl=elements_skipped_no_acl,
            edges_skipped_empty_intersection=edges_skipped_empty_intersection,
            elements_skipped_unknown_authority=elements_skipped_unknown_authority,
            quarantined=tuple(quarantined),
            graphs_rebuilt=graphs_rebuilt,
            graphs_deleted=graphs_deleted,
            graphs_unchanged=graphs_unchanged,
        )

    @staticmethod
    def _desired_by_subject(
        mapped: Sequence[_MappedElement],
    ) -> dict[str, list[tuple[str, str, TripleObject]]]:
        """By-subject index over the FULL desired union (element order, deduped by
        identity across the union — first-seen wins). Shared by :meth:`run` (NE-085e)
        and :meth:`reconcile`, both of which source :meth:`_desired_context` from the
        desired T2 mapping rather than the (possibly stale, or not-yet-written)
        store, so a cross-graph ``sh:minCount``/``sh:class`` can be satisfied purely
        from data that is part of the SAME load.
        """
        by_subject: dict[str, list[tuple[str, str, TripleObject]]] = {}
        union_seen: set[tuple[str, str, object]] = set()
        for element in mapped:
            for triple in element.triples:
                idkey = _identity_key(triple)
                if idkey not in union_seen:
                    union_seen.add(idkey)
                    by_subject.setdefault(triple[0], []).append(triple)
        return by_subject

    @staticmethod
    def _element_seeds(triples: Sequence[tuple[str, str, TripleObject]]) -> frozenset[str]:
        """Every triple SUBJECT + plain-``str`` IRI OBJECT in ``triples`` (NE-085e).

        The exact seed set :meth:`ValidatingTripleStore._cbd_context` (the store
        CBD) queries for a delta of these same ``triples`` — used by :meth:`run` to
        recognize which already-landed caller-context triples the store CBD will
        independently re-supply (see :meth:`run`'s docstring).
        """
        seeds: set[str] = set()
        for subject, _predicate, obj in triples:
            seeds.add(subject)
            if isinstance(obj, str) and obj.startswith(_CONTEXT_IRI_SCHEMES):
                seeds.add(obj)
        return frozenset(seeds)

    @staticmethod
    def _desired_context(
        graph_triples: Sequence[tuple[str, str, TripleObject]],
        by_subject: Mapping[str, Sequence[tuple[str, str, TripleObject]]],
    ) -> list[tuple[str, str, TripleObject]]:
        """Two-hop CBD context for a graph, sourced from the DESIRED union (not the
        stale store), mirroring the gateway's append-path CBD.

        Seeds = every triple subject + every plain-``str`` IRI object (``http``/
        ``https``/``urn``); :class:`DeclaredLiteral` objects are literals, never seeds.
        hop1 = all desired triples whose subject is a seed; hop2 = all desired triples
        whose subject is an IRI object of hop1. Context = (hop1 + hop2, union) minus the
        graph's own triples, first-seen order, deduped — so a cross-graph ``sh:class``
        (an edge target's ``rdf:type`` living in a different per-ACL graph) is satisfied
        WITHOUT pulling any stale triple from the store.
        """
        own = {_identity_key(t) for t in graph_triples}

        def _iri_seed(obj: TripleObject) -> str | None:
            if isinstance(obj, str) and obj.startswith(_CONTEXT_IRI_SCHEMES):
                return obj
            return None

        seeds: list[str] = []
        seen_seeds: set[str] = set()

        def _add_seed(candidate: str) -> None:
            if candidate not in seen_seeds:
                seen_seeds.add(candidate)
                seeds.append(candidate)

        for subject, _predicate, obj in graph_triples:
            _add_seed(subject)
            iri = _iri_seed(obj)
            if iri is not None:
                _add_seed(iri)

        def _expand(
            expand_seeds: Sequence[str],
        ) -> list[tuple[str, str, TripleObject]]:
            out: list[tuple[str, str, TripleObject]] = []
            ids: set[tuple[str, str, object]] = set()
            for seed in expand_seeds:
                for triple in by_subject.get(seed, []):
                    key = _identity_key(triple)
                    if key not in ids:
                        ids.add(key)
                        out.append(triple)
            return out

        hop1 = _expand(seeds)
        hop2_seeds: list[str] = []
        seen_h2: set[str] = set()
        for _subject, _predicate, obj in hop1:
            iri = _iri_seed(obj)
            if iri is not None and iri not in seen_h2:
                seen_h2.add(iri)
                hop2_seeds.append(iri)
        hop2 = _expand(hop2_seeds)

        context: list[tuple[str, str, TripleObject]] = []
        ctx_ids: set[tuple[str, str, object]] = set()
        for triple in [*hop1, *hop2]:
            key = _identity_key(triple)
            if key in own or key in ctx_ids:
                continue
            ctx_ids.add(key)
            context.append(triple)
        return context

    async def _push(
        self,
        triples: Sequence[tuple[str, str, TripleObject]],
        graphs: Sequence[str],
        quarantined: list[str],
        context: Sequence[tuple[str, str, TripleObject]] = (),
    ) -> bool:
        """Push ``triples`` into every target graph via the gateway. Returns True iff
        every batch conformed (nothing quarantined). A ``ShapeValidationError``
        propagates (denial); a ``TripleQuarantinedError`` records the id + continues.

        ``context`` (NE-085e) is the element's desired-union two-hop CBD — added to
        the gateway's validation payload only (never written), so an element with a
        SHACL-required outgoing edge validates together with that edge and its
        target's ``rdf:type`` even though they land in a different ACL-graph
        partition. Applies uniformly to every element kind (node/edge/sameas).
        """
        conformed = True
        for graph_iri in graphs:
            try:
                await self._store.insert_triples_into(
                    triples, graph_iri=graph_iri, context_triples=context
                )
            except TripleQuarantinedError as exc:
                quarantined.append(exc.quarantine_id)
                conformed = False
        return conformed

    @staticmethod
    def _node_triples(
        node: Mapping[str, Any], entity_class: str
    ) -> list[tuple[str, str, TripleObject]]:
        contract = ENTITY_CONTRACTS[entity_class]
        fields = contract.model_fields
        iri = entity_iri(str(node[NODE_KEY]))
        # rdf:type object is a plain-str IRI (no schema signal); slot objects are
        # slot-datatype-authoritative DeclaredLiterals (FIX-1).
        triples: list[tuple[str, str, TripleObject]] = [(iri, _RDF_TYPE, NEOP + entity_class)]
        for key in sorted(node):
            if key in GOVERNANCE_SKIP or key not in fields:
                continue
            value = node[key]
            if value is None:
                continue
            predicate = NEOP + key
            triples.append((iri, predicate, _slot_object(predicate, value)))
        return triples

    @staticmethod
    def _sameas_triples(
        edge: Mapping[str, Any], from_key: str, to_key: str
    ) -> list[tuple[str, str, TripleObject]]:
        subject = sameas_iri(str(edge[NODE_KEY]))
        # same_as_left/right are plain-str entity IRIs; source_ref is an xsd:string
        # slot (identity_assertion.ttl) — same defect class as Document.uri, so it
        # goes through _slot_object as a DeclaredLiteral (FIX-1).
        return [
            (subject, _RDF_TYPE, NEA + "IdentityAssertion"),
            (subject, NEA + "same_as_left", entity_iri(from_key)),
            (subject, NEA + "same_as_right", entity_iri(to_key)),
            (
                subject,
                NEA + "source_ref",
                _slot_object(NEA + "source_ref", edge.get(SOURCE_REF, "")),
            ),
        ]
