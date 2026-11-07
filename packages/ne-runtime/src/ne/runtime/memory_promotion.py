# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""MemoryPromoter — the curation-gated learned-fact → brain promotion path (NE-037).

Durable learnings reach the brain ONLY through a two-stage, curation-gated flow:

    agent propose()  →  audit  →  DISTINCT human curator promote()  →  audit
                                       │
                                       └─ ONE call to the frozen
                                          ``KnowledgeWrite.upsert`` seam
                                          (provenance ``learned-by-agent``)

The composition root constructs the brain-side NE-023 ``KnowledgeWriter`` and
injects it here as the frozen ``KnowledgeWrite`` Protocol. Every promoted item
flows through that governed path (shape-validation → accept-or-quarantine →
provenance stamp → its own audit) — there is NO direct-to-index path, and the
agent-side :meth:`MemoryPromoter.propose` step structurally CANNOT reach the
writer (it never touches ``self._writer``).

``ne-brain`` is neither imported nor touched (a concurrent unit owns it): the
provenance string is duplicated here BY DESIGN — it equals
``ne-brain``'s ``ProvenanceClass.LEARNED_BY_AGENT.value``, and the frozen
contract Protocol is the seam.

Note: this provenance string sets only the stamp's SOURCE. The queryable
provenance CLASS comes from the writer's ``default_provenance_class``, so the
composition root MUST construct the promotion writer with
``ProvenanceClass.LEARNED_BY_AGENT`` (see :func:`ne.runtime.create_memory_promoter`);
otherwise a CURATED-default writer stamps promoted learnings as class ``curated``
and they are not class-queryable as ``learned-by-agent`` (NE-037 Codex P2).

Each stage emits a ``config_hash``-stamped ``AuditEvent`` via the frozen
``AuditSink``; ``config_hash`` resolution is non-optional
(``ConfigHashNotFrozenError`` propagates if a root forgot to freeze), and a sink
failure propagates fail-closed.

Audit-integrity invariant (Checkpoint-D boundary-audit #6): a promoted /
retrievable fact can NEVER exist without its ``memory.promotion.promoted`` audit
record. The injected NE-023 writer upholds the same invariant on its OWN
``knowledge.upsert`` audit path (NE-CKD6): a single shared-audit-sink outage can
no longer surface a durable-but-unaudited chunk through ``upsert``.
``promote()`` orders upsert → promoted-audit → approval-row-LAST; on an
audit-append failure it UNWINDS the accepted chunks through the governed
``KnowledgeWrite.quarantine`` seam (park-before-delete) and raises
:class:`~ne.runtime.errors.MemoryPromotionAuditError`, mirroring ne-identity
``issue()``. The append-only approval row is written last, so a failed audit
never arms the duplicate-promotion guard and the promotion stays cleanly
retryable. ``propose()`` is AUDIT-FIRST because the episodic store is append-only
(no unwind possible): an orphan ``proposed`` audit event is fail-safe over-audit,
whereas an unaudited proposal row is the finding.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from ne.contracts.governance import AuditSink
from ne.contracts.knowledge import KnowledgeItem, KnowledgeWrite, UpsertReceipt
from ne.events import new_audit_event
from ne.runtime.errors import (
    MemoryEventNotFoundError,
    MemoryPromotionAuditError,
    MemoryPromotionError,
)
from ne.runtime.memory_store import MemoryEventKind, SqlAlchemyEpisodicStore
from ne.schema import AuditOutcome

#: Equals ``ne-brain`` ``ProvenanceClass.LEARNED_BY_AGENT.value`` — duplicated by
#: design so ne-brain is never imported (the frozen contracts are the seam).
LEARNED_BY_AGENT_PROVENANCE = "learned-by-agent"


class MemoryPromoter:
    """Two-stage, curation-gated promotion of learned facts into the brain."""

    def __init__(
        self,
        store: SqlAlchemyEpisodicStore,
        writer: KnowledgeWrite,
        *,
        sink: AuditSink,
    ) -> None:
        self._store = store
        self._writer = writer
        self._sink = sink

    async def propose(
        self,
        *,
        agent_id: str,
        event_ids: Sequence[str],
        session_id: str | None = None,
    ) -> str:
        """Agent-side: propose its OWN learned-fact events for promotion.

        Validates every event is the agent's own and is a ``learned-fact``, then
        emits the ``memory.promotion.proposed`` audit event AND — only after it is
        durably recorded — appends the ``promotion-proposed`` event. This step
        structurally CANNOT reach the writer.

        AUDIT-FIRST ordering (Checkpoint-D #6): the episodic store is append-only
        by construction (no delete, so a row can never be unwound). Auditing BEFORE
        the row is therefore the only ordering under which a proposal row can never
        exist unaudited. If the audit succeeds but the row append then fails, the
        orphan ``proposed`` audit event is fail-safe OVER-audit (harmless) and the
        caller simply retries with a fresh ``proposal_id`` — the inverse (a row
        with no audit) is the finding this closes.
        """
        if not event_ids:
            raise MemoryPromotionError("propose requires at least one event id")
        for event_id in event_ids:
            event = await self._store.get(agent_id=agent_id, event_id=event_id)
            if event.kind != MemoryEventKind.LEARNED_FACT:
                raise MemoryPromotionError(
                    f"only learned-fact events are promotable (event {event_id!r} "
                    f"is {event.kind!r})"
                )
        proposal_id = uuid4().hex
        detail = json.dumps(
            {"proposal_id": proposal_id, "event_ids": list(event_ids)}, sort_keys=True
        )
        await self._sink.append(
            new_audit_event(
                action="memory.promotion.proposed",
                resource=f"memory-proposal:{proposal_id}",
                outcome=AuditOutcome.success,
                actor=agent_id,
                detail=json.dumps(
                    {
                        "proposal_id": proposal_id,
                        "agent_id": agent_id,
                        "event_ids": list(event_ids),
                    },
                    sort_keys=True,
                ),
                provenance={"source": "ne-runtime", "component": "memory-promoter"},
            )
        )
        await self._store.append(
            agent_id=agent_id,
            kind=MemoryEventKind.PROMOTION_PROPOSED,
            content=detail,
            occurred_at=datetime.now(UTC),
            provenance=f"agent:{agent_id}",
            session_id=session_id,
        )
        return proposal_id

    async def _find_proposal_event_ids(
        self, *, agent_id: str, proposal_id: str
    ) -> list[str] | None:
        rows = await self._store.recall(
            agent_id=agent_id,
            kinds=[MemoryEventKind.PROMOTION_PROPOSED],
            limit=None,
        )
        for row in rows:
            payload = json.loads(row.content)
            if payload.get("proposal_id") == proposal_id:
                return list(payload["event_ids"])
        return None

    async def _already_promoted(self, *, agent_id: str, proposal_id: str) -> bool:
        rows = await self._store.recall(
            agent_id=agent_id,
            kinds=[MemoryEventKind.PROMOTION_APPROVED],
            limit=None,
        )
        return any(json.loads(row.content).get("proposal_id") == proposal_id for row in rows)

    async def promote(
        self,
        proposal_id: str,
        *,
        agent_id: str,
        approved_by: str,
    ) -> UpsertReceipt:
        """Curator-side: a DISTINCT named principal approves + promotes a proposal.

        The proposing agent can never approve its own promotion. On approval,
        every source learned-fact is upserted through the frozen
        ``KnowledgeWrite.upsert`` seam with provenance ``learned-by-agent`` and a
        colon-free ``learned-<event_id>`` id.

        Ordering (Checkpoint-D #6) — a promoted/retrievable fact can NEVER exist
        without its ``memory.promotion.promoted`` audit record:

          1. ``writer.upsert(items)`` — durable brain write; writer exceptions
             PROPAGATE before any promoter-side state exists, so a retry re-emits
             them (retry-re-emit compliant). On its INTERNAL ``knowledge.upsert``
             audit-append failure the NE-CKD6 writer is itself ATOMIC: it UNWINDS
             its own not-yet-audited vector-store writes (park-before-delete) and
             raises a typed ``KnowledgeAuditError`` rather than merely propagating,
             so no retrievable-but-unaudited chunk can surface through this call.
             Because that writer atomicity is PER-ITEM, a MULTI-item promotion may
             leave EARLIER items store+audit complete when a LATER item's audit
             fails; promote() therefore best-effort unwinds EVERY attempted item
             back through the governed ``KnowledgeWrite.quarantine`` seam before
             re-raising, so no promoted/retrievable fact survives without this
             promotion's ``memory.promotion.promoted`` record (invariant above).
          2. emit the ``memory.promotion.promoted`` audit event. If it FAILS,
             UNWIND every accepted chunk through the governed
             ``KnowledgeWrite.quarantine`` seam (park-before-delete, so payloads
             are never lost and the unwind itself is audited) and raise
             :class:`MemoryPromotionAuditError` — leaving no retrievable-but-
             unaudited fact and no approval row.
          3. append the ``promotion-approved`` row LAST — so a failed audit never
             arms the ``_already_promoted`` guard and the promotion stays
             retryable. If this row append fails AFTER the audit, a retry is
             safe: the same ``learned-<event_id>`` upsert is idempotent and a
             duplicate ``promoted`` audit event is acceptable (a MISSING one is
             the finding).

        This mirrors ne-identity ``issue()`` (unwind on audit failure). Residual:
        no distributed transaction spans Qdrant + the audit store, so a vector-
        store outage DURING the unwind can strand a chunk until the raised typed
        error is acted on — documented, fail-loud.
        """
        approved_by = approved_by.strip()
        if not approved_by or approved_by == agent_id:
            raise MemoryPromotionError(
                "the proposing agent can never approve its own promotion; "
                "approved_by must be a distinct named curator"
            )
        event_ids = await self._find_proposal_event_ids(agent_id=agent_id, proposal_id=proposal_id)
        if event_ids is None:
            raise MemoryEventNotFoundError(
                f"no promotion proposal {proposal_id!r} for agent {agent_id!r}"
            )
        # Check-then-append double-promotion race under concurrent curators is an
        # NE-070 note (single-writer-safe on CI).
        if await self._already_promoted(agent_id=agent_id, proposal_id=proposal_id):
            raise MemoryPromotionError(f"proposal {proposal_id!r} already promoted")
        items: list[KnowledgeItem] = []
        for event_id in event_ids:
            event = await self._store.get(agent_id=agent_id, event_id=event_id)
            items.append(
                KnowledgeItem(
                    item_id=f"learned-{event_id}",
                    content=event.content,
                    provenance=LEARNED_BY_AGENT_PROVENANCE,
                    metadata={
                        "agent_id": agent_id,
                        "session_id": event.session_id or "",
                        "source_event_id": event.event_id,
                        "proposal_id": proposal_id,
                        "approved_by": approved_by,
                        "occurred_at": event.occurred_at.isoformat(),
                    },
                )
            )
        # (1) The ONLY brain call. On success the NE-CKD6 writer is ATOMIC
        # (store+audit or neither) for EVERY accepted item, so no lying receipt is
        # returned. On failure it unwinds its own not-yet-audited store writes
        # (park-before-delete) and raises a typed KnowledgeAuditError — but that
        # atomicity is PER-ITEM, so a MULTI-item promotion can have EARLIER items
        # already store+audit complete when a LATER item's audit append fails.
        # Those survivors carry their knowledge.upsert record yet would be
        # retrievable WITHOUT this promotion's memory.promotion.promoted audit or
        # approval row (breaking invariant #6). So on ANY upsert failure, best-effort
        # unwind EVERY attempted item back through the governed KnowledgeWrite
        # .quarantine seam (park-before-delete; a no-op raising KnowledgeItemNotFound
        # for items the writer already unwound), then re-raise the original loud
        # typed error. No approval row is written; the promotion stays cleanly
        # retryable. This uses ONLY the frozen KnowledgeWrite seam, so ne-brain is
        # still never imported here.
        try:
            receipt = await self._writer.upsert(items)
        except Exception:
            for item in items:
                try:
                    await self._writer.quarantine(
                        item.item_id,
                        f"unwound: promotion upsert failed partway (proposal {proposal_id})",
                    )
                except Exception:
                    # Per-item isolation: an item the writer already unwound is gone
                    # (KnowledgeItemNotFoundError) and a still-down sink is covered by
                    # the re-raised typed error (no cross-store transaction exists).
                    continue
            raise
        payload = json.dumps(
            {
                "proposal_id": proposal_id,
                "approved_by": approved_by,
                "accepted": list(receipt.accepted),
                "quarantined": list(receipt.quarantined),
            },
            sort_keys=True,
        )
        # (2) Audit BEFORE the approval row. On failure, UNWIND the accepted chunks
        # through the governed quarantine seam (park-before-delete) so no
        # retrievable-but-unaudited fact remains, then fail closed.
        try:
            await self._sink.append(
                new_audit_event(
                    action="memory.promotion.promoted",
                    resource=f"memory-proposal:{proposal_id}",
                    outcome=AuditOutcome.success,
                    actor=approved_by,
                    detail=payload,
                    provenance={"source": "ne-runtime", "component": "memory-promoter"},
                )
            )
        except Exception as exc:
            for item_id in receipt.accepted:
                try:
                    await self._writer.quarantine(
                        item_id,
                        f"unwound: promotion audit append failed (proposal {proposal_id})",
                    )
                except Exception:
                    # Best-effort, per-item isolation — a residual chunk is covered
                    # by the raised typed error (no cross-store transaction exists).
                    continue
            raise MemoryPromotionAuditError(
                f"promotion audit append failed for proposal {proposal_id!r}; "
                "upserted chunks unwound via governed quarantine"
            ) from exc
        # (3) Approval row LAST — a failed audit never arms the _already_promoted
        # guard, so the promotion stays retryable.
        await self._store.append(
            agent_id=agent_id,
            kind=MemoryEventKind.PROMOTION_APPROVED,
            content=payload,
            occurred_at=datetime.now(UTC),
            provenance=f"curator:{approved_by}",
        )
        return receipt
