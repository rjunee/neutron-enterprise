# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``EntityGraph`` — the ONLY principal-scoped Tier-2 read surface (C7).

Every method takes ``principal`` and trims through the SAME label algebra as
Tier-1 chunk reads: ``labels = acl_match_labels(principal, expander)`` IMPORTED
from ``ne.brain.trimming`` (one algebra, zero drift — never re-implemented). The
labels are computed FIRST, before any store I/O, so an expander exception denies
by erroring before a query is issued. The trimming predicate is injected on EVERY
node AND edge variable of the generated cypher via
:class:`~ne.brain_graph.age.query.TrimmedCypherBuilder`; when the store's probed
rung is ``POST_FILTER`` the complete path is additionally filtered adapter-side
here (may under-return, never over-return).

Returned :class:`EntityNode`/:class:`EntityEdge` payloads STRIP ``acl`` and the
governance hashes UNCONDITIONALLY, from every element. The extraction
``evidence`` chunk-ref list is ALSO stripped (NE-T2X-3 closes the read-side
over-return the U2 writer docstring pinned) but ONLY from ``ne:llm`` elements
(Argus P3-3) — a backbone connector's contract-declared ``evidence`` business
field is not a floor field and still surfaces, since extraction evidence refs
exist only on elements this pathway itself wrote. There is no
``principal=None`` escape and no raw-cypher passthrough.
``assurance`` is surfaced as a typed field (NE-T2X-3): every construction site
below (``_to_node``/``_to_edge``) sets it EXPLICITLY via :func:`_assurance_of`
from the stored element, fail-closed — an ``ne:llm`` element with a
missing/forged assurance prop reads as ``"extracted"``, never ``"source"``,
and a non-``ne:llm`` element can never claim ``"confirmed"``/``"extracted"``
via a smuggled stored prop (design §5.2's correction block: ``EntityNode``/
``EntityEdge`` are frozen but not ``extra="forbid"``, so leaving this to a
default would fail OPEN toward the trusted-sounding value).

NE-164b read-path auditing: every public read optionally emits ONE config_hash-
stamped ``AuditEvent`` per call via an injected :class:`~ne.contracts.governance.
AuditSink` (mirrors the T1 ``knowledge.retrieve`` pattern shipped in NE-164's
``ne.brain.retriever.HybridRetriever``). See :meth:`EntityGraph._emit_read_audit`
for the exact C7 discipline (counts + adapter-side trim delta only — never an ACL
label value, entity key, display name, or cypher text).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ne.brain.source_health import (
    DegradedSourcesProvider,
    evaluate_degraded_sources,
)
from ne.brain.trimming import GroupExpander, acl_match_labels
from ne.brain_graph.age.query import PredicateStyle, TrimmedCypherBuilder, discovery_order_key
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.er.merge import ER_SOURCE_SYSTEM
from ne.brain_graph.errors import GraphStoreError
from ne.brain_graph.extraction import ASSURANCE_VALUES, LLM_SOURCE_SYSTEM
from ne.brain_graph.model import EntityEdge, EntityKey, EntityNode, GraphPathRecord, Subgraph
from ne.connect.identity import source_acl_hash
from ne.contracts.governance import AuditSink
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReader,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    canonical_source_authority_stamps,
)
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.schema import AuditOutcome

__all__ = [
    "ENTITY_DISCOVERY_CURSOR_CAP",
    "FACET_SCAN_BATCH",
    "FACET_SCAN_CAP",
    "EntityDiscoveryPage",
    "EntityFacets",
    "EntityGraph",
    "GraphReadStore",
    "InvalidDiscoveryCursor",
    "StampedGraphPathRecord",
]

# Node property keys that are structural (mapped to typed fields) or governance
# (stripped entirely, UNCONDITIONALLY, from every element regardless of
# source_system) — never surfaced in EntityNode.properties.
_NODE_STRUCTURAL = frozenset(
    {"key", "entity_class", "display_name", "source_system", "source_ref", "assurance"}
)
_GOVERNANCE = frozenset(
    {
        "acl",
        "content_hash",
        "config_hash",
        "schema_version",
        "synced_at",
        "source_authority_stamps",
        "source_status",
    }
)

#: ``evidence`` (the ``"<proposal_id>|<chunk_ref>"`` ref list ``extraction.
#: writer`` stamps) is stripped from a returned payload ONLY when the
#: element's ``source_system`` is ``LLM_SOURCE_SYSTEM`` (NE-T2X-3, closing
#: the U2-documented Argus-P2 over-return: a node re-evidenced from a
#: second, less-visible chunk must not disclose that chunk's ref/proposal id
#: through the metadata channel). This is DELIBERATELY SCOPED, not folded
#: into the unconditional ``_GOVERNANCE`` denylist above (Argus P3-3):
#: extraction evidence refs exist ONLY on ne:llm-written elements
#: (``extraction.writer._PLATFORM_OWNED_KEYS`` reserves the key on that
#: path), while ``construct/mappers.py`` has NO reserved-key wall for
#: backbone connector contracts — an unconditional strip would silently
#: drop a legitimate backbone-contract business field named ``evidence``.
#: Scoping to ne:llm closes the U2 leak without widening the strip onto
#: customer data it was never meant to touch.
#:
#: Residual (Argus P3, prior round): unlike :func:`_assurance_of` immediately
#: below — which is fail-closed against a TAMPERED stored ``assurance``
#: value — this strip is keyed on the stored ``source_system`` itself, so it
#: is NOT fail-closed against a mislabeled/tampered ``source_system``: an
#: element that actually carries extraction evidence but whose stored
#: ``source_system`` does not read as ``LLM_SOURCE_SYSTEM`` would surface
#: ``evidence`` where the pre-scoping unconditional strip did not. Not live
#: today — ``extraction.writer`` stamps ``LLM_SOURCE_SYSTEM`` on every
#: element it creates, ``llm_entity_key`` namespaces extracted keys
#: permanently away from backbone keys so the only path that can ever
#: mutate an ne:llm node's ``evidence`` is the extraction path itself,
#: ``er/merge.py`` copies an existing node's dict without touching
#: ``source_system``, and ``construct/mappers.py`` never writes
#: ``evidence`` — but the residual is a mislabeled/tampered
#: ``source_system`` converting a sealed field into a returned one, and is
#: stated here rather than left implicit.
_LLM_ONLY_STRIP = frozenset({"evidence"})
_UNATTRIBUTABLE_DERIVED_SOURCES = frozenset({LLM_SOURCE_SYSTEM, ER_SOURCE_SYSTEM})
_DISCOVERY_CURSOR_WIRE = 1
_DISCOVERY_ASSURANCE = frozenset({"source", "confirmed", "extracted"})
ENTITY_DISCOVERY_CURSOR_CAP = 1024

#: The bound on one :meth:`EntityGraph.facets` call: at most ``FACET_SCAN_CAP``
#: principal-visible entities folded into the facet sets AND at most
#: ``FACET_SCAN_CAP`` raw store rows examined, fetched ``FACET_SCAN_BATCH`` per
#: round trip. Both halves matter — capping only the visible rows would let a
#: principal with a three-entity slice of a million-node graph scan the million.
#: So the entity rail's per-load cost is at most
#: ``ceil(FACET_SCAN_CAP / FACET_SCAN_BATCH)`` store round trips (10 today), and
#: exactly ONE whenever the graph holds fewer than ``FACET_SCAN_BATCH`` rows.
#: Hitting either bound is reported honestly as ``EntityFacets.truncated`` — the
#: facet lists are then a possibly-incomplete prefix of what is visible, NEVER
#: padded out with a guess.
FACET_SCAN_CAP = 2000
FACET_SCAN_BATCH = 200


class InvalidDiscoveryCursor(ValueError):
    """The opaque entity-discovery cursor is malformed or filter-stale."""


@dataclass(frozen=True)
class EntityDiscoveryPage:
    """One principal-visible, deterministically ordered discovery page."""

    entities: tuple[EntityNode, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class EntityFacets:
    """The entity classes and source systems PRESENT in one principal's slice.

    Derived by folding :meth:`EntityGraph._scan_visible` — the same, single
    trimmed scan :meth:`EntityGraph.discover` serves its rows from — so a class
    or source reaches this list only when at least one entity carrying it
    survived every one of that scan's gates (in-cypher ``$acl_labels``
    predicate, adapter-side :func:`_node_visible`, the source-authority
    resolution in :meth:`EntityGraph._currently_authorized`, and the
    degraded-source health filter). There is NO schema-registry fallback and no
    second, weaker read path: an empty graph yields empty tuples, never the
    compile-time class list.

    ``truncated`` is ``True`` only when the bounded scan hit
    :data:`FACET_SCAN_CAP` before the store was exhausted — i.e. the lists may
    be incomplete. It is never used to imply the opposite (that they are
    complete when the scan failed).
    """

    entity_classes: tuple[str, ...]
    source_systems: tuple[str, ...]
    truncated: bool


@dataclass
class _ScanCounters:
    """Mutable trim tallies a :meth:`EntityGraph._scan_visible` pass accumulates.

    A generator cannot return a value to an ``async for`` caller, so the two
    audit deltas (``trimmed`` / ``health_trimmed``) and the store-exhaustion
    fact ride out on this object instead.
    """

    trimmed: int = 0
    health_trimmed: int = 0
    exhausted: bool = False
    last_raw: tuple[str, str, str] | None = field(default=None)


def _facet_order_key(value: str) -> tuple[str, str]:
    """Case-insensitive, tie-broken-by-exact-value ordering for a facet list."""
    return (value.casefold(), value)


@dataclass(frozen=True)
class StampedGraphPathRecord:
    """One trimmed path retaining the exact raw stamp set for each hop."""

    path: GraphPathRecord
    hop_stamps: tuple[tuple[SourceAuthorityStamp, ...], ...]


def _discovery_fingerprint(
    *, q: str | None, entity_class: str | None, source: str | None, assurance: str | None
) -> str:
    filters = {
        "assurance": assurance,
        "entity_class": entity_class,
        "q": q,
        "source": source,
    }
    payload = json.dumps(filters, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _encode_discovery_cursor(anchor: tuple[str, str, str], *, fingerprint: str) -> str:
    payload = json.dumps(
        {
            "display_name": anchor[0],
            "entity_class": anchor[1],
            "filters": fingerprint,
            "key": anchor[2],
            "wire": _DISCOVERY_CURSOR_WIRE,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_discovery_cursor(cursor: str, *, fingerprint: str) -> tuple[str, str, str]:
    try:
        if not cursor or "=" in cursor:
            raise ValueError
        padding = "=" * (-len(cursor) % 4)
        payload = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        decoded = json.loads(payload)
        if not isinstance(decoded, dict) or set(decoded) != {
            "display_name",
            "entity_class",
            "filters",
            "key",
            "wire",
        }:
            raise ValueError
        if (
            type(decoded["wire"]) is not int
            or decoded["wire"] != _DISCOVERY_CURSOR_WIRE
            or decoded["filters"] != fingerprint
        ):
            raise ValueError
        values = (decoded["display_name"], decoded["entity_class"], decoded["key"])
        if not all(isinstance(value, str) for value in values):
            raise ValueError
        if _encode_discovery_cursor(values, fingerprint=fingerprint) != cursor:
            raise ValueError
        return values
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise InvalidDiscoveryCursor("invalid entity discovery cursor") from exc


def _graph_source_is_healthy(source_system: object, degraded_sources: frozenset[str]) -> bool:
    """Fail closed for degraded backbone and un-attributable derived graph facts."""
    source = source_system if isinstance(source_system, str) else ""
    if not source.strip():
        return not degraded_sources
    if source in degraded_sources:
        return False
    return not (degraded_sources and source in _UNATTRIBUTABLE_DERIVED_SOURCES)


def _assurance_of(props: Mapping[str, Any]) -> str:
    """The fail-closed assurance truth-table (design §5.2 correction, NE-T2X-3).

    ``source_system == LLM_SOURCE_SYSTEM`` (``"ne:llm"``): the stored
    ``assurance`` value survives ONLY if it is one of
    :data:`~ne.brain_graph.extraction.ASSURANCE_VALUES` — the SINGLE SOURCE
    for this label set (Argus P3-1: derive, don't transliterate; imported
    above alongside ``LLM_SOURCE_SYSTEM`` from the same dependency-free
    constants leaf, so this module gains no write-surface dependency it
    didn't already have). A missing, stripped, or forged (``"source"``)
    value reads as ``"extracted"`` — an ne:llm element can NEVER read as
    ``"source"``, however its stored props are tampered.
    Any other ``source_system`` (backbone connector OR ``ne:er`` ER-derived)
    ALWAYS reads ``"source"`` unconditionally, regardless of any smuggled
    ``assurance`` prop — a backbone/ER element can never claim
    ``"confirmed"``/``"extracted"`` this way.
    """
    if str(props.get("source_system", "")) == LLM_SOURCE_SYSTEM:
        stamped = props.get("assurance")
        if isinstance(stamped, str) and stamped in ASSURANCE_VALUES:
            return stamped
        return "extracted"
    return "source"


class GraphReadStore(Protocol):
    """The narrow store surface EntityGraph consumes (frozen ``cypher`` + rung)."""

    async def cypher(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> Sequence[Mapping[str, Any]]: ...


def _stored_acl(props: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Return one well-formed stored ACL without normalizing its labels.

    Graph properties are an untyped storage boundary.  A bare string, mapping,
    set, empty sequence, non-string label, empty label, or label outside the
    source-authority bound is not an ACL and therefore cannot authorize a read.
    Ordering and duplicate labels remain intact here because
    :func:`source_acl_hash` owns their canonicalization.
    """
    acl = props.get("acl")
    if (
        not isinstance(acl, (list, tuple))
        or not acl
        or any(not isinstance(label, str) or not label or len(label) > 2048 for label in acl)
    ):
        return None
    return tuple(acl)


def _stamps_bind_stored_acl(
    props: Mapping[str, Any], stamps: Sequence[SourceAuthorityStamp]
) -> bool:
    """Bind graph ACL bytes to every retained stamp and selected artifact."""
    acl = _stored_acl(props)
    if acl is None or not stamps:
        return False
    acl_hash = source_acl_hash(acl)
    return all(
        stamp.acl_snapshot_hash == acl_hash
        and stamp.artifact_identity.acl_snapshot_hash == acl_hash
        for stamp in stamps
    )


def _node_visible(props: Mapping[str, Any], labels: frozenset[str]) -> bool:
    acl = _stored_acl(props)
    return acl is not None and bool(set(acl) & labels)


def _to_node(props: Mapping[str, Any]) -> EntityNode:
    properties = {
        k: v for k, v in props.items() if k not in _NODE_STRUCTURAL and k not in _GOVERNANCE
    }
    if str(props.get("source_system", "")) == LLM_SOURCE_SYSTEM:
        for key in _LLM_ONLY_STRIP:
            properties.pop(key, None)
    return EntityNode(
        key=str(props["key"]),
        entity_class=str(props.get("entity_class", "")),
        display_name=str(props.get("display_name", "")),
        source_system=str(props.get("source_system", "")),
        source_ref=str(props.get("source_ref", "")),
        assurance=_assurance_of(props),
        properties=properties,
    )


def _to_edge(props: Mapping[str, Any]) -> EntityEdge:
    return EntityEdge(
        key=str(props["key"]),
        edge_kind=str(props.get("edge_kind", "")),
        from_key=str(props.get("from_key", "")),
        to_key=str(props.get("to_key", "")),
        source_system=str(props.get("source_system", "")),
        source_ref=str(props.get("source_ref", "")),
        assurance=_assurance_of(props),
    )


def _path_all_visible(
    path_nodes: Sequence[Mapping[str, Any]],
    path_rels: Sequence[Mapping[str, Any]],
    labels: frozenset[str],
) -> bool:
    return all(_node_visible(n, labels) for n in path_nodes) and all(
        _node_visible(r, labels) for r in path_rels
    )


class EntityGraph:
    """Principal-scoped, fail-closed entity-graph reads."""

    def __init__(
        self,
        store: GraphReadStore,
        *,
        group_expander: GroupExpander | None,
        settings: BrainGraphSettings | None = None,
        audit_sink: AuditSink | None,
        source_health: DegradedSourcesProvider | None = None,
        source_authority: CurrentSourceAuthorityReader | None = None,
        access_context_hash: Callable[[Principal], str] | None = None,
    ) -> None:
        """NE-VG-2 (V6 shape ratchet, design §3-V6, ExtractionJob precedent):
        ``audit_sink`` is now REQUIRED keyword-only — still ``AuditSink | None``
        (a sink-less composition stays legitimate) but omission is now a
        pyright/``TypeError`` construction failure, never a silent default.
        Ratchet-hardening of an already-deletion-guarded binding (the platform
        composition root's ``audit_sink=sink`` at ``platform.py:1078`` is
        covered end-to-end by
        ``test_graph_api.py::test_neighborhood_route_emits_graph_read_audit_event``)
        — not a live-defect closure. Legitimate sink-less callers now pass
        ``audit_sink=None`` explicitly."""
        self._store = store
        self._group_expander = group_expander
        self._settings = settings or BrainGraphSettings()
        style = getattr(store, "predicate_style", PredicateStyle.ANY)
        self._builder = TrimmedCypherBuilder(style)
        self._audit_sink = audit_sink
        self._source_health = source_health
        self._source_authority = source_authority
        self._access_context_hash = access_context_hash

    async def _currently_authorized(self, props: Mapping[str, Any], principal: Principal) -> bool:
        """Resolve every retained source stamp before disclosing one graph element.

        Any retained stamp must bind the canonical hash of the element's exact
        stored ACL even when a legacy caller has no authority Reader.  Legacy
        unstamped elements preserve their pre-ENFORCEMENT behaviour; once the
        Reader is composed, missing, malformed or non-current stamps are Unknown
        and therefore withheld.  The sole source-free exception is an explicit
        server-authored ``no_source`` marker; absence never implies source-free.
        """
        raw_stamps = props.get("source_authority_stamps")
        no_retained_stamps = raw_stamps is None or (
            isinstance(raw_stamps, (list, tuple)) and not raw_stamps
        )
        if no_retained_stamps:
            return props.get("source_status") == "no_source" or self._source_authority is None
        stamps = self._retained_stamps(props)
        if stamps is None:
            return False
        if props.get("source_status") == "no_source":
            return True
        if self._source_authority is None:
            return True
        if self._access_context_hash is None:
            return False
        try:
            context_hash = self._access_context_hash(principal)
        except (TypeError, ValueError):
            return False
        for stamp in stamps:
            try:
                resolution = await self._source_authority.resolve(
                    stamp,
                    principal_id=principal.id,
                    access_context_hash=context_hash,
                )
            except Exception:
                return False
            if resolution.decision is not SourceAuthorityDecision.AUTHORIZED:
                return False
        return True

    @staticmethod
    def _retained_stamps(
        props: Mapping[str, Any],
    ) -> tuple[SourceAuthorityStamp, ...] | None:
        raw_stamps = props.get("source_authority_stamps")
        if not isinstance(raw_stamps, list | tuple) or not raw_stamps:
            return None
        try:
            stamps = tuple(SourceAuthorityStamp.model_validate(raw) for raw in raw_stamps)
        except (TypeError, ValueError):
            return None
        canonical = canonical_source_authority_stamps(stamps)
        if len(canonical) != len(stamps):
            return None
        if not _stamps_bind_stored_acl(props, canonical):
            return None
        return canonical

    def _labels(self, principal: Principal) -> frozenset[str]:
        # Computed BEFORE any store I/O; an expander exception propagates.
        return frozenset(acl_match_labels(principal, self._group_expander))

    async def _scan_visible(
        self,
        *,
        principal: Principal,
        labels: frozenset[str],
        degraded_sources: frozenset[str],
        query: str | None,
        entity_class: str | None,
        source: str | None,
        assurance: str | None,
        after: tuple[str, str, str] | None,
        batch_limit: int,
        want: int,
        counters: _ScanCounters,
        row_budget: int | None = None,
    ) -> AsyncIterator[EntityNode]:
        """The ONE trimmed entity scan. Yields at most ``want`` visible nodes.

        Both :meth:`discover` (the entity rail's rows) and :meth:`facets` (the
        rail's option lists) consume THIS generator — there is deliberately no
        second, cheaper-but-weaker path to "which entities exist", so breaking
        any gate below breaks discovery and the facet lists together and can
        never leave the option lists over-disclosing what the rows withhold.

        The gates, in order, are exactly the ones the pre-refactor ``discover``
        applied: the in-cypher ``$acl_labels`` predicate the builder injects,
        the adapter-side :func:`_node_visible` re-check (load-bearing on the
        ``POST_FILTER`` rung), the per-element source-authority resolution in
        :meth:`_currently_authorized`, the degraded-source health filter, and
        finally the derived-``assurance`` filter (which, as before, is NOT
        counted as an ACL trim). Ordered raw batches continue until ``want``
        visible matches have been found or the store is exhausted, so
        post-filtering never turns a non-empty later page into a false empty
        page.

        The caller supplies ``counters`` because a generator cannot return the
        trim deltas; ``counters.exhausted`` records that the store ran out
        before the scan stopped (the caller's only honest basis for saying a
        bounded scan was NOT truncated).

        ``row_budget`` additionally caps the RAW rows examined. ``discover``
        leaves it ``None`` (byte-identical to its pre-refactor behaviour: it
        pages until ``want`` visible rows exist or the store is exhausted, so a
        heavily-trimmed principal never gets a false empty page). ``facets``
        sets it, because its ``want`` is a whole page of vocabulary rather than
        one screen of rows: without a raw-row cap a principal who can see three
        entities in a million-node graph would scan the million. Spending the
        budget leaves ``counters.exhausted`` False, so the caller reports the
        result as truncated rather than as complete.
        """
        yielded = 0
        rows_seen = 0
        budget_spent = False
        last_raw = after
        counters.last_raw = after
        while yielded < want and not budget_spent:
            compiled = self._builder.build_discovery(
                sorted(labels),
                query=query,
                entity_class=entity_class,
                source_system=source,
                after=last_raw,
                limit=batch_limit,
            )
            rows = await self._store.cypher(compiled.text, compiled.params)
            if len(rows) > batch_limit:
                raise GraphStoreError("entity discovery backend exceeded its requested page")
            for row in rows:
                if row_budget is not None and rows_seen >= row_budget:
                    budget_spent = True
                    break
                if not isinstance(row, Mapping):
                    raise GraphStoreError("entity discovery backend returned a malformed row")
                props = row.get("node")
                if not isinstance(props, Mapping):
                    raise GraphStoreError("entity discovery backend returned a malformed row")
                try:
                    raw_key = (
                        str(props["display_name"]),
                        str(props["entity_class"]),
                        str(props["key"]),
                    )
                except KeyError as exc:
                    raise GraphStoreError(
                        "entity discovery backend omitted an ordering field"
                    ) from exc
                if last_raw is not None and discovery_order_key(raw_key) <= discovery_order_key(
                    last_raw
                ):
                    raise GraphStoreError("entity discovery backend order did not advance")
                last_raw = raw_key
                counters.last_raw = raw_key
                rows_seen += 1
                if not _node_visible(props, labels):
                    counters.trimmed += 1
                    continue
                if not await self._currently_authorized(props, principal):
                    counters.trimmed += 1
                    continue
                if not _graph_source_is_healthy(props.get("source_system"), degraded_sources):
                    counters.health_trimmed += 1
                    continue
                node = _to_node(props)
                if assurance is not None and node.assurance != assurance:
                    continue
                yielded += 1
                yield node
                if yielded == want:
                    break
            if yielded == want or budget_spent:
                break
            if len(rows) < batch_limit:
                counters.exhausted = True
                break
            if not rows or last_raw is None:
                raise GraphStoreError("entity discovery backend page did not advance")

    async def facets(
        self,
        *,
        principal: Principal,
        trace_id: str | None = None,
    ) -> EntityFacets:
        """The entity classes and source systems PRESENT in this principal's slice.

        This is the server-derived vocabulary behind the entity rail's Class and
        Source controls. It is folded from :meth:`_scan_visible` — the SAME
        trimmed scan :meth:`discover` serves rows from — so:

        * a class/source with no principal-visible instance never appears, even
          when the compile-time ``ne.schema`` registry names it; and
        * a class/source visible only to a DIFFERENT principal never appears,
          because the labels are this principal's and every gate that hides the
          instance hides its facet with it. The facet list therefore cannot
          disclose the existence of data the row list would withhold.

        No fabrication: an empty (or entirely invisible) graph yields two empty
        tuples for the caller to render as an honest absent-state. A bounded
        scan that hit :data:`FACET_SCAN_CAP` says so via ``truncated`` rather
        than implying completeness.

        Cost: one label expansion, one source-health snapshot, and at most
        ``ceil(FACET_SCAN_CAP / FACET_SCAN_BATCH)`` store round trips — ONE for
        any graph under :data:`FACET_SCAN_BATCH` visible nodes. Emits exactly
        one ``knowledge.graph_read`` audit event (``op=facets``), like every
        other public read here. NOT cached: the result is a function of the
        principal's label set, and a cache keyed on anything coarser would
        serve one principal's vocabulary to another.
        """
        labels = self._labels(principal)
        degraded_sources = await evaluate_degraded_sources(self._source_health)
        counters = _ScanCounters()
        entity_classes: set[str] = set()
        source_systems: set[str] = set()
        scanned = 0
        # cap+1: a node past the cap proves truncation without being folded in.
        async for node in self._scan_visible(
            principal=principal,
            labels=labels,
            degraded_sources=degraded_sources,
            query=None,
            entity_class=None,
            source=None,
            assurance=None,
            after=None,
            batch_limit=FACET_SCAN_BATCH,
            want=FACET_SCAN_CAP + 1,
            counters=counters,
            row_budget=FACET_SCAN_CAP,
        ):
            if scanned == FACET_SCAN_CAP:
                break
            scanned += 1
            if node.entity_class:
                entity_classes.add(node.entity_class)
            if node.source_system:
                source_systems.add(node.source_system)
        # The ONE truthful basis: the lists are complete iff the store ran out
        # before the scan stopped. Breaking at the cap above, or spending the
        # raw-row budget, both leave `exhausted` False.
        truncated = not counters.exhausted
        await self._emit_read_audit(
            principal=principal,
            op="facets",
            nodes=scanned,
            edges=0,
            hops=0,
            trimmed=counters.trimmed,
            health_trimmed=counters.health_trimmed,
            trace_id=trace_id,
        )
        return EntityFacets(
            entity_classes=tuple(sorted(entity_classes, key=_facet_order_key)),
            source_systems=tuple(sorted(source_systems, key=_facet_order_key)),
            truncated=truncated,
        )

    async def discover(
        self,
        *,
        principal: Principal,
        q: str | None = None,
        entity_class: str | None = None,
        source: str | None = None,
        assurance: str | None = None,
        cursor: str | None = None,
        limit: int = 25,
        trace_id: str | None = None,
    ) -> EntityDiscoveryPage:
        """Discover visible entities through a filter-bound stable keyset.

        The store applies query/class/source/ACL predicates, but this layer
        repeats ACL trimming and owns source-health plus derived-assurance
        filtering. Ordered raw batches continue until ``limit + 1`` visible
        matches have been found or the store is exhausted, so post-filtering
        never turns a non-empty later page into a false empty page.
        """
        if assurance is not None and assurance not in _DISCOVERY_ASSURANCE:
            raise ValueError("invalid discovery assurance")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("discovery limit out of bounds")
        normalized_q = q.strip() if q is not None else None
        if normalized_q == "":
            normalized_q = None
        fingerprint = _discovery_fingerprint(
            q=normalized_q,
            entity_class=entity_class,
            source=source,
            assurance=assurance,
        )
        after = (
            _decode_discovery_cursor(cursor, fingerprint=fingerprint)
            if cursor is not None
            else None
        )

        # Exactly one label expansion and one health snapshot per public read.
        labels = self._labels(principal)
        degraded_sources = await evaluate_degraded_sources(self._source_health)
        counters = _ScanCounters()
        matches = [
            node
            async for node in self._scan_visible(
                principal=principal,
                labels=labels,
                degraded_sources=degraded_sources,
                query=normalized_q,
                entity_class=entity_class,
                source=source,
                assurance=assurance,
                after=after,
                batch_limit=limit + 1,
                want=limit + 1,
                counters=counters,
            )
        ]
        trimmed = counters.trimmed
        health_trimmed = counters.health_trimmed

        served = tuple(matches[:limit])
        next_cursor = None
        if len(matches) > limit:
            anchor = (served[-1].display_name, served[-1].entity_class, served[-1].key)
            next_cursor = _encode_discovery_cursor(anchor, fingerprint=fingerprint)
            if len(next_cursor) > ENTITY_DISCOVERY_CURSOR_CAP:
                raise GraphStoreError("entity discovery cursor exceeded its wire cap")
        await self._emit_read_audit(
            principal=principal,
            op="discover",
            nodes=len(served),
            edges=0,
            hops=0,
            trimmed=trimmed,
            health_trimmed=health_trimmed,
            trace_id=trace_id,
        )
        return EntityDiscoveryPage(entities=served, next_cursor=next_cursor)

    async def _emit_read_audit(
        self,
        *,
        principal: Principal,
        op: str,
        nodes: int,
        edges: int,
        hops: int,
        trimmed: int,
        health_trimmed: int = 0,
        trace_id: str | None,
    ) -> None:
        """Emit ONE read-path ``AuditEvent`` for a completed public read.

        C7 discipline (mirrors NE-164's ``HybridRetriever._emit_read_audit``): the
        event carries the query SHAPE only — node/edge/hop COUNTS and the adapter-
        side ACL-trim DELTA — and rule identity (action/resource names). It NEVER
        carries an ACL label value, a principal group label, an entity key, a
        display name, or raw cypher text. ``actor`` is the standard audit-subject
        identity (``principal.id``), not a leaked label.

        ``trimmed`` (``acl_trim_delta`` in the detail string) counts ONLY store
        rows dropped by the adapter-side post-filter (``_node_visible`` /
        ``_path_all_visible`` above) — the in-cypher ``$acl_labels`` predicate
        trimming is invisible by construction (it never reaches this process as a
        row at all), so the delta is a LOWER BOUND: it is exactly 0 whenever the
        store's probed rung trims fully server-side (every shipped store today),
        and only becomes positive when a store degrades to ``POST_FILTER``.

        No-op when no ``audit_sink`` was injected (every pre-NE-164b composition
        stays byte-identical). When a sink IS injected, a sink ``append`` failure
        PROPAGATES — the read is fail-closed: it is never served unaudited
        (mirrors NE-164's/``ne.brain.write.writer``'s NE-CKD6 discipline). A
        ``ConfigHashNotFrozenError`` from ``new_audit_event`` propagates loudly for
        the same reason.

        Write-rate: exactly ONE event per public read call (``discover``/
        ``lookup``/``neighbors``/``subgraph``/``paths``), never per-node/per-edge
        — the same NE-164 write-rate rule (sampling explicitly deferred, not built
        here).
        """
        if self._audit_sink is None:
            return
        health_detail = f" source_health_trim_delta={health_trimmed}" if health_trimmed > 0 else ""
        await self._audit_sink.append(
            new_audit_event(
                action="knowledge.graph_read",
                resource="knowledge://graph",
                outcome=AuditOutcome.success,
                actor=principal.id,
                detail=(
                    f"op={op} nodes={nodes} edges={edges} hops={hops} "
                    f"acl_trim_delta={trimmed}{health_detail}"
                ),
                trace_id=trace_id,
            )
        )

    async def lookup(
        self,
        keys: Sequence[EntityKey] | None = None,
        *,
        principal: Principal,
        entity_class: str | None = None,
        name_query: str | None = None,
        source_refs: Sequence[str] | None = None,
        limit: int | None = None,
        trace_id: str | None = None,
    ) -> Sequence[EntityNode]:
        labels = self._labels(principal)
        limit = limit if limit is not None else min(50, self._settings.default_read_limit)
        compiled = self._builder.build_lookup(
            sorted(labels),
            keys=[k.key for k in keys] if keys is not None else None,
            entity_class=entity_class,
            name_query=name_query,
            source_refs=list(source_refs) if source_refs is not None else None,
            limit=limit,
        )
        rows = await self._store.cypher(compiled.text, compiled.params)
        degraded_sources = (
            await evaluate_degraded_sources(self._source_health) if rows else frozenset()
        )
        nodes: list[EntityNode] = []
        trimmed = 0
        health_trimmed = 0
        for row in rows:
            props = row["node"]
            if not _node_visible(props, labels):
                trimmed += 1
                continue
            if not await self._currently_authorized(props, principal):
                trimmed += 1
                continue
            if not _graph_source_is_healthy(props.get("source_system"), degraded_sources):
                health_trimmed += 1
                continue
            nodes.append(_to_node(props))
        await self._emit_read_audit(
            principal=principal,
            op="lookup",
            nodes=len(nodes),
            edges=0,
            hops=0,
            trimmed=trimmed,
            health_trimmed=health_trimmed,
            trace_id=trace_id,
        )
        return tuple(nodes)

    async def neighbors(
        self,
        keys: Sequence[EntityKey],
        *,
        principal: Principal,
        hops: int = 1,
        edge_kinds: Sequence[str] | None = None,
        direction: Literal["out", "in", "both"] = "both",
        limit: int | None = None,
        trace_id: str | None = None,
    ) -> Subgraph:
        labels = self._labels(principal)
        limit = limit if limit is not None else self._settings.default_read_limit
        compiled = self._builder.build_neighbors(
            sorted(labels),
            [k.key for k in keys],
            hops=hops,
            edge_kinds=edge_kinds,
            direction=direction,
            limit=limit,
            max_hops=self._settings.max_hops,
        )
        rows = await self._store.cypher(compiled.text, compiled.params)
        degraded_sources = (
            await evaluate_degraded_sources(self._source_health) if rows else frozenset()
        )
        sub, trimmed, health_trimmed = await self._rows_to_subgraph(
            rows, labels, degraded_sources, principal=principal
        )
        await self._emit_read_audit(
            principal=principal,
            op="neighbors",
            nodes=len(sub.nodes),
            edges=len(sub.edges),
            hops=hops,
            trimmed=trimmed,
            health_trimmed=health_trimmed,
            trace_id=trace_id,
        )
        return sub

    async def subgraph(
        self,
        keys: Sequence[EntityKey],
        *,
        principal: Principal,
        hops: int = 2,
        edge_kinds: Sequence[str] | None = None,
        limit_nodes: int = 500,
        limit_edges: int = 2000,
        trace_id: str | None = None,
    ) -> Subgraph:
        labels = self._labels(principal)
        raw_keys = [key.key for key in keys]
        # A subgraph is rooted at its requested anchors, including authorized
        # zero-hop records with no incident edge. Traversal-only Cypher cannot
        # return those isolated anchors, so fetch them through the same trimmed
        # lookup template and fold them into path-shaped rows before the ONE
        # adapter-side ACL/current-authority/source-health pass below.
        anchor_compiled = self._builder.build_lookup(
            sorted(labels),
            keys=raw_keys,
            limit=max(1, len(set(raw_keys))),
        )
        anchor_rows = await self._store.cypher(anchor_compiled.text, anchor_compiled.params)
        compiled = self._builder.build_subgraph(
            sorted(labels),
            raw_keys,
            hops=hops,
            edge_kinds=edge_kinds,
            limit=limit_edges,
            max_hops=self._settings.max_hops,
        )
        traversal_rows = await self._store.cypher(compiled.text, compiled.params)
        rows: list[Mapping[str, Any]] = []
        for row in anchor_rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("node"), Mapping):
                raise GraphStoreError("entity subgraph backend returned a malformed anchor row")
            rows.append({"path_nodes": [row["node"]], "path_rels": []})
        rows.extend(traversal_rows)
        degraded_sources = (
            await evaluate_degraded_sources(self._source_health) if rows else frozenset()
        )
        sub, trimmed, health_trimmed = await self._rows_to_subgraph(
            rows,
            labels,
            degraded_sources,
            principal=principal,
            limit_nodes=limit_nodes,
            limit_edges=limit_edges,
        )
        await self._emit_read_audit(
            principal=principal,
            op="subgraph",
            nodes=len(sub.nodes),
            edges=len(sub.edges),
            hops=hops,
            trimmed=trimmed,
            health_trimmed=health_trimmed,
            trace_id=trace_id,
        )
        return sub

    async def paths(
        self,
        from_key: EntityKey,
        to_key: EntityKey,
        *,
        principal: Principal,
        max_hops: int = 4,
        limit: int = 20,
        trace_id: str | None = None,
    ) -> Sequence[GraphPathRecord]:
        stamped = await self._paths(
            from_key,
            to_key,
            principal=principal,
            max_hops=max_hops,
            limit=limit,
            trace_id=trace_id,
            require_stamps=False,
        )
        return tuple(item.path for item in stamped)

    async def paths_with_source_stamps(
        self,
        from_key: EntityKey,
        to_key: EntityKey,
        *,
        principal: Principal,
        max_hops: int = 4,
        limit: int = 20,
        trace_id: str | None = None,
    ) -> Sequence[StampedGraphPathRecord]:
        """Return paths only when every node/edge retains exact typed stamps."""

        return await self._paths(
            from_key,
            to_key,
            principal=principal,
            max_hops=max_hops,
            limit=limit,
            trace_id=trace_id,
            require_stamps=True,
        )

    async def _paths(
        self,
        from_key: EntityKey,
        to_key: EntityKey,
        *,
        principal: Principal,
        max_hops: int,
        limit: int,
        trace_id: str | None,
        require_stamps: bool,
    ) -> tuple[StampedGraphPathRecord, ...]:
        labels = self._labels(principal)
        compiled = self._builder.build_paths(
            sorted(labels),
            from_key.key,
            to_key.key,
            max_hops=min(max_hops, self._settings.max_hops),
            limit=limit,
            hop_ceiling=self._settings.max_hops,
        )
        rows = await self._store.cypher(compiled.text, compiled.params)
        degraded_sources = (
            await evaluate_degraded_sources(self._source_health) if rows else frozenset()
        )
        records: list[StampedGraphPathRecord] = []
        trimmed = 0
        health_trimmed = 0
        for row in rows:
            path_nodes = row["path_nodes"]
            path_rels = row["path_rels"]
            if not _path_all_visible(path_nodes, path_rels, labels):
                trimmed += 1
                continue
            if not all(
                [
                    await self._currently_authorized(props, principal)
                    for props in (*path_nodes, *path_rels)
                ]
            ):
                trimmed += 1
                continue
            if not all(
                _graph_source_is_healthy(props.get("source_system"), degraded_sources)
                for props in (*path_nodes, *path_rels)
            ):
                health_trimmed += 1
                continue
            hop_stamps: list[tuple[SourceAuthorityStamp, ...]] = []
            for index, edge in enumerate(path_rels):
                element_stamps = tuple(
                    self._retained_stamps(props)
                    for props in (path_nodes[index], edge, path_nodes[index + 1])
                )
                if any(stamps is None for stamps in element_stamps):
                    hop_stamps = []
                    break
                hop_stamps.append(
                    canonical_source_authority_stamps(
                        tuple(
                            stamp
                            for stamps in element_stamps
                            if stamps is not None
                            for stamp in stamps
                        )
                    )
                )
            if require_stamps and (
                len(hop_stamps) != len(path_rels) or any(not stamps for stamps in hop_stamps)
            ):
                trimmed += 1
                continue
            path = GraphPathRecord(
                nodes=tuple(_to_node(n) for n in path_nodes),
                edges=tuple(_to_edge(e) for e in path_rels),
            )
            records.append(
                StampedGraphPathRecord(
                    path=path,
                    hop_stamps=tuple(hop_stamps),
                )
            )
        await self._emit_read_audit(
            principal=principal,
            op="paths",
            nodes=sum(len(r.path.nodes) for r in records),
            edges=sum(len(r.path.edges) for r in records),
            hops=min(max_hops, self._settings.max_hops),
            trimmed=trimmed,
            health_trimmed=health_trimmed,
            trace_id=trace_id,
        )
        return tuple(records)

    async def _rows_to_subgraph(
        self,
        rows: Sequence[Mapping[str, Any]],
        labels: frozenset[str],
        degraded_sources: frozenset[str],
        *,
        principal: Principal,
        limit_nodes: int | None = None,
        limit_edges: int | None = None,
    ) -> tuple[Subgraph, int, int]:
        nodes: dict[str, EntityNode] = {}
        edges: dict[str, EntityEdge] = {}
        trimmed = 0
        health_trimmed = 0
        for row in rows:
            path_nodes = row["path_nodes"]
            path_rels = row["path_rels"]
            if not _path_all_visible(path_nodes, path_rels, labels):
                trimmed += 1
                continue
            if not all(
                [
                    await self._currently_authorized(props, principal)
                    for props in (*path_nodes, *path_rels)
                ]
            ):
                trimmed += 1
                continue
            if not all(
                _graph_source_is_healthy(props.get("source_system"), degraded_sources)
                for props in (*path_nodes, *path_rels)
            ):
                health_trimmed += 1
                continue
            for n in path_nodes:
                nodes.setdefault(str(n["key"]), _to_node(n))
            for e in path_rels:
                edges.setdefault(str(e["key"]), _to_edge(e))
        node_tuple = tuple(nodes[k] for k in sorted(nodes))
        edge_tuple = tuple(edges[k] for k in sorted(edges))
        if limit_nodes is not None:
            node_tuple = node_tuple[:limit_nodes]
        if limit_edges is not None:
            edge_tuple = edge_tuple[:limit_edges]
        return Subgraph(nodes=node_tuple, edges=edge_tuple), trimmed, health_trimmed
