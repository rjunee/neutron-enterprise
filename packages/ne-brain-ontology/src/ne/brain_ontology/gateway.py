# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ValidatingTripleStore`` — the fail-closed SHACL write gateway (NE-085a).

Every instance-data write goes through here and is SHACL-validated BEFORE it
reaches the backend (delta + one-hop CBD context, fail-closed): a non-conforming
batch is quarantined whole (no partial writes) with a ``ShapeViolation`` event
and NEVER written; a validator error is a typed denial (no write, no quarantine).
The gateway structurally satisfies the FROZEN ``ne.contracts.knowledge.TripleStore``.

The raw backend is package-private and only ever reachable through this object
(backend-privacy invariant): the factories below return the gateway only — there
is no entry point that yields a raw backend. ``sparql`` is the steward/audit read
surface (SELECT/ASK only, deterministic LOAD/SERVICE/UPDATE refusal, C12);
agents get verified-answer templates (085c), never raw SPARQL.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from typing import Literal as TLiteral

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import SH, XSD

from ne.contracts.config import compute_config_hash
from ne.events import new_shape_violation
from ne.schema import ShapeViolation, ViolationSeverity

from .acl import ACL_GRAPH_PREFIX
from .config import OntologySettings
from .ontology import OntologyVersion, load_manifest, ontology_version_for, verify_assets
from .quarantine import QuarantinedTriples, SqliteTripleQuarantine, TripleQuarantine, _now_iso
from .shapes import CompiledShapes, PyShaclValidator, build_validator, load_compiled_shapes
from .store.fuseki import FusekiBackend
from .store.oxigraph import OxigraphBackend
from .store.protocols import DatasetSpec, TripleStoreBackend
from .store.sync import SyncReport, sync_ontologies

from ne.contracts.knowledge import ShapeReport  # isort: skip  (frozen contract)

_IRI_SCHEMES = ("http://", "https://", "urn:")

#: Schema-namespace prefix. The lift may never write instance data under it, and
#: full-graph validation excludes these graphs as instance data (they are the
#: schema, validated by the lint gate; the global sh:sparql shapes must not fire
#: on corpus rdfs:labels).
_ONTOLOGY_GRAPH_PREFIX = "urn:ne:ontology:"

# SPARQL keywords the steward read surface deterministically refuses (C12): all
# update/graph-management forms + the two C2 egress vectors (LOAD, SERVICE).
_FORBIDDEN_KEYWORDS = frozenset(
    {"LOAD", "SERVICE", "INSERT", "DELETE", "DROP", "CLEAR", "CREATE", "MOVE", "COPY", "ADD"}
)


class ShapeViolationSink(Protocol):
    """Where the gateway emits a :class:`ShapeViolation` per failed write."""

    def emit(self, event: ShapeViolation) -> None: ...


class _NullShapeViolationSink:
    """Default sink that drops events (the composition root injects the real one)."""

    def emit(self, event: ShapeViolation) -> None:
        return None


class TripleQuarantinedError(RuntimeError):
    """A write was rejected by SHACL and quarantined — NOT written."""

    def __init__(self, quarantine_id: str, violations: Sequence[str]) -> None:
        super().__init__(f"write quarantined ({quarantine_id}): {'; '.join(violations)}")
        self.quarantine_id = quarantine_id
        self.violations = tuple(violations)


class ShapeValidationError(RuntimeError):
    """The validator itself errored — a denial. Nothing written, nothing quarantined."""


def _datatype_map(shapes: Graph) -> dict[str, URIRef]:
    """Map each ``sh:path`` predicate → its ``sh:datatype`` from the compiled shapes.

    The gateway serializes a write's objects with the datatype the SHACL shapes
    ENFORCE, so a typed LinkML slot (``xsd:integer`` employee_count, ``xsd:dateTime``
    created_at, …) is written as ``Literal(value, datatype=xsd:…)`` and validates
    instead of being quarantined. The map is derived from the SAME compiled shapes
    that validation runs, so it can never drift from what validation enforces.

    ``xsd:string`` slots are EXCLUDED (skipped): in RDF 1.1 a plain literal IS an
    ``xsd:string``, so leaving them untyped is byte-identical to the prior behavior
    and keeps IRI-shaped string values flowing through the ``_IRI_SCHEMES`` rule.
    If a predicate carries CONFLICTING datatypes across shapes it is EXCLUDED too
    (deterministic untyped fallback → still fail-closed at validation). No such
    conflict exists in today's schema; the policy is defensive.
    """
    mapping: dict[str, URIRef] = {}
    conflicting: set[str] = set()
    for prop_shape, _p, path in shapes.triples((None, SH.path, None)):
        if not isinstance(path, URIRef):
            continue
        datatype = shapes.value(prop_shape, SH.datatype)
        if not isinstance(datatype, URIRef) or datatype == XSD.string:
            continue
        key = str(path)
        if key in conflicting:
            continue
        existing = mapping.get(key)
        if existing is not None and existing != datatype:
            # Conflicting datatypes for one predicate → untyped fallback.
            del mapping[key]
            conflicting.add(key)
            continue
        mapping[key] = datatype
    return mapping


@dataclass(frozen=True, slots=True)
class DeclaredLiteral:
    """The writer DECLARES this object is an RDF literal — schema-authoritative.

    A ``DeclaredLiteral`` is NEVER subject to ``_rdf_object``'s ``_IRI_SCHEMES``
    heuristic: the lift already knows each slot's shape-declared datatype (it drives
    typing from the SAME compiled shapes validation enforces), so an IRI-shaped
    value in an ``xsd:string`` slot (``Document.uri``, ``nea:source_ref``) lands as
    a Literal instead of being URIRef-coerced and quarantined (FIX-1). ``datatype``
    ``None`` means a plain literal (RDF 1.1 ``xsd:string``) with a fallback to the
    gateway's shape-datatype map for the predicate.
    """

    lexical: str
    datatype: str | None = None


#: A triple object at the (package-local) lift seam: a plain ``str`` (heuristic
#: path — no schema signal) or a schema-authoritative :class:`DeclaredLiteral`.
TripleObject = str | DeclaredLiteral

#: Outcome of :meth:`ValidatingTripleStore.replace_acl_graph` — the graph was
#: atomically ``"replaced"`` with new content, ``"deleted"`` (vacated: desired
#: content empty, prior content purged), or left ``"unchanged"`` (a byte-identical
#: re-lift is a true no-op with zero backend writes).
ReplaceOutcome = TLiteral["replaced", "deleted", "unchanged"]


def _rdf_object(value: str | DeclaredLiteral, datatype: URIRef | None) -> URIRef | Literal:
    """Serialize a triple object to an rdflib term.

    A :class:`DeclaredLiteral` is ALWAYS a literal (schema-authoritative): its own
    ``datatype`` wins, else it falls back to the gateway's per-predicate datatype
    (``None`` → today's plain RDF 1.1 literal). A plain ``str`` takes the legacy
    no-schema-signal path: an absolute http/https/urn IRI (checked FIRST) becomes a
    ``URIRef`` — so an IRI-shaped value supplied for a datatyped slot correctly
    FAILS ``sh:datatype`` → quarantine — otherwise a ``Literal`` carrying the slot
    datatype.
    """
    if isinstance(value, DeclaredLiteral):
        if value.datatype is not None:
            return Literal(value.lexical, datatype=URIRef(value.datatype))
        return Literal(value.lexical, datatype=datatype)
    if value.startswith(_IRI_SCHEMES):
        return URIRef(value)
    return Literal(value, datatype=datatype)


def _delta_graph(
    triples: Sequence[tuple[str, str, TripleObject]], datatypes: Mapping[str, URIRef]
) -> Graph:
    graph = Graph()
    for subject, predicate, obj in triples:
        graph.add((URIRef(subject), URIRef(predicate), _rdf_object(obj, datatypes.get(predicate))))
    return graph


def _strip_literals_and_comments(query: str) -> str:
    """Blank out string literals and ``#`` comments so keyword scans see only syntax.

    A ``#`` inside a string is not a comment, a ``#`` inside an ``<IRIREF>`` (e.g.
    the ``rdf-syntax-ns#`` namespace) is not a comment, and a quote inside a comment
    is not a string. This single left-to-right pass tracks all three states so an
    obfuscated ``FILTER(?x = "service")``, a ``# drop everything`` comment, or a
    ``PREFIX rdf: <...#>`` prologue IRI never trips or fools the C12 gate.

    IRIREF CONTENT is masked to the empty ``<>`` (angle brackets kept): a SPARQL
    keyword can never legally appear as SPARQL SYNTAX inside an ``<IRIREF>`` (the
    grammar admits no whitespace or ``<`` there), so the keyword ban must apply to
    query syntax ONLY — never to path segments of a data IRI. Emitting the IRI
    verbatim would let a legitimate SELECT/ASK over an IRI like
    ``<https://example.com/service/123>`` be wrongly refused because its ``/service/``
    segment tokenizes to the SPARQL SERVICE keyword. Masking to ``<>`` is strictly
    safer: it removes those false positives while a real ``SERVICE``/``LOAD``/
    ``INSERT`` keyword (which sits OUTSIDE any IRIREF) is still seen and refused,
    and ``<>`` still matches the ``<[^>]*>`` IRIREF token in ``_PROLOGUE_DECL`` so
    prologue peeling in ``_query_form`` is unaffected.
    """
    out: list[str] = []
    i = 0
    n = len(query)
    while i < n:
        ch = query[i]
        if ch == "#":
            while i < n and query[i] != "\n":
                i += 1
            continue
        if ch == "<":
            # An IRIREF iff a ``>`` closes it before any whitespace or ``<`` (per the
            # SPARQL grammar); else a bare ``<`` comparison operator (emit as-is).
            j = i + 1
            while j < n and query[j] not in "<> \t\r\n":
                j += 1
            if j < n and query[j] == ">":
                # Mask IRIREF content: the keyword ban applies to SPARQL syntax
                # only, never to an IRI's path segments (see the docstring).
                out.append("<>")
                i = j + 1
                continue
            out.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            triple = query[i : i + 3] == quote * 3
            delim = quote * 3 if triple else quote
            i += len(delim)
            while i < n and query[i : i + len(delim)] != delim:
                # Honor backslash escapes inside single/double (non-triple) strings.
                if query[i] == "\\" and not triple and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i += len(delim)
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _reject_forbidden(query: str) -> None:
    stripped = _strip_literals_and_comments(query).upper()
    tokens = set(re.findall(r"[A-Z]+", stripped))
    banned = tokens & _FORBIDDEN_KEYWORDS
    if banned:
        raise ValueError(
            f"steward SPARQL refuses {sorted(banned)}: read-only SELECT/ASK only "
            "(no update/graph-management, no LOAD/SERVICE — C12/C2)"
        )


# A single SPARQL prologue declaration: ``BASE <iri>`` or ``PREFIX label: <iri>``
# (empty label allowed). Anchored at the front so we can peel declarations off one
# at a time — the IRIREF/label tokens must NOT be mistaken for the query form.
_PROLOGUE_DECL = re.compile(
    r"\s*(?:BASE\s*<[^>]*>|PREFIX\s+[^\s:<>]*:\s*<[^>]*>)",
    re.IGNORECASE,
)


def _query_form(query: str) -> str:
    stripped = _strip_literals_and_comments(query)
    # Consume the whole prologue (keyword + label + IRIREF per declaration) so the
    # first REMAINING keyword is the actual query form, never a prefix label or an
    # IRI fragment like ``http`` from a BASE/PREFIX IRIREF.
    pos = 0
    while (match := _PROLOGUE_DECL.match(stripped, pos)) is not None:
        pos = match.end()
    remainder = re.search(r"[A-Za-z]+", stripped[pos:])
    return remainder.group(0).upper() if remainder else ""


def assert_read_only_sparql(query: str) -> str:
    """Refuse a non-read query and return its ``SELECT``/``ASK`` form.

    THE single implementation of the C12 read gate: the deterministic
    LOAD/SERVICE/UPDATE keyword refusal plus the SELECT/ASK-only form check that
    :meth:`ValidatingTripleStore.sparql` and :meth:`ValidatingTripleStore.sparql_scoped`
    both apply.  015A0-AUTHORITY-T3's selected-release reader executes against an
    immutable release Store rather than the gateway, so it must apply the IDENTICAL
    gate; it calls this function rather than restating the rule, so the two paths
    cannot drift.
    """
    _reject_forbidden(query)
    form = _query_form(query)
    if form not in ("SELECT", "ASK"):
        raise ValueError(f"steward SPARQL supports SELECT/ASK only, got {form!r}")
    return form


class ValidatingTripleStore:
    """The SHACL write gateway; satisfies the frozen ``TripleStore`` Protocol."""

    def __init__(
        self,
        backend: TripleStoreBackend,
        validator: PyShaclValidator,
        quarantine: TripleQuarantine,
        violation_sink: ShapeViolationSink,
        *,
        settings: OntologySettings,
        config_hash: str,
        compiled_shapes: CompiledShapes | None = None,
    ) -> None:
        self._backend = backend
        self._validator = validator
        self._quarantine = quarantine
        self._sink = violation_sink
        self._settings = settings
        self._config_hash = config_hash
        self._compiled = compiled_shapes or load_compiled_shapes()
        # Per-write slot datatypes from the SAME compiled shapes validation runs —
        # covers the generated operational.shapes.ttl AND curated per-write shapes,
        # so a typed write is serialized with the datatype the shapes enforce.
        self._datatypes = _datatype_map(self._compiled.per_write_graph())

    @property
    def supports_immutable_release_snapshot(self) -> bool:
        """Whether the wrapped backend can prove an immutable release snapshot.

        Backend privacy is preserved: this projects ONE boolean capability of the
        private backend, never the backend object. 015A0-AUTHORITY-T3's
        source-bearing guard reads this before any I/O, so a Fuseki-backed gateway
        is refused without issuing an HTTP request.
        """
        return bool(getattr(self._backend, "supports_immutable_release_snapshot", False))

    # -- the write path (fail-closed) -----------------------------------------

    async def insert_triples(self, triples: Sequence[tuple[str, str, str]]) -> None:
        await self._validated_insert(triples, graph_iri=self._settings.data_graph_iri)

    async def insert_triples_into(
        self,
        triples: Sequence[tuple[str, str, TripleObject]],
        *,
        graph_iri: str,
        context_triples: Sequence[tuple[str, str, TripleObject]] = (),
    ) -> None:
        """Validated write into a caller-chosen (non-schema) named graph (NE-085b).

        Identical fail-closed validate-before-write path as :meth:`insert_triples`,
        but lands in ``graph_iri`` — the RDF lift's per-ACL-label named graph. This
        is the SLOT-DATATYPE-AUTHORITATIVE seam: a triple object may be a
        :class:`DeclaredLiteral`, which the lift emits for every schema-typed slot
        so an IRI-shaped ``xsd:string`` value (``Document.uri``) is a Literal, not a
        URIRef (FIX-1) — the ``_rdf_object`` string-shape heuristic applies only to
        plain-``str`` objects with no schema signal. It REFUSES any
        ``urn:ne:ontology:*`` graph (schema-namespace containment, mirroring
        ``store.sync``): a lifted instance triple can never overwrite a corpus
        graph. Validation still reads the FULL union (a lifted edge's endpoint
        typing lives in a DIFFERENT acl graph and ``sh:class`` must see it);
        trimming is a READ concern only, never a write-time one.

        ``context_triples`` (NE-085e): the caller's desired-state two-hop CBD,
        added to the validation payload ONLY (mirrors :meth:`replace_acl_graph`)
        — never written to the backend. This lets an initial-load caller
        (:meth:`PropertyGraphLift.run`) satisfy a cross-graph ``sh:minCount``/
        ``sh:class`` (a required outgoing edge and its target's ``rdf:type``,
        which land in a DIFFERENT ACL-graph partition than the node) from data
        that is part of the SAME load, even though the store CBD (still always
        included via :meth:`_cbd_context`) has not been written yet.
        """
        if graph_iri.startswith(_ONTOLOGY_GRAPH_PREFIX):
            raise ValueError(
                f"insert_triples_into refuses schema graph {graph_iri!r}: the lift may "
                "never write under urn:ne:ontology:* (schema-writer containment)"
            )
        await self._validated_insert(triples, graph_iri=graph_iri, context_triples=context_triples)

    async def replace_acl_graph(
        self,
        triples: Sequence[tuple[str, str, TripleObject]],
        *,
        graph_iri: str,
        context_triples: Sequence[tuple[str, str, TripleObject]] = (),
    ) -> ReplaceOutcome:
        """VALIDATE-THEN-ATOMIC-SWAP of ONE per-ACL-label named graph (NE-085d).

        The mutation-aware reconcile primitive: the caller (:meth:`PropertyGraphLift.
        reconcile`) has computed the COMPLETE desired triple set for ``graph_iri`` from
        a full Tier-2 scan; this method validates that full content through the SAME
        fail-closed pySHACL validator as the append path and, ONLY on conformance,
        atomically replaces the graph's content via the backend's whole-named-graph
        ``replace_graph_turtle`` (PUT semantics). The swap granularity is one named
        graph: a changed single-valued slot → the rebuilt graph holds only the new
        value (no ``sh:maxCount`` conflict); an ACL move → the entity's triples land in
        the new label's graph while the vacated label's graph is rebuilt/purged WITHOUT
        them (closing the C7 over-return). Both 085b failure modes are fixed here.

        FAIL-CLOSED (the 085a invariant, SPEC §11): validation runs BEFORE any
        destructive backend op — there is NO delete-then-validate window. A violation
        records a quarantine row (carrying ``graph_iri``), emits a ``ShapeViolation``,
        and raises :class:`TripleQuarantinedError` — the EXISTING graph content is left
        INTACT. ``context_triples`` (the caller's desired-union two-hop CBD) is added to
        the validation payload ONLY (never written) so a cross-graph ``sh:class`` can be
        satisfied without pulling stale triples from the store.

        Containment (stricter than :meth:`insert_triples_into`): only a
        ``urn:ne:graph:acl:*`` graph is accepted — this seam can NEVER touch
        ``urn:ne:ontology:*``, ``urn:ne:ontology:sync-state``, or the default data
        graph. Returns ``"replaced"`` / ``"deleted"`` (a vacated graph purged) /
        ``"unchanged"`` (byte-identical desired content → a true no-op, zero backend
        writes). The empty-desired purge is NOT a pre-validation destructive delete: the
        FULL desired state is already known to be empty and empty content trivially
        conforms, so vacating the graph is the correct atomic swap to nothing.

        A reconcile-quarantine row is DIAGNOSTIC only: :meth:`release` would append the
        delta and re-validate against the still-stale store (fail-closed re-quarantine),
        never a corruption path — remediation is fix-T2-and-re-run-reconcile.
        """
        if not graph_iri.startswith(ACL_GRAPH_PREFIX):
            raise ValueError(
                f"replace_acl_graph refuses {graph_iri!r}: the reconcile swap accepts "
                "ONLY urn:ne:graph:acl:* graphs (never the schema, sync-state, or data "
                "graph — schema-writer containment)"
            )
        snapshot = await self._backend.construct_nt(
            "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
            dataset=DatasetSpec(default_graphs=(graph_iri,)),
        )
        if not triples:
            # EMPTY-DESIRED: the full desired state for this graph is empty. Purge a
            # vacated graph (its ACL label left T2); a fresh/empty graph is a no-op.
            if not snapshot.strip():
                return "unchanged"
            await self._backend.delete_graph(graph_iri)
            return "deleted"
        delta = _delta_graph(triples, self._datatypes)
        snapshot_graph = Graph()
        snapshot_graph.parse(data=snapshot.decode("utf-8"), format="nt")
        # UNCHANGED-SKIP: the lift emits NO blank nodes, so rdflib triple-set equality
        # is exact — a byte-identical re-lift re-swaps nothing (zero backend writes).
        if set(snapshot_graph) == set(delta):
            return "unchanged"
        delta_nt = delta.serialize(format="nt")
        context_nt = (
            _delta_graph(context_triples, self._datatypes).serialize(format="nt")
            if context_triples
            else ""
        )
        payload = (delta_nt + "\n" + context_nt) if context_nt.strip() else delta_nt
        try:
            report = await self._validator.validate({"data": payload, "format": "nt"})
        except Exception as exc:  # validator crash ⇒ denial, never write-anyway
            raise ShapeValidationError(f"SHACL validation errored: {exc}") from exc
        if not report.conforms:
            self._quarantine_batch(delta_nt, report.violations, graph_iri=graph_iri)
            # unreachable: _quarantine_batch always raises (prior graph left INTACT).
        await self._backend.replace_graph_turtle(delta_nt.encode("utf-8"), graph_iri=graph_iri)
        return "replaced"

    async def acl_graphs(self) -> tuple[str, ...]:
        """The lift's discovery seam: every ``urn:ne:graph:acl:*`` named graph present.

        Reconcile unions this with the DESIRED graph set to find VACATED graphs (a
        label whose last element left T2) that must be purged. The raw backend stays
        package-private — this returns only IRIs, never a backend.
        """
        return tuple(
            sorted(g for g in await self._backend.graphs() if g.startswith(ACL_GRAPH_PREFIX))
        )

    async def _validated_insert(
        self,
        triples: Sequence[tuple[str, str, TripleObject]],
        *,
        graph_iri: str,
        context_triples: Sequence[tuple[str, str, TripleObject]] = (),
    ) -> None:
        if not triples:
            return
        delta = _delta_graph(triples, self._datatypes)
        delta_nt = delta.serialize(format="nt")
        context_nt = await self._cbd_context(delta)
        caller_nt = (
            _delta_graph(context_triples, self._datatypes).serialize(format="nt")
            if context_triples
            else ""
        )
        payload = "\n".join(part for part in (delta_nt, context_nt, caller_nt) if part.strip())

        try:
            report = await self._validator.validate({"data": payload, "format": "nt"})
        except Exception as exc:  # validator crash ⇒ denial, never write-anyway
            raise ShapeValidationError(f"SHACL validation errored: {exc}") from exc

        if not report.conforms:
            self._quarantine_batch(delta_nt, report.violations, graph_iri=graph_iri)
            # unreachable: _quarantine_batch always raises
            return
        await self._backend.insert_nt(delta_nt.encode("utf-8"), graph_iri=graph_iri)

    async def _cbd_context(self, delta: Graph) -> str:
        # TWO-HOP CBD context: the outgoing triples of every delta SUBJECT/IRI
        # OBJECT (hop 1) *and* the outgoing triples of every IRI those reach (hop
        # 2). Referenced objects are essential because a class-ranged relationship
        # (e.g. ``Ticket.belongs_to -> Account``) compiles to an ``sh:class``
        # constraint pySHACL can satisfy only when the referenced entity's existing
        # ``rdf:type`` (and, since that makes it a validation target, its required
        # slots) is in the payload. The SECOND hop is what makes a RE-LIFT
        # idempotent (NE-085b): re-writing a node whose typed relationship ALREADY
        # lives in the store pulls that edge at hop 1 and the edge target's type at
        # hop 2, so the node re-validates instead of being spuriously quarantined.
        # Fail-closed is preserved: hops only pull EXISTING triples, so a
        # relationship whose target is NOT in the store pulls no type and still
        # fails ``sh:class`` → quarantine.
        nodes = {s for s in delta.subjects() if isinstance(s, URIRef)}
        nodes |= {o for o in delta.objects() if isinstance(o, URIRef)}
        if not nodes:
            return ""
        values = " ".join(f"<{s}>" for s in sorted(str(s) for s in nodes))
        # Branch 1: seed's outgoing triples. Branch 2: the outgoing triples of every
        # IRI object one hop out from a seed. FULL UNION (NE-085b): a lifted edge's
        # endpoint typing lives in a DIFFERENT per-ACL-label named graph than the
        # edge itself, so sh:class can only be satisfied when the context sees every
        # graph. Validation is a platform concern (never principal-trimmed); trimming
        # applies to READS only.
        query = (
            "CONSTRUCT { ?s ?p ?o } WHERE { "
            f"VALUES ?seed {{ {values} }} "
            "{ ?seed ?p ?o . BIND(?seed AS ?s) } "
            "UNION "
            "{ ?seed ?p0 ?s . FILTER(isIRI(?s)) ?s ?p ?o } "
            "}"
        )
        context = await self._backend.construct_nt(query, dataset=DatasetSpec())
        return context.decode("utf-8")

    def _quarantine_batch(
        self, delta_nt: str, violations: Sequence[str], *, graph_iri: str | None = None
    ) -> None:
        quarantine_id = f"urn:ne:quarantine:{uuid.uuid4()}"
        self._quarantine.record(
            QuarantinedTriples(
                quarantine_id=quarantine_id,
                delta_nt=delta_nt,
                violations=tuple(violations),
                provenance=self._settings.actor,
                shapes_hash=self._compiled.shapes_hash,
                created_at=_now_iso(),
                graph_iri=graph_iri,
            )
        )
        self._sink.emit(
            new_shape_violation(
                schema_name="ne-brain-ontology:shacl",
                schema_version=self._compiled.shapes_hash,
                subject_ref=quarantine_id,
                message="; ".join(violations),
                severity=ViolationSeverity.error,
                actor=self._settings.actor,
                quarantined=True,
                config_hash=self._config_hash,
            )
        )
        raise TripleQuarantinedError(quarantine_id, violations)

    # -- the steward read surface (C12) ---------------------------------------

    async def sparql(self, query: str) -> Sequence[Mapping[str, Any]]:
        """UNSCOPED steward/audit read (full graph union, SELECT/ASK-only, C12).

        Any NEW call site of this unscoped surface must be registered in
        ``packages/ne-brain-ontology/tests/test_c7_ontology_trimming_guardian.py``'s
        ``_SPARQL_CALL_ALLOWLIST``; principal-serving reads must use
        :meth:`sparql_scoped` instead, never this method.
        """
        form = assert_read_only_sparql(query)
        dataset = DatasetSpec()  # full union — steward/audit surface
        if form == "SELECT":
            return await self._backend.select(query, dataset=dataset)
        return [{"ask": await self._backend.ask(query, dataset=dataset)}]

    async def sparql_scoped(
        self, query: str, *, dataset: DatasetSpec
    ) -> Sequence[Mapping[str, Any]]:
        """C7-trimmed read: same SELECT/ASK-only refusal as :meth:`sparql`, but
        executes against a caller-supplied :class:`DatasetSpec`.

        The dataset is built ONLY by trusted code (085b's ``graphs_for_labels``
        over the principal's labels; 085c's scope layer feeds this). A lifted
        triple is trimmed identically to its T2 source because the dataset unions
        exactly the per-ACL-label named graphs the principal holds.
        """
        form = assert_read_only_sparql(query)
        if form == "SELECT":
            return await self._backend.select(query, dataset=dataset)
        return [{"ask": await self._backend.ask(query, dataset=dataset)}]

    # -- quarantine release + nightly full validation -------------------------

    async def release(self, quarantine_id: str) -> None:
        """Re-validate a quarantined delta; on conformance write it + remove the row.

        A still-invalid delta re-raises :class:`TripleQuarantinedError` with the
        FRESH violations (the row stays). ``write.internal.ontology.release`` is a
        tier-1 curation action at the composition root.
        """
        entry = self._quarantine.get(quarantine_id)
        if entry is None:
            raise KeyError(f"no quarantined batch {quarantine_id!r}")
        delta = Graph()
        delta.parse(data=entry.delta_nt, format="nt")
        context_nt = await self._cbd_context(delta)
        payload = (entry.delta_nt + "\n" + context_nt) if context_nt.strip() else entry.delta_nt
        try:
            report = await self._validator.validate({"data": payload, "format": "nt"})
        except Exception as exc:
            raise ShapeValidationError(f"SHACL validation errored on release: {exc}") from exc
        if not report.conforms:
            raise TripleQuarantinedError(quarantine_id, report.violations)
        # Re-insert into the ORIGINAL target graph — a quarantined LIFT batch
        # carries its per-ACL-label graph_iri; the default write path leaves it
        # None and releases into the data graph (NE-085b).
        target_graph = entry.graph_iri or self._settings.data_graph_iri
        await self._backend.insert_nt(entry.delta_nt.encode("utf-8"), graph_iri=target_graph)
        self._quarantine.remove(quarantine_id)

    async def validate_full_graph(self) -> ShapeReport:
        """Nightly hook: validate the ENTIRE instance graph against per-write + global shapes.

        This is a GLOBAL governance operation under platform identity: it unions the
        default data graph AND every per-ACL-label named graph the lift writes into
        (``backend.graphs()`` minus the ``urn:ne:ontology:*`` schema graphs), so the
        promoted conformance gate actually validates the lifted knowledge graph
        (FIX-2 — the pre-fix version saw ONLY ``data_graph_iri`` and was blind to all
        lifted RDF). Reading every instance graph here is BY DESIGN (fail-closed gate
        coverage) and does NOT touch the C7 principal-trimmed READ path
        (``sparql_scoped``). Schema graphs are excluded as instance data — they are
        validated by the lint gate, and the global ``sh:sparql`` shapes (e.g.
        unique-label) must not fire on corpus ``rdfs:label``\\ s. Emits a
        NON-quarantined ``ShapeViolation`` on nonconformance (the global ``sh:sparql``
        shapes that per-write validation cannot see are checked here).
        """
        named = await self._backend.graphs()
        instance_graphs = tuple(
            sorted(
                {self._settings.data_graph_iri}
                | {g for g in named if not g.startswith(_ONTOLOGY_GRAPH_PREFIX)}
            )
        )
        data = await self._backend.construct_nt(
            "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
            dataset=DatasetSpec(default_graphs=instance_graphs),
        )
        ont = None
        if self._settings.include_ontology_graph:
            from .shapes import load_corpus_graph

            ont = load_corpus_graph()
        full_validator = PyShaclValidator(self._compiled.combined_graph(), ont=ont)
        report = await full_validator.validate({"data": data.decode("utf-8"), "format": "nt"})
        if not report.conforms:
            self._sink.emit(
                new_shape_violation(
                    schema_name="ne-brain-ontology:shacl:full",
                    schema_version=self._compiled.shapes_hash,
                    subject_ref=self._settings.data_graph_iri,
                    message="; ".join(report.violations),
                    severity=ViolationSeverity.error,
                    actor=self._settings.actor,
                    quarantined=False,
                    config_hash=self._config_hash,
                )
            )
        return report

    # -- startup corpus load + version registry (NE-085b) ---------------------

    async def sync_ontology_corpus(self) -> SyncReport:
        """THE composition-root startup hook: load BFO+CCO+alignment into the store.

        Delegates to the hash-diff idempotent ``sync_ontologies`` over the vendored
        manifest (offline, C2): a first sync loads every corpus graph, a re-sync is
        a zero-write no-op, a changed pin swaps exactly the changed graphs. The
        writer refuses any graph IRI outside ``urn:ne:ontology:*``.

        C11 (byte-matched supply chain, fail-closed on the RUNTIME load path): the
        integrity gate ``verify_assets`` byte-verifies EVERY vendored TTL against its
        pinned sha256 BEFORE anything loads — a tampered/corrupted file raises here
        and never reaches the schema graphs. This closes the gap where ``sync.py``
        compared the manifest pins against the stored sync-state but never the actual
        on-disk bytes. Startup-only cost (~13 file hashes, milliseconds — the same
        file-read pattern as ``load_manifest``).
        """
        manifest = load_manifest()
        # C11 fail-closed: byte-verify EVERY vendored TTL against its pinned sha256
        # BEFORE anything loads — a tampered/corrupted file raises here and never
        # reaches the schema graphs.
        verify_assets(manifest)
        return await sync_ontologies(self._backend, manifest)

    def ontology_version(self) -> OntologyVersion:
        """The §7.4 in-package version registry (manifest pins + shapes hash).

        Surfaced for health checks / ``config_hash`` inputs; carries each source's
        pinned commit (or ``pin`` for the self-authored alignment), the per-file
        sha256s, and the compiled ``shapes_hash``.
        """
        return ontology_version_for(load_manifest(), self._compiled.shapes_hash)


# ---------------------------------------------------------------------------
# Factories — the ONLY public constructors; they return the gateway, never a
# raw backend (no entry point yields a bare backend).
# ---------------------------------------------------------------------------


def _build_gateway(
    backend: TripleStoreBackend,
    settings: OntologySettings,
    *,
    config_hash: str | None,
    quarantine: TripleQuarantine | None,
    violation_sink: ShapeViolationSink | None,
) -> ValidatingTripleStore:
    compiled = load_compiled_shapes()
    validator = build_validator(settings)
    return ValidatingTripleStore(
        backend,
        validator,
        quarantine or SqliteTripleQuarantine(settings.quarantine_db),
        violation_sink or _NullShapeViolationSink(),
        settings=settings,
        # The composition root injects the real platform config_hash; absent that,
        # default to this module's own settings hash (a valid 64-hex digest).
        config_hash=config_hash if config_hash is not None else compute_config_hash(settings),
        compiled_shapes=compiled,
    )


def build_oxigraph_store(
    settings: OntologySettings | None = None,
    *,
    config_hash: str | None = None,
    quarantine: TripleQuarantine | None = None,
    violation_sink: ShapeViolationSink | None = None,
) -> ValidatingTripleStore:
    """Build the gateway over an embedded oxigraph backend (no-docker dev tier)."""
    settings = settings or OntologySettings()
    backend = OxigraphBackend(path=settings.oxigraph_path)
    return _build_gateway(
        backend,
        settings,
        config_hash=config_hash,
        quarantine=quarantine,
        violation_sink=violation_sink,
    )


def build_fuseki_store(
    settings: OntologySettings | None = None,
    *,
    config_hash: str | None = None,
    quarantine: TripleQuarantine | None = None,
    violation_sink: ShapeViolationSink | None = None,
) -> ValidatingTripleStore:
    """Build the gateway over a Fuseki/TDB2 HTTP backend (production tier)."""
    settings = settings or OntologySettings()
    if not settings.fuseki_url:
        raise ValueError("fuseki backend requires OntologySettings.fuseki_url")
    auth = None
    if settings.fuseki_user is not None and settings.fuseki_password is not None:
        auth = (
            settings.fuseki_user.get_secret_value(),
            settings.fuseki_password.get_secret_value(),
        )
    backend = FusekiBackend(
        settings.fuseki_url,
        auth=auth,
        retry_attempts=settings.write_retry_attempts,
        retry_base_delay_s=settings.write_retry_base_delay_s,
    )
    return _build_gateway(
        backend,
        settings,
        config_hash=config_hash,
        quarantine=quarantine,
        violation_sink=violation_sink,
    )


def build_release_staging_store(
    settings: OntologySettings | None = None,
    *,
    config_hash: str | None = None,
    quarantine: TripleQuarantine | None = None,
    violation_sink: ShapeViolationSink | None = None,
) -> tuple[ValidatingTripleStore, OxigraphBackend]:
    """Build a gateway over a DISTINCT, private, in-memory oxigraph Store.

    015A0-AUTHORITY-T3 release writers never write into the Store a selected
    release pins: each release is staged into its own ``Store`` and published by
    CAS-selecting that whole object. This factory is the only one that also returns
    the backend, because the release wrapper must seal the EXACT Store it staged
    into (``build_oxigraph_store`` deliberately yields the gateway alone). The
    returned backend is handed straight to
    :class:`~ne.brain_ontology.verified.selected_release.MeridianReleaseStaging`,
    which is the only object that may hold it; backend privacy is preserved by
    that ownership, and by the read-only wrapper the sealed release exposes.

    ``oxigraph_path`` is deliberately ignored: a release writer is always a fresh
    in-memory Store, never the configured serving path.
    """
    settings = settings or OntologySettings()
    backend = OxigraphBackend(path=None)
    gateway = _build_gateway(
        backend,
        settings,
        config_hash=config_hash,
        quarantine=quarantine,
        violation_sink=violation_sink,
    )
    return gateway, backend
