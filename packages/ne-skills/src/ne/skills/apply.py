# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ``apply-skill`` seam — approve → register in ONE transaction (hazard B).

This is where a forged ``skill_candidate`` becomes a real ``ne_skill_versions``
row. The R-20 resolution: EVERY write rides the review service's ONE transition
transaction via the ``record_apply(extra_writes=…)`` same-conn hook — the
``ne_skills`` parent + ``ne_skill_versions`` row + ``ne_skill_lifecycle`` event +
the ``skill.apply`` C10 hash-chain audit entry commit ATOMICALLY with the NE-066
``approved → applied`` fold. Any failure rolls back EVERYTHING (no
applied-but-unregistered ghost).

The ``skill.apply`` audit entry binds ``{proposal_id, review_id, name, version,
content_hash, routing_hash, acl_labels}`` into the hash chain — so an ACL-label
assignment traces to a specific approval, not merely a self-consistent row (the
signed-registration obligation of ADR-0011 + the Codex adversarial finding). The
"signature" is the hash chain, not a crypto pack-signature (C11 pack-signing
stays R-9 / v1.1).

R-22 (ACL safety): labels are assigned ONLY here. The apply REFUSES unless
exactly one of {non-empty labels, explicit global visibility} holds — a blank
label set never publishes fleet-wide by accident. Every label must pass the
injected validator (D-6: reject unknown).

EVAL GATE (NE-SKILL-2): ENFORCED here, fail-closed. Between the house-standard
re-run and the label decision the apply seam calls :func:`verify_skill_eval_ref`
on the payload's ``eval_run`` ref — refusing any candidate whose ref is absent /
not passing / whose ``routing_hash`` does not match the recomputed pack, or whose
``run_id`` has no passing row in the ``EvalRunStore``. The gate binds to the
label-EXCLUDED ``routing_hash`` (D-12): because ``acl_labels`` are stamped only at
step (7) and are excluded from ``routing_hash`` by the frozen contract,
relabel-at-apply can NEVER invalidate a passing eval. The kind-guarded steward
approve route enforces the same precondition earlier.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.audit import append_record
from ne.contracts import get_config_hash
from ne.contracts.skills import (
    AuthoringStandard,
    SkillPack,
    SkillSource,
    WorkflowSnapshot,
    skill_content_hash,
)
from ne.eval.store import EvalRunStore
from ne.events import append as outbox_append
from ne.events import new_audit_event
from ne.flywheel.errors import UnknownProposalError
from ne.flywheel.proposals import ProposalKind, SqlAlchemyProposalStore
from ne.flywheel.review import ProposalReviewService, ProposalStatus, StoredReviewEvent
from ne.flywheel.signals import SignalProvenance
from ne.schema import AuditOutcome
from ne.skills.errors import (
    SkillApplyConfigError,
    SkillApplyGateError,
    SkillApplyIntegrityError,
    SkillApplyLabelError,
    SkillApplyVersionError,
    SkillEvalGateError,
)
from ne.skills.evals import SkillTriggerEvalSuite, verify_skill_eval_ref
from ne.skills.registry import SqlAlchemySkillRegistry
from ne.skills.standard import trigger_overlap_violations, validate_pack_fields

_APPLY_KINDS = frozenset(
    {ProposalKind.SKILL_CANDIDATE.value, ProposalKind.SKILL_UPDATE_CANDIDATE.value}
)
_COMPUTED_KEYS = ("content_hash", "routing_hash")


class SkillEdits(BaseModel):
    """Steward edit-before-approve fields (NE-SKILL-3). Body is NOT editable in v1.

    ``extra="forbid"`` makes a smuggled ``body`` key STRUCTURALLY impossible — a
    body edit is reject-and-re-forge, never an in-place edit (it would move
    attacker-influenced content past the birth gate). Any non-None field forces a
    FRESH synchronous ``SkillTriggerEvalSuite`` run on the edited pack BEFORE the
    one-transaction commit, fail-closed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str | None = None
    description: str | None = None
    triggers: tuple[str, ...] | None = None

    def any_set(self) -> bool:
        return self.name is not None or self.description is not None or self.triggers is not None


class AppliedSkill(BaseModel):
    """The result of a successful ``apply-skill`` (server-derived, no raw payload)."""

    model_config = ConfigDict(frozen=True)

    review: StoredReviewEvent
    name: str
    version: int
    content_hash: str
    acl_labels: tuple[str, ...]


def apply_skill_candidate(
    *,
    review_service: ProposalReviewService,
    skills_engine: sa.Engine,
    registry: SqlAlchemySkillRegistry,
    proposal_store: SqlAlchemyProposalStore,
    proposal_id: str,
    provenance: SignalProvenance,
    acl_labels: tuple[str, ...] | list[str],
    global_visibility: bool,
    label_validator: Callable[[str], bool],
    note: str | None = None,
    edits: SkillEdits | None = None,
    config_hash: str | None = None,
) -> AppliedSkill:
    """Apply an APPROVED skill(_update)_candidate — register in ONE transaction.

    When ``edits`` carries any non-None field (steward edit-before-approve,
    NE-SKILL-3), the pack is REBUILT via ``SkillPack.model_validate`` (never
    ``model_copy`` — an empty-triggers / oversize-description edit must re-trip
    pydantic) and, if the edit changes ``routing_hash``, a FRESH synchronous
    ``SkillTriggerEvalSuite`` runs on the edited pack BEFORE the commit; a failing
    re-run REFUSES the commit (fail-closed) with no review transition and no
    registry row. Body is never editable (that is enforced structurally at the
    wire by ``extra="forbid"``); an update candidate's name is never editable
    (renaming would mint a sibling).
    """
    # (1) same-DATABASE guard (the decided R-20 deviation: two engine objects
    #     share the flywheel signal DB URL; atomicity rides the review conn).
    if str(review_service.engine.url) != str(skills_engine.url):
        raise SkillApplyConfigError(
            "the review-service engine and the skills engine must be the same "
            "database (the one-transaction apply seam rides the review conn)"
        )

    # (2) load the proposal row by a DIRECT indexed lookup (never a capped page
    #     scan — the shared ne_fix_proposals table accumulates every lane, so a
    #     scan would miss any proposal past the page once it grows; Codex P2).
    #     A genuinely-unknown id raises UnknownProposalError → the route maps it
    #     to 404 (its documented semantics), NOT the 409 a gate error would give.
    stored = proposal_store.read_proposal(proposal_id)
    if stored is None:
        raise UnknownProposalError(proposal_id)
    if stored.kind not in _APPLY_KINDS:
        raise SkillApplyGateError(
            f"proposal {proposal_id!r} is kind {stored.kind!r}, not a skill candidate"
        )
    is_update = stored.kind == ProposalKind.SKILL_UPDATE_CANDIDATE.value

    # (3) require folded status APPROVED (the transition table forbids
    #     proposed → applied, but check explicitly for a clean gate error).
    if review_service.status_of(proposal_id) is not ProposalStatus.APPROVED:
        raise SkillApplyGateError(f"proposal {proposal_id!r} is not approved; cannot apply")

    # (4) reconstruct the pack; verify the payload's declared content_hash.
    raw_pack = stored.payload.get("pack")
    if not isinstance(raw_pack, dict):
        raise SkillApplyIntegrityError("proposal payload has no pack")
    declared_hash = raw_pack.get("content_hash")
    pack_fields = {k: v for k, v in raw_pack.items() if k not in _COMPUTED_KEYS}
    try:
        pack = SkillPack.model_validate(pack_fields)
    except Exception as exc:
        raise SkillApplyIntegrityError(f"payload pack invalid: {type(exc).__name__}") from exc
    if declared_hash is not None and skill_content_hash(pack) != declared_hash:
        raise SkillApplyIntegrityError(
            "payload pack content_hash mismatch (poisoned/tampered payload)"
        )
    original_routing_hash = pack.routing_hash

    # (4.5) NE-SKILL-3 edit-before-approve: rebuild the pack from the steward's
    #       name/description/triggers edits. model_validate (NOT model_copy) so an
    #       empty-triggers / over-length-description edit re-trips pydantic. Body is
    #       structurally un-editable (wire extra="forbid"); an update candidate's
    #       name is refused (renaming would mint a sibling).
    edited = edits is not None and edits.any_set()
    if edited:
        assert edits is not None
        if is_update and edits.name is not None:
            raise SkillApplyIntegrityError(
                "an update candidate name cannot be edited — renaming would mint a "
                "sibling; reject and re-forge instead"
            )
        base = pack.model_dump(mode="json", exclude=set(_COMPUTED_KEYS))
        if edits.name is not None:
            base["name"] = edits.name
        if edits.description is not None:
            base["description"] = edits.description
        if edits.triggers is not None:
            base["triggers"] = list(edits.triggers)
        try:
            pack = SkillPack.model_validate(base)
        except Exception as exc:
            raise SkillApplyIntegrityError(
                f"steward edit produced an invalid pack: {type(exc).__name__}"
            ) from exc

    # (5) re-run the house standard incl. the R-23 body lint.
    violations = validate_pack_fields(
        {
            "name": pack.name,
            "version": pack.version,
            "description": pack.description,
            "triggers": list(pack.triggers),
            "source": pack.source.value,
            "body": pack.body,
        },
        AuthoringStandard(),
    )
    if violations:
        raise SkillApplyIntegrityError(
            "pack fails the house standard at apply: " + ", ".join(v.rule for v in violations)
        )

    # (5.1) NE-SKILL-4 creation-time trigger-overlap lint: reject an apply whose
    #       pack shares a normalized trigger phrase with another ACTIVE pack. This
    #       closes the steward-edit hole (a hand-edited trigger colliding with a
    #       sibling); the model-authored overlap path is already routed to
    #       UPDATE-not-sibling by NE-SKILL-3's near-dup detector. An update
    #       candidate legitimately shares its OWN name's triggers — same-name is
    #       excluded — so only a collision with a DIFFERENT pack trips this.
    existing_triggers = {
        p.name: p.triggers for p in registry.verified_current_packs()[0] if p.name != pack.name
    }
    overlaps = trigger_overlap_violations(pack.name, pack.triggers, existing_triggers)
    if overlaps:
        raise SkillApplyIntegrityError(
            "pack trigger-overlap at apply: " + "; ".join(v.message for v in overlaps)
        )

    # (5.5) EVAL GATE (fail-closed). ``pack`` here is PRE-label, but routing_hash
    #       EXCLUDES acl_labels by frozen contract, so the labels stamped at step
    #       (7) below cannot invalidate the eval (D-12). A SkillEvalGateError is a
    #       SkillApplyGateError subclass ⇒ route 409.
    eval_ref = stored.payload.get("eval_run")
    payload_run_id = eval_ref.get("run_id") if isinstance(eval_ref, Mapping) else None
    fresh_eval_run_id: str | None = None
    if not edited or pack.routing_hash == original_routing_hash:
        # NO routing-relevant change ⇒ the stored (forge-time) eval still binds.
        verify_skill_eval_ref(
            eval_ref, expected_routing_hash=pack.routing_hash, engine=skills_engine
        )
    else:
        # NE-SKILL-3: the edit changed routing_hash ⇒ the forge-time eval no longer
        # applies. Run a FRESH SkillTriggerEvalSuite on the EDITED, PRE-label pack
        # synchronously BEFORE the review transaction; refuse the commit if it
        # fails (fail-closed — nothing is written yet). The eval binds to the
        # label-EXCLUDED routing_hash (D-12), so step-(7) labelling cannot
        # invalidate it. The append-only eval row rides its own transaction; an
        # orphan row (if the later commit fails) is harmless (the forge pattern).
        raw_snapshot = stored.payload.get("snapshot")
        if not isinstance(raw_snapshot, Mapping):
            raise SkillApplyGateError(
                "proposal predates edit-before-approve (no snapshot in the payload); "
                "apply without edits, or reject and re-forge"
            )
        try:
            snapshot = WorkflowSnapshot.model_validate(dict(raw_snapshot))
        except Exception as exc:
            raise SkillApplyGateError(
                f"proposal snapshot is not replayable for a fresh eval: {type(exc).__name__}"
            ) from exc
        eval_library = tuple(p for p in registry.verified_current_packs()[0] if p.name != pack.name)
        cfg = config_hash if config_hash is not None else get_config_hash()
        record = SkillTriggerEvalSuite(
            pack, eval_library, snapshot, store=EvalRunStore(skills_engine), top_k=8
        ).run_recorded_sync(config_hash=cfg)
        if not record.passed:
            raise SkillEvalGateError(
                "steward edit failed the routing eval; commit refused (fail-closed) "
                "— fix the edit or reject and re-forge"
            )
        fresh_eval_run_id = record.run_id

    # (6) R-22 explicit visibility decision + D-6 label validation.
    labels = tuple(dict.fromkeys(acl_labels))  # de-dup, preserve order
    if bool(labels) == bool(global_visibility):
        raise SkillApplyLabelError(
            "supply EITHER a non-empty acl_labels set OR global_visibility=true "
            "(never both, never neither — blank labels do not publish fleet-wide)"
        )
    for label in labels:
        if not label_validator(label):
            raise SkillApplyLabelError(f"unknown acl label: {label!r}")

    # (8) version discipline.
    exists = registry.name_exists(pack.name)
    current = registry.current_version_of(pack.name)
    if is_update:
        if not exists or current is None:
            raise SkillApplyVersionError(
                f"skill {pack.name!r} does not exist — cannot apply an update candidate"
            )
        if pack.version != current + 1:
            raise SkillApplyVersionError(
                f"stale update for {pack.name!r}: candidate v{pack.version} but "
                f"current is v{current} (expected v{current + 1})"
            )
    else:
        if exists:
            raise SkillApplyVersionError(
                f"skill {pack.name!r} already exists — propose an update candidate"
            )
        if pack.version != 1:
            raise SkillApplyVersionError(
                f"a skill_candidate must be version 1, got v{pack.version}"
            )

    # (7) stamp labels + provenance (relabel is a content change ⇒ new hash).
    approver = provenance.principal.id
    audit_eval_run_id = fresh_eval_run_id if fresh_eval_run_id is not None else payload_run_id
    stamped_provenance = dict(pack.provenance) | {"applied_by": approver}
    if edited:
        stamped_provenance |= {"edited_by": approver, "edit_eval_run_id": audit_eval_run_id}
    final_pack = pack.model_copy(update={"acl_labels": labels, "provenance": stamped_provenance})

    routing_hash = final_pack.routing_hash
    content_hash = final_pack.content_hash

    # (9) build the same-conn extra_writes closure.
    def _extra_writes(conn: sa.Connection, review_id: str) -> None:
        if is_update:
            registry.assert_parent_exists(conn, final_pack.name)
        else:
            registry.insert_parent_strict(conn, final_pack.name, SkillSource.FORGED.value)
        with_review = final_pack.model_copy(
            update={"provenance": dict(final_pack.provenance) | {"review_id": review_id}}
        )
        registry.insert_version_on(conn, with_review, origin=f"apply:{proposal_id}")
        registry.insert_lifecycle_on(
            conn,
            final_pack.name,
            "registered",
            actor=approver,
            reason=f"proposal:{proposal_id}",
        )
        # The C10 hash-chained signed-registration entry: binds label assignment
        # to THIS approval. Dual-append (outbox + audit chain) on the SAME conn.
        event = new_audit_event(
            action="skill.apply",
            resource=f"skill:{final_pack.name}@v{final_pack.version}",
            outcome=AuditOutcome.success,
            actor=approver,
            detail=_detail(
                proposal_id=proposal_id,
                review_id=review_id,
                name=final_pack.name,
                version=final_pack.version,
                content_hash=content_hash,
                routing_hash=routing_hash,
                acl_labels=labels,
                edited=edited,
                eval_run_id=audit_eval_run_id,
            ),
            provenance={"source": "ne-skills", "component": "apply"},
        )
        outbox_append(conn, event)
        append_record(conn, event)

    # (10) ONE transaction end-to-end via record_apply's extra_writes hook.
    review = review_service.record_apply(
        proposal_id,
        provenance,
        applied_ref=f"skill:{final_pack.name}@v{final_pack.version}",
        note=note,
        extra_writes=_extra_writes,
    )
    return AppliedSkill(
        review=review,
        name=final_pack.name,
        version=final_pack.version,
        content_hash=content_hash,
        acl_labels=labels,
    )


def _detail(
    *,
    proposal_id: str,
    review_id: str,
    name: str,
    version: int,
    content_hash: str,
    routing_hash: str,
    acl_labels: tuple[str, ...],
    edited: bool,
    eval_run_id: str | None,
) -> str:
    return json.dumps(
        {
            "proposal_id": proposal_id,
            "review_id": review_id,
            "name": name,
            "version": version,
            "content_hash": content_hash,
            "routing_hash": routing_hash,
            "acl_labels": list(acl_labels),
            "edited": edited,
            "eval_run_id": eval_run_id,
        }
    )


__all__ = ["AppliedSkill", "SkillEdits", "apply_skill_candidate"]
