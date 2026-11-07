# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Wire models — the request/response schemas mirroring the frozen console contract.

REQUEST models are ``extra='forbid'`` (spec §4-4): a smuggled identity field
(``approver``/``actor``/``department``/``filters``) is a LOUD 422, not a silently
ignored key. RESPONSE models are constructed FIELD-BY-FIELD from library value
objects, never ``model_dump``-ed — so :class:`ChunkView` has EXACTLY four fields
and STRUCTURALLY cannot serialize a chunk's ``acl`` (C7, proven by the recursive
no-acl-key assertion in ``test_no_bypass``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ne.api.steward_metrics import LatencySummary, WeeklyDigest
from ne.api.verified_curation import AppliedVerifiedTemplate
from ne.audit import AnchorResolution, AuditRecord, ChainVerification
from ne.connect.source_url import canonical_external_source_url
from ne.contracts.frozen import _thaw
from ne.contracts.types import GraphPath, RetrievalResult
from ne.eval.store import EvalRunRecord
from ne.flywheel.diffs import DiffArtifact
from ne.flywheel.proposals import StoredFixProposal
from ne.flywheel.review import RejectionCode, StoredReviewEvent
from ne.runtime.approvals import ApprovalPayloadDisclosure, PendingApproval


def _to_utc(value: datetime) -> datetime:
    """Normalize a possibly-naive datetime to tz-aware UTC (SQLite reads naive)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _non_empty(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty or whitespace")
    return value


# --- request models (extra=forbid) ----------------------------------------

MAX_PRINCIPAL_REFERENCES = 500
MAX_PRINCIPAL_REFERENCE_LENGTH = 255


class ResolvePrincipalReferencesRequest(BaseModel):
    """Name principals attached to already-visible server records.

    The client supplies opaque approval/audit record references, never principal
    ids.  The route re-derives actors from authorized records before consulting
    the directory, closing the arbitrary-id oracle reverted in PR #401.
    """

    model_config = ConfigDict(extra="forbid")

    scope: Literal["approvals", "audit"]
    references: list[str] = Field(min_length=1, max_length=MAX_PRINCIPAL_REFERENCES)

    @field_validator("references")
    @classmethod
    def _validate_references(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or len(value) > MAX_PRINCIPAL_REFERENCE_LENGTH:
                raise ValueError(
                    f"references must be 1..{MAX_PRINCIPAL_REFERENCE_LENGTH} characters"
                )
        return values


class CreateSessionRequest(BaseModel):
    """POST /v1/chat/sessions body — the console sends only ``agent_name``."""

    model_config = ConfigDict(extra="forbid")

    agent_name: str


class ChatMessageRequest(BaseModel):
    """POST /v1/chat/sessions/{id}/messages body — prompt + optional model alias."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    requested_model: str | None = None

    @field_validator("prompt", "requested_model")
    @classmethod
    def _validate_non_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _non_empty(v)


class AgenticRunRequest(BaseModel):
    """Agentic run/start body; deterministic workflows deliberately do not use it."""

    model_config = ConfigDict(extra="forbid")

    requested_model: str | None = None

    @field_validator("requested_model")
    @classmethod
    def _validate_requested_model(cls, v: str | None) -> str | None:
        return None if v is None else _non_empty(v)


class ThumbsSignalRequest(BaseModel):
    """POST /v1/chat/sessions/{id}/signals/thumbs body (NE-137 flywheel intake).

    ``extra='forbid'``: a smuggled ``surface``/``actor``/``session_id``/``principal``
    is a LOUD 422 — provenance is ALWAYS server-built from the verified bearer + the
    R3-verified chat session. Only the payload fields ride the wire.
    """

    model_config = ConfigDict(extra="forbid")

    up: bool
    target_ref: str
    comment: str | None = None
    message_id: str | None = None

    @field_validator("target_ref")
    @classmethod
    def _validate_target_ref(cls, v: str) -> str:
        return _non_empty(v)


class WrongSourceSignalRequest(BaseModel):
    """POST /v1/chat/sessions/{id}/signals/wrong-source body (NE-137 flywheel intake).

    ``extra='forbid'`` (identity is never on the wire); provenance is server-built.
    """

    model_config = ConfigDict(extra="forbid")

    answer_ref: str
    citation_ref: str
    comment: str | None = None
    message_id: str | None = None

    @field_validator("answer_ref", "citation_ref")
    @classmethod
    def _validate_refs(cls, v: str) -> str:
        return _non_empty(v)


class SignalReceiptView(BaseModel):
    """POST /v1/chat/sessions/{id}/signals/* response — the built CorrectionSignal id."""

    event_id: str


class BrainSearchRequest(BaseModel):
    """POST /v1/brain/search body — no user ``filters`` key is ever accepted."""

    model_config = ConfigDict(extra="forbid")

    query: str
    top_k: int = Field(default=10, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def _validate_query(cls, v: str) -> str:
        return _non_empty(v)


class BrainRetrieveRequest(BaseModel):
    """POST /v1/brain/retrieve body — a bounded list of chunk ids."""

    model_config = ConfigDict(extra="forbid")

    chunk_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("chunk_ids")
    @classmethod
    def _validate_chunk_ids(cls, v: list[str]) -> list[str]:
        for chunk_id in v:
            if not chunk_id.strip():
                raise ValueError("chunk_ids entries must not be empty")
        return v


class ApproveRequest(BaseModel):
    """POST /v1/approvals/{id}/approve body — EMPTY (the SECURITY CRUX, spec §4-4).

    Zero fields + ``extra='forbid'`` means a smuggled ``{"approver": "mallory"}``
    is a LOUD 422 — the approver is ALWAYS ``principal.id`` from the verified
    bearer, never anything on the wire.
    """

    model_config = ConfigDict(extra="forbid")


class RejectRequest(BaseModel):
    """POST /v1/approvals/{id}/reject body — only a free-text ``reason`` (may be "")."""

    model_config = ConfigDict(extra="forbid")

    reason: str = ""


# --- steward request models (extra=forbid; identity is NEVER on the wire) ---


class StewardApproveRequest(BaseModel):
    """POST /v1/steward/proposals/{id}/approve body — only a ``note`` (blank ⇒ None).

    A smuggled ``actor``/``surface``/``session_id``/``principal`` key is a LOUD 422
    (``extra='forbid'``); the actor + provenance are ALWAYS server-derived from the
    verified bearer.
    """

    model_config = ConfigDict(extra="forbid")

    note: str = ""


class StewardRejectRequest(BaseModel):
    """POST /v1/steward/proposals/{id}/reject body — a typed ``code`` + optional ``note``.

    ``code`` is the ``ne.flywheel.review.RejectionCode`` enum, so an unknown code is
    a LOUD 422 through the string-detail flatten handler (wire values match
    ``RejectionCodeWire`` verbatim).
    """

    model_config = ConfigDict(extra="forbid")

    code: RejectionCode
    note: str = ""


class StewardSkillRotRequest(BaseModel):
    """POST /v1/steward/skills/{name}/retire|restore body — a required ``reason``.

    Retire/restore are HUMAN steward transitions (never an agent actor). The
    identity is server-derived; the body carries ONLY the reason (``extra='forbid'``
    makes a smuggled actor/name a LOUD 422). Retire loudly — the reason is
    persisted on the lifecycle event."""

    model_config = ConfigDict(extra="forbid")

    reason: str

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, v: str) -> str:
        return _non_empty(v)


class StewardApplyRequest(BaseModel):
    """POST /v1/steward/proposals/{id}/apply body — a non-empty ``applied_ref`` + note.

    ne-api RECORDS a human-performed apply; it NEVER executes one. An empty or
    whitespace ``applied_ref`` is a 422 before the service is ever called.
    """

    model_config = ConfigDict(extra="forbid")

    applied_ref: str
    note: str = ""

    @field_validator("applied_ref")
    @classmethod
    def _validate_applied_ref(cls, v: str) -> str:
        return _non_empty(v)


class SkillEditFieldsRequest(BaseModel):
    """Steward edit-before-approve fields (NE-SKILL-3) — NEVER ``body`` in v1.

    Mirrors ``ne.skills.apply.SkillEdits``. A body edit is reject-and-re-forge,
    not an in-place edit: ``extra='forbid'`` makes a smuggled ``body`` (or any
    other) key a LOUD 422 at the wire, so an attacker-influenced body can never be
    slipped past the birth gate. Any supplied field re-runs the routing eval
    server-side BEFORE the commit, fail-closed.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    triggers: list[str] | None = None


class StewardApplySkillRequest(BaseModel):
    """POST /v1/steward/proposals/{id}/apply-skill body (NE-SKILL-1b / NE-SKILL-3).

    The steward's ACL-label decision is the ASSIGNMENT MOMENT (R-22): supply
    EITHER a non-empty ``acl_labels`` set OR ``global_visibility=true`` — never
    both, never neither (blank labels must not publish a forged skill fleet-wide
    by accident). Identity is NEVER on the wire (``extra='forbid'``); the
    ``applied_ref`` is server-derived (``skill:<name>@v<n>``).

    ``edits`` (NE-SKILL-3) carries optional steward corrections to
    ``name``/``description``/``triggers`` (never ``body``). Any edit re-runs the
    routing eval server-side before the one-transaction commit, fail-closed — a
    routing-breaking edit is refused (409) and nothing commits.
    """

    model_config = ConfigDict(extra="forbid")

    acl_labels: list[str] = []
    global_visibility: bool = False
    note: str = ""
    edits: SkillEditFieldsRequest | None = None


class StewardProposeVerifiedTemplateRequest(BaseModel):
    """POST /v1/steward/proposals/verified-template body (NE-ONTOLOGY-FLYWHEEL).

    A steward authors a NEW/MODIFIED verified-answer template TOML + a short
    summary. Identity is NEVER on the wire (``extra='forbid'``); the template is
    validated by the SAME ``QueryTemplate`` load gate at propose-time (fail-closed
    ⇒ 422, no proposal row). ``template_toml`` is bounded (64 KiB) so an oversized
    body is a LOUD 422 before any parse.
    """

    model_config = ConfigDict(extra="forbid")

    template_toml: str = Field(min_length=1, max_length=65536)
    summary: str = Field(min_length=1, max_length=500)

    @field_validator("template_toml", "summary")
    @classmethod
    def _validate_non_empty(cls, v: str) -> str:
        return _non_empty(v)


class StewardApplyVerifiedTemplateRequest(BaseModel):
    """POST /v1/steward/proposals/{id}/apply-verified-template body.

    Only an optional ``note`` (recorded on the NE-066 review row). The
    ``applied_ref`` is SERVER-derived (``verified-template:<id>@v<n>:<hash16>``) —
    never client-supplied — so the console omits the applied_ref input for this
    kind. ``extra='forbid'`` rejects a smuggled identity/applied_ref key (422).
    """

    model_config = ConfigDict(extra="forbid")

    note: str = ""


# --- response models (constructed field-by-field) -------------------------


class PrincipalView(BaseModel):
    """GET /v1/me — id + groups + the server's OWN entitlement answer.

    ``claims`` are still NEVER echoed. ``entitlements`` is a list of coarse
    surface names this principal's groups satisfy, computed from the SAME
    ``ApiSettings`` role tuples the route guards use (``approver_roles`` /
    ``steward_roles`` / ``ops_roles``, empty ⇒ nobody, fail closed).

    WHY IT EXISTS. The console had no way to know, before issuing a request,
    that the server would refuse it — so it polled ``/v1/approvals/pending``,
    ``/v1/steward/proposals`` and ``/v1/audit/events`` on every route change for
    every principal, and every refusal was audited by design. A reader's home
    screen was three 403 receipts deep, ``/audit`` was ~100 rows of the console's
    own denials, and the KPI window (which counts audited events) climbed while a
    buyer merely browsed. This field lets the console SKIP a call it is already
    told will be refused, and mark the routes it cannot open.

    It grants nothing and decides nothing: every route still runs its own
    fail-closed ``require_role`` check against the verified bearer. This is the
    server describing itself, not the client authorizing itself.
    """

    id: str
    groups: list[str]
    entitlements: list[str] = []


class ResolvedPrincipalView(BaseModel):
    """A directory-backed human label for a record-derived principal id."""

    id: str
    display_name: str
    removed: bool


class ResolvedPrincipalsView(BaseModel):
    """Reference-bound principal resolution response.

    Only identities derived from the submitted visible record references can
    appear. Unknown references and principals without a non-blank mirrored name
    simply produce no row; the console retains the identifier fallback.
    """

    resolved: list[ResolvedPrincipalView]


class ChatSessionView(BaseModel):
    """POST /v1/chat/sessions response."""

    session_id: str
    agent_name: str


class ChatSessionMetadataView(BaseModel):
    """Content-free metadata for an owned Ask conversation."""

    session_id: str
    agent_name: str
    department: str
    created_at: datetime
    updated_at: datetime
    last_sequence: int


class ChatSessionListView(BaseModel):
    """Owner-scoped list; no message, title, model context, or signal bytes."""

    sessions: list[ChatSessionMetadataView]


class ChatGroundingModeView(BaseModel):
    """Opaque server mapping for the optional grounded chat session surface."""

    model_config = ConfigDict(extra="forbid")

    agent_name: str


class ChatCapabilitiesView(BaseModel):
    """Deployment-served optional chat modes; absence is an explicit null."""

    model_config = ConfigDict(extra="forbid")

    grounded: ChatGroundingModeView | None


class ContextComponentView(BaseModel):
    """One content-free persisted turn component in the active context object."""

    seq: int
    component_id: str
    created_at: datetime


class ContextBudgetObservationView(BaseModel):
    """The runtime ceiling plus the newest authoritative provider usage fact.

    ``input_tokens``/``observed_at`` are a pair: both are absent when the latest
    session trace contains no successful model call or the provider omitted token
    usage.  Absence is never serialized as zero or a browser-derived timestamp.
    """

    input_budget: int | None
    input_tokens: int | None
    observed_at: datetime | None


class ContextModelFlipView(BaseModel):
    """A post-session route flip bound to the context object at its chain event."""

    old_model: str
    new_model: str
    audit_seq: int
    changed_at: datetime
    context_id_at_flip: str


class ContextLedgerView(BaseModel):
    """GET /v1/chat/sessions/{id}/context-ledger — hashes/counts only, no text."""

    session_id: str
    context_id: str
    resides: Literal["Postgres", "SQLite"]
    components: list[ContextComponentView]
    total_components: int
    active_components: int
    truncated: bool
    budget: ContextBudgetObservationView
    current_model: str | None
    served_model: str | None
    flip: ContextModelFlipView | None


class AgentContextComponentView(BaseModel):
    """One canonical, content-free Agent Conversation context component."""

    turn_sequence: int
    run_id: str
    turn_id: str
    input_target_id: str
    terminal_hash: str
    component_id: str


class AgentContextLedgerView(BaseModel):
    """Canonical Agent Conversation context without model or payload detail."""

    conversation_id: str
    agent_id: str
    context_id: str
    resides: Literal["Postgres", "SQLite"]
    components: list[AgentContextComponentView]
    total_components: int
    active_components: int
    truncated: bool


class ChunkView(BaseModel):
    """A retrieved chunk on the wire — EXACTLY four fields, NO ``acl`` (C7)."""

    chunk_id: str
    document_id: str
    text: str
    metadata: dict[str, Any]


class ScoredChunkView(BaseModel):
    """A chunk with its relevance score."""

    chunk: ChunkView
    score: float


class CitationView(BaseModel):
    """A source-of-record pointer with optional server-owned click-through.

    ``source_url`` is enriched only from the exact ACL-trimmed chunk that
    produced this citation.  It is absent when the persisted connector locator
    cannot be revalidated; the browser never constructs or guesses one.
    """

    chunk_id: str
    document_id: str
    source_system: str
    source_ref: str
    source_url: str | None = Field(default=None, exclude_if=lambda value: value is None)


def _persisted_source_url(value: object) -> str | None:
    """Revalidate one already-authorized connector locator for wire use.

    The connector mapper's deployment settings are the authority decision; they
    are not available at this read boundary and MUST NOT be reconstructed from
    the URL.  This second pass only proves that the persisted value still has
    the canonical external-HTTPS shape produced by ``ne.connect.source_url``.
    Exact connector/citation identity is checked separately below.
    """
    if not isinstance(value, str):
        return None
    try:
        host = urlsplit(value).hostname
    except ValueError:
        return None
    if not host:
        return None
    canonical = canonical_external_source_url(value, allowed_hosts=(host,))
    return value if canonical == value else None


def citation_views(result: RetrievalResult) -> list[CitationView]:
    """Project citations plus optional locators from the same trimmed result.

    A locator survives only when a served chunk matches the citation's complete
    four-field identity and carries the connector's own matching identity stamp.
    Conflicting locators fail closed to an absent link.  This is shared by the
    brain HTTP response and chat capture so inline and durable transports cannot
    disagree about which external record a citation opens.
    """
    views: list[CitationView] = []
    for citation in result.citations:
        locators: set[str] = set()
        for scored in result.chunks:
            chunk = scored.chunk
            metadata = chunk.metadata
            if (
                chunk.chunk_id != citation.chunk_id
                or chunk.document_id != citation.document_id
                or metadata.get("connector") != citation.source_system
                or metadata.get("source_system") != citation.source_system
                or metadata.get("source_ref") != citation.source_ref
            ):
                continue
            locator = _persisted_source_url(metadata.get("source_url"))
            if locator is not None:
                locators.add(locator)
        views.append(
            CitationView(
                chunk_id=citation.chunk_id,
                document_id=citation.document_id,
                source_system=citation.source_system,
                source_ref=citation.source_ref,
                source_url=next(iter(locators)) if len(locators) == 1 else None,
            )
        )
    return views


def citation_payloads(result: RetrievalResult) -> list[dict[str, str]]:
    """Return the exact optional-field citation payload used by SSE/workflows."""
    return [view.model_dump(mode="json") for view in citation_views(result)]


class GraphEntityRefView(BaseModel):
    """A Tier-2 graph entity pointer on the wire (ADR-0005 ``GraphEntityRef``).

    Constructed field-by-field like :class:`ChunkView`; ``GraphEntityRef`` carries
    no ``acl`` field, so this view is structurally acl-free. Trimmed-by-construction
    (only ever built from ``EntityGraph`` output, F3/C7).

    ``assurance`` (NE-T2X-4, design §5.2 correction 2) is REQUIRED, with NO
    default — deliberately, the anti-silent-drop stance. ``GraphEntityRef`` in
    ne-contracts maps field-by-field here (never ``model_dump``), so the
    ``grounded.strictness`` automatic-passthrough precedent does NOT apply to
    this wire boundary; a construction site that forgets to pass ``assurance``
    must fail LOUD (a ``pydantic.ValidationError``, caught by CI) rather than
    silently defaulting to the trusted-sounding ``"source"`` value and shipping
    a console that cannot distinguish an extracted fact from a backbone one
    (design §6.1's read-time-flagging contract depends on this field arriving
    honestly on every hop/ref).

    ``source_ref`` (NE-T2X-5-ENDPOINT-EVIDENCE-REF, ADR-0015) is likewise
    REQUIRED, with NO default — the SAME anti-silent-drop stance as
    ``assurance``, for the same reason. Unlike the contracts-side
    ``GraphEntityRef.source_ref`` (defaulted ``""`` for pre-1.12 payload
    compatibility), this view has no legacy payloads to stay compatible
    with: it is rebuilt field-by-field on every response, so a construction
    site that forgets it must fail LOUD (a ``pydantic.ValidationError``,
    caught by CI) rather than silently defaulting to an empty ref — an
    empty-but-valid ``source_ref`` would recreate exactly the
    NE-T2X-5-ENDPOINT-EVIDENCE-REF defect: the console falling back to
    displaying a DIFFERENT element's (the edge's) provenance for an
    extracted endpoint.
    """

    entity_class: str
    entity_key: str
    display: str
    assurance: str
    source_ref: str


class GraphHopView(BaseModel):
    """One traversed relation on the wire (ADR-0005 ``GraphHop``).

    ``source_system``/``source_ref`` carry the ASSERTING record's provenance —
    the same click-through semantics as :class:`CitationView`. Structurally
    acl-free (``GraphHop`` has no acl field).

    ``assurance`` (NE-T2X-4) labels the ASSERTING relation itself, independent
    of ``subject.assurance``/``obj.assurance`` — see :class:`GraphEntityRefView`
    for the no-default anti-silent-drop rationale, which applies identically
    here.
    """

    subject: GraphEntityRefView
    edge_kind: str
    obj: GraphEntityRefView
    source_system: str
    source_ref: str
    assurance: str


class GraphPathView(BaseModel):
    """An ordered, trimmed-by-construction traversal path (ADR-0005 ``GraphPath``)."""

    hops: list[GraphHopView]


class RetrievalResultView(BaseModel):
    """The brain search/retrieve response envelope.

    ``graph_paths`` is surfaced DELIBERATELY (NE-BRAIN-T2-WIRING F3 — it was
    previously silently dropped as a fail-safe while Tier-2 was unexposed). No
    default: the mapper constructs it explicitly (``[]`` when the tier is off).

    ``provisional_source_count`` (NE-T2X-4, design §6.1) is REQUIRED, no
    default — mirroring ``graph_paths``' no-default precedent. It is DERIVED
    at this wire boundary (design decision (a): no ``RetrievalResult``
    contracts change — see :func:`_provisional_source_count` for the pinned
    definition) rather than added to the library ``RetrievalResult`` itself,
    because every input the count needs (per-hop/per-ref ``assurance``) is
    already present on ``graph_paths`` — a derivation, not a new fact.
    """

    chunks: list[ScoredChunkView]
    citations: list[CitationView]
    graph_paths: list[GraphPathView]
    provisional_source_count: int


#: Assurance labels that do NOT count toward ``provisional_source_count`` — the
#: floor values a human has vouched for (design §2.1/§5.2): the structured
#: backbone (``"source"``) and an extracted element a steward has since
#: approved (``"confirmed"``). Every OTHER label — including ``"extracted"``
#: and any unrecognized/misspelled string — counts as provisional: the
#: pinned stance is to fail TOWARD flagging, never toward trust (an unknown
#: label is exactly the shape a tampered or stale payload would take).
_VOUCHED_ASSURANCE = frozenset({"source", "confirmed"})


@dataclass(frozen=True)
class ProvisionalSummary:
    """The ONE traversal's output: the pinned count PLUS the provisional hops'
    writer-stamped evidence refs (NE-T2X-5-RECORDER-WIRE).

    ``provisional_source_refs`` is a NARROWER signal than
    ``provisional_source_count``: only provisional HOPS contribute to
    ``refs`` — a provisional ENTITY (subject/object position) contributes to
    the count but never to ``refs``, DELIBERATELY, as of this unit.

    This is NOT because the entity lacks a ref anymore: since
    NE-T2X-5-ENDPOINT-EVIDENCE-REF (ADR-0015), every ``GraphEntityRefView``
    carries its OWN wire-level ``source_ref`` (threaded from
    ``ne-brain-graph``'s ``EntityNode.source_ref``), so the entity-has-no-ref
    gap this docstring used to describe is CLOSED at the ``hop.subject``/
    ``hop.obj`` level — that is what lets the console show an extracted
    ENDPOINT's own evidence instead of the asserting edge's. What stays
    UNCHANGED in this unit is narrower: folding entity refs into THIS
    function's ``refs`` list would widen the shape of the §6.3 PDP/approval
    payload that ``ne.runtime.provisional`` consumes from it — a decision
    for that consumer's own lane, not a side effect of a wire-model fix. So
    ``refs`` here stays hop-only by explicit choice, not because the datum
    is unavailable. Refs are ORDERED, first-occurrence-deduplicated (same
    identity as the hop dedup below: a repeated
    ``(subject, edge_kind, obj, source_ref)`` contributes its ref once).

    **The dedup key is the HOP, not the ref string — so ``refs`` MAY REPEAT a
    ref across DISTINCT hops** (Argus P3). That is common in practice: the
    writer stamps ``llm:<sha16>:<chunk-ref>`` per source CHUNK, and one chunk
    typically evidences several edges. Stated explicitly because "ORDERED,
    first-occurrence-deduplicated" reads as string-dedup, and it is not.
    Harmless downstream today — ``ne.runtime.provisional``'s accumulator
    dedupes by ref STRING before the PDP payload is built — but that is a
    property of the CONSUMER, not of this function, so it must not be relied
    on here. Pinned by
    ``test_provisional_summary_repeats_a_ref_across_distinct_hops``.
    """

    provisional_source_count: int
    provisional_source_refs: tuple[str, ...]


def provisional_summary(paths: Sequence[GraphPathView]) -> ProvisionalSummary:
    """The ONE authoritative traversal over ``graph_paths`` — count AND refs.

    **Pinned count definition (design §6.1, NE-T2X-4 — resolves the design's
    undefined 'provisional_source_count' string with a concrete, tested
    count; unchanged by the NE-T2X-5-RECORDER-WIRE refactor that moved this
    traversal here from :func:`_provisional_source_count`):**

    - An entity ref (subject or object position, in any path) is provisional
      iff its ``assurance`` is NOT in :data:`_VOUCHED_ASSURANCE`. Identity for
      dedup is ``entity_key`` — the SAME entity appearing as the subject of one
      hop and the object of another, or repeated across paths, counts ONCE.
    - A hop (the asserting edge itself) is provisional iff its OWN
      ``assurance`` is NOT in :data:`_VOUCHED_ASSURANCE` — independent of its
      subject's/object's assurance (an extracted edge may connect two backbone
      entities, or vice versa). Identity for dedup is
      ``(subject.entity_key, edge_kind, obj.entity_key, source_ref)`` — the
      SAME asserted relation repeated across paths counts ONCE.
    - Chunks and citations NEVER contribute (Tier-1 carries no ``assurance``
      field at all).
    - The count is ``len(provisional_entity_keys) + len(provisional_hop_ids)``
      — refs and hops are counted as separate elements (an extracted hop
      between two backbone entities is one provisional element; an extracted
      entity referenced by only backbone hops is a different one).

    **Refs (NE-T2X-5-RECORDER-WIRE, additive to the pinned count above):** for
    every hop counted as provisional (the SAME predicate as the count's hop
    leg), its ``source_ref`` — the writer-stamped ``llm:<sha16>:<ref>``
    identifier (C12: opaque, never fact text/displays) — is appended to the
    ordered ref list on FIRST occurrence of that hop's dedup identity. A
    provisional ENTITY contributes to the count but has NO ref in THIS list
    — see :class:`ProvisionalSummary`'s docstring for why (the datum exists
    on ``hop.subject.source_ref``/``hop.obj.source_ref`` as of
    NE-T2X-5-ENDPOINT-EVIDENCE-REF; it is a deliberate lane boundary that
    keeps it out of this particular list, not an availability gap).

    This is the ONE shared traversal both :func:`_provisional_source_count`
    (the wire response) and the ne-api recorder binding
    (``ne.api.tools.build_chat_bindings``) consume — never re-derive a rival
    count/ref list over ``graph_paths`` anywhere else (the derive-don't-
    transliterate discipline; NE-BEW-GROUPS-ZEROGROUPS is the cautionary
    precedent for a second implementation disagreeing with the first).
    """
    entity_keys: set[str] = set()
    hop_ids: set[tuple[str, str, str, str]] = set()
    refs: list[str] = []
    seen_refs: set[tuple[str, str, str, str]] = set()
    for path in paths:
        for hop in path.hops:
            if hop.subject.assurance not in _VOUCHED_ASSURANCE:
                entity_keys.add(hop.subject.entity_key)
            if hop.obj.assurance not in _VOUCHED_ASSURANCE:
                entity_keys.add(hop.obj.entity_key)
            if hop.assurance not in _VOUCHED_ASSURANCE:
                hop_id = (hop.subject.entity_key, hop.edge_kind, hop.obj.entity_key, hop.source_ref)
                hop_ids.add(hop_id)
                if hop_id not in seen_refs:
                    seen_refs.add(hop_id)
                    refs.append(hop.source_ref)
    return ProvisionalSummary(
        provisional_source_count=len(entity_keys) + len(hop_ids),
        provisional_source_refs=tuple(refs),
    )


def _provisional_source_count(paths: list[GraphPathView]) -> int:
    """The number of DISTINCT provisional graph elements across ``graph_paths``.

    A one-line delegate to :func:`provisional_summary` — the SHARED traversal
    (NE-T2X-5-RECORDER-WIRE). Kept as its own function (rather than inlining
    the call at every use site) so existing callers/tests need no change; see
    :func:`provisional_summary` for the pinned definition text.
    """
    return provisional_summary(paths).provisional_source_count


def graph_path_views(paths: Sequence[GraphPath]) -> list[GraphPathView]:
    """Map domain ``GraphPath`` objects to the acl-stripped wire ``GraphPathView``.

    The ONE domain->view mapping both the ``/v1/brain`` wire response
    (:func:`retrieval_result_view`) and the ne-api recorder binding
    (``ne.api.tools.build_chat_bindings``) use to feed :func:`provisional_summary`
    (NE-T2X-5-RECORDER-WIRE) — extracted from ``retrieval_result_view`` so
    neither caller hand-rolls a second traversal over domain objects. Every
    field is copied EXPLICITLY (never ``model_dump``), so ``acl`` cannot leak
    onto the wire by construction. ``source_ref`` on each endpoint
    (NE-T2X-5-ENDPOINT-EVIDENCE-REF, ADR-0015) is threaded from the SAME
    ``GraphEntityRef`` the entity's own ``entity_class``/``entity_key``/
    ``display``/``assurance`` come from — never copied from the hop's own
    ``source_ref`` (that would recreate the endpoint-evidence defect this
    unit fixes).
    """
    return [
        GraphPathView(
            hops=[
                GraphHopView(
                    subject=GraphEntityRefView(
                        entity_class=h.subject.entity_class,
                        entity_key=h.subject.entity_key,
                        display=h.subject.display,
                        assurance=h.subject.assurance,
                        source_ref=h.subject.source_ref,
                    ),
                    edge_kind=h.edge_kind,
                    obj=GraphEntityRefView(
                        entity_class=h.obj.entity_class,
                        entity_key=h.obj.entity_key,
                        display=h.obj.display,
                        assurance=h.obj.assurance,
                        source_ref=h.obj.source_ref,
                    ),
                    source_system=h.source_system,
                    source_ref=h.source_ref,
                    assurance=h.assurance,
                )
                for h in path.hops
            ]
        )
        for path in paths
    ]


def retrieval_result_view(result: RetrievalResult) -> RetrievalResultView:
    """Map a library ``RetrievalResult`` to the acl-stripped wire view.

    Every field is copied EXPLICITLY (never ``model_dump`` of the contracts
    ``Chunk``/``GraphPath``), so ``acl`` cannot leak onto the wire by construction.
    ``graph_paths`` are surfaced DELIBERATELY (NE-BRAIN-T2-WIRING F3 — previously
    silently dropped); each hop rides its ACL-trimmed provenance
    (``source_system``/``source_ref``), governed by construction. ``assurance``
    (NE-T2X-4) is threaded field-by-field from the domain ``GraphEntityRef``/
    ``GraphHop`` objects via :func:`graph_path_views` — see
    :class:`GraphEntityRefView`'s docstring for why this cannot be automatic.
    ``provisional_source_count`` is derived from the mapped ``graph_paths``
    themselves (design decision (a)) via :func:`_provisional_source_count`.
    """
    graph_paths = graph_path_views(result.graph_paths)
    return RetrievalResultView(
        chunks=[
            ScoredChunkView(
                chunk=ChunkView(
                    chunk_id=sc.chunk.chunk_id,
                    document_id=sc.chunk.document_id,
                    text=sc.chunk.text,
                    # Deep-thaw: chunk metadata is a frozen tree (e.g. a nested
                    # `provenance` FrozenDict); a shallow dict() leaves nested
                    # FrozenDict values that pydantic cannot serialize -> the search
                    # response 500s (PydanticSerializationError). _thaw is the frozen
                    # module's canonical recursive serialization inverse.
                    metadata=_thaw(sc.chunk.metadata),
                ),
                score=sc.score,
            )
            for sc in result.chunks
        ],
        citations=citation_views(result),
        graph_paths=graph_paths,
        provisional_source_count=_provisional_source_count(graph_paths),
    )


# --- approvals (§1.4) -----------------------------------------------------


class PendingApprovalView(BaseModel):
    """One pending-approval queue row — payload-REDACTED (no payloads joined in)."""

    request_id: str
    action: str
    tier: int
    principal_id: str
    requested_at: datetime
    sla_deadline: datetime
    expires_at: datetime
    escalated: bool
    #: The REAL decision horizon for a run-bound approval: the instant the run
    #: OWNER's frozen authorization snapshot runs out, after which the parked
    #: effect can never execute and a decision can only 409.
    #:
    #: This exists because ``expires_at`` above is the approval REQUEST's own
    #: deadline (24 h by default) and reading it as the decision deadline is
    #: wrong by two orders of magnitude: on the 2026-08-28 demo box every
    #: stranded run's owner authorization died ~15 MINUTES in, while this queue
    #: showed a day. Approvers were looking at the wrong clock.
    #: (docs/architecture/approval-horizon.md, "Mitigations worth shipping
    #: regardless": show the horizon.)
    #:
    #: ``None`` for a standalone (non-run-bound) approval, which has no owner
    #: snapshot and therefore no such horizon — NOT "unknown", and never
    #: rendered as a deadline that passed.
    authorization_expires_at: datetime | None = None


class PendingApprovalsView(BaseModel):
    """GET /v1/approvals/pending response envelope."""

    approvals: list[PendingApprovalView]


class ApprovalRunContextView(BaseModel):
    """Content-free canonical-Run identity disclosed only to its approver."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    agent_id: str
    run_id: str


class ApprovalPayloadView(BaseModel):
    """GET /v1/approvals/{request_id}/payload response — the governed disclosure.

    Owner ruling 2026-07-31: served ONLY to the named approver(s) for THIS
    request (see ``ne.api.routers.approvals.read_payload``); every other
    approver-role holder is refused (403) before this model is ever built.
    Mirrors :class:`~ne.runtime.approvals.ApprovalPayloadDisclosure` field-for-
    field via :func:`approval_payload_view` — never ``model_dump``.
    """

    request_id: str
    action: str
    tier: int
    principal_id: str
    target_resource: str
    description: str
    material_parameters: dict[str, Any] | None
    reversibility: str
    disclosure_status: Literal["available"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    run_context: ApprovalRunContextView | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


def approval_payload_view(d: ApprovalPayloadDisclosure) -> ApprovalPayloadView:
    """Copy a library ``ApprovalPayloadDisclosure`` field-by-field to the wire view."""
    return ApprovalPayloadView(
        request_id=d.request_id,
        action=d.action,
        tier=d.tier,
        principal_id=d.principal_id,
        target_resource=d.target_resource,
        description=d.description,
        material_parameters=(
            dict(d.material_parameters) if d.material_parameters is not None else None
        ),
        reversibility=d.reversibility,
    )


class ApprovalActionResultView(BaseModel):
    """POST approve/reject response — status + (once decided) approver/decided_at."""

    status: str
    approver: str | None = None
    decided_at: datetime | None = None


def pending_approval_view(
    row: PendingApproval, *, authorization_expires_at: datetime | None = None
) -> PendingApprovalView:
    """Copy a library ``PendingApproval`` field-by-field to the wire view.

    ``authorization_expires_at`` is resolved by the ROUTE (it needs the run
    join and the run store, neither of which belongs in a wire mapper) and
    threaded through here so this stays a pure field copy.
    """
    return PendingApprovalView(
        request_id=row.request_id,
        action=row.action,
        tier=row.tier,
        principal_id=row.principal_id,
        requested_at=_to_utc(row.requested_at),
        sla_deadline=_to_utc(row.sla_deadline),
        expires_at=_to_utc(row.expires_at),
        escalated=row.escalated,
        authorization_expires_at=authorization_expires_at,
    )


# --- audit (§1.5) ---------------------------------------------------------


class AuditRecordView(BaseModel):
    """One audit-chain row on the wire — every value is payload-derived (C6)."""

    id: int
    seq: int
    event_id: str
    event_type: str
    config_hash: str
    actor: str
    occurred_at: datetime
    kpi_tag: str | None
    trace_id: str | None
    action: str | None
    resource: str | None
    outcome: str | None
    payload: dict[str, Any]
    prev_hash: str
    record_hash: str


#: WHICH PROOF PRODUCED THE VERDICT — on the wire, because the three are
#: DIFFERENT CLAIMS and a console that renders one as another is the bug.
#:
#: * ``"full-chain"`` — a real :func:`~ne.audit.verify.verify_chain` walked the
#:   whole ledger on this request.  This is the strongest verdict the server
#:   emits and the only one that may be rendered as "the entire chain was
#:   verified".
#: * ``"anchored-extension"`` — only the rows appended since a head a full
#:   verify blessed (up to :data:`~ne.audit.CHAIN_ANCHOR_MAX_AGE_S` ago) were
#:   re-hashed on this request.  The verdict COVERS the whole chain; the work
#:   on this call did not.  ``detail`` states the split in words.
#: * ``"windowed"`` — ``verify_chain(start=…, end=…)``: the walk was bounded to
#:   the request's ``from``/``to`` window, so a row tampered OUTSIDE it is
#:   invisible to this verdict by construction.  Not a whole-chain reading at
#:   all, and never comparable to the other two.
WireVerificationBasis = Literal["full-chain", "anchored-extension", "windowed"]


class ChainVerificationView(BaseModel):
    """Server-computed tamper verdict shipped WITH every audit page (rendered verbatim).

    ``valid`` alone is deliberately NOT the whole verdict.  ``basis`` names the
    proof that produced it and ``contradiction`` says whether the refusal is one
    the read-during-append race could have manufactured; a consumer that latches
    breaks, or that prints a sentence about what was verified, needs both.
    """

    valid: bool
    #: Which proof ran — see :data:`WireVerificationBasis`.  The console decides
    #: what it is allowed to SAY from this, and what may withdraw a standing
    #: break (a weaker proof may not withdraw a stronger proof's finding).
    basis: WireVerificationBasis
    #: This ``valid: false`` is a refusal the read-during-append race cannot
    #: produce AND will not repeat — the server proved the ledger no longer
    #: extends a head it verified, then re-anchored.  Mirrors
    #: :attr:`ne.audit.AnchoredVerification.contradiction`; read its docstring
    #: for why it has to be on the wire.  A consumer that requires N consecutive
    #: disagreeing reads must believe this one on its first and only appearance,
    #: or the strongest signal the server has can never reach a screen.
    contradiction: bool
    checked: int
    first_break_seq: int | None
    first_break_event_id: str | None
    reason: str | None
    detail: str


class AuditAnchorView(BaseModel):
    """The SERVER's verdict on an ``anchor_seq`` read (C7 — the console renders
    it verbatim and never infers reachability from row counts).

    ``status``: ``found`` (the entry is in ``events``), ``filtered_out`` (the
    entry exists but the request's filters exclude it), ``not_found`` (no such
    entry in the chain). ``head_seq`` is the chain head at read time, so a miss
    names where the log actually ends instead of failing generically.
    """

    seq: int
    status: Literal["found", "filtered_out", "not_found"]
    head_seq: int


class AuditEventsPageView(BaseModel):
    """GET /v1/audit/events response — the page plus its verification verdict.

    ``anchor`` is the anchored-read verdict: present exactly when the request
    carried ``anchor_seq`` (the ``?seq=`` deep link), ``None`` otherwise — an
    unanchored read never fabricates an anchor verdict.
    """

    events: list[AuditRecordView]
    verification: ChainVerificationView
    anchor: AuditAnchorView | None = None


class ReplayResultView(BaseModel):
    """GET /v1/audit/replay response — provenance of a decision."""

    events: list[AuditRecordView]
    config_hashes: list[str]
    context_ids: list[str]


def audit_record_view(record: AuditRecord) -> AuditRecordView:
    """Copy an ``AuditRecord`` field-by-field; keep the tz-aware guard on occurred_at."""
    return AuditRecordView(
        id=record.id,
        seq=record.seq,
        event_id=record.event_id,
        event_type=record.event_type,
        config_hash=record.config_hash,
        actor=record.actor,
        occurred_at=_to_utc(record.occurred_at),
        kpi_tag=record.kpi_tag,
        trace_id=record.trace_id,
        action=record.action,
        resource=record.resource,
        outcome=record.outcome,
        payload=dict(record.payload),
        prev_hash=record.prev_hash,
        record_hash=record.record_hash,
    )


def audit_anchor_view(resolution: AnchorResolution) -> AuditAnchorView:
    """Copy an ``AnchorResolution`` field-by-field to the wire view."""
    return AuditAnchorView(
        seq=resolution.seq,
        status=resolution.status,
        head_seq=resolution.head_seq,
    )


def chain_verification_view(
    cv: ChainVerification, *, basis: WireVerificationBasis = "full-chain"
) -> ChainVerificationView:
    """Copy a ``ChainVerification`` field-by-field to the wire view.

    ``basis`` defaults to ``"full-chain"`` because that is what an unbounded
    :func:`~ne.audit.verify.verify_chain` result IS — a caller that bounded the
    walk to a window passes ``"windowed"`` and the default is never silently
    right for it.  ``contradiction`` is always ``False`` here: a bare
    ``ChainVerification`` has no anchor to contradict, so only
    :class:`~ne.audit.AnchoredVerification` can carry that verdict (see
    ``ne.api.routers.audit._anchored_verification_view``).
    """
    return ChainVerificationView(
        valid=cv.valid,
        basis=basis,
        contradiction=False,
        checked=cv.checked,
        first_break_seq=cv.first_break_seq,
        first_break_event_id=cv.first_break_event_id,
        reason=cv.reason,
        detail=cv.detail,
    )


# --- trace index (B11, observability plan §6 U1) --------------------------


class TraceIndexRowView(BaseModel):
    """One run in ``GET /v1/traces`` — identifiers, enums, counts, timestamps
    and money ONLY (C12/data-minimization, plan §3 row 8): this view
    STRUCTURALLY cannot carry ``detail`` text, prompt/response bodies, note
    bodies, account names, or any unenumerated provenance — there is no
    free-text field below, and every value is derived from the enumerated
    usage/outcome keys in :mod:`ne.api.trace_index`. Field derivations are
    documented there (they are the load-bearing spec).
    """

    trace_id: str
    started_at: datetime
    ended_at: datetime
    duration_ms: float
    task: str | None
    root_action: str | None
    actor: str | None
    models: list[str]
    calls: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    priced_calls: int
    unpriced_calls: int
    denials: int
    errors: int
    approvals: int


class TraceIndexView(BaseModel):
    """``GET /v1/traces`` response — the run index page, its page-level chain
    verdict (FULL-chain — see :mod:`ne.api.trace_index` for why it is never
    windowed), and whether more pages exist beyond ``limit``."""

    traces: list[TraceIndexRowView]
    verification: ChainVerificationView
    truncated: bool


# --- eval (§1.6 #11) ------------------------------------------------------


class EvalRunView(BaseModel):
    """One eval run on the wire — ``passed`` is server-authoritative."""

    run_id: str
    suite: str
    config_hash: str
    golden_set_hash: str
    judge_model_id: str | None
    judge_family: str | None
    rubric_versions: dict[str, Any]
    metrics: dict[str, float]
    thresholds: dict[str, float]
    statuses: dict[str, str]
    judge_error_rate: float
    passed: bool
    created_at: datetime


class EvalRunsView(BaseModel):
    """GET /v1/eval/runs response envelope."""

    runs: list[EvalRunView]


def eval_run_view(record: EvalRunRecord) -> EvalRunView:
    """Copy an ``EvalRunRecord`` field-by-field; normalize the naive SQLite created_at."""
    return EvalRunView(
        run_id=record.run_id,
        suite=record.suite,
        config_hash=record.config_hash,
        golden_set_hash=record.golden_set_hash,
        judge_model_id=record.judge_model_id,
        judge_family=record.judge_family,
        rubric_versions=dict(record.rubric_versions),
        metrics=dict(record.metrics),
        thresholds=dict(record.thresholds),
        statuses=dict(record.statuses),
        judge_error_rate=record.judge_error_rate,
        passed=record.passed,
        created_at=_to_utc(record.created_at),
    )


# --- kpi (§1.6 #12) -------------------------------------------------------


KpiMeasurementStatus = Literal["measured", "undefined"]


class KpiEntryView(BaseModel):
    """One KPI metric over the report window.

    ``undefined`` means the owning collector ran but the rate had no
    denominator in this window.  That is different from a tag omitted because
    its instrumentation is unavailable.  A measured zero therefore remains a
    numeric ``0.0``; it is never conflated with either state.
    """

    kpi_tag: str
    value: float | None
    measurement_status: KpiMeasurementStatus
    unit: str | None
    dimensions: dict[str, str]
    window_start: datetime
    window_end: datetime

    @model_validator(mode="after")
    def _status_matches_value(self) -> KpiEntryView:
        if self.measurement_status == "measured" and self.value is None:
            raise ValueError("a measured KPI must carry a numeric value")
        if self.measurement_status == "undefined" and self.value is not None:
            raise ValueError("an undefined KPI must not carry a numeric value")
        return self


class KpiSummaryView(BaseModel):
    """GET /v1/kpi/summary response envelope."""

    kpis: list[KpiEntryView]
    generated_at: datetime


# --- posture (§4.1 / §4.2 — NE-131) ---------------------------------------


class PostureConstraintView(BaseModel):
    """One C1-C12 constraint on the /v1/posture wire — attestation is server-real.

    ``attested``/``mode``/``evidence`` are copied field-by-field from the
    drift-guarded :data:`ne.api.posture.CONSTRAINT_ATTESTATIONS` (never fabricated);
    a constraint with no mechanical guardian renders honestly
    ``attested=False``/``mode='manual'`` (§0 law 5; currently none).
    """

    id: str
    short: str
    enforcement: str
    attested: bool
    mode: str
    evidence: list[str]


class ModelLaneView(BaseModel):
    """One composed model-registry lane (gateway alias, provider, privacy lane).

    CONFIGURED, NOT MEASURED — the distinction :class:`LaneServingView` below
    exists to protect. ``model_id`` is the ROUTING ALIAS (``frontier-default``),
    which on its own answers nothing about what that lane resolves to;
    ``configured_model`` and ``context_window`` are read straight off the frozen
    ``GatewaySettings`` so the alias can be resolved to a model without asking a
    human. Every field here is a config read: NOTHING on this view is evidence
    that the lane is reachable, that the named model is the one an endpoint
    would answer as, or that the declared window is the one actually served.
    That evidence is ``LaneServingView``, it is measured over HTTP, it exists
    only for probeable private lanes, and a consumer must not blend the two.

    ``configured_model`` is derived with :func:`ne.api.serving.expected_served_model`
    — the SAME derivation ``LaneServingView.configured_model`` uses, so a lane
    that appears on both surfaces names its model identically in both.
    """

    model_id: str
    provider: str
    lane: str
    #: The litellm target with its provider prefix stripped — what this lane
    #: RESOLVES to, per configuration (e.g. ``claude-sonnet-5``, ``qwen3-0.6b``).
    configured_model: str
    #: The DECLARED serving window this deployment configured for the lane, in
    #: tokens. It is what the context-budget pre-flight and the runtime trim
    #: guard derive from — not an endpoint's self-report.
    context_window: int


class ConfigHashSpanView(BaseModel):
    """One config_hash span on the timeline — ``from`` serializes the reserved word.

    The field is named ``from_`` (``from`` is a Python keyword) and carries a
    ``serialization_alias`` so the WIRE key is exactly ``'from'`` (the SPEC-row
    shape) — FastAPI serializes response models by alias by default. Using
    ``serialization_alias`` (not a plain ``alias``) keeps the constructor kwarg the
    field name ``from_``, so the field-by-field construction is pyright-clean. The
    in-force span has ``to=None``.
    """

    hash: str
    from_: datetime = Field(serialization_alias="from")
    to: datetime | None


class AirGapPendingView(BaseModel):
    """The §2.5 pending-change marker for a parked governed air-gap change
    (NE-AIRGAP-TOGGLE): the approval request id + the requester. Ids only — the
    change's parameters are disclosed exclusively through the approver-gated
    payload read (B1)."""

    request_id: str
    requested_by: str


class PostureView(BaseModel):
    """GET /v1/posture response — the sovereignty-posture surface (NE-131).

    ``air_gap`` / ``egress_allowlist`` are a direct read from the same provider
    the enforcing egress guard reads per call (NE-AIRGAP-TOGGLE), not a
    liveness probe. ``network_observed_at`` dates that read.
    ``configuration_observed_at`` dates the frozen-config + audit-history read.
    Keeping both server-authored instants on the payload means a downstream
    cache can only serve a visibly aging observation; it cannot make old data
    current by re-dating it in the browser. ``air_gap_pending`` is the §2.5
    pending-change read-back: non-null while a governed air-gap change is
    parked at the approval gate (the displayed posture remains in force)."""

    constraints: list[PostureConstraintView]
    air_gap: bool
    egress_allowlist: list[str]
    network_observed_at: datetime
    model_lanes: list[ModelLaneView]
    config_hash: str
    config_hash_history: list[ConfigHashSpanView]
    configuration_observed_at: datetime
    attestation_source: str
    air_gap_pending: AirGapPendingView | None = None


# --- live serving status (NE-SERVING-STATUS) -------------------------------

#: The closed observation vocabulary. ``ok`` / ``timeout`` / ``unreachable`` /
#: ``http_error`` / ``malformed`` are OBSERVED verdicts (each formed by an
#: actual probe attempt at ``checked_at``); ``not_probed`` is the fail-closed
#: refusal to contact an SSRF-shaped endpoint (no observation exists, so
#: ``checked_at`` is None). None of the failure states ever carries observed
#: model data — an unreachable lane structurally cannot render a model name.
ServingState = Literal["ok", "timeout", "unreachable", "http_error", "malformed", "not_probed"]


class LaneServingView(BaseModel):
    """One probeable lane's live serving observation (GET /v1/models/serving).

    MEASURED, not configured (ne.api.serving module docstring): every non-config
    field was observed over HTTP from the endpoint the gateway routes this lane
    to. ``endpoint`` is the probed identity as ``scheme://host[:port]`` ONLY —
    rebuilt from the parsed URL, so config-string userinfo / query tokens
    structurally cannot serialize (C12 ruling). ``checked_at`` is when THIS
    verdict was formed (a cache hit keeps the old value — age is carried, never
    hidden); ``last_ok_at`` is the newest successful observation this process
    has formed, surviving failures so "down since when" is answerable.
    """

    model_id: str
    lane: str
    endpoint: str
    #: What the CONFIG says the endpoint should serve (litellm target, provider
    #: prefix stripped) — served so a mismatch with ``served_models`` renders as
    #: the endpoint's word against the configuration's, never silently.
    configured_model: str
    state: ServingState
    checked_at: datetime | None
    latency_ms: int | None
    http_status: int | None
    #: The model ids the ENDPOINT reports serving. Non-empty only on ``ok``.
    served_models: list[str]
    #: ``served_models`` contains the configured target. None unless ``ok``.
    serves_configured_model: bool | None
    #: vLLM's ``max_model_len`` for the matched model — the RUNTIME serving
    #: window, the value the RunPod incident proved can differ from the card.
    observed_context_window: int | None
    #: The serving engine's self-reported version (vLLM ``/version``); None
    #: when the endpoint does not expose one — never inferred.
    engine_version: str | None
    last_ok_at: datetime | None


class ServingStatusView(BaseModel):
    """GET /v1/models/serving — one row per probeable lane, config order.

    A deployment with no probeable lane serves an EMPTY list, and the console
    renders nothing for it — never a fabricated row (frontier lanes are not
    "down", they are not this surface's claim)."""

    lanes: list[LaneServingView]


class FrontierReadinessView(BaseModel):
    """GET /v1/models/frontier-readiness — has the boot warm landed yet?

    An ops signal (the deploy's acceptance gate), never an evidence source:
    ``not-applicable`` (nothing to wait on — no warmer, air gap engaged, or no
    frontier-routed task), ``unknown`` (the registers could not be read —
    treat as not-ready, never as a green light), ``warming`` (an admission now
    would pay the inline warm plus the O(chain) read), or ``ready`` (evidence
    is current and the records cache is primed)."""

    status: Literal["not-applicable", "unknown", "warming", "ready"]


# --- governed live per-run model choices (BD-15) --------------------------


class ModelChoiceView(BaseModel):
    """One boot-registered model offered for a run surface.

    ``evidence``/``observed_at`` are populated only by a catalog that MEASURED
    availability.  The run-model picker lists the CONFIGURED models, so both are
    ``null`` there: an absent measurement is rendered as absence, never as a
    stale or invented observation time.  Liveness for that surface is settled at
    submit, where an unavailable model is refused by name."""

    model: str
    provider: str
    lane: str
    evidence: Literal["private-serving-probe", "frontier-provider-attempt"] | None = None
    observed_at: datetime | None = None


class ModelChoiceCatalogView(BaseModel):
    """The live choices for one server-mapped console surface."""

    surface: Literal["ask", "agentic-workflow"]
    task: str
    effective_default: str | None
    choices: list[ModelChoiceView]


class AirGapChangeRequest(BaseModel):
    """POST /v1/network/air-gap request body (NE-AIRGAP-TOGGLE): the TARGET
    posture. ``extra='forbid'`` (C12); identity comes from the verified bearer,
    never the body."""

    model_config = ConfigDict(extra="forbid")

    air_gap: bool


class GovernedChangeView(BaseModel):
    """A governed write endpoint's §2.5 answer: ``applied`` (Tier 0/1) or
    ``pending`` (parked; ``request_id`` set). ``denied`` is never in this model —
    a policy refusal is a 403 with the server's reason in ``detail``."""

    status: Literal["applied", "pending"]
    request_id: str | None = None


# --- model routing (NE-MODEL-ROUTING) --------------------------------------


class RouteTargetView(BaseModel):
    """One candidate model for a task, with the SERVER's verdict on it.

    ``selectable`` and ``reason`` are the C7 load-bearing fields: which models a
    task may be repointed to is ``registered ∧ lane ∧ budget floor ∧ not-current
    ∧ air-gap``, a judgement combining the boot model set, the gateway's budget
    arithmetic and the tier table. The console renders this and computes none of
    it. ``action``/``tier`` say WHAT the change would ride, so the page can name
    the ceremony without deciding it.

    C12: a model ALIAS and a lane name only. No ``api_base``, no provider
    credential, no endpoint — this surface never carries one.
    """

    model: str
    lane: str
    action: str
    tier: int
    selectable: bool
    reason: str | None = None


class PendingRoutingChange(BaseModel):
    """A parked route mutation, exposed so the console cannot submit it twice."""

    request_id: str
    requested_by: str
    model: str


class TaskRoutingView(BaseModel):
    """One gateway task on ``GET /v1/models/routing``.

    ``model`` is what the RUNNING gateway routes to right now — read from the
    same register the enforcing route provider reads, so reported == enforced.
    ``default_model`` is what the deployment shipped, and ``source`` says which
    of the two is in force.
    """

    task: str
    model: str
    lane: str
    default_model: str
    source: Literal["default", "override"]
    changed_by: str | None = None
    approved_by: str | None = None
    changed_at: datetime | None = None
    pending_change: PendingRoutingChange | None = None
    available: bool = True
    unavailable_reason: str | None = None
    targets: list[RouteTargetView]


# --- governed inference target lifecycle (ADR-0021) -----------------------


class PendingInferenceLifecycleChange(BaseModel):
    request_id: str
    requested_by: str
    desired_state: Literal["ready", "stopped"]


class InferenceLifecycleTargetView(BaseModel):
    target_id: str
    display_name: str
    aliases: list[str]
    state: str
    desired_state: str
    version: int
    transition_id: str | None
    code: str
    observed_at: datetime | None
    updated_at: datetime
    cooldown_until: datetime | None
    pending_change: PendingInferenceLifecycleChange | None
    available_actions: list[Literal["start", "stop"]]


class InferenceLifecycleView(BaseModel):
    registry_hash: str
    transport_available: bool
    targets: list[InferenceLifecycleTargetView]


class InferenceStopConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)


class InferenceStopConfirmationView(BaseModel):
    confirmation_token: str
    expires_at: datetime


class InferenceTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desired_state: Literal["ready", "stopped"]
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    confirmation_token: str | None = Field(default=None, min_length=16, max_length=128)


class ModelRoutingView(BaseModel):
    """``GET /v1/models/routing`` — every routed task, ordered by name."""

    tasks: list[TaskRoutingView]


class RouteChangeRequest(BaseModel):
    """``POST /v1/models/routing`` request body: the TARGET route.

    ``extra='forbid'`` (C12); identity comes from the verified bearer, never the
    body. The body names a task and a MODEL ALIAS — never an endpoint, never a
    credential: a routing change re-selects among boot-registered models and can
    introduce neither.
    """

    model_config = ConfigDict(extra="forbid")

    task: str
    model: str


# --- connect status (§4.3 — NE-132) ---------------------------------------


class QuarantineReasonView(BaseModel):
    """One sanitized quarantine reason-CATEGORY bucket on the /v1/connect/status wire.

    ``reason`` is an ALLOWLISTED category label (e.g. ``'schema-validation'``,
    ``'unknown-table'``, ``'acl-unresolvable'``, ``'other'``) derived from the parked
    write's raw reason by a closed-set signature match — never the raw reason text,
    so no JSON path / offending value / record id / payload fragment can reach the
    wire (C7; see ``connect_status._reason_category``). ``count`` is the number of
    parked writes in that category across the readable quarantine store(s).
    """

    reason: str
    count: int


class ConnectorStatusView(BaseModel):
    """One connector lane on the /v1/connect/status wire (the SPEC-row shape).

    TWO DISTINCT FRESHNESS FACTS (SPEC §12 item 9) — never collapse them:
    ``last_checked_age_s`` is when the sync cycle last RAN (the latest
    ``connector.sync``/``cdc.ingest`` event's age; advances every cycle whether or
    not anything changed), and ``cursor_age_s`` is how old the newest ACCEPTED
    record is — on a pull lane the cursor row's last write, on the CDC push lane
    the newest ``cdc.ingest`` batch whose counts say it accepted a record
    (``written > 0`` — an empty/heartbeat/all-quarantined batch advances only
    ``last_checked_age_s``; see ``connect_status.latest_accepting_cdc_ingest``).
    A connector polling a quiet source has a small ``last_checked_age_s`` and a
    large ``cursor_age_s``. The console labels them ``Last checked`` and ``Newest
    record``.

    EVERY nullable field is honestly absent — no state source had data (§0 law 5):
    a configured-but-never-synced connector carries ``last_checked_age_s=None`` /
    ``acl_mirror=None`` / ``last_sync_seq=None`` (no ``connector.sync`` /
    ``cdc.ingest`` audit event yet — the console's ``Never checked``),
    ``cursor_age_s=None`` (no cursor row — no record accepted yet, a cursorless
    connector, or an unreadable state store), and ``quarantined_count=None`` (no
    readable quarantine store). A ``None`` is NEVER a fabricated fresh/green/zero.

    ``acl_mirror`` reports the LATEST sync event's fail-closed health: a ``success``
    outcome → ``'ok'`` (the ACL mirror verified), any other outcome (``denied`` /
    ``error`` — the AclResolutionError path) → ``'degraded'`` (fail-closed reporting).
    An ACL-DENIED cycle still advances ``last_checked_age_s`` — the run happened;
    its failure is ``acl_mirror``'s fact, not an excuse to render the run as
    absent (pinned by ``test_degraded_acl_mirror_fail_closed``, the one failure
    path ``SyncRunner.run`` audits). A cycle that CRASHES before emitting its
    ``connector.sync`` event — a source HTTP failure, a writer/flush/state
    exception — advances nothing: no event exists, so ``last_checked_age_s``
    honestly goes stale. That producer gap is a named residual (PR #402), not a
    property this field can promise away.
    """

    id: str
    kind: str
    #: Server-owned execution posture. ``snapshot`` is retained data with no
    #: provider pull, manual job surface, or scheduled sync; it is NEVER a
    #: claim of current provider health.
    posture: Literal["live", "snapshot"]
    cursor_age_s: float | None
    last_checked_age_s: float | None
    acl_mirror: Literal["ok", "degraded"] | None
    last_sync_seq: int | None
    quarantined_count: int | None
    #: Server-authored advisory visibility. POST rechecks role + PDP authoritatively.
    sync_allowed: bool = False


class ConnectStatusView(BaseModel):
    """GET /v1/connect/status response — the connector-sync health surface (NE-132).

    ``quarantine_total`` is ``None`` when NO quarantine store was readable (honestly
    unknown, never a fake zero); it is an integer (possibly 0) when at least one
    store was read. ``quarantine_reasons`` is an ADDITIVE key beyond the SPEC-row
    shape, required by design §4.3 ('count + reason categories') — the sanitized
    category ledger driving the quarantine-vessel reason rows.
    """

    connectors: list[ConnectorStatusView]
    quarantine_total: int | None
    quarantine_reasons: list[QuarantineReasonView]


class ConnectSyncGraphCountsView(BaseModel):
    """The Tier-2 graph projection's terminal COUNTS, mirroring ``GraphSyncReport``.

    A Graph-enabled source (m365 with a wired ``m365_graph_sync_host``) appends
    its graph report to the same terminal payload as the T1 connector counts
    (``ne.worker.ingest._sync_handler``). Every field here is a CARDINALITY or
    the run's mode label: no node or edge is named, keyed, or described, no
    ``source_ref``/``source_system``/locator/display name and no extracted text
    reaches this wire, so an operator learns HOW MUCH the projection moved and
    nothing about WHAT it moved. ``extra="forbid"`` keeps it that way — a graph
    report that grows a content-bearing field cannot ride out here silently, it
    fails validation until someone declares it deliberately.

    Fields are REQUIRED, not defaulted: the producer dumps the whole report, so
    a missing count means a malformed terminal, and defaulting one would report
    a fabricated zero (§0 law 5).
    """

    model_config = ConfigDict(extra="forbid")

    mode: str
    seen: int
    upserted: int
    unchanged: int
    acl_healed: int
    quarantined: int
    unmapped: int
    pruned: int
    pending_edges: int
    evidence_pending: int


class ConnectSyncCountsView(BaseModel):
    """The connector's terminal counts-only report; cursor tokens are excluded.

    ``graph`` carries the Tier-2 projection's counts for a Graph-enabled source
    and is ``None`` for every source that runs no graph projection — absent, not
    a zeroed stand-in. It is DECLARED rather than tolerated: ``extra="forbid"``
    still holds, so any OTHER key in the terminal payload remains an
    ``InvalidTerminalResult`` instead of being waved through.
    """

    model_config = ConfigDict(extra="forbid")

    mode: str
    records_seen: int
    written: int
    skipped_unchanged: int
    invalid: int
    quarantined: int
    graph: ConnectSyncGraphCountsView | None = None


class ConnectSyncTriggerRequest(BaseModel):
    """POST Sync now accepts no caller-supplied execution or identity fields."""

    model_config = ConfigDict(extra="forbid")


class ConnectSyncRequestView(BaseModel):
    """One short-lived governed Sync now request, with no customer body or secret."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    source: str
    status: Literal["queued", "running", "succeeded", "failed"]
    created_at: datetime
    expires_at: datetime
    counts: ConnectSyncCountsView | None = None
    error_class: str | None = None


class AuthorityAttemptView(BaseModel):
    """Closed metadata-only projection of one current authority attempt.

    Every field is an opaque id, hash, closed source/stage label, epoch, or
    provider revision. Customer content, ACLs, credentials, locators, principals,
    raw store rows, Writer handles and continuation receipts are unrepresentable.
    """

    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    source_kind: Literal["m365-sharepoint", "salesforce"]
    source_binding: Literal["m365", "salesforce"]
    scope_hash: str
    identity_hash: str
    stage: Literal["claimed", "deletion_validated", "authority_tombstoned"]
    source_revision: str
    contract_epoch: int
    prior_artifact_id: str | None
    receipt_id: str | None
    receipt_hash: str | None


class AuthorityAttemptsView(BaseModel):
    """Stable current pending authority-attempt queue."""

    model_config = ConfigDict(extra="forbid")

    attempts: list[AuthorityAttemptView]


class QuarantinedItemView(BaseModel):
    """One parked write on the OPS-GATED /v1/connect/quarantine drill-down wire.

    UNLIKE :class:`QuarantineReasonView`, this carries the RAW stored reason —
    the contract validator's own message naming the field and the offending
    value — plus the parked row itself (``payload``, string values bounded with
    ``payload_truncated`` honesty). A parked write is customer data that failed
    validation, which is exactly why the surface exists (the quarantine lane is
    INSPECTABLE by design) and exactly why it is restricted to
    ``ApiSettings.ops_roles`` (403 + audited otherwise — see routers/connect.py's
    access decision). ``source``/``record_id`` are parsed from the park's
    provenance; ``None`` when the provenance shape is unrecognised (honest,
    never guessed). ``category`` is the same allowlisted label the status
    surface buckets by, so the two surfaces reconcile.
    """

    item_id: str
    source: str | None
    record_id: str | None
    reason: str
    category: str
    shape: str
    created_at: str
    payload: dict[str, object]
    payload_truncated: bool


class ConnectQuarantineView(BaseModel):
    """GET /v1/connect/quarantine response (ops-gated drill-down).

    ``total`` follows the status surface's honesty contract: ``None`` when NO
    quarantine store was readable (never a fake zero); otherwise the FULL
    parked count, of which up to the requested limit ride in ``items``
    (newest first).
    """

    items: list[QuarantinedItemView]
    total: int | None


# --- steward (§1.7) -------------------------------------------------------


class DiffArtifactView(BaseModel):
    """A server-rendered, injection-safe reviewable artifact (types.ts:235-243).

    ``patch``/``pr_body`` are UNTRUSTED proposal text ALREADY routed through the
    renderer's inline/fenced/comment-line sanitizers server-side; the console
    renders them as INERT preformatted text.
    """

    proposal_id: str
    lane: str
    kind: str
    path: str
    title: str
    patch: str
    pr_body: str


class SkillCandidateView(BaseModel):
    """A forged skill candidate on the wire — field-by-field, server-derived.

    The raw ``payload`` NEVER crosses the wire; every field is copied explicitly
    from ``payload['pack']`` + ``payload['suggested_acl_labels']``. ``body`` is
    rendered INERT by the console (a plain text node, never HTML) with an
    explicit 'untrusted — injected into every agent' framing, because a forged
    body is attacker-influenced content the approver must review adversarially
    (R-23).
    """

    name: str
    version: int
    description: str
    triggers: list[str]
    suggested_acl_labels: list[str]
    provenance_line: str
    body: str


class StewardProposalView(BaseModel):
    """One steward-queue proposal on the wire — EXACTLY these 13 keys.

    STRUCTURALLY omits the raw ``summary``/``payload`` (types.ts:252-265):
    proposal-carried free text reaches the console ONLY inside the sanitized
    ``artifact`` (mirrors how :class:`ChunkView` omits ``acl``). ``skill`` is
    non-None ONLY for skill kinds (NE-SKILL-1b): a field-by-field server-derived
    :class:`SkillCandidateView`, never the raw payload.
    """

    proposal_id: str
    lane: str
    kind: str
    status: str
    cluster_id: str
    confidence: float
    priority: str
    evidence_event_ids: list[str]
    evidence_signal_ids: list[int]
    config_hash: str
    created_at: datetime
    artifact: DiffArtifactView
    skill: SkillCandidateView | None = None


class WithheldStewardProposalView(BaseModel):
    """Opaque safe shell for a proposal that is not currently disclosable."""

    disclosure_status: Literal["content_withheld", "retryable_unknown"]
    proposal_id: str
    status: str
    created_at: datetime


class StewardProposalsView(BaseModel):
    """GET /v1/steward/proposals response envelope."""

    proposals: list[StewardProposalView | WithheldStewardProposalView]


class ReviewEventView(BaseModel):
    """A review-log row on the wire — EXACTLY the 13 types.ts fields (NO surrogate id).

    ``actor``/``surface``/``session_id`` are SERVER-derived from the verified
    bearer + the console surface, never client-supplied.
    """

    review_id: str
    proposal_id: str
    action: str
    from_status: str
    to_status: str
    actor: str
    surface: str
    session_id: str
    rejection_code: str | None
    note: str | None
    applied_ref: str | None
    config_hash: str
    created_at: datetime


class StewardTotalsView(BaseModel):
    """The digest's cross-lane totals."""

    proposed: int
    approved: int
    rejected: int
    applied: int


class StewardLaneDigestView(BaseModel):
    """One lane's proposal/review counts over the digest window."""

    lane: str
    proposed: int
    approved: int
    rejected: int
    applied: int


class StewardSkillsRetirementView(BaseModel):
    """One retirement candidate in the digest's Skill Gardener card."""

    name: str
    reason: str
    fired_count: int


class StewardSkillsFailureView(BaseModel):
    """One failure flag (≥50%-of-≥4 fires) — review, never auto-retire."""

    name: str
    known_fires: int
    failed: int
    ratio: float


class StewardSkillsOverlapView(BaseModel):
    """One trigger phrase claimed by ≥2 active packs."""

    phrase: str
    names: list[str]


class StewardSkillsNearDupView(BaseModel):
    """One near-duplicate pack pair."""

    a: str
    b: str
    method: str
    score: float


class StewardSkillsUnknownToolsView(BaseModel):
    """One pack declaring an unregistered required tool."""

    name: str
    unknown: list[str]


class StewardSkillsStaleProposalView(BaseModel):
    """One stale (>14d open) skill proposal — batch-reject rides the reject route."""

    proposal_id: str
    kind: str
    name: str
    age_days: int


class StewardSkillsDigestView(BaseModel):
    """The NE-SKILL-4 rot card — the latest Skill Gardener run's sections."""

    ran_at: str
    retirement_candidates: list[StewardSkillsRetirementView]
    failure_flags: list[StewardSkillsFailureView]
    trigger_overlaps: list[StewardSkillsOverlapView]
    near_dups: list[StewardSkillsNearDupView]
    unknown_requires_tools: list[StewardSkillsUnknownToolsView]
    stale_proposals: list[StewardSkillsStaleProposalView]


class StewardDigestView(BaseModel):
    """GET /v1/steward/digest response — ``week_start``/``week_end`` serialize YYYY-MM-DD."""

    week_start: date
    week_end: date
    generated_at: datetime
    scope: Literal["principal_visible"] = "principal_visible"
    totals: StewardTotalsView
    by_lane: list[StewardLaneDigestView]
    #: The optional Skill Gardener rot card (NE-SKILL-4) — ``None`` before the
    #: first monthly run (the console omits the card entirely).
    skills: StewardSkillsDigestView | None = None


class StewardLatencyView(BaseModel):
    """GET /v1/steward/latency response — nulls when the relevant set is empty."""

    median_wait_s: float | None
    p90_wait_s: float | None
    decided_count: int
    pending_count: int
    oldest_pending_age_s: float | None
    generated_at: datetime


def steward_proposal_view(
    proposal: StoredFixProposal,
    status: str,
    artifact: DiffArtifact,
    *,
    skill: SkillCandidateView | None = None,
) -> StewardProposalView:
    """Map a ``StoredFixProposal`` + folded status + sanitized artifact to the wire.

    Every field is copied EXPLICITLY (never ``model_dump`` of the proposal), so
    ``summary``/``payload`` cannot leak onto the wire by construction. ``skill``
    is the server-derived :class:`SkillCandidateView` for skill kinds only.
    """
    return StewardProposalView(
        proposal_id=proposal.proposal_id,
        lane=proposal.lane,
        kind=proposal.kind,
        status=status,
        cluster_id=proposal.cluster_id,
        confidence=proposal.confidence,
        priority=proposal.priority,
        evidence_event_ids=list(proposal.evidence_event_ids),
        evidence_signal_ids=list(proposal.evidence_signal_ids),
        config_hash=proposal.config_hash,
        created_at=_to_utc(proposal.created_at),
        artifact=DiffArtifactView(
            proposal_id=artifact.proposal_id,
            lane=artifact.lane,
            kind=artifact.kind.value,
            path=artifact.path,
            title=artifact.title,
            patch=artifact.patch,
            pr_body=artifact.pr_body,
        ),
        skill=skill,
    )


def review_event_view(event: StoredReviewEvent) -> ReviewEventView:
    """Copy a ``StoredReviewEvent`` field-by-field; drop the surrogate ``id``."""
    return ReviewEventView(
        review_id=event.review_id,
        proposal_id=event.proposal_id,
        action=event.action,
        from_status=event.from_status,
        to_status=event.to_status,
        actor=event.actor,
        surface=event.surface,
        session_id=event.session_id,
        rejection_code=event.rejection_code,
        note=event.note,
        applied_ref=event.applied_ref,
        config_hash=event.config_hash,
        created_at=_to_utc(event.created_at),
    )


def _steward_skills_view(section: object) -> StewardSkillsDigestView | None:
    """Map the ``WeeklyDigest.skills`` section field-by-field (never model_dump)."""
    from ne.api.steward_metrics import SkillsGardenerSection

    if not isinstance(section, SkillsGardenerSection):
        return None
    return StewardSkillsDigestView(
        ran_at=section.ran_at,
        retirement_candidates=[
            StewardSkillsRetirementView(name=r.name, reason=r.reason, fired_count=r.fired_count)
            for r in section.retirement_candidates
        ],
        failure_flags=[
            StewardSkillsFailureView(
                name=f.name, known_fires=f.known_fires, failed=f.failed, ratio=f.ratio
            )
            for f in section.failure_flags
        ],
        trigger_overlaps=[
            StewardSkillsOverlapView(phrase=o.phrase, names=list(o.names))
            for o in section.trigger_overlaps
        ],
        near_dups=[
            StewardSkillsNearDupView(a=d.a, b=d.b, method=d.method, score=d.score)
            for d in section.near_dups
        ],
        unknown_requires_tools=[
            StewardSkillsUnknownToolsView(name=u.name, unknown=list(u.unknown))
            for u in section.unknown_requires_tools
        ],
        stale_proposals=[
            StewardSkillsStaleProposalView(
                proposal_id=s.proposal_id, kind=s.kind, name=s.name, age_days=s.age_days
            )
            for s in section.stale_proposals
        ],
    )


def steward_digest_view(digest: WeeklyDigest) -> StewardDigestView:
    """Map a computed ``WeeklyDigest`` to the wire view."""
    return StewardDigestView(
        week_start=digest.week_start,
        week_end=digest.week_end,
        generated_at=_to_utc(digest.generated_at),
        skills=_steward_skills_view(digest.skills),
        totals=StewardTotalsView(
            proposed=digest.proposed,
            approved=digest.approved,
            rejected=digest.rejected,
            applied=digest.applied,
        ),
        by_lane=[
            StewardLaneDigestView(
                lane=lane.lane,
                proposed=lane.proposed,
                approved=lane.approved,
                rejected=lane.rejected,
                applied=lane.applied,
            )
            for lane in digest.by_lane
        ],
    )


def steward_latency_view(summary: LatencySummary) -> StewardLatencyView:
    """Map a computed ``LatencySummary`` to the wire view."""
    return StewardLatencyView(
        median_wait_s=summary.median_wait_s,
        p90_wait_s=summary.p90_wait_s,
        decided_count=summary.decided_count,
        pending_count=summary.pending_count,
        oldest_pending_age_s=summary.oldest_pending_age_s,
        generated_at=_to_utc(summary.generated_at),
    )


class VerifiedTemplateApplyView(BaseModel):
    """POST /v1/steward/proposals/{id}/apply-verified-template response.

    Carries the folded review row PLUS the audited config_hash / registry_hash
    delta and ``restart_required`` — the console renders the delta + a
    'restart the platform to activate' notice (activation is restart-to-activate
    by design, NE-ONTOLOGY-FLYWHEEL). No template TOML is echoed.
    """

    review: ReviewEventView
    template_id: str
    version: int
    toml_sha256: str
    old_registry_hash: str
    new_registry_hash: str
    old_config_hash: str
    new_config_hash: str
    restart_required: bool


def verified_template_apply_view(applied: AppliedVerifiedTemplate) -> VerifiedTemplateApplyView:
    """Map an ``AppliedVerifiedTemplate`` to the wire view (review + hash delta)."""
    return VerifiedTemplateApplyView(
        review=review_event_view(applied.review),
        template_id=applied.template_id,
        version=applied.version,
        toml_sha256=applied.toml_sha256,
        old_registry_hash=applied.old_registry_hash,
        new_registry_hash=applied.new_registry_hash,
        old_config_hash=applied.old_config_hash,
        new_config_hash=applied.new_config_hash,
        restart_required=True,
    )


class SkillApplyView(BaseModel):
    """POST /v1/steward/proposals/{id}/apply-skill response (NE-SKILL-1b).

    Carries the folded review row + the registered skill's name/version, its
    ``content_hash`` (the version identity — relabel is a content change), and the
    assigned ``acl_labels``. No pack body is echoed.
    """

    review: ReviewEventView
    name: str
    version: int
    content_hash: str
    acl_labels: list[str]


# --- ontology + verified answers (NE-135) ---------------------------------
#
# NO ``ne.brain_ontology`` import in this module: the routes construct these views
# field-by-field from the library value objects (the ChunkView C7 discipline). The
# violation views deliberately carry counts/ids/hashes ONLY — never ``delta_nt`` or
# any violation message string (C12 redaction). NO request model exposes a raw-SPARQL
# or ``query`` field (the ontology reads ride a fixed module-constant query only).


class OntologyClassView(BaseModel):
    """One ontology class in the BFO/CCO/neop browser tree (iri + label + parents)."""

    iri: str
    label: str | None
    parents: tuple[str, ...]
    source: str


class OntologyClassesResponse(BaseModel):
    """GET /v1/ontology/classes response — the class tree + sources + shapes hash."""

    classes: tuple[OntologyClassView, ...]
    total: int
    sources: dict[str, str]
    shapes_hash: str


class OntologyShapeView(BaseModel):
    """One compiled SHACL node shape on the wire (iri + target class + scope)."""

    iri: str
    target_class: str | None
    scope: str


class OntologyShapesResponse(BaseModel):
    """GET /v1/ontology/shapes response — the compiled shape list + shapes hash."""

    shapes: tuple[OntologyShapeView, ...]
    shapes_hash: str


class OntologyViolationView(BaseModel):
    """One quarantine-lane entry on the wire — REDACTED (counts/ids/hashes only).

    STRUCTURALLY omits ``delta_nt`` and every violation message string (C12): the
    console sees the batch id / graph / shapes hash / violation COUNT /
    quarantined-whole flag / provenance — never the triple data or the SHACL messages.
    """

    quarantine_id: str
    created_at: str
    graph_iri: str | None
    shapes_hash: str
    violation_count: int
    quarantined_whole: bool
    provenance: str


class OntologyViolationsResponse(BaseModel):
    """GET /v1/ontology/violations response — honest ``configured=false`` when unset."""

    configured: bool
    count: int
    entries: tuple[OntologyViolationView, ...]


class VerifiedTemplateView(BaseModel):
    """One registered verified-answer template on the wire.

    ``title`` is the template's ``description`` (the registry has no separate title
    field — DECIDED, NE-135); ``args_schema`` is the generated strict args JSON Schema.
    ``citation_var`` names the machine-identifier column (the citation anchor);
    ``label_var`` names the human-readable label column, or ``None`` when the
    template projects no label (renderers then degrade to the identifier). Both
    are the template's OWN declarations, echoed — never inferred here.
    """

    template_id: str
    version: int
    title: str
    tool_name: str
    args_schema: dict[str, Any]
    columns: tuple[str, ...]
    citation_var: str
    label_var: str | None
    max_rows: int | None
    #: The template's own ``[scope] graph_classes`` declaration, echoed verbatim —
    #: the ACL-labelled graph families this query may read (the caller's held labels
    #: intersect these at execution; an empty intersection refuses). Published so the
    #: console can state WHICH governed graphs a verified query reads instead of
    #: leaving that scoping invisible. Schema, never data (C12).
    graph_classes: tuple[str, ...] = ()


class VerifiedTemplatesResponse(BaseModel):
    """GET /v1/verified/templates response — ``enabled=false`` when verified is off."""

    enabled: bool
    registry_hash: str | None
    templates: tuple[VerifiedTemplateView, ...]


class VerifiedInvokeRequest(BaseModel):
    """POST /v1/verified/invoke body — a template id (+ optional version) + typed args.

    ``extra='forbid'``: a smuggled ``sparql``/``query``/``principal`` key is a LOUD
    422, never a silently-ignored bypass. ``args`` are the template's declared typed
    args (validated fail-closed by the generated model inside the governed executor).
    """

    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1, max_length=200)
    version: int | None = None
    args: dict[str, Any] = Field(default_factory=dict)


class VerifiedAnswerView(BaseModel):
    """The deterministic verified-answer table on the wire (built field-by-field)."""

    template_id: str
    template_version: int
    args: dict[str, Any]
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    citations: tuple[str, ...]
    truncated: bool
    row_limit: int
    #: NE-CTXB-5 envelope (§4.1), mirrored from ``VerifiedAnswer``: the shown-row
    #: count (== len(rows)); the trusted total (== returned_rows when untruncated,
    #: else None until NE-CTXB-7's COUNT); and the truncation reason class.
    returned_rows: int
    total_rows: int | None
    truncation_reason: Literal["rows", "tokens"] | None
    config_hash: str
    ontology_version: str
    executed_at: datetime


class VerifiedInvokeResponse(BaseModel):
    """POST /v1/verified/invoke response — the answer + the replay-diff verdict."""

    answer: VerifiedAnswerView
    replay_match: bool
