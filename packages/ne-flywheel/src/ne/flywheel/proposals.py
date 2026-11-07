# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed fix proposals + their append-only store (NE-065).

The curation agent's ENTIRE business-write surface is a proposal row. A
:class:`FixProposal` is a TYPED, config_hash-stamped suggestion for a change —
never the change itself. Its ``status`` is hard-validated to ``"proposed"``:
this unit only ever proposes; NE-066's propose-then-approve gate owns every
transition (approved/applied/rejected) and the actual application of a change.

APPEND-ONLY is a hard invariant, mirroring
``ne.flywheel.store.SqlAlchemyCorrectionSignalStore`` verbatim in style: the
complete public mutation surface of :class:`SqlAlchemyProposalStore` is a single
idempotent INSERT (:meth:`SqlAlchemyProposalStore.append`). No update / delete /
edit method exists anywhere in this module. ``proposal_id`` is deterministic
(content-addressed over kind+lane+cluster), so re-running curation over the same
signals appends nothing new (the SAVEPOINT-idempotent INSERT returns the
existing id).

Retrieval-lane clusters additionally emit a :class:`GoldenSetCandidate` (a
``golden_set_candidate`` proposal): a ref-based Q + expected-source skeleton for
the NE-024 eval golden set (feeds NE-075). C12: raw query/doc TEXT never
persists here, so ``expected_source_refs`` is intentionally EMPTY in v1 — the
steward resolves the actual question + expected source at NE-066 review from the
refs and evidence carried on the candidate.
"""

from __future__ import annotations

import enum
import hashlib
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from ne.audit import ensure_transactional_sqlite
from ne.contracts.source_authority import SourceAuthorityStamp
from ne.flywheel.lanes import FixLane, ProposalPriority

#: The only status a proposal may carry in this unit. NE-066 owns transitions.
PROPOSED_STATUS = "proposed"


class ProposalKind(enum.StrEnum):
    """The kind of change a proposal suggests.

    ``ENTITY_MERGE_CANDIDATE`` is emitted by the ne-brain-graph Splink ER job on
    the ``FixLane.ENTITY_RESOLUTION`` lane; it rides the SAME NE-066 gate as every
    other proposal (the ``kind``/``lane`` columns are plain ``str`` — no migration).

    ``VERIFIED_ANSWER_CANDIDATE`` is a steward-authored governed T3 verified-answer
    template proposal (NE-ONTOLOGY-FLYWHEEL) on the ``FixLane.ONTOLOGY`` lane; it
    too is a plain ``str`` column value — no ne-contracts migration.

    ``SKILL_CANDIDATE`` / ``SKILL_UPDATE_CANDIDATE`` are emitted by the ne-skills
    ``SkillForge`` job on the ``FixLane.SKILLS`` lane (NE-SKILL-1b): a forged skill
    draft awaiting the NE-066 gate + the ``apply-skill`` steward seam. Plain ``str``
    column values — no migration, no ne-contracts change.

    ``GRAPH_EXTRACTION_CANDIDATE`` is emitted by the ne-brain-graph LLM
    extraction job (NE-T2X-2, future) on the EXISTING ``FixLane.ENTITY_RESOLUTION``
    lane — exactly the same lane ``ENTITY_MERGE_CANDIDATE`` already rides. It is a
    plain ``str`` column value: no migration, no ne-contracts change. Its payload
    is a :class:`GraphExtractionBatch` (see that class for the schema). BATCH
    granularity is normative, not an optimization: ONE proposal per
    (document x run), carrying its WHOLE assertion set as one reviewable diff —
    per the NE-T2X design doc §2.4, a per-assertion proposal design produces an
    unreviewable ~5-25k-row queue on a realistic first backfill, which collapses
    the review acts by roughly an order of magnitude when batched per document
    instead. Every extraction mode rides this ONE kind through the UNCHANGED
    NE-066 state machine: ``ingest_review`` gates the graph write with it,
    ``read_flagged`` uses it as the confirmation ledger (approve flips
    ``assurance`` to confirmed), and ``autonomous`` uses it as the retroactive
    review surface (approve/reject any time after an immediate write). One
    mechanism, three gates (design §2.3).
    """

    FIX = "fix"
    GOLDEN_SET_CANDIDATE = "golden_set_candidate"
    ENTITY_MERGE_CANDIDATE = "entity_merge_candidate"
    VERIFIED_ANSWER_CANDIDATE = "verified_answer_candidate"
    SKILL_CANDIDATE = "skill_candidate"
    SKILL_UPDATE_CANDIDATE = "skill_update_candidate"
    GRAPH_EXTRACTION_CANDIDATE = "graph_extraction_candidate"


class GoldenSetCandidate(BaseModel):
    """A ref-based candidate for the NE-024 eval golden set (retrieval lane -> NE-075).

    ``expected_source_refs`` is EMPTY in v1 on purpose (C12: raw query/doc text
    never persists) — the steward resolves the actual expected source at NE-066
    review from ``question_ref`` + ``wrong_source_refs`` + the evidence events.
    """

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    question_ref: str
    wrong_source_refs: tuple[str, ...]
    expected_source_refs: tuple[str, ...] = ()
    evidence_event_ids: tuple[str, ...]
    notes: str = ""


class GraphExtractionAssertionKind(enum.StrEnum):
    """The T2X design §3.1 validation-wall triple an extraction assertion carries."""

    LINK = "link"
    NEW_ENTITY = "new_entity"
    NEW_EDGE = "new_edge"


#: A POSITIVE allow-list for ``properties`` values (Argus round-3 P2-A/P2-B).
#: Pydantic does NOT validate values typed ``Any`` — the previous reject-list
#: validator rejected only ``dict``/``list``, so a ``tuple`` or ``set`` value
#: bypassed BOTH the nesting check and the length cap and serialized straight
#: into the stored payload as a JSON array via ``model_dump(mode="json")`` —
#: reaching ``artifact.pr_body`` end to end (repro:
#: ``test_graph_extraction_properties_reject_non_scalar_values``). Typing the
#: VALUE itself as this union makes every non-scalar container (``dict``,
#: ``list``, ``tuple``, ``set``) or arbitrary object unrepresentable rather
#: than un-enumerated, so the next container type never reopens the vector.
#: (``bytes``/``bytearray`` ARE accepted — pydantic lax-decodes them to
#: ``str`` before this union ever sees a container, so post-validation they
#: are plain strings, not a container vector; pinned by
#: ``test_graph_extraction_properties_accept_contract_scale_values``.)
#:
#: There is deliberately NO length cap. The generated write-side contract this
#: spine feeds (design §2.1 F1: extracted elements must validate through
#: ``ne.schema`` ``ENTITY_CONTRACTS``) accepts unbounded strings —
#: ``TicketEntity.description`` and ``DocumentEntity.uri`` in
#: ``packages/ne-schema/src/ne/schema/generated/operational_graph.py`` carry no
#: length constraint — so a cap here REJECTED values the contract ACCEPTS, and
#: under BATCH granularity one rejected assertion loses the whole document's
#: proposal. See ``test_graph_extraction_properties_accept_contract_scale_values``
#: for the trace from this no-cap decision to the real generated contract.
#:
#: The allow-list is exactly the JSON-scalar set (``str``/``int``/``float``/
#: ``bool``/``None``) — the right rule for a payload stored via
#: ``model_dump(mode="json")`` — but it is narrower than the contract for two
#: field TYPES the same generated contract also declares: ``datetime``
#: (e.g. ``TicketEntity.opened_at``, required) and ``Decimal``
#: (``OrderEntity.total_amount``, ``ge=0``). Verified empirically: a raw
#: ``datetime`` object is REJECTED here (ValidationError at construction —
#: under batch granularity that loses the whole document's proposal, the same
#: harm the removed length cap caused); a raw ``Decimal`` object is instead
#: silently smart-union-coerced to ``float``, which risks losing money
#: precision on a field like ``total_amount``. The CONVENTION for both field
#: types is to carry them as their ISO-8601 / decimal-literal STRING form,
#: which the generated contract itself lax-coerces back to
#: ``datetime``/``Decimal`` on the write side — see
#: ``test_graph_extraction_properties_accept_contract_scale_values`` for the
#: pinned round-trip through both ``TicketEntity.opened_at`` and
#: ``OrderEntity.total_amount``.
#:
#: The ``float`` member of this union is NOT further bounded by the type
#: alias itself: pydantic's ``float`` validator accepts NaN and (+/-)Infinity,
#: neither of which is a valid JSON number, so
#: :meth:`GraphExtractionAssertion._properties_floats_are_finite` rejects them
#: at construction (Codex C1) rather than let them reach
#: ``model_dump(mode="json")`` and risk a PostgreSQL JSONB write failure. See
#: ``test_graph_extraction_properties_reject_non_finite_floats``.
PropertyValue = str | int | float | bool | None


class GraphExtractionAssertion(BaseModel):
    """ONE typed assertion inside a :class:`GraphExtractionBatch`, carrying a
    typed projection of extracted content — never raw chunk prose.

    ``extra="forbid"`` is the C12 no-smuggling structural guard (design §0
    E-P2-2): a future field that starts carrying raw chunk text cannot be added
    to this model without a schema change, and that schema change must confront
    ``test_graph_extraction_models_forbid_extra_keys``. ``properties`` carries
    contract-validated TYPED business fields ONLY (extracted property values are
    business data destined for the graph, which is fine); evidence rides as a
    chunk REF (``evidence_ref``) in the T1 ``source_ref`` grammar, NEVER as
    verbatim chunk text.

    ``kind``-shape coherence is schema-enforced (design §3.1): a ``LINK``
    assertion names an existing entity (``entity_class`` + ``entity_key``); a
    ``NEW_ENTITY`` assertion ALSO requires ``entity_key`` in addition to
    ``entity_class`` + ``display`` — for a brand-new entity this is the
    extraction job's provisional/candidate key; it becomes canonical only
    after NE-066 approval. Per design §3.3 the intended producer (NE-T2X-2)
    derives it as ``entity_key_for("ne:llm", entity_class, "extracted",
    canonical_form(display))`` — i.e. entity_key is ITSELF display-derived,
    NOT an independent identifier. Consequence, stated plainly (see the
    idempotency caveat on :func:`graph_extraction_proposal_id`): re-wording a
    display name in a way that survives §3.3's casefold/whitespace/
    punctuation normalization (e.g. "Acme" vs "Acme Corp.") produces a
    DIFFERENT entity_key on re-run, which changes the content address and
    mints a NEW proposal — design §3.3's deliberate choice ("surface-form
    variance... fragments into two nodes... healed by the existing Splink ER
    lane"), not an idempotency bug. entity_key is required at all precisely
    so the content address (:func:`_assertion_content_tuple`) distinguishes
    two DIFFERENT new entities of the same class extracted from the same
    evidence — e.g. two distinct company names mentioned in one chunk —
    instead of colliding on an identical (kind, class, "", "", "", "", ref)
    tuple (design §0 E-P1-1; a collision here would silently drop the second
    entity's proposal at the store's idempotent-append boundary, since
    ``proposal_id`` is unique). A ``NEW_EDGE`` assertion names its
    subject/kind/object triple.

    C12 caveat on ``properties`` (Argus round-3 P2-A/P2-B, design §0 E-P2-2):
    it carries contract-validated TYPED business values (fine per design §0
    E-P2-2) and is deliberately a free ``dict`` so any well-formed business
    field can ride it — but that freedom means ``extra="forbid"`` alone does
    NOT structurally stop a misbehaving job from stuffing raw chunk prose into
    a property VALUE (e.g. ``properties={"excerpt": "<verbatim chunk
    text>"}``); ``extra="forbid"`` only blocks WHOLESALE new fields, not the
    content of an existing one. The guard against a SMUGGLED STRUCTURE is now
    the type system itself: ``properties`` is typed
    ``dict[str, PropertyValue]`` (see :data:`PropertyValue`), a POSITIVE
    allow-list of the scalars actually permitted — so a non-scalar container
    of ANY kind (``dict``, ``list``, ``tuple``, ``set``, or an arbitrary
    object) is unrepresentable at construction, not merely un-enumerated by a
    validator (``test_graph_extraction_properties_reject_non_scalar_values``
    proves this closes the round-2 validator's ``tuple``/``set`` bypass;
    ``bytes``/``bytearray`` are the one apparent exception, but they lax-decode
    to ``str`` before the union sees a container, so post-validation they are
    not a container vector — see :data:`PropertyValue` and
    ``test_graph_extraction_properties_accept_contract_scale_values``). There
    is NO length cap: the generated write-side contract this spine feeds
    (design §2.1 F1) accepts unbounded strings (``TicketEntity.description``,
    ``DocumentEntity.uri``), and under BATCH granularity a rejected assertion
    loses the whole document's proposal, not just one row
    (``test_graph_extraction_properties_accept_contract_scale_values`` traces
    the no-cap decision to that contract). That same test also traces the
    ``datetime``/``Decimal`` STRING convention (:data:`PropertyValue`'s last
    paragraph) required for the two other contract field types this allow-list
    cannot admit as native objects.

    This is a STRUCTURAL bound on the VALUE's TYPE, not a semantic one on its
    CONTENT: it cannot and does not claim to catch every plain ``str`` value
    that happens to be an excerpt of prose rather than a business value —
    nothing distinguishes a legitimate short string from raw chunk prose of
    any length riding the same ``str`` slot (see
    ``test_graph_extraction_properties_leak_by_convention_only_not_by_schema``
    for that honestly-documented residual gap, exercised with an input longer
    than the removed 200-char cap to show length is no longer a defense).
    Closing THAT requires the ENTITY_CLASSES contract validation NE-T2X-2's
    ``ExtractionWriter`` performs at write time (design §3.1's F1 validation
    wall: "properties through the contract"), which needs the graph's
    entity-class schema and is squarely that unit's scope, not this one's.

    Non-hashability caveat: ``frozen=True`` makes pydantic derive
    ``__eq__``/``__hash__`` from field values, but ``properties`` is a plain
    mutable ``dict`` — ``hash(assertion)`` therefore raises ``TypeError``,
    and ``assertion.properties[...] = ...`` silently mutates a model that is
    otherwise immutable. Callers needing a hashable/set-friendly assertion
    (e.g. a future dedup pass) must derive their own hashable projection
    (e.g. :func:`_assertion_content_tuple`) rather than hashing the model
    itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    kind: GraphExtractionAssertionKind
    entity_class: str = ""
    entity_key: str = ""
    display: str = ""
    properties: dict[str, PropertyValue] = {}
    subject_key: str = ""
    edge_kind: str = ""
    object_key: str = ""
    confidence: float
    evidence_ref: str

    @field_validator("row_id")
    @classmethod
    def _row_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("row_id must be a non-empty string")
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_bounded(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be in [0, 1]")
        return v

    @field_validator("evidence_ref")
    @classmethod
    def _evidence_ref_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("evidence_ref must be a non-empty string (T1 source_ref grammar)")
        return v

    @field_validator("properties")
    @classmethod
    def _properties_floats_are_finite(cls, v: dict[str, PropertyValue]) -> dict[str, PropertyValue]:
        # Codex C1: `PropertyValue`'s allow-list admits `float`, but pydantic's
        # `float` validator does not reject NaN / (+/-)Infinity, and a Python
        # float literal with an out-of-range exponent (e.g. `1e999`) silently
        # overflows to `inf` rather than raising. None of these are valid JSON
        # numbers, so a batch carrying one constructs fine here yet can fail
        # to persist into the PostgreSQL JSONB `payload` column — aborting the
        # WHOLE document's proposal under batch granularity, the same harm
        # P2-B's removed length cap caused. Rejecting them here, at
        # construction, keeps that failure a ValidationError instead of a
        # store-time surprise. See
        # ``test_graph_extraction_properties_reject_non_finite_floats``.
        for key, value in v.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(
                    f"properties[{key!r}] = {value!r} is not a finite float "
                    "(NaN/Infinity have no JSON representation)"
                )
        return v

    @model_validator(mode="after")
    def _kind_shape_is_coherent(self) -> GraphExtractionAssertion:
        # `.strip()` throughout — mirrors the field-level non-empty validators
        # above (row_id/evidence_ref) so a whitespace-only value cannot pass
        # this shape wall while still rendering an effectively-empty bullet
        # and contributing a whitespace-only component to the content address.
        if self.kind is GraphExtractionAssertionKind.LINK:
            if not self.entity_class.strip():
                raise ValueError("a 'link' assertion requires a non-empty entity_class")
            if not self.entity_key.strip():
                raise ValueError("a 'link' assertion requires a non-empty entity_key")
        elif self.kind is GraphExtractionAssertionKind.NEW_ENTITY:
            if not self.entity_class.strip():
                raise ValueError("a 'new_entity' assertion requires a non-empty entity_class")
            if not self.display.strip():
                raise ValueError("a 'new_entity' assertion requires a non-empty display")
            if not self.entity_key.strip():
                # Required so two distinct new entities of the same class
                # extracted from the same evidence do not collide on an
                # identical content-address tuple (E-P1-1, see the class
                # docstring and _assertion_content_tuple).
                raise ValueError("a 'new_entity' assertion requires a non-empty entity_key")
        elif self.kind is GraphExtractionAssertionKind.NEW_EDGE:
            if not self.subject_key.strip():
                raise ValueError("a 'new_edge' assertion requires a non-empty subject_key")
            if not self.edge_kind.strip():
                raise ValueError("a 'new_edge' assertion requires a non-empty edge_kind")
            if not self.object_key.strip():
                raise ValueError("a 'new_edge' assertion requires a non-empty object_key")
        return self


class GraphExtractionBatch(BaseModel):
    """The payload of a ``graph_extraction_candidate`` proposal (NE-T2X design §2.3).

    ONE batch is stored via ``model_dump(mode="json")`` into the proposal's JSON
    ``payload`` column. Batch granularity IS the point (design §2.4): a
    per-assertion proposal design produces an unreviewable queue on a realistic
    first backfill (~5-25k new-entity/relation assertions); collapsing every
    assertion extracted from ONE (document x run) into a SINGLE reviewable diff
    (per-row approve/edit/decline, rendered by :mod:`ne.flywheel.diffs`) cuts the
    number of human review acts by roughly an order of magnitude. There is
    deliberately no single-assertion payload model to reach for instead.

    ``run_id`` is provenance ONLY — it is deliberately EXCLUDED from the content
    address (:func:`graph_extraction_proposal_id`) so re-running the same
    extraction job over the same document does not mint a new proposal merely
    because the run id changed (E-P1-1 idempotency, see that function's
    docstring).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_ref: str
    run_id: str
    source_system: str = ""
    source_authority_stamps: tuple[SourceAuthorityStamp, ...] = ()
    assertions: tuple[GraphExtractionAssertion, ...]

    @field_validator("document_ref")
    @classmethod
    def _document_ref_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("document_ref must be a non-empty string")
        return v

    @field_validator("run_id")
    @classmethod
    def _run_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("run_id must be a non-empty string")
        return v

    @field_validator("assertions")
    @classmethod
    def _assertions_non_empty(
        cls, v: tuple[GraphExtractionAssertion, ...]
    ) -> tuple[GraphExtractionAssertion, ...]:
        if not v:
            raise ValueError(
                "a GraphExtractionBatch must carry at least one assertion "
                "(batch granularity means a batch always carries rows)"
            )
        return v

    @model_validator(mode="after")
    def _row_ids_unique(self) -> GraphExtractionBatch:
        seen: set[str] = set()
        for a in self.assertions:
            if a.row_id in seen:
                raise ValueError(f"duplicate assertion row_id: {a.row_id!r}")
            seen.add(a.row_id)
        if len(self.source_authority_stamps) != len(set(self.source_authority_stamps)):
            raise ValueError("source authority stamps must be unique")
        return self


def _assertion_content_tuple(a: GraphExtractionAssertion) -> tuple[str, ...]:
    """The CONTENT-ADDRESSED projection of one assertion (E-P1-1, design §0).

    ``display``/``properties``/``confidence``/``run_id`` are DELIBERATELY
    excluded: model phrasing (a display name reworded on re-run) and score
    variance (confidence drifting a hair between runs) must never break
    idempotency. Only the assertion's TYPED identity — kind, class, keys, edge
    triple, evidence ref — is content-addressed.
    """
    return (
        a.kind.value,
        a.entity_class,
        a.entity_key,
        a.subject_key,
        a.edge_kind,
        a.object_key,
        a.evidence_ref,
    )


def graph_extraction_row_id(assertion: GraphExtractionAssertion) -> str:
    """A deterministic, content-addressed row id for one extraction assertion.

    ``sha256("graph-extract-row:" + json(content_tuple))[:16]`` — the same
    domain-prefixed content-address discipline as :func:`graph_extraction_proposal_id`.

    Contract (advisory, NOT schema-enforced): nothing binds
    ``GraphExtractionAssertion.row_id`` to this function's output —
    ``row_id`` is a free-form caller-chosen string, and
    :class:`GraphExtractionBatch` does not validate
    ``row_id == graph_extraction_row_id(assertion)``. A producer (NE-T2X-2)
    that wants content-addressed row ids must call this helper itself when
    minting each assertion's ``row_id``, and must DEDUPLICATE by content
    tuple (:func:`_assertion_content_tuple`) BEFORE constructing the batch:
    two assertions that are byte-identical in content but were independently
    minted with distinct caller-chosen ``row_id``s both survive the batch's
    row_id-uniqueness check and render as two separate reviewable rows — they
    are NOT silently merged. Conversely, a producer that DOES use this
    helper to mint ``row_id`` and produces two content-identical assertions
    hits :class:`GraphExtractionBatch`'s duplicate-``row_id`` ``ValidationError``
    on batch construction: the batch REJECTS rather than dedups. Callers
    wanting dedup-then-continue semantics must dedup upstream of
    ``GraphExtractionBatch(...)``.
    """
    digest = hashlib.sha256(
        json.dumps(
            ["graph-extract-row", _assertion_content_tuple(assertion)], sort_keys=True
        ).encode()
    ).hexdigest()
    return digest[:16]


def graph_extraction_proposal_id(
    document_ref: str,
    assertions: Sequence[GraphExtractionAssertion],
    source_authority_stamps: Sequence[SourceAuthorityStamp] = (),
) -> tuple[str, str]:
    """A deterministic ``(proposal_id, sha16)`` content-address over a batch.

    Mirrors ``ne.brain_graph.er.job._pair_id`` exactly: ``sha256("graph-extract:"
    + document_ref + sorted(content_tuples))``, returned as
    ``(f"sha256:{digest}", digest[:16])``. The content-tuple LIST is sorted so
    assertion ORDER never changes the id — re-running the same extraction over
    the same document, in any assertion order, with the SAME entity/subject/
    object keys, yields the IDENTICAL id regardless of run_id/confidence/
    display/properties variance between runs (E-P1-1: re-runs are
    store-idempotent despite THAT kind of model-output nondeterminism).

    LOSSY HALF, stated honestly (Argus round-3 P3): excluding
    ``display``/``properties``/``confidence``/``run_id`` from the content
    address is a trade-off, not a pure benefit. A re-run that extracts
    CORRECTED property values (or an improved ``display``/``confidence``) for
    an otherwise content-identical assertion set — same kind/class/keys/edge
    triple/evidence_ref — produces the SAME ``proposal_id``. The store's
    append is idempotent on that id (:meth:`SqlAlchemyProposalStore.append`),
    so the corrected re-run's INSERT hits the unique-violation path and
    DISCARDS the corrected payload, returning the FIRST run's existing row
    id. The steward reviews the first run's stale ``display``/``properties``
    values indefinitely, until that proposal is resolved through NE-066 (at
    which point a genuinely new extraction, if any, mints a fresh proposal
    only if some content-addressed field also changed). This is the
    deliberate price of E-P1-1 re-run idempotency (design §0): the field list
    is chosen so run_id/score/wording noise never mints spurious duplicate
    proposals, and that same exclusion is what lets a corrected value slip
    through unnoticed on an otherwise-identical re-run.

    CAVEAT, stated honestly (design §3.3): idempotency holds only up to the
    STABILITY of ``entity_key``, which this schema requires on ``NEW_ENTITY``
    assertions but does not itself derive. Per design §3.3 the intended
    producer computes ``entity_key = entity_key_for("ne:llm", entity_class,
    "extracted", canonical_form(display))`` — i.e. ``entity_key`` is ITSELF
    display-derived. A display re-wording that survives casefold/whitespace/
    punctuation normalization into a DIFFERENT canonical form (e.g. "Globex
    Corp" -> "Globex Corporation") therefore changes ``entity_key`` — which
    IS part of the content address — and DOES mint a new ``proposal_id`` on
    re-run. That is design §3.3's deliberate, accepted consequence
    ("surface-form variance... fragments into two nodes... healed by the
    existing Splink ER lane"), not a defect in this function: this helper is
    idempotent against WITHIN-canonical-form model-output noise (score
    drift, a restated-but-equivalent display, a new run id) — it is not, and
    cannot be, idempotent against a genuine re-wording that a §3.3-compliant
    producer resolves to a different key. Changing any content-address field
    (evidence_ref, entity_key, ...) yields a different id, by design — INCLUDING
    when that field changed only because of upstream display-derivation.

    The sha16 half is for the caller's ``cluster_id`` (NE-T2X-2's choice of
    pattern; this unit only supplies the helper).
    """
    tuples = sorted(_assertion_content_tuple(a) for a in assertions)
    stamp_tuples = sorted(
        json.dumps(stamp.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        for stamp in source_authority_stamps
    )
    digest = hashlib.sha256(
        json.dumps(["graph-extract", document_ref, stamp_tuples, tuples], sort_keys=True).encode()
    ).hexdigest()
    return f"sha256:{digest}", digest[:16]


class FixProposal(BaseModel):
    """A typed, config_hash-stamped PROPOSAL — never an applied change.

    ``status`` is validated to ``"proposed"`` (this unit only proposes).
    ``proposal_id`` is deterministic/content-addressed so a re-run over the same
    cluster is idempotent.
    """

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    kind: ProposalKind
    lane: FixLane
    cluster_id: str
    evidence_event_ids: tuple[str, ...]
    evidence_signal_ids: tuple[int, ...]
    summary: str
    confidence: float
    priority: ProposalPriority
    status: str = PROPOSED_STATUS
    config_hash: str
    created_at: datetime
    payload: dict[str, Any] = {}

    @field_validator("summary")
    @classmethod
    def _summary_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("summary must be a non-empty string")
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_bounded(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be in [0, 1]")
        return v

    @field_validator("status")
    @classmethod
    def _status_is_proposed(cls, v: str) -> str:
        if v != PROPOSED_STATUS:
            raise ValueError(
                f"status must be {PROPOSED_STATUS!r}; this unit only proposes "
                "(NE-066 owns status transitions and the actual application)"
            )
        return v

    @field_validator("created_at")
    @classmethod
    def _created_at_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return v


# Its OWN MetaData — independent of the correction-signal store's flywheel_metadata
# and every other package's, so ``ensure_proposal_schema`` stays independent.
proposal_metadata = sa.MetaData()

ne_fix_proposals = sa.Table(
    "ne_fix_proposals",
    proposal_metadata,
    # BigInteger on Postgres (Identity); Integer on SQLite so it aliases the rowid
    # and autoincrements (SQLite only autoincrements INTEGER PRIMARY KEY).
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    ),
    sa.Column("proposal_id", sa.String, nullable=False, unique=True),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("lane", sa.String, nullable=False),
    sa.Column("cluster_id", sa.String, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("config_hash", sa.String, nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("priority", sa.String, nullable=False),
    sa.Column(
        "evidence",
        sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
    ),
    sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_ne_fix_proposals_lane_created", "lane", "created_at"),
    sa.Index("ix_ne_fix_proposals_kind_created", "kind", "created_at"),
)


def ensure_proposal_schema(engine: sa.Engine) -> None:
    """Create the fix-proposal table if absent. Idempotent (``create_all``)."""
    proposal_metadata.create_all(engine)


def _as_utc(dt: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC (SQLite reads back naive)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class StoredFixProposal(BaseModel):
    """A frozen, fully-materialized fix proposal as stored + read back."""

    model_config = ConfigDict(frozen=True)

    id: int
    proposal_id: str
    kind: str
    lane: str
    cluster_id: str
    status: str
    config_hash: str
    summary: str
    confidence: float
    priority: str
    evidence_event_ids: tuple[str, ...]
    evidence_signal_ids: tuple[int, ...]
    payload: dict[str, Any]
    created_at: datetime


def _row_to_stored(row: sa.Row[Any]) -> StoredFixProposal:
    evidence = dict(row.evidence)
    return StoredFixProposal(
        id=int(row.id),
        proposal_id=str(row.proposal_id),
        kind=str(row.kind),
        lane=str(row.lane),
        cluster_id=str(row.cluster_id),
        status=str(row.status),
        config_hash=str(row.config_hash),
        summary=str(row.summary),
        confidence=float(row.confidence),
        priority=str(row.priority),
        evidence_event_ids=tuple(str(e) for e in evidence.get("event_ids", [])),
        evidence_signal_ids=tuple(int(s) for s in evidence.get("signal_ids", [])),
        payload=dict(row.payload),
        created_at=_as_utc(row.created_at),
    )


class SqlAlchemyProposalStore:
    """Append-only fix-proposal store over sync SQLAlchemy-Core.

    The COMPLETE public mutation surface is :meth:`append` — one idempotent
    INSERT. No update / delete / edit method exists anywhere in this class: a
    proposal is a durable suggestion; NE-066 records its lifecycle elsewhere.
    """

    def __init__(self, engine: sa.Engine) -> None:
        ensure_transactional_sqlite(engine)
        self._engine = engine

    @property
    def engine(self) -> sa.Engine:
        return self._engine

    def append(self, conn: sa.Connection, proposal: FixProposal) -> int:
        """Append ONE proposal on the CALLER's connection; return its id.

        Runs on ``conn`` (not its own engine) so the curation engine composes
        every proposal INSERT atomically with the cursor upsert and the
        dual-appended audit event in ONE transaction.

        Idempotent on a duplicate ``proposal_id``: the INSERT runs in a SAVEPOINT
        (mirroring the correction-signal store ``append`` verbatim); a
        unique-violation rolls back just the SAVEPOINT and the EXISTING row's id
        is returned, leaving the caller's enclosing transaction usable — so a
        re-run over the same clusters appends nothing new.
        """
        stmt = ne_fix_proposals.insert().values(
            proposal_id=proposal.proposal_id,
            kind=str(proposal.kind),
            lane=str(proposal.lane),
            cluster_id=proposal.cluster_id,
            status=proposal.status,
            config_hash=proposal.config_hash,
            summary=proposal.summary,
            confidence=proposal.confidence,
            priority=str(proposal.priority),
            evidence={
                "event_ids": list(proposal.evidence_event_ids),
                "signal_ids": list(proposal.evidence_signal_ids),
            },
            payload=proposal.payload,
            created_at=_as_utc(proposal.created_at),
        )
        try:
            with conn.begin_nested():
                result = conn.execute(stmt)
            pk = result.inserted_primary_key
            if pk is None:  # pragma: no cover - a single-row insert always yields a pk
                raise RuntimeError("fix-proposal insert returned no primary key")
            return int(pk[0])
        except IntegrityError:
            existing = conn.execute(
                sa.select(ne_fix_proposals.c.id).where(
                    ne_fix_proposals.c.proposal_id == proposal.proposal_id
                )
            ).scalar_one()
            return int(existing)

    def read_proposals(
        self,
        *,
        after_id: int = 0,
        limit: int = 100,
        lane: str | None = None,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[StoredFixProposal]:
        """Cursor-ordered read: ``id > after_id``, id ASC, capped.

        Filters (all optional, ANDed): ``lane``, ``kind``, ``status`` (exact).
        """
        stmt = sa.select(ne_fix_proposals).where(ne_fix_proposals.c.id > after_id)
        if lane is not None:
            stmt = stmt.where(ne_fix_proposals.c.lane == lane)
        if kind is not None:
            stmt = stmt.where(ne_fix_proposals.c.kind == kind)
        if status is not None:
            stmt = stmt.where(ne_fix_proposals.c.status == status)
        stmt = stmt.order_by(ne_fix_proposals.c.id.asc()).limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_row_to_stored(row) for row in rows]

    def read_proposal(self, proposal_id: str) -> StoredFixProposal | None:
        """Direct lookup by the unique ``proposal_id`` (indexed), or ``None``.

        A single-row query on the UNIQUE ``proposal_id`` column — never a paged
        scan. The apply-skill seam needs an exact fetch by id; scanning a capped
        page of the shared, all-lane ``ne_fix_proposals`` table would silently
        miss any proposal beyond that page once the table grows (Codex P2,
        NE-SKILL-1b review).
        """
        stmt = sa.select(ne_fix_proposals).where(ne_fix_proposals.c.proposal_id == proposal_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).one_or_none()
        return _row_to_stored(row) if row is not None else None

    def count_by_lane(self) -> dict[str, int]:
        """Convenience: a ``{lane: count}`` map over the whole proposal store."""
        stmt = sa.select(ne_fix_proposals.c.lane, sa.func.count()).group_by(ne_fix_proposals.c.lane)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return {str(lane): int(count) for lane, count in rows}


__all__ = [
    "PROPOSED_STATUS",
    "FixProposal",
    "GoldenSetCandidate",
    "GraphExtractionAssertion",
    "GraphExtractionAssertionKind",
    "GraphExtractionBatch",
    "ProposalKind",
    "SqlAlchemyProposalStore",
    "StoredFixProposal",
    "ensure_proposal_schema",
    "graph_extraction_proposal_id",
    "graph_extraction_row_id",
    "ne_fix_proposals",
    "proposal_metadata",
]
