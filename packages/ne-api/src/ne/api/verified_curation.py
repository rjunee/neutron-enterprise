# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Governed HITL curation of T3 verified-answer templates (NE-ONTOLOGY-FLYWHEEL).

This is the ne-api composition seam that closes the continuous-improvement
flywheel loop for the verified-answer layer: a STEWARD proposes a NEW/MODIFIED
SPARQL-template TOML, it rides the EXISTING NE-066 ``ProposalReviewService`` gate
(propose → approve/reject → apply → audit), and on steward APPLY the approved
template is persisted to the governed, ``config_hash``-tracked
``ne_verified_templates`` store + the effective registry re-fingerprints + the
projected ``config_hash`` delta is chain-audited.

Why the orchestration lives HERE (not in ne-brain-ontology): the merged
``.importlinter`` brain-ontology-lane contract forbids ``ne.brain_ontology``
importing ``ne.flywheel`` / ``ne.audit`` — so the flywheel gate + the audit chain
wiring live in ne-api, exactly mirroring the ``ne.api.verified_tools`` precedent.
Every ``ne.brain_ontology`` import is LAZY (inside functions) so importing this
module pulls no rdflib/sqlalchemy on the default-off path.

ACTIVATION IS RESTART-TO-ACTIVATE (a FLAGGED design decision — see the PR/risks):
``config_hash`` is process-FROZEN by architectural decision (NE-CKC5: re-freeze
with a different hash raises), and the ``GovernedToolExecutor`` binding set +
the guardrail ``ToolArgPolicy`` map are frozen at boot inside the C6/C12
chokepoints. So an apply PERSISTS the governed template + AUDITS the projected
``config_hash`` delta; the NEXT process restart (a ``compose restart`` — NOT a
redeploy: no config bundle, no image, no deploy pipeline) re-freezes to EXACTLY
the audited projected hash and the approved template becomes a live governed
tool. The re-fingerprint provability test proves the freeze equality.

Crash-safety of :func:`apply_verified_template` (the ``apply_merge`` precedent):
the persist + the fingerprint/delta audit commit in ONE transaction (chain
failure rolls the persist back, C6 fail-closed); the NE-066 ``record_apply``
row-lock transition runs AFTER that commit. A crash between the two recovers on
retry because the persist is idempotent (content-addressed ``(id, version)``) and
the proposal status is still ``approved``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.audit import append_record
from ne.contracts import get_config_hash
from ne.events import append as outbox_append
from ne.events import new_audit_event
from ne.flywheel.errors import ReviewAuditError, UnknownProposalError
from ne.flywheel.hygiene import scrub_text
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import (
    FixProposal,
    ProposalKind,
    SqlAlchemyProposalStore,
    StoredFixProposal,
    ne_fix_proposals,
)
from ne.flywheel.review import ProposalReviewService, ProposalStatus, StoredReviewEvent
from ne.flywheel.signals import SignalProvenance
from ne.schema import AuditOutcome

if TYPE_CHECKING:
    from ne.brain_ontology.config import OntologySettings
    from ne.brain_ontology.verified.templates import (
        QueryTemplate,
        TemplateRegistry,
    )

__all__ = [
    "AppliedVerifiedTemplate",
    "VerifiedCurationContext",
    "apply_verified_template",
    "effective_registry",
    "propose_verified_template",
]

_PROPOSE_ACTION = "verified_template.propose"
_APPLY_ACTION = "verified_template.apply"
_PROVENANCE_SOURCE = {"source": "ne-api", "component": "verified_curation"}
_MAX_SUMMARY_CHARS = 4000


@dataclass(frozen=True)
class VerifiedCurationContext:
    """The governed-curation seam captured at boot (attached to the Platform).

    ``project_config_hash`` is a closure over the frozen module composition that
    recomputes the platform ``config_hash`` for a HYPOTHETICAL projected template
    REGISTRY — every other module's frozen settings held fixed, only the two inputs
    a HITL template touches recomputed FROM the registry: the
    ``ne-brain-ontology-templates`` fingerprint AND the ``ne-govern`` guardrail
    tool-policy map (a new template contributes a new ``ToolArgPolicy``). It is the
    audit-integrity anchor: the apply records the projected delta, and a fresh boot
    freezes to exactly that hash (provability).
    """

    ontology_settings: OntologySettings
    boot_dir: Path
    project_config_hash: Callable[[TemplateRegistry], str]


class AppliedVerifiedTemplate(BaseModel):
    """The governed-apply result: the persisted template + the audited hash delta."""

    model_config = ConfigDict(frozen=True)

    template_id: str
    version: int
    toml_sha256: str
    old_registry_hash: str
    new_registry_hash: str
    old_config_hash: str
    new_config_hash: str
    review: StoredReviewEvent


def effective_registry(ctx: VerifiedCurationContext, conn: sa.Connection) -> TemplateRegistry:
    """The LIVE effective registry = boot bundle overlaid with the governed store.

    Always computed live from ``conn`` (never a boot snapshot), so version
    discipline can't go stale between propose and apply.
    """
    from ne.brain_ontology.verified.store import read_verified_templates
    from ne.brain_ontology.verified.templates import load_template_registry_with_overlay

    return load_template_registry_with_overlay(ctx.boot_dir, read_verified_templates(conn))


def _proposal_id(template_id: str, version: int, toml_sha256: str) -> str:
    """Content-addressed proposal id (the ``er/job.py`` ``_pair_id`` pattern)."""
    digest = hashlib.sha256(
        json.dumps(
            ["verified-template", template_id, version, toml_sha256], sort_keys=True
        ).encode()
    ).hexdigest()
    return f"sha256:{digest}"


def _check_version_discipline(registry: TemplateRegistry, template: QueryTemplate) -> None:
    """A new id ⇒ v1; an existing id ⇒ the next contiguous version (fail-loud)."""
    from ne.brain_ontology.verified.errors import TemplateRegistryError

    current = {t.template_id: t.version for t in registry.templates}
    existing = current.get(template.template_id)
    if existing is None:
        if template.version != 1:
            raise TemplateRegistryError(
                f"new template {template.template_id!r} must be version 1, got "
                f"v{template.version} (a first version is always v1)"
            )
    elif template.version != existing + 1:
        raise TemplateRegistryError(
            f"template {template.template_id!r} exists at v{existing}; the proposed version "
            f"must be v{existing + 1}, got v{template.version} (contiguous versioning)"
        )


def propose_verified_template(
    ctx: VerifiedCurationContext,
    engine: sa.Engine,
    proposal_store: SqlAlchemyProposalStore,
    provenance: SignalProvenance,
    *,
    template_toml: str,
    summary: str,
) -> StoredFixProposal:
    """Validate + propose a steward-authored verified-answer template (fail-closed).

    The template is parsed through the SAME ``QueryTemplate`` load gate as a boot
    template (SELECT-only, banned keywords, typed args, citation projection) —
    ``TemplateRegistryError`` propagates BEFORE any row is written, so an invalid
    template can never become a proposal. The proposal row + a
    ``verified_template.propose`` audit event dual-append in ONE transaction (chain
    failure rolls the whole propose back, C6 fail-closed).
    """
    from ne.brain_ontology.verified.templates import parse_template_toml

    template = parse_template_toml(template_toml)  # fail-closed; NO row on failure.
    toml_sha256 = hashlib.sha256(template_toml.encode("utf-8")).hexdigest()
    proposal_id = _proposal_id(template.template_id, template.version, toml_sha256)
    now = datetime.now(UTC)
    config_hash = get_config_hash()

    with engine.begin() as conn:
        registry = effective_registry(ctx, conn)
        _check_version_discipline(registry, template)  # raises TemplateRegistryError.
        proposal = FixProposal(
            proposal_id=proposal_id,
            kind=ProposalKind.VERIFIED_ANSWER_CANDIDATE,
            lane=FixLane.ONTOLOGY,
            cluster_id=f"verified-template:{template.template_id}@v{template.version}",
            evidence_event_ids=(),
            evidence_signal_ids=(),
            summary=scrub_text(summary, max_chars=_MAX_SUMMARY_CHARS),
            confidence=1.0,
            priority=ProposalPriority.MEDIUM,
            config_hash=config_hash,
            created_at=now,
            payload={
                "template_id": template.template_id,
                "version": template.version,
                "toml_sha256": toml_sha256,
                "template_toml": template_toml,
            },
        )
        row_id = proposal_store.append(conn, proposal)
        event = new_audit_event(
            action=_PROPOSE_ACTION,
            resource=f"flywheel:proposal:{proposal_id}",
            outcome=AuditOutcome.success,
            actor=provenance.principal.id,
            detail=json.dumps(
                {
                    "proposal_id": proposal_id,
                    "template_id": template.template_id,
                    "version": template.version,
                    "toml_sha256": toml_sha256,
                }
            ),
            provenance={
                **_PROVENANCE_SOURCE,
                "surface": provenance.surface.value,
                "session_id": provenance.session_id,
            },
        )
        outbox_append(conn, event)
        _chain_append(conn, event)

    return StoredFixProposal(
        id=row_id,
        proposal_id=proposal_id,
        kind=ProposalKind.VERIFIED_ANSWER_CANDIDATE.value,
        lane=FixLane.ONTOLOGY.value,
        cluster_id=proposal.cluster_id,
        status=proposal.status,
        config_hash=config_hash,
        summary=proposal.summary,
        confidence=proposal.confidence,
        priority=ProposalPriority.MEDIUM.value,
        evidence_event_ids=(),
        evidence_signal_ids=(),
        payload=dict(proposal.payload),
        created_at=now,
    )


def apply_verified_template(
    ctx: VerifiedCurationContext,
    review_service: ProposalReviewService,
    engine: sa.Engine,
    proposal_store: SqlAlchemyProposalStore,
    proposal_id: str,
    provenance: SignalProvenance,
    *,
    note: str | None = None,
) -> AppliedVerifiedTemplate:
    """Apply an APPROVED verified-answer-template proposal (governed + audited).

    Ordering (the ``apply_merge`` precedent + crash-retry-safe):

    1. load the proposal row; a non-``verified_answer_candidate`` kind is a
       :class:`VerifiedTemplateGateError` (the F5 kind-guard);
    2. the NE-066 status MUST be ``approved`` (fail-closed; covers rejected/applied
       terminals);
    3. re-validate the payload TOML + sha + version discipline against the LIVE
       effective registry (a crash-retry that finds this EXACT template already
       persisted skips the version-discipline check — see step 4 — so the still-
       ``approved`` proposal is not stranded by its own committed persist);
    4. ONE transaction: persist (idempotent) → re-fingerprint → project the
       ``config_hash`` delta → dual-append the ``verified_template.apply`` audit
       (chain failure rolls the persist back);
    5. AFTER commit: ``record_apply`` folds the NE-066 status ``approved →
       applied`` (row-locked + unique ``(proposal_id, action)`` — racing applies
       serialize; the loser raises ``InvalidTransitionError`` → 409).
    """
    from ne.brain_ontology.verified.errors import (
        TemplateRegistryError,
        VerifiedTemplateGateError,
    )
    from ne.brain_ontology.verified.store import (
        VerifiedTemplateRecord,
        append_verified_template,
        read_verified_templates,
    )
    from ne.brain_ontology.verified.templates import (
        load_template_registry_with_overlay,
        parse_template_toml,
    )

    # (1) Load the proposal row (kind + payload) — fail-closed on absence/kind.
    kind, payload = _load_proposal(engine, proposal_id)
    if kind != ProposalKind.VERIFIED_ANSWER_CANDIDATE.value:
        raise VerifiedTemplateGateError(
            f"proposal {proposal_id!r} is kind {kind!r}, not "
            f"{ProposalKind.VERIFIED_ANSWER_CANDIDATE.value!r} (wrong-kind apply refused)"
        )

    # (2) Gate FIRST — refuse unless APPROVED, before any read/write.
    status = review_service.status_of(proposal_id)
    if status is not ProposalStatus.APPROVED:
        raise VerifiedTemplateGateError(
            f"cannot apply verified template for {proposal_id!r}: status is {status.value!r}, "
            "not 'approved' (the NE-066 gate must approve first; fail-closed)"
        )

    # (3) Re-validate the payload against the SAME gate + the sha + version discipline.
    template_toml = str(payload["template_toml"])
    declared_sha = str(payload["toml_sha256"])
    template = parse_template_toml(template_toml)
    actual_sha = hashlib.sha256(template_toml.encode("utf-8")).hexdigest()
    if actual_sha != declared_sha:
        raise TemplateRegistryError(
            f"proposal {proposal_id!r} payload sha256 mismatch (stored {declared_sha!r} != "
            f"recomputed {actual_sha!r}) — refusing to apply"
        )

    now = datetime.now(UTC)
    old_config_hash = get_config_hash()

    with engine.begin() as conn:
        stored = read_verified_templates(conn)
        # Crash-retry idempotency (the recovery guarantee in this module's docstring):
        # if this EXACT (template_id, version, sha) is ALREADY persisted — a crash
        # AFTER step (4)'s commit but BEFORE record_apply re-enters here with the
        # proposal still 'approved' — the store already holds this version, so
        # version discipline would WRONGLY reject it as "not the next version" and
        # strand the proposal forever. Detect the identical row and skip the
        # discipline check; append_verified_template is idempotent (returns the
        # existing id) and record_apply still runs to fold approved → applied.
        already_persisted = any(
            s.template_id == template.template_id
            and s.version == template.version
            and s.toml_sha256 == actual_sha
            for s in stored
        )
        old_registry = load_template_registry_with_overlay(ctx.boot_dir, stored)
        if not already_persisted:
            _check_version_discipline(old_registry, template)  # still the next version?
        append_verified_template(
            conn,
            VerifiedTemplateRecord(
                template_id=template.template_id,
                version=template.version,
                template_toml=template_toml,
                toml_sha256=actual_sha,
                proposal_id=proposal_id,
                actor=provenance.principal.id,
                config_hash=old_config_hash,
                created_at=now,
            ),
        )
        new_registry = effective_registry(ctx, conn)
        old_registry_hash = old_registry.fingerprint.registry_hash
        new_registry_hash = new_registry.fingerprint.registry_hash
        new_config_hash = ctx.project_config_hash(new_registry)
        event = new_audit_event(
            action=_APPLY_ACTION,
            resource=f"ontology:verified-template:{template.template_id}@v{template.version}",
            outcome=AuditOutcome.success,
            actor=provenance.principal.id,
            detail=json.dumps(
                {
                    "proposal_id": proposal_id,
                    "template_id": template.template_id,
                    "version": template.version,
                    "toml_sha256": actual_sha,
                    "old_registry_hash": old_registry_hash,
                    "new_registry_hash": new_registry_hash,
                    "old_config_hash": old_config_hash,
                    "new_config_hash": new_config_hash,
                    "restart_required": True,
                }
            ),
            provenance={
                **_PROVENANCE_SOURCE,
                "surface": provenance.surface.value,
                "session_id": provenance.session_id,
            },
        )
        outbox_append(conn, event)
        _chain_append(conn, event)

    # (5) AFTER commit: record the NE-066 apply transition (row-locked fold).
    applied_ref = (
        f"verified-template:{template.template_id}@v{template.version}:{new_registry_hash[:16]}"
    )
    review = review_service.record_apply(
        proposal_id, provenance, applied_ref=applied_ref, note=note
    )

    return AppliedVerifiedTemplate(
        template_id=template.template_id,
        version=template.version,
        toml_sha256=actual_sha,
        old_registry_hash=old_registry_hash,
        new_registry_hash=new_registry_hash,
        old_config_hash=old_config_hash,
        new_config_hash=new_config_hash,
        review=review,
    )


def _load_proposal(engine: sa.Engine, proposal_id: str) -> tuple[str, dict[str, object]]:
    """Return ``(kind, payload)`` for ``proposal_id``; ``UnknownProposalError`` if absent."""
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_fix_proposals.c.kind, ne_fix_proposals.c.payload).where(
                ne_fix_proposals.c.proposal_id == proposal_id
            )
        ).first()
    if row is None:
        raise UnknownProposalError(f"proposal_id {proposal_id!r} is not in ne_fix_proposals")
    return str(row.kind), dict(row.payload)


def _chain_append(conn: sa.Connection, event: object) -> None:
    """Append to the hash chain; fail closed on any failure (C6, review._chain_append)."""
    try:
        append_record(conn, event)  # type: ignore[arg-type]
    except Exception as exc:
        raise ReviewAuditError(
            "audit-chain append failed for a verified-template curation action; the whole "
            "transaction rolls back (C6 fail closed)"
        ) from exc
