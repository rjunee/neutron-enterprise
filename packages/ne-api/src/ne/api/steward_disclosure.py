# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Server-owned current-source disclosure policy for Steward proposals."""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import ValidationError

from ne.brain.current_source_filter import CurrentSourceDisclosureEvaluator
from ne.contracts.source_authority import SourceAuthorityDecision
from ne.contracts.types import Principal
from ne.flywheel.proposals import GraphExtractionBatch, ProposalKind, StoredFixProposal

__all__ = [
    "ProposalDisclosure",
    "ProposalDisposition",
    "disclose_proposal",
    "proposal_disposition",
]


class ProposalDisposition(StrEnum):
    """Exhaustive producer disposition; unrecognised kinds never become source-free."""

    SUPPORTED_SOURCE_BEARING = "supported_source_bearing"
    UNSUPPORTED_SOURCE_BEARING = "unsupported_source_bearing"
    SOURCE_FREE_DEFINITION = "source_free_definition"
    UNKNOWN = "unknown"


_DISPOSITIONS: dict[ProposalKind, ProposalDisposition] = {
    ProposalKind.FIX: ProposalDisposition.UNKNOWN,
    ProposalKind.GOLDEN_SET_CANDIDATE: ProposalDisposition.UNSUPPORTED_SOURCE_BEARING,
    ProposalKind.ENTITY_MERGE_CANDIDATE: ProposalDisposition.UNSUPPORTED_SOURCE_BEARING,
    ProposalKind.VERIFIED_ANSWER_CANDIDATE: ProposalDisposition.SOURCE_FREE_DEFINITION,
    ProposalKind.SKILL_CANDIDATE: ProposalDisposition.SOURCE_FREE_DEFINITION,
    ProposalKind.SKILL_UPDATE_CANDIDATE: ProposalDisposition.SOURCE_FREE_DEFINITION,
    ProposalKind.GRAPH_EXTRACTION_CANDIDATE: ProposalDisposition.SUPPORTED_SOURCE_BEARING,
}
if set(_DISPOSITIONS) != set(ProposalKind):  # pragma: no cover - import-time startup guard
    raise RuntimeError("every ProposalKind requires an explicit disclosure disposition")


def proposal_disposition(kind: str) -> ProposalDisposition:
    try:
        return _DISPOSITIONS[ProposalKind(kind)]
    except (ValueError, KeyError):
        return ProposalDisposition.UNKNOWN


class ProposalDisclosure(StrEnum):
    AVAILABLE = "available"
    CONTENT_WITHHELD = "content_withheld"
    RETRYABLE_UNKNOWN = "retryable_unknown"


def _valid_source_free_definition(proposal: StoredFixProposal) -> bool:
    payload = proposal.payload
    if proposal.kind == ProposalKind.VERIFIED_ANSWER_CANDIDATE.value:
        if set(payload) != {"template_id", "version", "toml_sha256", "template_toml"}:
            return False
        template = payload.get("template_toml")
        digest = payload.get("toml_sha256")
        return (
            isinstance(payload.get("template_id"), str)
            and bool(str(payload["template_id"]).strip())
            and isinstance(payload.get("version"), int)
            and int(payload["version"]) > 0
            and isinstance(template, str)
            and isinstance(digest, str)
            and hashlib.sha256(template.encode()).hexdigest() == digest
        )
    if proposal.kind in {
        ProposalKind.SKILL_CANDIDATE.value,
        ProposalKind.SKILL_UPDATE_CANDIDATE.value,
    }:
        # SkillForge is the sole producer. Requiring its complete typed envelope
        # prevents a same-kind arbitrary/evidence payload from being inferred free.
        return set(payload) == {
            "pack",
            "signature",
            "worthiness",
            "suggested_acl_labels",
            "near_dup",
            "eval_run",
            "snapshot",
        } and all(
            isinstance(payload.get(key), expected)
            for key, expected in {
                "pack": dict,
                "signature": str,
                "worthiness": dict,
                "suggested_acl_labels": list,
                "eval_run": dict,
                "snapshot": dict,
            }.items()
        )
    return False


async def disclose_proposal(
    proposal: StoredFixProposal,
    *,
    evaluator: CurrentSourceDisclosureEvaluator | None,
    principal: Principal,
    access_context_hash: str | None,
) -> ProposalDisclosure:
    """Resolve one proposal without consulting lane, client hints, or historical ACLs."""

    disposition = proposal_disposition(proposal.kind)
    if disposition is ProposalDisposition.SOURCE_FREE_DEFINITION:
        return (
            ProposalDisclosure.AVAILABLE
            if _valid_source_free_definition(proposal)
            else ProposalDisclosure.RETRYABLE_UNKNOWN
        )
    if disposition is not ProposalDisposition.SUPPORTED_SOURCE_BEARING:
        return ProposalDisclosure.CONTENT_WITHHELD
    try:
        batch = GraphExtractionBatch.model_validate(proposal.payload)
    except ValidationError:
        return ProposalDisclosure.RETRYABLE_UNKNOWN
    if not batch.source_authority_stamps or evaluator is None or access_context_hash is None:
        return ProposalDisclosure.RETRYABLE_UNKNOWN
    saw_unknown = False
    for stamp in batch.source_authority_stamps:
        try:
            decision = await evaluator.evaluate(
                stamp,
                principal=principal,
                access_context_hash=access_context_hash,
            )
        except Exception:
            saw_unknown = True
            continue
        if decision.decision is SourceAuthorityDecision.PERMANENT_WITHHELD:
            return ProposalDisclosure.CONTENT_WITHHELD
        if decision.decision is not SourceAuthorityDecision.AUTHORIZED:
            saw_unknown = True
    return ProposalDisclosure.RETRYABLE_UNKNOWN if saw_unknown else ProposalDisclosure.AVAILABLE
