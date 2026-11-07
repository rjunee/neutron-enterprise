# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Run-bound source-evidence authors for governed Knowledge Tool reads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ne.contracts.source_authority import (
    EvidenceAssurance,
    SourceAuthorityStamp,
    SourceBoundToolResultRef,
    SourceBoundToolResultSetRef,
    VerifiedEmptyResultReceipt,
    VerifiedRowContributorReceipt,
    canonical_source_authority_stamps,
)
from ne.runtime.run_audit_receipts import (
    ExistingRunEvidenceContext,
    current_run_observation_audit,
    current_run_tool_call,
)
from ne.runtime.run_store import (
    EvidenceContributorDeclaration,
    EvidenceDeclaration,
    EvidenceRecord,
    EvidenceRowDeclaration,
    EvidenceSetDeclaration,
    EvidenceSetRecord,
)
from ne.runtime.source_results import encode_protected_reference

MAX_CONTRIBUTORS = 128
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type KnowledgeReadKind = Literal["search", "graph_path", "verified_t3"]
type EvidenceStatus = Literal["located", "evidence_locator_unavailable"]


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _verified_row_anchor(receipt: VerifiedRowContributorReceipt) -> str:
    """Normalize one row receipt to a content-free, fixed-width opaque anchor."""

    material = b"ne.verified-evidence-row-anchor.v1\x00" + receipt.receipt_hash.encode("ascii")
    return _sha256(material)


class _KnowledgeEvidenceDetail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_status: EvidenceStatus
    contributor_count: Annotated[int, Field(ge=1, le=MAX_CONTRIBUTORS)]
    evidence_assurance: EvidenceAssurance


class SearchEvidenceDetail(_KnowledgeEvidenceDetail):
    read_kind: Literal["search"] = "search"


class GraphPathEvidenceDetail(_KnowledgeEvidenceDetail):
    read_kind: Literal["graph_path"] = "graph_path"


class VerifiedT3EvidenceDetail(_KnowledgeEvidenceDetail):
    read_kind: Literal["verified_t3"] = "verified_t3"
    row_anchor: Sha256
    receipt_hash: Sha256


class VerifiedT3EmptyEvidenceDetail(_KnowledgeEvidenceDetail):
    read_kind: Literal["verified_t3_empty"] = "verified_t3_empty"
    receipt_hash: Sha256


type KnowledgeEvidenceDetail = Annotated[
    SearchEvidenceDetail
    | GraphPathEvidenceDetail
    | VerifiedT3EvidenceDetail
    | VerifiedT3EmptyEvidenceDetail,
    Field(discriminator="read_kind"),
]


@dataclass(frozen=True, slots=True)
class ExistingKnowledgeRead:
    """Canonical identities supplied by an already-admitted Run Tool call."""

    evidence: ExistingRunEvidenceContext
    owner_principal_id: str
    attempt_id: str
    turn_id: str


def current_existing_knowledge_read() -> ExistingKnowledgeRead | None:
    tool_call = current_run_tool_call()
    if tool_call is None:
        return None
    return ExistingKnowledgeRead(
        evidence=tool_call.evidence.next_read(),
        owner_principal_id=tool_call.run.owner_principal_id,
        attempt_id=tool_call.receipt.attempt_id,
        turn_id=tool_call.turn_id,
    )


@dataclass(frozen=True, slots=True)
class ClaimedKnowledgeResult:
    """Opaque protected-result reference plus its canonical evidence record."""

    protected_ref: str
    evidence: EvidenceRecord


@dataclass(frozen=True, slots=True)
class ClaimedKnowledgeResultSet:
    """Opaque protected-result reference plus its complete contributor set."""

    protected_ref: str
    evidence: EvidenceSetRecord


class EvidenceClaimStore(Protocol):
    """The only 009 writes available to a Knowledge evidence adapter."""

    def claim_evidence(
        self,
        *,
        run_id: str,
        owner_principal_id: str,
        attempt_id: str,
        declaration: EvidenceDeclaration,
    ) -> Awaitable[EvidenceRecord]: ...

    def claim_evidence_set(
        self,
        *,
        run_id: str,
        owner_principal_id: str,
        attempt_id: str,
        declaration: EvidenceSetDeclaration,
    ) -> Awaitable[EvidenceSetRecord]: ...


class KnowledgeSourceEvidenceAuthor:
    """Claim source evidence through 009 storage without authoring authority."""

    def __init__(self, store: EvidenceClaimStore) -> None:
        self._store = store

    async def claim(
        self,
        *,
        existing: ExistingKnowledgeRead,
        protected_result: bytes,
        locator: Mapping[str, object] | None,
        source_stamps: Sequence[SourceAuthorityStamp],
        read_kind: Literal["search", "graph_path"],
        assurance: EvidenceAssurance,
    ) -> ClaimedKnowledgeResult:
        canonical_stamps = canonical_source_authority_stamps(source_stamps)
        if not canonical_stamps:
            raise ValueError("source-bearing Knowledge evidence requires a contributor stamp")
        if len(canonical_stamps) > MAX_CONTRIBUTORS:
            raise ValueError("source-bearing Knowledge evidence exceeds contributor bound")
        if not isinstance(assurance, EvidenceAssurance):
            raise TypeError("Knowledge evidence assurance must use EvidenceAssurance")
        status: EvidenceStatus = "evidence_locator_unavailable" if locator is None else "located"
        detail_type = SearchEvidenceDetail if read_kind == "search" else GraphPathEvidenceDetail
        detail = detail_type(
            evidence_status=status,
            contributor_count=len(canonical_stamps),
            evidence_assurance=assurance,
        )
        locator_bytes: bytes | None = None
        if locator is not None:
            supplied = locator.get("evidence_assurance")
            if supplied is not None and supplied != assurance.value:
                raise ValueError("locator assurance conflicts with typed evidence assurance")
            locator_bytes = _canonical_bytes(
                {**dict(locator), "evidence_assurance": assurance.value}
            )
        context = existing.evidence
        content_hash = _sha256(protected_result)
        record = await self._store.claim_evidence(
            run_id=context.run_id,
            owner_principal_id=existing.owner_principal_id,
            attempt_id=existing.attempt_id,
            declaration=EvidenceDeclaration(
                turn_id=existing.turn_id,
                step_id=context.step_id,
                tool_call_id=context.tool_call_id,
                read_ordinal=context.read_ordinal,
                content_hash=content_hash,
                safe_event_detail=detail.model_dump(mode="json"),
                locator_plaintext=locator_bytes,
                pending_result_plaintext=protected_result,
                source_stamps=canonical_stamps,
                assurance=assurance,
            ),
        )
        await self._audit_evidence(record, actor=existing.owner_principal_id)
        reference = SourceBoundToolResultRef(
            reference_id=record.pending_result_id,
            result_hash=content_hash,
            stamps=canonical_stamps,
        )
        return ClaimedKnowledgeResult(
            protected_ref=encode_protected_reference(reference), evidence=record
        )

    async def claim_verified_set(
        self,
        *,
        existing: ExistingKnowledgeRead,
        protected_result: bytes,
        receipts: Sequence[VerifiedRowContributorReceipt],
        empty_member_receipt: VerifiedEmptyResultReceipt | None = None,
    ) -> ClaimedKnowledgeResultSet:
        if not receipts or len(receipts) > 128:
            raise ValueError("verified evidence requires 1..128 row receipts")
        rows: list[EvidenceRowDeclaration] = []
        for receipt in receipts:
            row_anchor = _verified_row_anchor(receipt)
            contributors: list[EvidenceContributorDeclaration] = []
            for index, stamp in enumerate(receipt.contributors):
                assurance = EvidenceAssurance.DIRECT
                detail = VerifiedT3EvidenceDetail(
                    evidence_status="located",
                    contributor_count=1,
                    evidence_assurance=assurance,
                    row_anchor=row_anchor,
                    receipt_hash=receipt.receipt_hash,
                )
                locator = {
                    "kind": "verified_t3",
                    "row_anchor": row_anchor,
                    "receipt": receipt.model_dump(mode="json"),
                    "contributor_index": index,
                    "contributor": stamp.model_dump(mode="json"),
                    "evidence_assurance": assurance.value,
                }
                contributors.append(
                    EvidenceContributorDeclaration(
                        locator_plaintext=_canonical_bytes(locator),
                        source_stamp=stamp,
                        assurance=assurance,
                        safe_event_detail=detail.model_dump(mode="json"),
                    )
                )
            rows.append(
                EvidenceRowDeclaration(
                    row_anchor=row_anchor,
                    receipt_hash=receipt.receipt_hash,
                    contributors=tuple(contributors),
                )
            )
        # NE-KG-MULTIKEY partial coverage: the minted empty-member receipt joins the
        # SAME atomic evidence set as its sibling rows, as one extra declaration whose
        # locator is the EXACT ``verified_t3_empty`` shape the projection already
        # parses. The anchor derives from the receipt hash under its OWN domain tag,
        # so it can never collide with a row anchor. If the row cap leaves no room
        # (128 receipts), the rows still claim exactly as before — the coverage entry
        # degrades away rather than failing a read that used to succeed.
        if empty_member_receipt is not None and len(receipts) < 128:
            empty_stamps = canonical_source_authority_stamps(empty_member_receipt.contributors)
            if not empty_stamps or len(empty_stamps) > MAX_CONTRIBUTORS:
                raise ValueError("verified empty-member evidence requires 1..128 contributors")
            empty_assurance = EvidenceAssurance.DIRECT
            empty_anchor = _sha256(
                b"ne.verified-evidence-empty-member-anchor.v1\x00"
                + empty_member_receipt.receipt_hash.encode("ascii")
            )
            empty_locator = _canonical_bytes(
                {
                    "kind": "verified_t3_empty",
                    "receipt": empty_member_receipt.model_dump(mode="json"),
                    "evidence_assurance": empty_assurance.value,
                }
            )
            rows.append(
                EvidenceRowDeclaration(
                    row_anchor=empty_anchor,
                    receipt_hash=empty_member_receipt.receipt_hash,
                    contributors=tuple(
                        EvidenceContributorDeclaration(
                            locator_plaintext=empty_locator,
                            source_stamp=stamp,
                            assurance=empty_assurance,
                            safe_event_detail=VerifiedT3EmptyEvidenceDetail(
                                evidence_status="located",
                                contributor_count=1,
                                evidence_assurance=empty_assurance,
                                receipt_hash=empty_member_receipt.receipt_hash,
                            ).model_dump(mode="json"),
                        )
                        for stamp in empty_stamps
                    ),
                )
            )
        context = existing.evidence
        content_hash = _sha256(protected_result)
        claimed = await self._store.claim_evidence_set(
            run_id=context.run_id,
            owner_principal_id=existing.owner_principal_id,
            attempt_id=existing.attempt_id,
            declaration=EvidenceSetDeclaration(
                turn_id=existing.turn_id,
                step_id=context.step_id,
                tool_call_id=context.tool_call_id,
                read_ordinal=context.read_ordinal,
                content_hash=content_hash,
                pending_result_plaintext=protected_result,
                rows=tuple(rows),
            ),
        )
        for record in claimed.evidence:
            await self._audit_evidence(record, actor=existing.owner_principal_id)
        # The reference stamp set mirrors run_store's packed set, which is derived
        # from EVERY declared row — including the empty-member coverage entry when
        # one was declared above. Diverging here would split the canonical stamp set
        # between the packed payload and the served reference.
        stamps = canonical_source_authority_stamps(
            [stamp for receipt in receipts for stamp in receipt.contributors]
            + [
                stamp
                for stamp in (
                    empty_member_receipt.contributors
                    if empty_member_receipt is not None and len(receipts) < 128
                    else ()
                )
            ]
        )
        reference = SourceBoundToolResultSetRef(
            reference_id=claimed.pending_result_id,
            result_hash=content_hash,
            stamps=stamps,
            evidence_ids=tuple(item.evidence_id for item in claimed.evidence),
        )
        return ClaimedKnowledgeResultSet(
            protected_ref=encode_protected_reference(reference), evidence=claimed
        )

    async def claim_verified_empty(
        self,
        *,
        existing: ExistingKnowledgeRead,
        protected_result: bytes,
        receipt: VerifiedEmptyResultReceipt,
    ) -> ClaimedKnowledgeResult:
        """Claim a complete zero-row verified result against its scoped snapshot."""

        stamps = canonical_source_authority_stamps(receipt.contributors)
        if not stamps or len(stamps) > MAX_CONTRIBUTORS:
            raise ValueError("verified empty evidence requires 1..128 contributors")
        assurance = EvidenceAssurance.DIRECT
        detail = VerifiedT3EmptyEvidenceDetail(
            evidence_status="located",
            contributor_count=len(stamps),
            evidence_assurance=assurance,
            receipt_hash=receipt.receipt_hash,
        )
        locator = _canonical_bytes(
            {
                "kind": "verified_t3_empty",
                "receipt": receipt.model_dump(mode="json"),
                "evidence_assurance": assurance.value,
            }
        )
        context = existing.evidence
        content_hash = _sha256(protected_result)
        record = await self._store.claim_evidence(
            run_id=context.run_id,
            owner_principal_id=existing.owner_principal_id,
            attempt_id=existing.attempt_id,
            declaration=EvidenceDeclaration(
                turn_id=existing.turn_id,
                step_id=context.step_id,
                tool_call_id=context.tool_call_id,
                read_ordinal=context.read_ordinal,
                content_hash=content_hash,
                safe_event_detail=detail.model_dump(mode="json"),
                locator_plaintext=locator,
                pending_result_plaintext=protected_result,
                source_stamps=stamps,
                assurance=assurance,
            ),
        )
        await self._audit_evidence(record, actor=existing.owner_principal_id)
        reference = SourceBoundToolResultRef(
            reference_id=record.pending_result_id,
            result_hash=content_hash,
            stamps=stamps,
        )
        return ClaimedKnowledgeResult(
            protected_ref=encode_protected_reference(reference), evidence=record
        )

    @staticmethod
    async def _audit_evidence(record: EvidenceRecord, *, actor: str) -> None:
        audit = current_run_observation_audit()
        if audit is None:
            if current_run_tool_call() is not None:
                raise RuntimeError(
                    "Run Knowledge evidence lacks its mandatory observation audit sink"
                )
            return
        await audit.append_evidence_receipt(
            producing_event_id=record.producing_event_id,
            actor=actor,
        )
