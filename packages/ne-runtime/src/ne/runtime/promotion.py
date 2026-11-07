# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PromotionTracker — the 'assist → execute → own' evidence trail (SPEC §4.2).

An action-type earns autonomy by accumulating clean, human-approved instances.
This tracker RECORDS that evidence and LOGS an eligibility verdict — it NEVER
mutates any tier or policy. Promoting an action to a lower tier stays a human
policy-bundle change; the tracker is provably incapable of changing tier/policy
state (it has no write path to any policy store — only the append-only evidence
trail and a logged AuditEvent). §4.2 eligibility: approval rate ≥ 0.95 and zero
policy violations over a window of at least ``promotion_min_instances`` decisions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from ne.contracts.governance import AuditSink
from ne.events import new_audit_event
from ne.runtime.approval_store import SqlAlchemyApprovalStore
from ne.runtime.config import RuntimeSettings
from ne.schema import AuditOutcome


class PromotionEvidence(BaseModel):
    """A logged, immutable promotion-eligibility verdict for an action."""

    model_config = ConfigDict(frozen=True)

    action: str
    window: int
    total: int
    approved: int
    rejected_or_expired: int
    approval_rate: float
    violations: int
    min_instances: int
    eligible: bool


class PromotionTracker:
    """Evaluates + logs promotion evidence. Changes NO tier/policy state, ever."""

    def __init__(
        self, store: SqlAlchemyApprovalStore, settings: RuntimeSettings | None = None
    ) -> None:
        self._store = store
        self._settings = settings if settings is not None else RuntimeSettings()

    async def evaluate(self, action: str) -> PromotionEvidence:
        """Score the latest window of terminal decisions for ``action``."""
        cfg = self._settings.approvals
        rows = await self._store.evidence_window(action, cfg.promotion_window)
        total = len(rows)
        approved = sum(1 for r in rows if r.clean)
        rejected_or_expired = total - approved
        approval_rate = (approved / total) if total else 0.0
        since = min((r.decided_at for r in rows), default=None)
        violations = await self._store.violations_since(action, since)
        eligible = (
            total >= cfg.promotion_min_instances
            and approval_rate >= cfg.promotion_min_approval_rate
            and violations == 0
        )
        return PromotionEvidence(
            action=action,
            window=cfg.promotion_window,
            total=total,
            approved=approved,
            rejected_or_expired=rejected_or_expired,
            approval_rate=approval_rate,
            violations=violations,
            min_instances=cfg.promotion_min_instances,
            eligible=eligible,
        )

    async def evaluate_and_record(self, action: str, *, sink: AuditSink) -> PromotionEvidence:
        """Evaluate, then emit the eligibility verdict as a LOGGED AuditEvent.

        This is a 'logged data decision' (SPEC §4.2): it records evidence and the
        eligibility flag; it does NOT promote anything. No tier or policy state is
        touched.
        """
        evidence = await self.evaluate(action)
        detail = json.dumps(
            {
                "action": action,
                "eligible": evidence.eligible,
                "approval_rate": round(evidence.approval_rate, 4),
                "total": evidence.total,
                "violations": evidence.violations,
                "window": evidence.window,
            }
        )
        await sink.append(
            new_audit_event(
                action="approval.promotion.evaluated",
                resource=f"action:{action}",
                outcome=AuditOutcome.success,
                actor="system",
                detail=detail,
                provenance={"source": "ne-runtime", "component": "promotion-tracker"},
            )
        )
        return evidence

    async def record_violation(self, action: str, detail: str) -> None:
        """Record a policy violation (error-class names only) that zeroes eligibility."""
        await self._store.record_violation(action, detail, now=datetime.now(UTC))
