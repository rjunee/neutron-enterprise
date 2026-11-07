# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SqlAlchemyApprovalStore — durable, restart-surviving HITL approval requests.

Mirrors :mod:`ne.runtime.store` exactly: sync SQLAlchemy-Core over a portable
engine, so the async store methods make DIRECT sync calls on short-lived
transactions (no async DB driver, no pytest-asyncio). Portable Core column types
only, so SQLite (CI) and Postgres (prod) behave identically by construction.

Three tables:

* ``ne_approval_requests`` — one row per approval request, carrying its tier, the
  principal, the payload — the tool name plus a ``disclosure`` block (target,
  description, material parameters) read back ONLY by a named approver via
  ``read_payload``, never over the queue listing — the lifecycle status, the
  §4.2 SLA/expiry timestamps, and a nullable
  ``action_scope`` (the DBOS-stable turn token — see below). A table-level
  ``CheckConstraint`` (``ck_approval_named_approver``) makes an approver-less (or,
  at Tier-3, second-approver-less) ``approved`` row UNREPRESENTABLE on both SQLite
  and Postgres — the FIRST of the four never-auto-approve enforcement layers.
* ``ne_approval_evidence`` — the promotion-on-evidence trail: one row per terminal
  decision, with a ``clean`` flag (= the decision was an approval).
* ``ne_promotion_violations`` — recorded policy violations for an action
  (error-class names only — no values), zeroing promotion eligibility.

Every mutating method writes its row change AND appends its typed ``ApprovalEvent``
to the ne-events outbox in ONE transaction (the outbox pattern): no event without
its row, no row without its event. ``ensure_approval_schema`` also creates the
outbox tables, since the outbox rides the same engine.

The never-auto-approve invariant is enforced redundantly at the store layer too:
:meth:`SqlAlchemyApprovalStore.decide` raises :class:`ApprovalIntegrityError`
BEFORE any SQL when an ``approved`` decision carries no named approver (the SECOND
enforcement layer; the gate policy is the third and ``_to_outcome`` the fourth).

Single-terminal-transition invariant (CAS): every status transition is an atomic
compare-and-set — ``UPDATE ... WHERE request_id AND status still pending`` with a
``rowcount`` check — inside the one outbox transaction. A row reaches a terminal
status EXACTLY ONCE: under concurrent approvers (read-committed Postgres or SQLite
alike) the loser's UPDATE matches zero rows and it raises
:class:`ApprovalAlreadyDecidedError` WITHOUT writing any outbox event or evidence
row. A racing APPROVE can never overwrite a DENY (nor an expiry a landed decision).

**Scope-keyed claim (NE-INV-APPROVAL-SCOPE, invariant-audit Finding A).**
``action_scope`` carries the ambient, opaque, DBOS-stable turn token from
:func:`ne.runtime.correlation.current_action_scope` — NEVER task text, arguments,
or any other PII (mirrors the redaction note on ``correlation.py``); ``NULL`` means
the request was submitted outside any scoped turn. A partial UNIQUE index,
``uq_approval_pending_scope`` on ``(principal_id, action, tier, action_scope,``
``args_hash)`` WHERE the row is non-terminal AND both ``action_scope`` AND
``args_hash`` are NOT NULL, makes at most one PENDING row per key representable
— the atomic claim :class:`~ne.runtime.approvals.DurableApprovalGate` uses to
reattach a DBOS step-retry to the original request instead of orphaning it.
``tier`` is part of the key: two calls for the same ``(principal, action,``
``scope)`` at DIFFERENT tiers are genuinely different requests and must never
merge onto one row (that would let a higher-tier request silently ride the lower
tier's weaker approval policy — Codex review finding C1).

**Per-invocation discriminator (Argus re-check P1, round 2).** ``args_hash`` is
a sha256 DIGEST (never raw argument values — redaction discipline preserved) of
the raw tool-call args, computed by the middleware via
:func:`ne.runtime.approvals.canonical_args_hash`. Without it, two GENUINELY
DISTINCT concurrent calls (same principal/action/tier/scope, different
arguments — pydantic-ai dispatches a turn's tool calls concurrently with no
sequential guard) would collapse onto ONE approval row, letting a single human
decision authorize BOTH external writes. Pinned by
``packages/ne-runtime/tests/test_approval_scope.py`` and
``test_approval_store.py::test_pending_scope_unique_index_blocks_duplicate_insert``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa

from ne.audit import ensure_transactional_sqlite
from ne.audit.engine import call_db, default_offload
from ne.events import append, ensure_outbox_schema
from ne.runtime.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalIntegrityError,
    ApprovalRequestNotFoundError,
)
from ne.runtime.store import ne_agent_conversation_turns
from ne.schema import ApprovalEvent

approval_metadata = sa.MetaData()

#: Non-terminal statuses (still actionable / awaiting a human).
PENDING_STATUSES = ("pending", "pending_second")
#: Terminal status recording that an ADMITTED source-authority change (ACL
#: contraction/revocation, authoritative deletion, source-generation replacement)
#: permanently denies a request's arguments for the REQUEST'S
#: OWN execution principal. It may never be authored from a reading or deciding
#: approver's ACL standing — that is a per-principal 403 that leaves the row
#: pending (U2 adversarial review, standalone-gate ruling). Deliberately a
#: status of its own and NOT ``expired``: expiry is a clock fact about a deadline a
#: withheld request never reached, and ``rejected`` claims a human decided. A store
#: that cannot say WHICH fact happened cannot be audited honestly, so a proven
#: denial gets its own terminal state rather than an overloaded one.
SOURCE_WITHHELD_STATUS = "source_withheld"
#: Terminal statuses (decided; never transition again).
TERMINAL_STATUSES = ("approved", "rejected", "expired", SOURCE_WITHHELD_STATUS)

#: Name of the partial unique index enforcing at most one PENDING row per
#: ``(principal_id, action, tier, action_scope, args_hash)`` key
#: (NE-INV-APPROVAL-SCOPE). ``tier`` is part of the key (not just ``(principal,``
#: ``action, scope)``) because an action's tier can vary call-to-call (e.g.
#: amount-based tiering): omitting it would let a lower-tier pending row silently
#: absorb a higher-tier request under the same action/scope, granting it on the
#: WEAKER tier's approval policy (Codex review finding C1, NE-INV-APPROVAL-SCOPE).
#: ``args_hash`` is part of the key (round 2, Argus re-check P1) so that two
#: DISTINCT concurrent same-action/tier/principal/scope calls (different
#: arguments) never collapse onto one approval row — pydantic-ai dispatches a
#: turn's tool calls concurrently via ``asyncio.create_task`` with no sequential
#: guard for governed tools, so without this dimension a single human decision
#: could authorize two genuinely different external writes.
PENDING_SCOPE_INDEX_NAME = "uq_approval_pending_scope"
PENDING_ATTEMPT_INDEX_NAME = "uq_approval_pending_attempt"
#: Statuses derived from PENDING_STATUSES so the index can never silently drift
#: from the store's own definition of "non-terminal".
_PENDING_SCOPE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {PENDING_SCOPE_INDEX_NAME} "
    "ON ne_approval_requests (principal_id, action, tier, action_scope, args_hash) "
    f"WHERE status IN ({', '.join(repr(s) for s in PENDING_STATUSES)}) "
    "AND action_scope IS NOT NULL AND args_hash IS NOT NULL "
    "AND turn_attempt_id IS NULL"
)
_PENDING_ATTEMPT_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {PENDING_ATTEMPT_INDEX_NAME} "
    "ON ne_approval_requests (principal_id, action, tier, action_scope, args_hash, "
    "conversation_id, turn_attempt_id) "
    f"WHERE status IN ({', '.join(repr(s) for s in PENDING_STATUSES)}) "
    "AND action_scope IS NOT NULL AND args_hash IS NOT NULL "
    "AND turn_attempt_id IS NOT NULL"
)


ne_approval_requests = sa.Table(
    "ne_approval_requests",
    approval_metadata,
    sa.Column("request_id", sa.String, primary_key=True),
    sa.Column("action", sa.String, nullable=False),
    sa.Column("tier", sa.Integer, nullable=False),
    sa.Column("principal_id", sa.String, nullable=False),
    # Owner ruling 2026-07-31 SUPERSEDES the prior claim here ("only {'tool':
    # name}; never raw argument/recipient values"): this column MAY now embed a
    # 'disclosure' object (target resource, description, raw tool-call args as
    # material_parameters, tier-derived reversibility) — see
    # ne.runtime.approvals.DurableApprovalGate.read_payload, the ONLY read path,
    # which is named-approver-gated (Tier-3: membership in
    # ApprovalSettings.tier3_named_approvers; Tier<3: the ne-api approver_roles
    # pre-check) and audited (approval.payload_read) BEFORE it returns anything.
    # This column is NEVER joined onto list_pending — the queue LISTING stays
    # payload-free; only this at-rest column's sensitivity has widened.
    sa.Column("payload", sa.Text, nullable=False),
    # Opaque DBOS-stable turn token from ne.runtime.correlation.action_scope — NEVER
    # task text or any other PII (mirrors correlation.py's redaction note). NULL =
    # submitted outside any scoped turn (legacy / unscoped callers, unconstrained).
    sa.Column("action_scope", sa.String, nullable=True),
    # sha256 hex DIGEST of the raw tool-call args (middleware-computed via
    # ne.runtime.approvals.canonical_args_hash) — a DIGEST only, NEVER raw
    # argument/recipient values, mirroring action_tools' payload_hash discipline
    # (C12/redaction preserved). NULL = unscoped/legacy submission that omitted an
    # explicit hash (the gate falls back to hashing the redacted payload for those
    # callers — see approvals.py's submit() docstring).
    sa.Column("args_hash", sa.String, nullable=True),
    sa.Column("conversation_id", sa.String, nullable=True),
    sa.Column("turn_department", sa.String, nullable=True),
    sa.Column("turn_agent_name", sa.String, nullable=True),
    sa.Column("turn_attachment_id", sa.String, nullable=True),
    sa.Column("turn_attempt_id", sa.String, nullable=True),
    sa.Column("turn_trace_id", sa.String, nullable=True),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("sla_deadline", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("first_approver", sa.String, nullable=True),
    sa.Column("second_approver", sa.String, nullable=True),
    sa.Column("decided_reason", sa.String, nullable=True),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    # THE STRUCTURAL never-auto-approve (enforcement layer 1): an APPROVED row
    # MUST carry a non-empty first_approver, and at Tier-3 a non-empty second
    # approver too. Unrepresentable otherwise on SQLite AND Postgres.
    sa.CheckConstraint(
        "status != 'approved' OR (first_approver IS NOT NULL AND first_approver != '' "
        "AND (tier < 3 OR (second_approver IS NOT NULL AND second_approver != '')))",
        name="ck_approval_named_approver",
    ),
    sa.CheckConstraint(
        "(conversation_id IS NULL AND turn_department IS NULL "
        "AND turn_agent_name IS NULL AND turn_attachment_id IS NULL "
        "AND turn_attempt_id IS NULL AND turn_trace_id IS NULL) OR "
        "(conversation_id IS NOT NULL AND turn_department IS NOT NULL "
        "AND turn_agent_name IS NOT NULL AND turn_attachment_id IS NOT NULL "
        "AND turn_attempt_id IS NOT NULL AND turn_trace_id IS NOT NULL)",
        name="ck_approval_attempt_binding_all_or_none",
    ),
)

ne_approval_evidence = sa.Table(
    "ne_approval_evidence",
    approval_metadata,
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    ),
    sa.Column("request_id", sa.String, nullable=False, index=True),
    sa.Column("action", sa.String, nullable=False, index=True),
    sa.Column("tier", sa.Integer, nullable=False),
    sa.Column("outcome", sa.String, nullable=False),
    sa.Column("approver", sa.String, nullable=True),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("clean", sa.Boolean, nullable=False),
)

ne_promotion_violations = sa.Table(
    "ne_promotion_violations",
    approval_metadata,
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    ),
    sa.Column("action", sa.String, nullable=False, index=True),
    # error-class names only — no values (redaction discipline).
    sa.Column("detail", sa.String, nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_approval_schema(engine: sa.Engine) -> None:
    """Create the approval tables AND the ne-events outbox tables (idempotent).

    The outbox rides the same engine as the approval rows so that each approval
    row change and its typed ``ApprovalEvent`` commit atomically.

    Additive, idempotent upgrade (NE-INV-APPROVAL-SCOPE, mirrors the NE-070
    ``ensure_outbox_schema`` precedent): a pre-existing ``ne_approval_requests``
    table from before this unit is ALTERed to add the nullable ``action_scope``
    and ``args_hash`` columns (a no-op on a freshly created table, since
    ``create_all`` above already includes them). The partial unique index is
    unconditionally DROPPED then (re)created every call: a round-1-shaped
    4-column index (``principal_id, action, tier, action_scope``) would otherwise
    survive untouched under plain ``CREATE ... IF NOT EXISTS`` (same index NAME,
    different definition) and silently fail to enforce the widened 5-column key
    (round 2, Argus re-check P1). DROP+CREATE is idempotent and transactional on
    both SQLite and Postgres; it runs only at startup against a small table.
    """
    approval_metadata.create_all(engine)
    inspector = sa.inspect(engine)
    existing = {col["name"] for col in inspector.get_columns(ne_approval_requests.name)}
    if "action_scope" not in existing:
        with engine.begin() as conn:
            conn.execute(
                sa.text("ALTER TABLE ne_approval_requests ADD COLUMN action_scope VARCHAR")
            )
    if "args_hash" not in existing:
        with engine.begin() as conn:
            conn.execute(sa.text("ALTER TABLE ne_approval_requests ADD COLUMN args_hash VARCHAR"))
    for column in (
        "conversation_id",
        "turn_department",
        "turn_agent_name",
        "turn_attachment_id",
        "turn_attempt_id",
        "turn_trace_id",
    ):
        if column not in existing:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(f"ALTER TABLE ne_approval_requests ADD COLUMN {column} VARCHAR")
                )
    with engine.begin() as conn:
        conn.execute(sa.text(f"DROP INDEX IF EXISTS {PENDING_SCOPE_INDEX_NAME}"))
        conn.execute(sa.text(f"DROP INDEX IF EXISTS {PENDING_ATTEMPT_INDEX_NAME}"))
        conn.execute(sa.text(_PENDING_SCOPE_INDEX_SQL))
        conn.execute(sa.text(_PENDING_ATTEMPT_INDEX_SQL))
    ensure_outbox_schema(engine)


class SqlAlchemyApprovalStore:
    """Durable HITL approval requests over sync SQLAlchemy-Core (store.py style)."""

    def __init__(self, engine: sa.Engine, *, offload_db_io: bool | None = None) -> None:
        ensure_transactional_sqlite(engine)
        self._engine = engine
        # Offload the blocking approval + outbox I/O to a worker thread on non-sqlite
        # dialects so a held audit-head/approval row lock never pins the ASGI event
        # loop (P2-16). sqlite stays on-loop (thread-bound pysqlite connections).
        self._offload = default_offload(engine) if offload_db_io is None else offload_db_io

    @property
    def engine(self) -> sa.Engine:
        return self._engine

    async def create(
        self,
        *,
        request_id: str,
        action: str,
        tier: int,
        principal_id: str,
        payload: str,
        now: datetime,
        sla_deadline: datetime,
        expires_at: datetime,
        event: ApprovalEvent,
        action_scope: str | None = None,
        args_hash: str | None = None,
        attempt_binding: tuple[str, str, str, str, str, str] | None = None,
    ) -> None:
        """Insert a fresh ``pending`` request AND its ``requested`` event atomically.

        ``action_scope`` and ``args_hash`` are both optional (additive,
        NE-INV-APPROVAL-SCOPE): uniqueness binds ONLY when BOTH are non-NULL — a
        second concurrent ``create`` for the same ``(principal_id, action, tier,``
        ``action_scope, args_hash)`` key loses the partial unique index and raises
        ``sqlalchemy.exc.IntegrityError`` — the atomic half of the gate's
        claim-or-find loop. The gate guarantees a non-NULL ``args_hash`` on every
        scoped submit (explicit or payload-fallback — see ``approvals.py``).
        """
        await call_db(
            self._offload,
            self._create_sync,
            request_id,
            action,
            tier,
            principal_id,
            payload,
            now,
            sla_deadline,
            expires_at,
            event,
            action_scope,
            args_hash,
            attempt_binding,
        )

    def _create_sync(
        self,
        request_id: str,
        action: str,
        tier: int,
        principal_id: str,
        payload: str,
        now: datetime,
        sla_deadline: datetime,
        expires_at: datetime,
        event: ApprovalEvent,
        action_scope: str | None = None,
        args_hash: str | None = None,
        attempt_binding: tuple[str, str, str, str, str, str] | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            self._create_conn(
                conn,
                request_id=request_id,
                action=action,
                tier=tier,
                principal_id=principal_id,
                payload=payload,
                now=now,
                sla_deadline=sla_deadline,
                expires_at=expires_at,
                event=event,
                action_scope=action_scope,
                args_hash=args_hash,
                attempt_binding=attempt_binding,
            )

    def _create_conn(
        self,
        conn: sa.Connection,
        *,
        request_id: str,
        action: str,
        tier: int,
        principal_id: str,
        payload: str,
        now: datetime,
        sla_deadline: datetime,
        expires_at: datetime,
        event: ApprovalEvent,
        action_scope: str | None = None,
        args_hash: str | None = None,
        attempt_binding: tuple[str, str, str, str, str, str] | None = None,
        run_bound: bool = False,
    ) -> None:
        """Connection-bound primitive for the canonical Run transaction."""
        if not run_bound and action_scope is not None:
            tables = set(sa.inspect(conn).get_table_names())
            if "ne_agent_runs" in tables:
                canonical_run = conn.execute(
                    sa.text("SELECT 1 FROM ne_agent_runs WHERE run_id = :run_id"),
                    {"run_id": action_scope},
                ).first()
                if canonical_run is not None:
                    raise ApprovalIntegrityError(
                        "standalone approval writer rejects a canonical Run target"
                    )
        binding_values = (
            {
                name: None
                for name in (
                    "conversation_id",
                    "turn_department",
                    "turn_agent_name",
                    "turn_attachment_id",
                    "turn_attempt_id",
                    "turn_trace_id",
                )
            }
            if attempt_binding is None
            else dict(
                zip(
                    (
                        "conversation_id",
                        "turn_department",
                        "turn_agent_name",
                        "turn_attachment_id",
                        "turn_attempt_id",
                        "turn_trace_id",
                    ),
                    attempt_binding,
                    strict=True,
                )
            )
        )
        conn.execute(
            ne_approval_requests.insert().values(
                request_id=request_id,
                action=action,
                tier=tier,
                principal_id=principal_id,
                payload=payload,
                action_scope=action_scope,
                args_hash=args_hash,
                **binding_values,
                status="pending",
                requested_at=now,
                sla_deadline=sla_deadline,
                expires_at=expires_at,
            )
        )
        append(conn, event)

    async def get(self, request_id: str) -> sa.Row[Any] | None:
        """Return the request row, or ``None`` if no such id exists."""
        return await call_db(self._offload, self._get_sync, request_id)

    def _get_sync(self, request_id: str) -> sa.Row[Any] | None:
        with self._engine.connect() as conn:
            return conn.execute(
                sa.select(ne_approval_requests).where(
                    ne_approval_requests.c.request_id == request_id
                )
            ).first()

    async def is_bound_attempt_current(
        self,
        request_id: str,
        *,
        attempt_binding: tuple[str, str, str, str, str, str] | None = None,
    ) -> bool:
        """Return whether a bound approval still names the active claim epoch.

        Unbound/background approvals retain their existing lifecycle.  A bound
        approval is current only while the exact principal/department/Agent/
        attachment/attempt/trace row remains the executable claimed turn.
        """
        return await call_db(
            self._offload,
            self._is_bound_attempt_current_sync,
            request_id,
            attempt_binding,
        )

    def _is_bound_attempt_current_sync(
        self,
        request_id: str,
        attempt_binding: tuple[str, str, str, str, str, str] | None,
    ) -> bool:
        with self._engine.connect() as conn:
            approval = conn.execute(
                sa.select(
                    ne_approval_requests.c.principal_id,
                    ne_approval_requests.c.conversation_id,
                    ne_approval_requests.c.turn_department,
                    ne_approval_requests.c.turn_agent_name,
                    ne_approval_requests.c.turn_attachment_id,
                    ne_approval_requests.c.turn_attempt_id,
                    ne_approval_requests.c.turn_trace_id,
                ).where(ne_approval_requests.c.request_id == request_id)
            ).first()
            if approval is None:
                return False
            if approval.turn_attempt_id is None:
                return attempt_binding is None
            persisted = (
                str(approval.conversation_id),
                str(approval.turn_department),
                str(approval.turn_agent_name),
                str(approval.turn_attachment_id),
                str(approval.turn_attempt_id),
                str(approval.turn_trace_id),
            )
            if attempt_binding is not None and persisted != attempt_binding:
                return False
            current = conn.execute(
                sa.select(ne_agent_conversation_turns.c.id).where(
                    ne_agent_conversation_turns.c.conversation_id == persisted[0],
                    ne_agent_conversation_turns.c.owner_principal_id == str(approval.principal_id),
                    ne_agent_conversation_turns.c.department == persisted[1],
                    ne_agent_conversation_turns.c.agent_name == persisted[2],
                    ne_agent_conversation_turns.c.attachment_id == persisted[3],
                    ne_agent_conversation_turns.c.attempt_id == persisted[4],
                    ne_agent_conversation_turns.c.trace_id == persisted[5],
                    ne_agent_conversation_turns.c.publication_state == "claimed",
                )
            ).scalar_one_or_none()
        return current is not None

    @staticmethod
    def _assert_bound_attempt_current_conn(conn: sa.Connection, request_id: str) -> None:
        approval = conn.execute(
            sa.select(
                ne_approval_requests.c.principal_id,
                ne_approval_requests.c.conversation_id,
                ne_approval_requests.c.turn_department,
                ne_approval_requests.c.turn_agent_name,
                ne_approval_requests.c.turn_attachment_id,
                ne_approval_requests.c.turn_attempt_id,
                ne_approval_requests.c.turn_trace_id,
            )
            .where(ne_approval_requests.c.request_id == request_id)
            .with_for_update()
        ).first()
        if approval is None:
            raise ApprovalRequestNotFoundError(request_id)
        if approval.turn_attempt_id is None:
            return
        current = conn.execute(
            sa.select(ne_agent_conversation_turns.c.id)
            .where(
                ne_agent_conversation_turns.c.conversation_id == str(approval.conversation_id),
                ne_agent_conversation_turns.c.owner_principal_id == str(approval.principal_id),
                ne_agent_conversation_turns.c.department == str(approval.turn_department),
                ne_agent_conversation_turns.c.agent_name == str(approval.turn_agent_name),
                ne_agent_conversation_turns.c.attachment_id == str(approval.turn_attachment_id),
                ne_agent_conversation_turns.c.attempt_id == str(approval.turn_attempt_id),
                ne_agent_conversation_turns.c.trace_id == str(approval.turn_trace_id),
                ne_agent_conversation_turns.c.publication_state == "claimed",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if current is None:
            raise ApprovalRequestNotFoundError(request_id)

    def _request_is_attempt_bound_sync(self, request_id: str) -> bool:
        with self._engine.connect() as conn:
            value = conn.execute(
                sa.select(ne_approval_requests.c.turn_attempt_id).where(
                    ne_approval_requests.c.request_id == request_id
                )
            ).scalar_one_or_none()
        return value is not None

    async def find_pending_by_scope(
        self,
        *,
        principal_id: str,
        action: str,
        tier: int,
        action_scope: str,
        args_hash: str,
        attempt_binding: tuple[str, str, str, str, str, str] | None = None,
    ) -> sa.Row[Any] | None:
        """The claim-or-find read: the existing non-terminal row for this key, if any.

        Mirrors the partial unique index exactly (``principal_id``, ``action``,
        ``tier``, ``action_scope``, ``args_hash``, status in
        :data:`PENDING_STATUSES`) — the gate's atomic claim-or-find loop
        (NE-INV-APPROVAL-SCOPE) uses this to reattach a DBOS step-retry to the
        original pending request instead of orphaning it. ``tier`` MUST be
        included: a request's tier can vary call-to-call for the same action
        (e.g. amount-based tiering), and omitting it would let a higher-tier
        request silently reattach to — and inherit the weaker approval policy of
        — a lower-tier pending row (Codex review finding C1). ``args_hash`` MUST
        be included (round 2, Argus re-check P1): omitting it would let two
        DISTINCT concurrent calls (same principal/action/tier/scope, different
        arguments) collapse onto one row, letting a single human decision
        authorize two genuinely different external writes.
        """
        return await call_db(
            self._offload,
            self._find_pending_by_scope_sync,
            principal_id,
            action,
            tier,
            action_scope,
            args_hash,
            attempt_binding,
        )

    def _find_pending_by_scope_sync(
        self,
        principal_id: str,
        action: str,
        tier: int,
        action_scope: str,
        args_hash: str,
        attempt_binding: tuple[str, str, str, str, str, str] | None,
    ) -> sa.Row[Any] | None:
        binding_predicate: tuple[Any, ...]
        if attempt_binding is None:
            binding_predicate = (ne_approval_requests.c.turn_attempt_id.is_(None),)
        else:
            (
                conversation_id,
                department,
                agent_name,
                attachment_id,
                attempt_id,
                trace_id,
            ) = attempt_binding
            binding_predicate = (
                ne_approval_requests.c.conversation_id == conversation_id,
                ne_approval_requests.c.turn_department == department,
                ne_approval_requests.c.turn_agent_name == agent_name,
                ne_approval_requests.c.turn_attachment_id == attachment_id,
                ne_approval_requests.c.turn_attempt_id == attempt_id,
                ne_approval_requests.c.turn_trace_id == trace_id,
            )
        with self._engine.connect() as conn:
            return conn.execute(
                sa.select(ne_approval_requests)
                .where(
                    ne_approval_requests.c.principal_id == principal_id,
                    ne_approval_requests.c.action == action,
                    ne_approval_requests.c.tier == tier,
                    ne_approval_requests.c.action_scope == action_scope,
                    ne_approval_requests.c.args_hash == args_hash,
                    ne_approval_requests.c.status.in_(PENDING_STATUSES),
                    *binding_predicate,
                )
                .order_by(ne_approval_requests.c.requested_at)
            ).first()

    async def list_pending(self) -> list[sa.Row[Any]]:
        """Return all non-terminal (pending / pending_second) request rows."""
        return await call_db(self._offload, self._list_pending_sync)

    def _list_pending_sync(self) -> list[sa.Row[Any]]:
        with self._engine.connect() as conn:
            return list(
                conn.execute(
                    sa.select(ne_approval_requests)
                    .where(ne_approval_requests.c.status.in_(PENDING_STATUSES))
                    .order_by(ne_approval_requests.c.requested_at)
                ).all()
            )

    async def record_first_approval(
        self, request_id: str, *, approver: str, now: datetime, event: ApprovalEvent
    ) -> None:
        """Tier-3 first check: ``pending`` -> ``pending_second`` with first_approver set.

        Atomic compare-and-set on ``status == 'pending'`` (rowcount-checked): a
        concurrent transition can never be clobbered.
        """
        await call_db(
            self._offload, self._record_first_approval_sync, request_id, approver, now, event
        )

    def _record_first_approval_sync(
        self, request_id: str, approver: str, now: datetime, event: ApprovalEvent
    ) -> None:
        attempt_bound = self._request_is_attempt_bound_sync(request_id)
        with self._engine.begin() as conn:
            if attempt_bound:
                self._assert_bound_attempt_current_conn(conn, request_id)
            result = conn.execute(
                ne_approval_requests.update()
                .where(
                    ne_approval_requests.c.request_id == request_id,
                    ne_approval_requests.c.status == "pending",
                )
                .values(status="pending_second", first_approver=approver)
            )
            if result.rowcount != 1:
                current = conn.execute(
                    sa.select(ne_approval_requests.c.status).where(
                        ne_approval_requests.c.request_id == request_id
                    )
                ).first()
                if current is None:
                    raise ApprovalRequestNotFoundError(request_id)
                raise ApprovalAlreadyDecidedError(
                    f"request {request_id!r} is {current.status!r}, not 'pending'"
                )
            append(conn, event)

    async def decide(
        self,
        request_id: str,
        status: str,
        *,
        approver: str | None = None,
        second_approver: str | None = None,
        reason: str | None = None,
        now: datetime,
        event: ApprovalEvent,
        evidence_row: dict[str, Any],
    ) -> None:
        """Write a terminal decision + its event + its evidence row in ONE transaction.

        Enforcement layer 2: an ``approved`` status with no named ``approver`` raises
        :class:`ApprovalIntegrityError` BEFORE any SQL (never auto-approve).

        Single-terminal-transition compare-and-set: the terminal UPDATE is guarded by
        ``status IN pending`` and a ``rowcount == 1`` check, so under concurrent
        approvers on read-committed Postgres (and SQLite) exactly one decision lands.
        The losing decide matches zero rows and raises
        :class:`ApprovalAlreadyDecidedError` having written NO outbox event and NO
        evidence row (both run only after the rowcount check).
        """
        await call_db(
            self._offload,
            self._decide_sync,
            request_id,
            status,
            approver,
            second_approver,
            reason,
            now,
            event,
            evidence_row,
        )

    def _decide_sync(
        self,
        request_id: str,
        status: str,
        approver: str | None,
        second_approver: str | None,
        reason: str | None,
        now: datetime,
        event: ApprovalEvent,
        evidence_row: dict[str, Any],
    ) -> None:
        if status == "approved" and not (isinstance(approver, str) and approver):
            raise ApprovalIntegrityError(
                f"refused to write an APPROVED decision for {request_id!r} with no "
                "named approver (never auto-approve, SPEC §4.2)"
            )
        attempt_bound = self._request_is_attempt_bound_sync(request_id)
        with self._engine.begin() as conn:
            if attempt_bound:
                self._assert_bound_attempt_current_conn(conn, request_id)
            result = conn.execute(
                ne_approval_requests.update()
                .where(
                    ne_approval_requests.c.request_id == request_id,
                    ne_approval_requests.c.status.in_(PENDING_STATUSES),
                )
                .values(
                    status=status,
                    first_approver=approver,
                    second_approver=second_approver,
                    decided_reason=reason,
                    decided_at=now,
                )
            )
            if result.rowcount != 1:
                current = conn.execute(
                    sa.select(ne_approval_requests.c.status).where(
                        ne_approval_requests.c.request_id == request_id
                    )
                ).first()
                if current is None:
                    raise ApprovalRequestNotFoundError(request_id)
                raise ApprovalAlreadyDecidedError(
                    f"request {request_id!r} is already {current.status!r}"
                )
            append(conn, event)
            conn.execute(ne_approval_evidence.insert().values(**evidence_row))

    async def mark_escalated(self, request_id: str, *, now: datetime, event: ApprovalEvent) -> bool:
        """Set ``escalated_at`` once (idempotent). Returns True iff it escalated now.

        Atomic compare-and-set on ``escalated_at IS NULL AND status pending``
        (rowcount-checked): concurrent escalations resolve to a single winner.
        """
        return await call_db(self._offload, self._mark_escalated_sync, request_id, now, event)

    def _mark_escalated_sync(self, request_id: str, now: datetime, event: ApprovalEvent) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                ne_approval_requests.update()
                .where(
                    ne_approval_requests.c.request_id == request_id,
                    ne_approval_requests.c.escalated_at.is_(None),
                    ne_approval_requests.c.status.in_(PENDING_STATUSES),
                )
                .values(escalated_at=now)
            )
            if result.rowcount != 1:
                current = conn.execute(
                    sa.select(ne_approval_requests.c.status).where(
                        ne_approval_requests.c.request_id == request_id
                    )
                ).first()
                if current is None:
                    raise ApprovalRequestNotFoundError(request_id)
                return False
            append(conn, event)
            return True

    async def expire(
        self,
        request_id: str,
        *,
        now: datetime,
        event: ApprovalEvent,
        evidence_row: dict[str, Any],
    ) -> bool:
        """Terminal ``expired`` transition from a pending state. Returns True iff it expired now.

        Atomic compare-and-set on ``status IN pending`` (rowcount-checked): expiry can
        never clobber a concurrently-landed approve/reject; the loser returns False
        having written NO event and NO evidence row.
        """
        return await call_db(self._offload, self._expire_sync, request_id, now, event, evidence_row)

    def _expire_sync(
        self,
        request_id: str,
        now: datetime,
        event: ApprovalEvent,
        evidence_row: dict[str, Any],
    ) -> bool:
        attempt_bound = self._request_is_attempt_bound_sync(request_id)
        with self._engine.begin() as conn:
            if attempt_bound:
                self._assert_bound_attempt_current_conn(conn, request_id)
            result = conn.execute(
                ne_approval_requests.update()
                .where(
                    ne_approval_requests.c.request_id == request_id,
                    ne_approval_requests.c.status.in_(PENDING_STATUSES),
                )
                .values(status="expired", decided_at=now)
            )
            if result.rowcount != 1:
                current = conn.execute(
                    sa.select(ne_approval_requests.c.status).where(
                        ne_approval_requests.c.request_id == request_id
                    )
                ).first()
                if current is None:
                    raise ApprovalRequestNotFoundError(request_id)
                return False
            append(conn, event)
            conn.execute(ne_approval_evidence.insert().values(**evidence_row))
            return True

    async def mark_source_withheld(
        self,
        request_id: str,
        *,
        now: datetime,
        reason: str,
        evidence_row: dict[str, Any],
    ) -> bool:
        """Terminal ``source_withheld`` transition from pending. True iff it landed now.

        PRE-015A0-ENFORCEMENT. Atomic compare-and-set on ``status IN pending``
        (rowcount-checked) in ONE transaction with the evidence row: a concurrent
        approve/reject/expire either lands first — this returns False having written
        NOTHING — or loses its own equally-guarded CAS against this one. There is no
        window in which the request is both drifted and still actionable, and no
        second write is needed to finish the transition.

        No outbox ``ApprovalEvent`` is appended, unlike :meth:`decide`/:meth:`expire`.
        The ne-schema ``ApprovalDecision`` enum is generated and frozen and has no
        member for this fact; emitting ``expired`` or ``rejected`` would report a
        DIFFERENT fact than the one that occurred. The free-form
        ``ne_approval_evidence.outcome`` column written here, plus the gate's own
        ``approval.source_withheld`` AuditEvent, carry it truthfully instead.

        The bound-attempt currency check that guards :meth:`decide` and :meth:`expire`
        is deliberately NOT applied. Those gate a DECISION on a still-current turn;
        this one only removes actionability. Refusing to terminalize a superseded
        attempt's row would leave a drifted request actionable — the exact state this
        transition exists to eliminate.
        """
        return await call_db(
            self._offload, self._mark_source_withheld_sync, request_id, now, reason, evidence_row
        )

    def _mark_source_withheld_sync(
        self,
        request_id: str,
        now: datetime,
        reason: str,
        evidence_row: dict[str, Any],
    ) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                ne_approval_requests.update()
                .where(
                    ne_approval_requests.c.request_id == request_id,
                    ne_approval_requests.c.status.in_(PENDING_STATUSES),
                )
                .values(status=SOURCE_WITHHELD_STATUS, decided_reason=reason, decided_at=now)
            )
            if result.rowcount != 1:
                current = conn.execute(
                    sa.select(ne_approval_requests.c.status).where(
                        ne_approval_requests.c.request_id == request_id
                    )
                ).first()
                if current is None:
                    raise ApprovalRequestNotFoundError(request_id)
                return False
            conn.execute(ne_approval_evidence.insert().values(**evidence_row))
            return True

    async def record_violation(self, action: str, detail: str, *, now: datetime) -> None:
        """Record a policy violation (error-class names only) against ``action``."""
        await call_db(self._offload, self._record_violation_sync, action, detail, now)

    def _record_violation_sync(self, action: str, detail: str, now: datetime) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                ne_promotion_violations.insert().values(
                    action=action, detail=detail, occurred_at=now
                )
            )

    async def evidence_window(self, action: str, window: int) -> list[sa.Row[Any]]:
        """The latest ``window`` evidence rows for ``action`` (decided_at desc)."""
        return await call_db(self._offload, self._evidence_window_sync, action, window)

    def _evidence_window_sync(self, action: str, window: int) -> list[sa.Row[Any]]:
        with self._engine.connect() as conn:
            return list(
                conn.execute(
                    sa.select(ne_approval_evidence)
                    .where(ne_approval_evidence.c.action == action)
                    .order_by(
                        ne_approval_evidence.c.decided_at.desc(),
                        ne_approval_evidence.c.id.desc(),
                    )
                    .limit(window)
                ).all()
            )

    async def violations_since(self, action: str, since: datetime | None) -> int:
        """Count recorded violations for ``action`` at/after ``since`` (all if None)."""
        return await call_db(self._offload, self._violations_since_sync, action, since)

    def _violations_since_sync(self, action: str, since: datetime | None) -> int:
        stmt = (
            sa.select(sa.func.count())
            .select_from(ne_promotion_violations)
            .where(ne_promotion_violations.c.action == action)
        )
        if since is not None:
            stmt = stmt.where(ne_promotion_violations.c.occurred_at >= since)
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)
