# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exact-join, current-authority projection for Agent Run evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NamedTuple, Protocol, cast

import sqlalchemy as sa
from anyio import to_thread

from ne.api.evidence_wire import (
    ClaimItemView,
    EvidenceEnvelope,
    EvidenceItemView,
    EvidenceLocatorView,
    GraphEntityRefView,
    GraphHopView,
    RunOutputView,
    ServedDocumentVersionView,
)
from ne.api.governance_projection import (
    ExactGovernanceJoins,
    PersistedAuditReceipt,
    project_governance,
)
from ne.contracts.source_authority import (
    ProtectedClaimSliceRef,
    ProtectedSourceOutputRef,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    VerifiedEmptyResultReceipt,
    VerifiedRowContributorReceipt,
)
from ne.contracts.types import Principal
from ne.runtime.run_store import (
    AgentRunStore,
    EvidenceProjectionUnavailable,
    StoredSourcePayload,
    ne_agent_claim_verifier_receipts,
    ne_agent_delivery_receipts,
    ne_agent_evidence_locators,
    ne_agent_evidence_records,
    ne_agent_run_audit_receipts,
    ne_agent_run_claim_evidence,
    ne_agent_run_claims,
    ne_agent_run_events,
    ne_agent_run_finalizations,
    ne_agent_runs,
)
from ne.runtime.source_results import (
    EvidenceProjectionPayloadReader,
    MissingAuthenticatedContextError,
    RunStoreSourceBoundResultResolver,
    SourceStampEvaluator,
    require_authenticated_context,
)

ProjectionDecision = Literal["authorized", "withheld", "unknown"]
RunStatus = Literal[
    "submitted",
    "queued",
    "running",
    "waiting_approval",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
    "expired",
    "context_exhausted",
    "budget_exhausted",
]


class EvidenceResourceNotFound(LookupError):
    """One outcome for an invisible Agent, foreign Run, and random identifier."""

    def __init__(self) -> None:
        super().__init__("evidence resource not found")


@dataclass(frozen=True, slots=True)
class MaterializedDisclosure:
    """Ephemeral result of a fresh current-authority resolution."""

    decision: ProjectionDecision
    payload: bytes | None = None

    def __post_init__(self) -> None:
        if (self.decision == "authorized") != (self.payload is not None):
            raise ValueError("only an authorized disclosure may carry bytes")


@dataclass(frozen=True, slots=True)
class PersistedEvidence:
    evidence_id: str
    run_id: str
    owner_principal_id: str
    attempt_id: str
    step_id: str
    tool_call_id: str
    read_ordinal: int
    locator_status: Literal["located", "evidence_locator_unavailable"]
    locator_id: str | None
    locator_hash: str | None
    pending_result_id: str
    producing_event_id: str
    content_hash: str
    assurance: Literal["direct", "inferred", "extracted", "unavailable"]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PersistedAssociation:
    association_id: str
    run_id: str
    claim_id: str
    evidence_id: str
    locator_hash: str
    served_citation_ref_hash: str
    producing_event_id: str
    delivery_receipt_id: str | None
    association_hash: str


@dataclass(frozen=True, slots=True)
class PersistedVerifier:
    run_id: str
    claim_key: str
    claim_content_hash: str
    served_output_hash: str
    span_start_byte: int
    span_end_byte: int
    event_id: str
    receipt_hash: str


@dataclass(frozen=True, slots=True)
class PersistedClaim:
    claim_id: str
    run_id: str
    claim_key: str
    claim_index: int
    claim_verdict: Literal["verified", "grounded", "unavailable"]
    display_state: Literal["protected_source_free_slice", "protected_source_slice"]
    protected_ref: str
    served_output_hash: str
    span_start_byte: int
    span_end_byte: int
    claim_content_hash: str
    association_status: Literal["associated", "association_unavailable"]
    association: PersistedAssociation | None
    verifier: PersistedVerifier | None


@dataclass(frozen=True, slots=True)
class PersistedRunEvidence:
    run_id: str
    owner_principal_id: str
    agent_id: str
    agent_version: int
    status: RunStatus
    config_hash: str
    capability_snapshot_hash: str
    output_verdict: Literal["verified", "grounded", "refused", "unavailable"] | None
    mapping_state: Literal["mapping_pending", "complete", "partial", "unavailable"]
    protected_output_kind: Literal[
        "protected_turn_content", "protected_source_output", "unavailable"
    ]
    protected_output_ref: str | None
    protected_output_hash: str | None
    event_ids: frozenset[str]
    delivery_receipt_ids: frozenset[str]
    evidence: tuple[PersistedEvidence, ...]
    claims: tuple[PersistedClaim, ...]
    audit_receipts: tuple[PersistedAuditReceipt, ...]
    governance_joins: ExactGovernanceJoins | None = None


class AgentVisibility(Protocol):
    """Principal-visible catalog check performed before any Run lookup."""

    def __call__(self, agent_id: str, principal: Principal) -> bool: ...


class EvidenceProjectionReader(Protocol):
    async def load_exact(
        self, *, agent_id: str, run_id: str, owner_principal_id: str
    ) -> PersistedRunEvidence | None: ...


class CurrentEvidenceMaterializer(Protocol):
    """Read-only sixth-target resolver; every call performs a fresh authority check.

    ``resolve_locator`` must authenticate the retained immutable locator/artifact and
    independently authorize every contributor and graph hop. ``resolve_output`` must
    target the exact Run/output row. Neither method may write or backfill durable state.
    """

    async def resolve_locator(
        self,
        evidence: PersistedEvidence,
        *,
        principal_id: str,
        access_context_hash: str,
        projection_attempt_id: str,
    ) -> MaterializedDisclosure: ...

    async def resolve_output(
        self,
        run: PersistedRunEvidence,
        *,
        principal_id: str,
        access_context_hash: str,
        projection_attempt_id: str,
    ) -> MaterializedDisclosure: ...

    async def resolve_claim(
        self,
        run: PersistedRunEvidence,
        claim: PersistedClaim,
        *,
        principal_id: str,
        access_context_hash: str,
        projection_attempt_id: str,
    ) -> MaterializedDisclosure: ...


@dataclass(frozen=True, slots=True)
class _BoundProjectionPayloadReader(EvidenceProjectionPayloadReader):
    """One-call reader binding one typed reference to one already exact store read."""

    reference: ProtectedSourceOutputRef | ProtectedClaimSliceRef
    owner_principal_id: str
    run_id: str
    stored: StoredSourcePayload

    async def load_for_evidence_projection(
        self,
        reference: ProtectedSourceOutputRef | ProtectedClaimSliceRef,
        *,
        owner_principal_id: str,
        run_id: str,
    ) -> StoredSourcePayload:
        if not (
            reference == self.reference
            and hmac.compare_digest(owner_principal_id, self.owner_principal_id)
            and hmac.compare_digest(run_id, self.run_id)
        ):
            raise LookupError("evidence projection payload unavailable")
        return self.stored


class RunStoreCurrentEvidenceMaterializer:
    """Bounded bridge from canonical Run rows to the sixth resolver target."""

    def __init__(self, store: AgentRunStore, evaluator: SourceStampEvaluator) -> None:
        self._store = store
        self._evaluator = evaluator

    @staticmethod
    def _decision(
        decision: SourceAuthorityDecision, payload: bytes | None
    ) -> MaterializedDisclosure:
        if decision is SourceAuthorityDecision.AUTHORIZED and payload is not None:
            return MaterializedDisclosure("authorized", payload)
        if decision is SourceAuthorityDecision.PERMANENT_WITHHELD:
            return MaterializedDisclosure("withheld")
        return MaterializedDisclosure("unknown")

    async def _authorize_locator(
        self,
        stored: StoredSourcePayload,
        *,
        principal_id: str,
        access_context_hash: str,
    ) -> MaterializedDisclosure:
        try:
            context = require_authenticated_context()
            if not (
                hmac.compare_digest(context.principal_id, principal_id)
                and hmac.compare_digest(context.context_hash, access_context_hash)
                and stored.stamps
            ):
                return MaterializedDisclosure("unknown")
            decisions = [await self._evaluator(stamp, context) for stamp in stored.stamps]
        except (LookupError, MissingAuthenticatedContextError, ValueError):
            return MaterializedDisclosure("unknown")
        if any(item is SourceAuthorityDecision.RETRYABLE_UNKNOWN for item in decisions):
            return MaterializedDisclosure("unknown")
        if any(item is SourceAuthorityDecision.PERMANENT_WITHHELD for item in decisions):
            return MaterializedDisclosure("withheld")
        return MaterializedDisclosure("authorized", stored.plaintext)

    async def _resolve(
        self,
        reference: ProtectedSourceOutputRef | ProtectedClaimSliceRef,
        stored: StoredSourcePayload,
        *,
        principal_id: str,
        access_context_hash: str,
        run_id: str,
        projection_attempt_id: str,
    ) -> MaterializedDisclosure:
        resolver = RunStoreSourceBoundResultResolver(
            self._store,
            self._evaluator,
            evidence_projection_reader=_BoundProjectionPayloadReader(
                reference=reference,
                owner_principal_id=principal_id,
                run_id=run_id,
                stored=stored,
            ),
        )
        resolved = await resolver.for_evidence_projection(
            reference,
            principal_id=principal_id,
            access_context_hash=access_context_hash,
            run_id=run_id,
            evidence_projection_attempt_id=projection_attempt_id,
        )
        payload = (
            None if resolved.materialized_text is None else resolved.materialized_text.encode()
        )
        return self._decision(resolved.decision, payload)

    async def resolve_locator(
        self,
        evidence: PersistedEvidence,
        *,
        principal_id: str,
        access_context_hash: str,
        projection_attempt_id: str,
    ) -> MaterializedDisclosure:
        del projection_attempt_id
        if evidence.locator_id is None or evidence.locator_hash is None:
            return MaterializedDisclosure("unknown")
        try:
            stored = await self._store.load_evidence_projection_locator(
                owner_principal_id=principal_id,
                run_id=evidence.run_id,
                locator_ref=evidence.locator_id,
                expected_hash=evidence.locator_hash,
            )
        except EvidenceProjectionUnavailable:
            return MaterializedDisclosure("unknown")
        return await self._authorize_locator(
            stored,
            principal_id=principal_id,
            access_context_hash=access_context_hash,
        )

    async def resolve_output(
        self,
        run: PersistedRunEvidence,
        *,
        principal_id: str,
        access_context_hash: str,
        projection_attempt_id: str,
    ) -> MaterializedDisclosure:
        if (
            run.protected_output_kind != "protected_source_output"
            or run.protected_output_ref is None
            or run.protected_output_hash is None
        ):
            return MaterializedDisclosure("unknown")
        try:
            stored = await self._store.load_evidence_projection_output(
                owner_principal_id=principal_id,
                run_id=run.run_id,
                protected_ref=run.protected_output_ref,
                expected_hash=run.protected_output_hash,
            )
        except EvidenceProjectionUnavailable:
            return MaterializedDisclosure("unknown")
        reference = ProtectedSourceOutputRef(
            reference_id=run.protected_output_ref,
            output_hash=run.protected_output_hash,
            stamps=stored.stamps,
        )
        return await self._resolve(
            reference,
            stored,
            principal_id=principal_id,
            access_context_hash=access_context_hash,
            run_id=run.run_id,
            projection_attempt_id=projection_attempt_id,
        )

    async def resolve_claim(
        self,
        run: PersistedRunEvidence,
        claim: PersistedClaim,
        *,
        principal_id: str,
        access_context_hash: str,
        projection_attempt_id: str,
    ) -> MaterializedDisclosure:
        if claim.display_state != "protected_source_slice":
            return MaterializedDisclosure("unknown")
        try:
            stored = await self._store.load_evidence_projection_claim_slice(
                owner_principal_id=principal_id,
                run_id=run.run_id,
                protected_ref=claim.protected_ref,
                served_output_hash=claim.served_output_hash,
                span_start_byte=claim.span_start_byte,
                span_end_byte=claim.span_end_byte,
                claim_content_hash=claim.claim_content_hash,
            )
        except EvidenceProjectionUnavailable:
            return MaterializedDisclosure("unknown")
        reference = ProtectedClaimSliceRef(
            reference_id=claim.protected_ref,
            slice_hash=claim.claim_content_hash,
            stamps=stored.stamps,
        )
        return await self._resolve(
            reference,
            stored,
            principal_id=principal_id,
            access_context_hash=access_context_hash,
            run_id=run.run_id,
            projection_attempt_id=projection_attempt_id,
        )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_string(value: object, *, maximum: int = 512) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= maximum else None


def _safe_id(value: object) -> str | None:
    return _safe_string(value, maximum=256)


_GRAPH_ENTITY_FIELDS = frozenset(
    {"entity_class", "entity_key", "display", "assurance", "source_ref"}
)
_GRAPH_HOP_FIELDS = frozenset(
    {"subject", "edge_kind", "obj", "source_system", "source_ref", "assurance"}
)
_GRAPH_LOCATOR_FIELDS = frozenset({"kind", "path", "hop_source_stamps", "evidence_assurance"})


def _parse_graph_entity(raw: object) -> GraphEntityRefView:
    if not isinstance(raw, dict) or set(raw) != _GRAPH_ENTITY_FIELDS:
        raise ValueError("graph entity is malformed")
    return GraphEntityRefView.model_validate(raw)


def _parse_hop(raw: object) -> GraphHopView:
    if not isinstance(raw, dict) or set(raw) != _GRAPH_HOP_FIELDS:
        raise ValueError("graph hop is malformed")
    return GraphHopView(
        subject=_parse_graph_entity(raw["subject"]),
        edge_kind=raw["edge_kind"],
        obj=_parse_graph_entity(raw["obj"]),
        source_system=raw["source_system"],
        source_ref=raw["source_ref"],
        assurance=raw["assurance"],
        review_state=None,
    )


def _parse_graph_locator(
    raw: dict[str, object],
    *,
    served_document_versions: tuple[ServedDocumentVersionView, ...] = (),
) -> EvidenceLocatorView:
    if set(raw) != _GRAPH_LOCATOR_FIELDS:
        raise ValueError("graph locator is malformed")
    if raw["kind"] != "knowledge_graph_path" or raw["evidence_assurance"] != "inferred":
        raise ValueError("graph locator kind or assurance is invalid")
    path = raw["path"]
    if not isinstance(path, dict) or set(path) != {"hops"}:
        raise ValueError("graph path is malformed")
    hops = path["hops"]
    stamp_groups = raw["hop_source_stamps"]
    if (
        not isinstance(hops, list | tuple)
        or not 1 <= len(hops) <= 32
        or not isinstance(stamp_groups, list | tuple)
        or len(stamp_groups) != len(hops)
        or not all(
            isinstance(group, list | tuple)
            and bool(group)
            and all(isinstance(stamp, dict) and bool(stamp) for stamp in group)
            for group in stamp_groups
        )
    ):
        raise ValueError("graph path contributors are malformed")
    for group in stamp_groups:
        for stamp in group:
            typed_stamp = SourceAuthorityStamp.model_validate(stamp)
            if typed_stamp.model_dump(mode="json") != stamp:
                raise ValueError("graph path contributor is not canonical")
    return EvidenceLocatorView(
        graph_path=tuple(_parse_hop(item) for item in hops),
        served_document_versions=served_document_versions,
    )


def _verified_row_anchor(receipt: VerifiedRowContributorReceipt) -> str:
    material = b"ne.verified-evidence-row-anchor.v1\x00" + receipt.receipt_hash.encode("ascii")
    return _sha256(material)


def _parse_verified_t3_locator(
    raw: dict[str, object],
    *,
    served_document_versions: tuple[ServedDocumentVersionView, ...] = (),
) -> EvidenceLocatorView:
    expected_fields = {
        "kind",
        "row_anchor",
        "receipt",
        "contributor_index",
        "contributor",
        "evidence_assurance",
    }
    if set(raw) != expected_fields or raw["kind"] != "verified_t3":
        raise ValueError("verified T3 locator is malformed")
    if raw["evidence_assurance"] != "direct":
        raise ValueError("verified T3 locator assurance is invalid")
    try:
        receipt = VerifiedRowContributorReceipt.model_validate(raw["receipt"])
        contributor = SourceAuthorityStamp.model_validate(raw["contributor"])
    except ValueError as exc:
        raise ValueError("verified T3 locator receipt is invalid") from exc
    if (
        receipt.model_dump(mode="json") != raw["receipt"]
        or contributor.model_dump(mode="json") != raw["contributor"]
    ):
        raise ValueError("verified T3 locator receipt is not canonical")
    index = raw["contributor_index"]
    if (
        not isinstance(index, int)
        or isinstance(index, bool)
        or not 0 <= index < len(receipt.contributors)
        or contributor != receipt.contributors[index]
    ):
        raise ValueError("verified T3 locator contributor is invalid")
    row_anchor = raw["row_anchor"]
    if not isinstance(row_anchor, str) or not hmac.compare_digest(
        row_anchor, _verified_row_anchor(receipt)
    ):
        raise ValueError("verified T3 locator row anchor is invalid")
    return EvidenceLocatorView(
        kind="verified_t3",
        row_anchor=row_anchor,
        receipt_hash=receipt.receipt_hash,
        served_document_versions=served_document_versions,
    )


def _parse_verified_t3_empty_locator(raw: dict[str, object]) -> EvidenceLocatorView:
    if set(raw) != {"kind", "receipt", "evidence_assurance"} or raw["kind"] != (
        "verified_t3_empty"
    ):
        raise ValueError("verified empty T3 locator is malformed")
    if raw["evidence_assurance"] != "direct":
        raise ValueError("verified empty T3 locator assurance is invalid")
    try:
        receipt = VerifiedEmptyResultReceipt.model_validate(raw["receipt"])
    except ValueError as exc:
        raise ValueError("verified empty T3 locator receipt is invalid") from exc
    if receipt.model_dump(mode="json") != raw["receipt"]:
        raise ValueError("verified empty T3 locator receipt is not canonical")
    return EvidenceLocatorView(
        kind="verified_t3_empty",
        receipt_hash=receipt.receipt_hash,
    )


_SEARCH_LOCATOR_FIELDS = frozenset({"kind", "contributors", "chunk_ids", "evidence_assurance"})

_MAX_SERVED_VERSION_KEYS = 128


class ServedVersionKey(NamedTuple):
    """The immutable identity a contributor stamp resolves a version through."""

    source_kind: str
    source_snapshot_hash: str
    acl_snapshot_hash: str


def _stamp_artifact_key(stamp: object) -> ServedVersionKey | None:
    """The version-resolving identity of one retained contributor stamp.

    Reads ONLY the immutable triple the artifact identity already carries. The
    receipt id is deliberately NOT used: `bind_source_authority` keeps one row
    per `(document_id, version_seq)` and REBINDS it on every re-admission, so a
    receipt id stops resolving as soon as the same version is re-admitted, while
    the content/ACL snapshot triple never rebinds.
    """

    if not isinstance(stamp, dict):
        return None
    identity = stamp.get("source_identity")
    artifact = stamp.get("artifact_identity")
    if not isinstance(identity, dict) or not isinstance(artifact, dict):
        return None
    source_kind = identity.get("source_kind")
    snapshot_hash = artifact.get("source_snapshot_hash")
    acl_hash = artifact.get("acl_snapshot_hash")
    if (
        not isinstance(source_kind, str)
        or not 0 < len(source_kind) <= 128
        or not isinstance(snapshot_hash, str)
        or len(snapshot_hash) != 64
        or not isinstance(acl_hash, str)
        or len(acl_hash) != 64
    ):
        return None
    return ServedVersionKey(source_kind, snapshot_hash, acl_hash)


def _stamp_artifact_keys(stamps: object) -> tuple[ServedVersionKey, ...] | None:
    if not isinstance(stamps, list) or len(stamps) > _MAX_SERVED_VERSION_KEYS:
        return None
    keys: list[ServedVersionKey] = []
    for stamp in stamps:
        key = _stamp_artifact_key(stamp)
        if key is None:
            return None
        keys.append(key)
    return tuple(keys)


def _locator_artifact_keys(payload: bytes) -> tuple[ServedVersionKey, ...]:
    """Version-resolving artifact keys of a locator payload, else `()`.

    Extraction only, no validation beyond shape safety, because the keys feed a
    server-side lineage lookup whose result is projected instead of the stamps
    themselves. `_parse_locator` remains the one validator; a payload this
    helper reads keys from and that parser then refuses projects nothing anyway.

    THREE locator kinds retain contributor stamps and therefore resolve:
    `knowledge_search` (its `contributors`), `knowledge_graph_path` (every hop's
    `hop_source_stamps`) and `verified_t3` (the row receipt's own
    `contributors`). Before this, only the search arm did, and the live box has
    served ZERO search reads since the train-63 deploy against 62 graph reads
    and 21 verified reads -- so in practice NOTHING carried a version.

    A verified read on a GOVERNED-LOCAL T3 head resolves to nothing, and is
    expected to: those stamps name a `governed_local_t3` source identity, not a
    document snapshot, so the document lineage store has no row for them
    (measured 2026-08-28 on the live box: 17,752 such receipts, zero document
    bindings). Absence renders as absence, exactly as for an unknown key.
    """

    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, dict):
        return ()
    kind = raw.get("kind")
    keys: tuple[ServedVersionKey, ...] | None
    if kind == "knowledge_search":
        keys = _stamp_artifact_keys(raw.get("contributors"))
    elif kind == "knowledge_graph_path":
        groups = raw.get("hop_source_stamps")
        if not isinstance(groups, list) or len(groups) > 32:
            return ()
        collected: list[ServedVersionKey] = []
        for group in groups:
            hop_keys = _stamp_artifact_keys(group)
            if hop_keys is None:
                return ()
            collected.extend(hop_keys)
        keys = tuple(collected)
    elif kind == "verified_t3":
        receipt = raw.get("receipt")
        if not isinstance(receipt, dict):
            return ()
        keys = _stamp_artifact_keys(receipt.get("contributors"))
    else:
        return ()
    if keys is None:
        return ()
    return tuple(dict.fromkeys(keys))[:_MAX_SERVED_VERSION_KEYS]


def _parse_search_locator(
    raw: dict[str, object],
    *,
    served_document_versions: tuple[ServedDocumentVersionView, ...] = (),
) -> EvidenceLocatorView:
    """Parse the locator `ne.api.tools` writes for every knowledge SEARCH read.

    THIS ARM IS WHY THE DEMO'S CITATIONS WERE BLANK. The search tool has always
    persisted `{"kind": "knowledge_search", "contributors": [...], "chunk_ids":
    [...]}` (tools.py, the `evidence.author.claim(read_kind="search", ...)`
    call), but this parser had no arm for it: the generic fallthrough extracts
    none of those fields, so `EvidenceLocatorView` refused the empty view
    ("evidence locator requires one projectable field"), the record projected
    `locator_unavailable`, and every claim anchored to a search read -- which on
    the live corpus is EVERY claim -- collapsed to "Claim mapping unavailable"
    even though its association, verifier, spans and hashes were all valid.

    What is projected is ONLY the chunk-id set: the same opaque ids the reader
    already received in the served rendering, which the console resolves through
    its own authorized retrieval. The contributor stamps are validated (they
    must round-trip canonically, exactly as the verified-T3 arm requires) but
    NEVER projected -- source identity and ACL material stay server-side (C12).

    ``served_document_versions`` is the one stamp-DERIVED projection, and it is
    no longer this arm's alone: the caller resolves each contributor stamp's
    ``(source_kind, source_snapshot_hash, acl_snapshot_hash)`` artifact identity
    through the document lineage store into the exact ``(document_id,
    version_seq)`` that snapshot was admitted as, and hands the resolved views
    in here -- for search, graph-path and verified-row locators alike (see
    :func:`_locator_artifact_keys`).  Version number, content hash and display
    name of a version this evidence was disclosed for are Documents-workspace
    material, not stamp material; receipt ids, snapshot hashes and ACL fields
    still never cross the wire.
    """

    if set(raw) != _SEARCH_LOCATOR_FIELDS or raw["kind"] != "knowledge_search":
        raise ValueError("knowledge search locator is malformed")
    if raw["evidence_assurance"] != "direct":
        raise ValueError("knowledge search locator assurance is invalid")
    contributors = raw["contributors"]
    if not isinstance(contributors, list) or not 1 <= len(contributors) <= 128:
        raise ValueError("knowledge search locator contributors are invalid")
    for item in contributors:
        try:
            stamp = SourceAuthorityStamp.model_validate(item)
        except ValueError as exc:
            raise ValueError("knowledge search locator contributor is invalid") from exc
        if stamp.model_dump(mode="json") != item:
            raise ValueError("knowledge search locator contributor is not canonical")
    chunks = raw["chunk_ids"]
    if (
        not isinstance(chunks, list)
        or not 1 <= len(chunks) <= 128
        or any(not isinstance(chunk, str) or not 0 < len(chunk) <= 256 for chunk in chunks)
    ):
        raise ValueError("knowledge search locator chunks are invalid")
    return EvidenceLocatorView(
        kind="knowledge_search",
        chunk_ids=tuple(chunks),
        served_document_versions=served_document_versions,
    )


def _parse_locator(
    payload: bytes,
    *,
    expected_assurance: Literal["direct", "inferred", "extracted", "unavailable"],
    served_document_versions: tuple[ServedDocumentVersionView, ...] = (),
) -> EvidenceLocatorView:
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("evidence locator is invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError("evidence locator is not an object")
    supplied_assurance = raw.get("evidence_assurance")
    if supplied_assurance is not None and supplied_assurance != expected_assurance:
        raise ValueError("evidence locator assurance does not match its record")
    if raw.get("kind") == "knowledge_graph_path":
        return _parse_graph_locator(raw, served_document_versions=served_document_versions)
    if raw.get("kind") == "knowledge_search":
        return _parse_search_locator(raw, served_document_versions=served_document_versions)
    if raw.get("kind") == "verified_t3":
        return _parse_verified_t3_locator(raw, served_document_versions=served_document_versions)
    if raw.get("kind") == "verified_t3_empty":
        return _parse_verified_t3_empty_locator(raw)
    graph_raw = raw.get("graph_path", ())
    if not isinstance(graph_raw, list | tuple) or len(graph_raw) > 32:
        raise ValueError("evidence graph path is invalid")
    page = raw.get("page")
    if page is not None and (not isinstance(page, int) or isinstance(page, bool)):
        raise ValueError("evidence page is invalid")
    content_hash = _safe_string(raw.get("content_hash"), maximum=64)
    if content_hash is not None and (
        len(content_hash) != 64 or any(char not in "0123456789abcdef" for char in content_hash)
    ):
        raise ValueError("evidence content hash is invalid")
    return EvidenceLocatorView(
        source_system=_safe_string(raw.get("source_system")),
        title=_safe_string(raw.get("title")),
        source_revision=_safe_string(raw.get("source_revision") or raw.get("revision")),
        as_of=_safe_string(raw.get("as_of")),
        content_hash=content_hash,
        chunk_id=_safe_id(raw.get("chunk_id")),
        page=page,
        row_id=_safe_id(raw.get("row_id")),
        entity_id=_safe_id(raw.get("entity_id")),
        graph_path=tuple(_parse_hop(item) for item in graph_raw),
    )


def _valid_association(
    run: PersistedRunEvidence,
    claim: PersistedClaim,
    evidence_by_id: dict[str, PersistedEvidence],
) -> tuple[PersistedAssociation, PersistedEvidence] | None:
    association = claim.association
    if claim.association_status != "associated" or association is None:
        return None
    evidence = evidence_by_id.get(association.evidence_id)
    if evidence is None:
        return None
    if not (
        hmac.compare_digest(association.run_id, run.run_id)
        and hmac.compare_digest(claim.run_id, run.run_id)
        and hmac.compare_digest(association.claim_id, claim.claim_id)
        and hmac.compare_digest(evidence.run_id, run.run_id)
        and hmac.compare_digest(evidence.owner_principal_id, run.owner_principal_id)
        and evidence.locator_hash is not None
        and hmac.compare_digest(association.locator_hash, evidence.locator_hash)
        and hmac.compare_digest(association.producing_event_id, evidence.producing_event_id)
    ):
        return None
    # Source-delivered claim checks require the exact persisted model-delivery
    # receipt. A trace id or adjacent timestamp can never substitute for this id.
    if claim.display_state == "protected_source_slice" and (
        association.delivery_receipt_id is None
        or association.delivery_receipt_id not in run.delivery_receipt_ids
    ):
        return None
    return association, evidence


def _valid_verifier(run: PersistedRunEvidence, claim: PersistedClaim) -> PersistedVerifier | None:
    verifier = claim.verifier
    if claim.claim_verdict != "verified":
        return None
    if verifier is None:
        return None
    if not (
        hmac.compare_digest(verifier.run_id, run.run_id)
        and hmac.compare_digest(verifier.claim_key, claim.claim_key)
        and hmac.compare_digest(verifier.claim_content_hash, claim.claim_content_hash)
        and hmac.compare_digest(verifier.served_output_hash, claim.served_output_hash)
        and verifier.span_start_byte == claim.span_start_byte
        and verifier.span_end_byte == claim.span_end_byte
        and verifier.event_id in run.event_ids
    ):
        return None
    return verifier


# WHICH SENTENCE EACH FAILED GATE PRODUCES. One row per distinct cause; the
# strings are members of `ClaimItemView.message`'s closed Literal, so a new
# cause cannot ship without its own wire sentence. Every sentence is
# content-free (C12) and describes only this owner's own run rows -- it can
# never become an existence oracle because the same caller already sees the
# evidence list, association ids and verifier ids in this same envelope.
# "withheld" states are reserved for actual materializer DECISIONS (the claim's
# own, or its evidence record's); every absence or integrity failure is
# "unavailable".
_CLAIM_CAUSE_VIEWS: dict[
    str,
    tuple[
        Literal["available", "withheld", "unavailable"],
        Literal[
            "Claim withheld — access changed",
            "Claim evidence withheld — access changed",
            "Claim not associated with evidence",
            "Claim verifier receipt unavailable",
            "Claim evidence locator unresolved",
            "Claim content hash mismatch",
            "Claim span mismatch",
            "Claim unavailable — answer not disclosed",
            "Claim mapping unavailable",
        ],
    ],
] = {
    "answer": ("unavailable", "Claim unavailable — answer not disclosed"),
    "association": ("unavailable", "Claim not associated with evidence"),
    "verifier": ("unavailable", "Claim verifier receipt unavailable"),
    "evidence_withheld": ("withheld", "Claim evidence withheld — access changed"),
    "evidence_locator": ("unavailable", "Claim evidence locator unresolved"),
    "refused": ("withheld", "Claim withheld — access changed"),
    "hash": ("unavailable", "Claim content hash mismatch"),
    "span": ("unavailable", "Claim span mismatch"),
    "mapping": ("unavailable", "Claim mapping unavailable"),
}


def _slice_claim_cause(output: bytes, claim: PersistedClaim) -> Literal["hash", "span"]:
    """Name WHY `_slice_claim` returned None: bad span geometry or bad content.

    Split so the source-free arm reports the same two integrity causes the
    protected arm reports, instead of collapsing both into one word. Bounds and
    code-point-boundary failures are SPAN facts; a well-placed span whose bytes
    do not hash to `claim_content_hash` is a CONTENT fact.
    """

    start, end = claim.span_start_byte, claim.span_end_byte
    if start < 0 or end <= start or end > len(output):
        return "span"
    if not hmac.compare_digest(_sha256(output[start:end]), claim.claim_content_hash):
        return "hash"
    return "span"


def _slice_claim(output: bytes, claim: PersistedClaim) -> str | None:
    start, end = claim.span_start_byte, claim.span_end_byte
    if start < 0 or end <= start or end > len(output):
        return None
    selected = output[start:end]
    if not hmac.compare_digest(_sha256(selected), claim.claim_content_hash):
        return None
    try:
        prefix = output[:start].decode("utf-8")
        display = selected.decode("utf-8")
        output[:end].decode("utf-8")
    except UnicodeDecodeError:
        return None
    # The prefix decode is deliberately retained: it proves the start offset is a
    # code-point boundary rather than merely that the selected bytes happen to decode.
    del prefix
    return display


@dataclass(frozen=True, slots=True)
class DisclosedRunOutput:
    """One Run's terminal answer, as the disclosure decision plus its bytes.

    THE STATE IS THE POINT. `output_preview: str | None` cannot tell "this Run
    produced nothing" apart from "this Run's answer exists and current source
    authority no longer permits you to read it", and the console rendered both
    as the same blank. They are different facts and get different words.
    """

    state: Literal["available", "withheld", "unavailable"]
    text: str | None = None


class SourceBoundRunOutputReader:
    """The SOURCE-DERIVED half of one Run's answer, reauthorized on every read.

    WHY THIS EXISTS. `run_finalizer._protect_output` picks the storage table on
    one condition -- `ne_agent_protected_source_outputs if deliveries else
    ne_agent_protected_turn_contents` -- and until now each console surface read
    exactly one of them. `run_projection._preview_target` reads only
    `protected_turn_content`; the Evidence drawer's `resolve_output` bails on
    anything that is not `protected_source_output`. Disjoint by construction, so
    an implementation detail (which table the bytes landed in) decided WHICH
    SCREEN the reader had to go find their answer on -- and the better answer,
    the one that actually read source records, was the hidden one.

    THE STORAGE SPLIT IS LEGITIMATE AND STAYS. Source-derived bytes carry
    `SourceAuthorityStamp`s and must be re-checked against CURRENT source
    authority before disclosure; source-free bytes have no stamps and nothing to
    re-check. This class does not weaken that by one step: it performs the exact
    same `resolve_output` call, with the same principal/context/attempt triple,
    and the same served-hash equality check, that `EvidenceProjector` performs.
    What changes is only WHERE the answer may be READ, never WHO may read it or
    ON WHAT TERMS.

    Deliberately NOT used by any list projection. A page enriches up to 25 rows;
    driving 25 source-authority reauthorizations off one list read is its own
    decision with its own review, and is not taken here.
    """

    def __init__(
        self,
        *,
        visible_agent: AgentVisibility,
        reader: EvidenceProjectionReader,
        materializer: CurrentEvidenceMaterializer,
    ) -> None:
        self._visible_agent = visible_agent
        self._reader = reader
        self._materializer = materializer

    async def disclose(
        self,
        *,
        agent_id: str,
        run_id: str,
        principal: Principal,
        access_context_hash: str,
        projection_attempt_id: str,
    ) -> DisclosedRunOutput:
        # Visibility first, exactly as the evidence projection orders it: a known
        # Run id may not be used to learn that a hidden Agent exists.
        if not self._visible_agent(agent_id, principal):
            return DisclosedRunOutput("unavailable")
        run = await self._reader.load_exact(
            agent_id=agent_id, run_id=run_id, owner_principal_id=principal.id
        )
        if run is None:
            return DisclosedRunOutput("unavailable")
        # This reader owns the source-bound store ONLY. The source-free store is
        # `run_projection._preview_target`'s, is already served, and has no
        # stamps to reauthorize -- routing it through here would claim a
        # reauthorization that did not happen.
        if (
            run.protected_output_kind != "protected_source_output"
            or run.protected_output_ref is None
            or run.protected_output_hash is None
        ):
            return DisclosedRunOutput("unavailable")
        disclosed = await self._materializer.resolve_output(
            run,
            principal_id=principal.id,
            access_context_hash=access_context_hash,
            projection_attempt_id=projection_attempt_id,
        )
        if disclosed.decision == "authorized" and disclosed.payload is not None:
            if not hmac.compare_digest(_sha256(disclosed.payload), run.protected_output_hash):
                return DisclosedRunOutput("unavailable")
            try:
                return DisclosedRunOutput("available", disclosed.payload.decode("utf-8"))
            except UnicodeDecodeError:
                return DisclosedRunOutput("unavailable")
        if disclosed.decision == "withheld":
            # A DECISION, not an absence. The answer exists; current source
            # authority says this reader may no longer see it, and the thread
            # says exactly that rather than rendering an empty turn.
            return DisclosedRunOutput("withheld")
        return DisclosedRunOutput("unavailable")


class ServedVersionResolver(Protocol):
    """Resolve retained contributor-stamp artifact keys to document versions.

    Composition hands the projector this seam over the document lineage store;
    an unknown artifact key simply does not appear in the result.  ``None`` (no
    resolver composed) and an empty resolution both project the honest empty
    tuple — never a guessed "current" version.
    """

    async def __call__(
        self, keys: tuple[ServedVersionKey, ...]
    ) -> tuple[ServedDocumentVersionView, ...]: ...


class EvidenceProjector:
    MAX_ITEMS = 256

    def __init__(
        self,
        *,
        visible_agent: AgentVisibility,
        reader: EvidenceProjectionReader,
        materializer: CurrentEvidenceMaterializer,
        served_version_resolver: ServedVersionResolver | None = None,
    ) -> None:
        self._visible_agent = visible_agent
        self._reader = reader
        self._materializer = materializer
        self._served_version_resolver = served_version_resolver

    async def _served_document_versions(
        self, payload: bytes
    ) -> tuple[ServedDocumentVersionView, ...]:
        """The version binding for a locator payload; ``()`` when unknown.

        Resolution failure degrades to absence, never to a fabricated binding
        and never to a failed projection: the locator still projects its chunk
        set, graph path or row anchor, exactly as before this field existed.
        """
        if self._served_version_resolver is None:
            return ()
        keys = _locator_artifact_keys(payload)
        if not keys:
            return ()
        try:
            return await self._served_version_resolver(keys)
        except Exception:
            return ()

    async def project(
        self,
        *,
        agent_id: str,
        run_id: str,
        principal: Principal,
        access_context_hash: str,
        projection_attempt_id: str,
    ) -> EvidenceEnvelope:
        # Agent visibility is deliberately first. A caller cannot use a known Run id
        # to learn that a hidden Agent exists.
        if not self._visible_agent(agent_id, principal):
            raise EvidenceResourceNotFound
        principal_id = principal.id
        run = await self._reader.load_exact(
            agent_id=agent_id, run_id=run_id, owner_principal_id=principal_id
        )
        if run is None:
            raise EvidenceResourceNotFound

        evidence_rows = tuple(
            sorted(run.evidence, key=lambda item: (item.read_ordinal, item.evidence_id))
        )
        claim_rows = tuple(sorted(run.claims, key=lambda item: (item.claim_index, item.claim_id)))
        if len(evidence_rows) > self.MAX_ITEMS or len(claim_rows) > self.MAX_ITEMS:
            raise RuntimeError("bounded evidence projection unavailable")

        evidence_views: list[EvidenceItemView] = []
        evidence_available: dict[str, bool] = {}
        for evidence in evidence_rows:
            exact_mapping = hmac.compare_digest(
                evidence.run_id, run.run_id
            ) and hmac.compare_digest(evidence.owner_principal_id, run.owner_principal_id)
            if (
                not exact_mapping
                or evidence.locator_status != "located"
                or evidence.locator_id is None
                or evidence.locator_hash is None
            ):
                evidence_views.append(
                    EvidenceItemView(
                        evidence_id=evidence.evidence_id,
                        state="locator_unavailable",
                        message="Evidence locator unavailable",
                        assurance="unavailable",
                        producing_event_id=evidence.producing_event_id,
                        produced_at=_utc(evidence.created_at),
                    )
                )
                evidence_available[evidence.evidence_id] = False
                continue
            disclosure = await self._materializer.resolve_locator(
                evidence,
                principal_id=principal_id,
                access_context_hash=access_context_hash,
                projection_attempt_id=projection_attempt_id,
            )
            locator: EvidenceLocatorView | None = None
            locator_invalid = False
            if disclosure.decision == "authorized" and disclosure.payload is not None:
                if hmac.compare_digest(_sha256(disclosure.payload), evidence.locator_hash):
                    try:
                        locator = _parse_locator(
                            disclosure.payload,
                            expected_assurance=evidence.assurance,
                            served_document_versions=await self._served_document_versions(
                                disclosure.payload
                            ),
                        )
                    except ValueError:
                        locator_invalid = True
                else:
                    locator_invalid = True
            if locator_invalid:
                evidence_views.append(
                    EvidenceItemView(
                        evidence_id=evidence.evidence_id,
                        state="locator_unavailable",
                        message="Evidence locator unavailable",
                        assurance="unavailable",
                        producing_event_id=evidence.producing_event_id,
                        produced_at=_utc(evidence.created_at),
                    )
                )
                evidence_available[evidence.evidence_id] = False
            elif locator is None:
                evidence_views.append(
                    EvidenceItemView(
                        evidence_id=evidence.evidence_id,
                        state="withheld",
                        message="Evidence withheld — access changed",
                        assurance="unavailable",
                        producing_event_id=evidence.producing_event_id,
                        produced_at=_utc(evidence.created_at),
                        locator_hash=evidence.locator_hash,
                    )
                )
                evidence_available[evidence.evidence_id] = False
            else:
                evidence_views.append(
                    EvidenceItemView(
                        evidence_id=evidence.evidence_id,
                        state="available",
                        message="Evidence available",
                        assurance=evidence.assurance,
                        producing_event_id=evidence.producing_event_id,
                        produced_at=_utc(evidence.created_at),
                        locator_hash=evidence.locator_hash,
                        locator=locator,
                    )
                )
                evidence_available[evidence.evidence_id] = True

        output_bytes: bytes | None = None
        output_state: Literal["available", "withheld", "unavailable"] = "unavailable"
        if run.protected_output_ref is not None and run.protected_output_hash is not None:
            disclosed = await self._materializer.resolve_output(
                run,
                principal_id=principal_id,
                access_context_hash=access_context_hash,
                projection_attempt_id=projection_attempt_id,
            )
            if disclosed.decision == "authorized" and disclosed.payload is not None:
                if hmac.compare_digest(_sha256(disclosed.payload), run.protected_output_hash):
                    output_bytes = disclosed.payload
                    output_state = "available"
            elif disclosed.decision == "withheld":
                # A DECISION, AND ONLY A DECISION.
                #
                # This branch used to read `elif run.protected_output_kind ==
                # "protected_source_output"`, which is a fact about WHERE the
                # bytes are stored, not about what the materializer decided. So
                # `unknown` -- a resolution FAILURE, the retryable one -- was
                # reported as "Answer withheld — access changed": an access
                # decision that had never been made. It sent the investigation
                # of a total product outage looking for a permissions change for
                # a day, and there had never been one.
                #
                # The materializer is three-valued and is now read as three
                # values. `unknown` falls through to the initialized
                # "unavailable", which is what `SourceBoundRunOutputReader`
                # (the Run-detail read of the SAME call) has always returned for
                # it -- so the two surfaces now state the same thing about the
                # same run instead of disagreeing.
                output_state = "withheld"
        try:
            output_display = None if output_bytes is None else output_bytes.decode("utf-8")
        except UnicodeDecodeError:
            output_bytes, output_display, output_state = None, None, "unavailable"
        output_message = cast(
            Literal[
                "Answer available",
                "Answer withheld — access changed",
                "Answer unavailable",
            ],
            {
                "available": "Answer available",
                "withheld": "Answer withheld — access changed",
                "unavailable": "Answer unavailable",
            }[output_state],
        )

        evidence_by_id = {item.evidence_id: item for item in evidence_rows}
        # The evidence loop above already decided, item by item, WHAT this
        # reader may know about each record. The claim loop below reuses those
        # exact states -- never a fresh guess -- so a claim's reported cause can
        # never disagree with the evidence list beside it.
        evidence_states = {view.evidence_id: view.state for view in evidence_views}
        del evidence_available  # superseded by the per-item states
        claim_views: list[ClaimItemView] = []
        for claim in claim_rows:
            exact = _valid_association(run, claim, evidence_by_id)
            verifier = _valid_verifier(run, claim)
            association, associated_evidence = exact if exact is not None else (None, None)
            display: str | None = None
            # ONE CAUSE, NAMED. "Claim mapping unavailable" used to be the
            # fallthrough for every distinct way a claim could fail to display
            # -- exactly the conflation that cost the answer path a day of
            # misdirected investigation. Each gate below records WHICH condition
            # actually failed; the message table at the end says it verbatim.
            # `None` means every gate passed and the claim displays.
            cause: (
                Literal[
                    "answer",
                    "association",
                    "verifier",
                    "evidence_withheld",
                    "evidence_locator",
                    "refused",
                    "hash",
                    "span",
                    "mapping",
                ]
                | None
            ) = None
            if output_bytes is None:
                # The answer itself did not resolve; no claim slice can exist
                # without it, and saying "mapping unavailable" here blamed the
                # claim for the answer's condition.
                cause = "answer"
            elif claim.display_state == "protected_source_slice" and (
                association is None or associated_evidence is None
            ):
                # No VALID association. The server did not bind this claim to an
                # evidence record (or the persisted binding failed its integrity
                # joins, which must read the same: an invalid binding is not a
                # binding). Never inferred from proximity or ordering.
                cause = "association"
            elif claim.claim_verdict == "verified" and verifier is None:
                # A verified verdict without its typed verifier receipt is not
                # renderable -- the verdict would be asserting a receipt that is
                # not there.
                cause = "verifier"
            elif (
                claim.display_state == "protected_source_slice"
                and associated_evidence is not None
                and (anchor_state := evidence_states.get(associated_evidence.evidence_id))
                != "available"
            ):
                # The associated evidence record did not disclose. Its own view
                # in this same envelope already states why, and the claim
                # repeats that fact rather than inventing a new one: a WITHHELD
                # record is an access decision (and the claim is withheld with
                # it); anything else is an unresolved locator.
                cause = "evidence_withheld" if anchor_state == "withheld" else "evidence_locator"
            elif claim.display_state == "protected_source_slice":
                disclosed_claim = await self._materializer.resolve_claim(
                    run,
                    claim,
                    principal_id=principal_id,
                    access_context_hash=access_context_hash,
                    projection_attempt_id=projection_attempt_id,
                )
                if disclosed_claim.decision == "authorized" and disclosed_claim.payload is not None:
                    if not hmac.compare_digest(
                        _sha256(disclosed_claim.payload), claim.claim_content_hash
                    ):
                        cause = "hash"
                    elif (
                        disclosed_claim.payload
                        != output_bytes[claim.span_start_byte : claim.span_end_byte]
                    ):
                        cause = "span"
                    else:
                        try:
                            display = disclosed_claim.payload.decode("utf-8")
                        except UnicodeDecodeError:
                            # Hash and bytes matched the span; only the span's
                            # placement on a code-point boundary failed.
                            cause = "span"
                elif disclosed_claim.decision == "withheld":
                    # A DECISION, AND ONLY A DECISION -- the same rule as the
                    # answer above. Only a materializer that actually said
                    # "withheld" produces that word.
                    cause = "refused"
                else:
                    # `unknown`: a resolution FAILURE, the retryable one.
                    cause = "mapping"
            else:
                display = _slice_claim(output_bytes, claim)
                if display is None:
                    cause = _slice_claim_cause(output_bytes, claim)
            if cause is None and display is not None:
                state: Literal["available", "withheld", "unavailable"] = "available"
                message = "Claim available"
            else:
                state, message = _CLAIM_CAUSE_VIEWS[cause or "mapping"]
            claim_views.append(
                ClaimItemView(
                    claim_id=claim.claim_id,
                    claim_verdict=claim.claim_verdict,
                    state=state,
                    message=message,
                    display=display,
                    served_output_hash=claim.served_output_hash,
                    claim_content_hash=claim.claim_content_hash,
                    evidence_id=None if association is None else association.evidence_id,
                    association_hash=None if association is None else association.association_hash,
                    verifier_event_id=None if verifier is None else verifier.event_id,
                    verifier_receipt_hash=None if verifier is None else verifier.receipt_hash,
                )
            )

        mapping_message = cast(
            Literal[
                "Claim mapping pending",
                "Claim mapping complete",
                "Claim mapping partial",
                "Claim mapping unavailable",
            ],
            {
                "mapping_pending": "Claim mapping pending",
                "complete": "Claim mapping complete",
                "partial": "Claim mapping partial",
                "unavailable": "Claim mapping unavailable",
            }[run.mapping_state],
        )
        governance = project_governance(
            run_id=run.run_id,
            principal_id=principal_id,
            agent_id=run.agent_id,
            agent_version=run.agent_version,
            config_hash=run.config_hash,
            capability_snapshot_hash=run.capability_snapshot_hash,
            event_ids=run.event_ids,
            receipts=run.audit_receipts,
            joins=run.governance_joins,
        )
        return EvidenceEnvelope(
            agent_id=run.agent_id,
            run_id=run.run_id,
            run_status=run.status,
            mapping_state=run.mapping_state,
            mapping_message=mapping_message,
            output=RunOutputView(
                verdict=run.output_verdict,
                state=output_state,
                message=output_message,
                display=output_display,
            ),
            claims=tuple(claim_views),
            evidence=tuple(evidence_views),
            governance=governance,
        )


def _event_assurance(raw: object) -> Literal["direct", "inferred", "extracted", "unavailable"]:
    try:
        detail = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return "unavailable"
    if isinstance(detail, dict) and detail.get("evidence_assurance") in {
        "direct",
        "inferred",
        "extracted",
    }:
        return cast(Literal["direct", "inferred", "extracted"], detail["evidence_assurance"])
    return "unavailable"


class SqlAlchemyEvidenceProjectionReader:
    """Read-only exact-id adapter over the canonical 009/010F tables."""

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    async def load_exact(
        self, *, agent_id: str, run_id: str, owner_principal_id: str
    ) -> PersistedRunEvidence | None:
        return await to_thread.run_sync(self._load_exact_sync, agent_id, run_id, owner_principal_id)

    def _load_exact_sync(
        self, agent_id: str, run_id: str, owner_principal_id: str
    ) -> PersistedRunEvidence | None:
        with self._engine.connect() as conn:
            run = conn.execute(
                sa.select(ne_agent_runs).where(
                    ne_agent_runs.c.run_id == run_id,
                    ne_agent_runs.c.agent_id == agent_id,
                    ne_agent_runs.c.owner_principal_id == owner_principal_id,
                )
            ).one_or_none()
            if run is None:
                return None
            events = list(
                conn.execute(
                    sa.select(ne_agent_run_events).where(ne_agent_run_events.c.run_id == run_id)
                ).all()
            )
            event_by_id = {str(item.event_id): item for item in events}
            evidence_rows = list(
                conn.execute(
                    sa.select(
                        ne_agent_evidence_records,
                        ne_agent_evidence_locators.c.locator_hash,
                    )
                    .outerjoin(
                        ne_agent_evidence_locators,
                        sa.and_(
                            ne_agent_evidence_locators.c.locator_id
                            == ne_agent_evidence_records.c.locator_id,
                            ne_agent_evidence_locators.c.run_id
                            == ne_agent_evidence_records.c.run_id,
                            ne_agent_evidence_locators.c.owner_principal_id
                            == ne_agent_evidence_records.c.owner_principal_id,
                        ),
                    )
                    .where(
                        ne_agent_evidence_records.c.run_id == run_id,
                        ne_agent_evidence_records.c.owner_principal_id == owner_principal_id,
                    )
                ).all()
            )
            association_rows = list(
                conn.execute(
                    sa.select(ne_agent_run_claim_evidence).where(
                        ne_agent_run_claim_evidence.c.run_id == run_id
                    )
                ).all()
            )
            association_by_claim = {str(item.claim_id): item for item in association_rows}
            verifier_rows = list(
                conn.execute(
                    sa.select(ne_agent_claim_verifier_receipts).where(
                        ne_agent_claim_verifier_receipts.c.run_id == run_id
                    )
                ).all()
            )
            verifier_by_key = {str(item.claim_key): item for item in verifier_rows}
            claim_rows = list(
                conn.execute(
                    sa.select(ne_agent_run_claims).where(
                        ne_agent_run_claims.c.run_id == run_id,
                        ne_agent_run_claims.c.owner_principal_id == owner_principal_id,
                    )
                ).all()
            )
            finalization = conn.execute(
                sa.select(ne_agent_run_finalizations).where(
                    ne_agent_run_finalizations.c.run_id == run_id
                )
            ).one_or_none()
            delivery_ids = frozenset(
                str(value)
                for value in conn.execute(
                    sa.select(ne_agent_delivery_receipts.c.delivery_receipt_id).where(
                        ne_agent_delivery_receipts.c.run_id == run_id,
                        ne_agent_delivery_receipts.c.owner_principal_id == owner_principal_id,
                    )
                ).scalars()
            )
            receipt_rows = list(
                conn.execute(
                    sa.select(ne_agent_run_audit_receipts).where(
                        ne_agent_run_audit_receipts.c.run_id == run_id
                    )
                ).all()
            )

        evidence = tuple(
            PersistedEvidence(
                evidence_id=str(row.evidence_id),
                run_id=str(row.run_id),
                owner_principal_id=str(row.owner_principal_id),
                attempt_id=str(row.attempt_id),
                step_id=str(row.step_id),
                tool_call_id=str(row.tool_call_id),
                read_ordinal=int(row.read_ordinal),
                locator_status=cast(
                    Literal["located", "evidence_locator_unavailable"], str(row.locator_status)
                ),
                locator_id=None if row.locator_id is None else str(row.locator_id),
                locator_hash=None if row.locator_hash is None else str(row.locator_hash),
                pending_result_id=str(row.pending_result_id),
                producing_event_id=str(row.producing_event_id),
                content_hash=str(row.content_hash),
                assurance=_event_assurance(
                    None
                    if str(row.producing_event_id) not in event_by_id
                    else event_by_id[str(row.producing_event_id)].safe_detail
                ),
                created_at=_utc(row.created_at),
            )
            for row in evidence_rows
        )
        claims: list[PersistedClaim] = []
        for row in claim_rows:
            raw_association = association_by_claim.get(str(row.claim_id))
            association = (
                None
                if raw_association is None
                else PersistedAssociation(
                    association_id=str(raw_association.association_id),
                    run_id=str(raw_association.run_id),
                    claim_id=str(raw_association.claim_id),
                    evidence_id=str(raw_association.evidence_id),
                    locator_hash=str(raw_association.locator_hash),
                    served_citation_ref_hash=str(raw_association.served_citation_ref_hash),
                    producing_event_id=str(raw_association.producing_event_id),
                    delivery_receipt_id=(
                        None
                        if raw_association.delivery_receipt_id is None
                        else str(raw_association.delivery_receipt_id)
                    ),
                    association_hash=str(raw_association.association_hash),
                )
            )
            raw_verifier = verifier_by_key.get(str(row.claim_key))
            verifier = (
                None
                if raw_verifier is None
                else PersistedVerifier(
                    run_id=str(raw_verifier.run_id),
                    claim_key=str(raw_verifier.claim_key),
                    claim_content_hash=str(raw_verifier.claim_content_hash),
                    served_output_hash=str(raw_verifier.served_output_hash),
                    span_start_byte=int(raw_verifier.span_start_byte),
                    span_end_byte=int(raw_verifier.span_end_byte),
                    event_id=str(raw_verifier.event_id),
                    receipt_hash=str(raw_verifier.receipt_hash),
                )
            )
            claims.append(
                PersistedClaim(
                    claim_id=str(row.claim_id),
                    run_id=str(row.run_id),
                    claim_key=str(row.claim_key),
                    claim_index=int(row.claim_index),
                    claim_verdict=cast(
                        Literal["verified", "grounded", "unavailable"], str(row.claim_verdict)
                    ),
                    display_state=cast(
                        Literal["protected_source_free_slice", "protected_source_slice"],
                        str(row.display_state),
                    ),
                    protected_ref=str(row.protected_ref),
                    served_output_hash=str(row.served_output_hash),
                    span_start_byte=int(row.span_start_byte),
                    span_end_byte=int(row.span_end_byte),
                    claim_content_hash=str(row.claim_content_hash),
                    association_status=cast(
                        Literal["associated", "association_unavailable"],
                        str(row.association_status),
                    ),
                    association=association,
                    verifier=verifier,
                )
            )
        if finalization is None:
            mapping_state: Literal["mapping_pending", "complete", "partial", "unavailable"] = (
                "mapping_pending" if str(run.status) == "waiting_approval" else "unavailable"
            )
            output_kind = "unavailable"
            output_ref = output_hash = None
        else:
            mapping_state = cast(
                Literal["complete", "partial", "unavailable"], str(finalization.mapping_state)
            )
            output_kind = str(finalization.protected_output_kind)
            output_ref = (
                None
                if finalization.protected_output_ref is None
                else str(finalization.protected_output_ref)
            )
            output_hash = (
                None
                if finalization.protected_output_hash is None
                else str(finalization.protected_output_hash)
            )
        receipts = tuple(
            PersistedAuditReceipt(
                run_audit_receipt_id=str(row.run_audit_receipt_id),
                run_id=str(row.run_id),
                run_event_id=str(row.run_event_id),
                attempt_id=str(row.attempt_id),
                decision_kind=str(row.decision_kind),
                step_id=None if row.step_id is None else str(row.step_id),
                tool_call_id=None if row.tool_call_id is None else str(row.tool_call_id),
                approval_id=None if row.approval_id is None else str(row.approval_id),
                audit_event_id=str(row.audit_event_id),
                audit_sequence=int(row.audit_sequence),
                audit_record_hash=str(row.audit_record_hash),
                config_hash=str(row.config_hash),
                created_at=_utc(row.created_at),
            )
            for row in receipt_rows
        )
        return PersistedRunEvidence(
            run_id=str(run.run_id),
            owner_principal_id=str(run.owner_principal_id),
            agent_id=str(run.agent_id),
            agent_version=int(run.agent_version),
            status=cast(RunStatus, str(run.status)),
            config_hash=str(run.config_hash),
            capability_snapshot_hash=str(run.capability_snapshot_hash),
            output_verdict=(
                None
                if run.output_verdict is None
                else cast(
                    Literal["verified", "grounded", "refused", "unavailable"],
                    str(run.output_verdict),
                )
            ),
            mapping_state=mapping_state,
            protected_output_kind=cast(
                Literal["protected_turn_content", "protected_source_output", "unavailable"],
                output_kind,
            ),
            protected_output_ref=output_ref,
            protected_output_hash=output_hash,
            event_ids=frozenset(event_by_id),
            delivery_receipt_ids=delivery_ids,
            evidence=evidence,
            claims=tuple(claims),
            audit_receipts=receipts,
        )


def catalog_visibility(get_visible: Callable[[str, Principal], object | None]) -> AgentVisibility:
    """Adapt a principal-aware catalog lookup without weakening its decision."""

    return lambda agent_id, principal: get_visible(agent_id, principal) is not None
