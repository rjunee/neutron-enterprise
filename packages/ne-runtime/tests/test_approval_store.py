# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SqlAlchemyApprovalStore — schema constraints + store transition laws."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ne.events import new_approval_event
from ne.runtime.approval_store import (
    PENDING_SCOPE_INDEX_NAME,
    SqlAlchemyApprovalStore,
    ensure_approval_schema,
    ne_approval_evidence,
    ne_approval_requests,
)
from ne.runtime.approvals import canonical_args_hash
from ne.runtime.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalIntegrityError,
)
from ne.schema import ApprovalDecision

_NOW = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)


def _requested_event(request_id: str, tier: int = 2, action: str = "write.external.email.send"):
    return new_approval_event(
        approval_id=request_id,
        tier=tier,
        decision=ApprovalDecision.requested,
        subject_ref=f"tool:{action}",
        actor="user:ada",
    )


def _decision_event(request_id: str, decision: ApprovalDecision, tier: int = 2):
    return new_approval_event(
        approval_id=request_id,
        tier=tier,
        decision=decision,
        subject_ref="tool:write.external.email.send",
        actor="alice",
    )


def _evidence(
    request_id: str, *, outcome: str, approver: str | None, tier: int = 2
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "action": "write.external.email.send",
        "tier": tier,
        "outcome": outcome,
        "approver": approver,
        "decided_at": _NOW,
        "clean": outcome == "approved",
    }


async def _create(
    store: SqlAlchemyApprovalStore,
    request_id: str,
    *,
    tier: int = 2,
    action: str = "write.external.email.send",
    principal_id: str = "user:ada",
    action_scope: str | None = None,
    args_hash: str | None = None,
) -> None:
    await store.create(
        request_id=request_id,
        action=action,
        tier=tier,
        principal_id=principal_id,
        payload=f'{{"tool": "{action}"}}',
        now=_NOW,
        sla_deadline=_NOW + timedelta(hours=4),
        expires_at=_NOW + timedelta(hours=24),
        event=_requested_event(request_id, tier=tier, action=action),
        action_scope=action_scope,
        args_hash=args_hash,
    )


def test_create_and_get_roundtrip_and_list_pending(approval_store: SqlAlchemyApprovalStore) -> None:
    async def scenario() -> None:
        await _create(approval_store, "r1")
        row = await approval_store.get("r1")
        assert row is not None
        assert row.status == "pending"
        assert row.action == "write.external.email.send"
        assert row.tier == 2
        pending = await approval_store.list_pending()
        assert [p.request_id for p in pending] == ["r1"]
        assert await approval_store.get("missing") is None

    asyncio.run(scenario())


def test_approved_row_without_approver_impossible_check_constraint(
    approval_store: SqlAlchemyApprovalStore,
) -> None:
    async def scenario() -> None:
        await _create(approval_store, "r1")

    asyncio.run(scenario())
    # A raw UPDATE to 'approved' with an empty first_approver violates the CHECK.
    with pytest.raises(IntegrityError), approval_store.engine.begin() as conn:
        conn.execute(
            ne_approval_requests.update()
            .where(ne_approval_requests.c.request_id == "r1")
            .values(status="approved", first_approver="")
        )


def test_tier3_approved_row_without_second_approver_impossible(
    approval_store: SqlAlchemyApprovalStore,
) -> None:
    async def scenario() -> None:
        await _create(approval_store, "r3", tier=3)

    asyncio.run(scenario())
    # Tier-3 approved with a first but no second approver violates the CHECK.
    with pytest.raises(IntegrityError), approval_store.engine.begin() as conn:
        conn.execute(
            ne_approval_requests.update()
            .where(ne_approval_requests.c.request_id == "r3")
            .values(status="approved", first_approver="alice", second_approver=None)
        )


def test_store_decide_refuses_empty_approver_before_sql(
    approval_store: SqlAlchemyApprovalStore,
) -> None:
    async def scenario() -> None:
        await _create(approval_store, "r1")
        with pytest.raises(ApprovalIntegrityError):
            await approval_store.decide(
                "r1",
                "approved",
                approver="",
                now=_NOW,
                event=_decision_event("r1", ApprovalDecision.approved),
                evidence_row=_evidence("r1", outcome="approved", approver=""),
            )
        # Row is untouched (still pending) — the guard ran before any SQL.
        row = await approval_store.get("r1")
        assert row is not None and row.status == "pending"

    asyncio.run(scenario())


def test_decide_twice_raises_already_decided(approval_store: SqlAlchemyApprovalStore) -> None:
    async def scenario() -> None:
        await _create(approval_store, "r1")
        await approval_store.decide(
            "r1",
            "approved",
            approver="alice",
            now=_NOW,
            event=_decision_event("r1", ApprovalDecision.approved),
            evidence_row=_evidence("r1", outcome="approved", approver="alice"),
        )
        with pytest.raises(ApprovalAlreadyDecidedError):
            await approval_store.decide(
                "r1",
                "rejected",
                approver="bob",
                now=_NOW,
                event=_decision_event("r1", ApprovalDecision.rejected),
                evidence_row=_evidence("r1", outcome="rejected", approver="bob"),
            )

    asyncio.run(scenario())


def test_decide_writes_outbox_event_and_evidence_atomically(
    approval_store: SqlAlchemyApprovalStore,
) -> None:
    async def scenario() -> None:
        await _create(approval_store, "r1")
        await approval_store.decide(
            "r1",
            "approved",
            approver="alice",
            now=_NOW,
            event=_decision_event("r1", ApprovalDecision.approved),
            evidence_row=_evidence("r1", outcome="approved", approver="alice"),
        )

    asyncio.run(scenario())
    with approval_store.engine.connect() as conn:
        approval_events = conn.execute(
            sa.text("SELECT event_type FROM ne_outbox_events WHERE event_type = 'approval'")
        ).all()
        evidence = conn.execute(
            sa.select(ne_approval_evidence).where(ne_approval_evidence.c.request_id == "r1")
        ).all()
    # requested (on create) + approved (on decide) = 2 approval outbox rows.
    assert len(approval_events) == 2
    assert len(evidence) == 1
    assert evidence[0].clean is True
    assert evidence[0].approver == "alice"


def test_concurrent_decide_exactly_one_terminal_transition(tmp_path: Any) -> None:
    """Two concurrent decide() (approve vs reject) on one pending request → one winner.

    The loser raises ApprovalAlreadyDecidedError with NO duplicate outbox event or
    evidence row (the fix-#1 CAS regression). Runs real threads against a file-backed
    SQLite store (the in-memory fixture would give each thread its own DB).
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"timeout": 30})
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    asyncio.run(_create(store, "r1"))

    barrier = threading.Barrier(2)
    wins: list[str] = []
    losses: list[Exception] = []

    def worker(status: str, approver: str, decision: ApprovalDecision) -> None:
        barrier.wait()
        try:
            asyncio.run(
                store.decide(
                    "r1",
                    status,
                    approver=approver,
                    now=_NOW,
                    event=_decision_event("r1", decision),
                    evidence_row=_evidence("r1", outcome=status, approver=approver),
                )
            )
            wins.append(status)
        except ApprovalAlreadyDecidedError as exc:
            losses.append(exc)

    threads = [
        threading.Thread(target=worker, args=("approved", "alice", ApprovalDecision.approved)),
        threading.Thread(target=worker, args=("rejected", "bob", ApprovalDecision.rejected)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert len(wins) == 1
        assert len(losses) == 1
        assert isinstance(losses[0], ApprovalAlreadyDecidedError)
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_approval_requests.c.status).where(
                    ne_approval_requests.c.request_id == "r1"
                )
            ).first()
            assert row is not None and row.status == wins[0]
            evidence = conn.execute(
                sa.select(ne_approval_evidence).where(ne_approval_evidence.c.request_id == "r1")
            ).all()
            approval_events = conn.execute(
                sa.text("SELECT event_type FROM ne_outbox_events WHERE event_type = 'approval'")
            ).all()
        # exactly one terminal decision row → one evidence row.
        assert len(evidence) == 1
        # requested (on create) + the single winning decision = 2 approval events.
        assert len(approval_events) == 2
    finally:
        engine.dispose()


def test_mark_escalated_is_idempotent(approval_store: SqlAlchemyApprovalStore) -> None:
    async def scenario() -> None:
        await _create(approval_store, "r1")
        first = await approval_store.mark_escalated(
            "r1", now=_NOW, event=_decision_event("r1", ApprovalDecision.escalated)
        )
        second = await approval_store.mark_escalated(
            "r1", now=_NOW, event=_decision_event("r1", ApprovalDecision.escalated)
        )
        assert first is True
        assert second is False
        row = await approval_store.get("r1")
        assert row is not None and row.escalated_at is not None

    asyncio.run(scenario())


# --- action_scope-keyed claim (NE-INV-APPROVAL-SCOPE, invariant-audit Finding A) ---


def test_find_pending_by_scope_matches_triple_and_excludes_terminal(
    approval_store: SqlAlchemyApprovalStore,
) -> None:
    async def scenario() -> None:
        args_hash = canonical_args_hash({"tool": "write.external.email.send"})
        await _create(approval_store, "scoped", action_scope="turn-1", args_hash=args_hash)
        await _create(approval_store, "unscoped")  # action_scope=None, must never match
        await _create(
            approval_store,
            "wrong-action",
            action="finance.payment.initiate",
            action_scope="turn-1",
            args_hash=canonical_args_hash({"tool": "finance.payment.initiate"}),
        )
        await _create(
            approval_store,
            "wrong-principal",
            principal_id="user:bob",
            action_scope="turn-1",
            args_hash=args_hash,
        )
        # Same (principal, action, scope) but a DIFFERENT tier: a distinct request,
        # must never be returned for a tier=2 lookup (Codex review finding C1 — a
        # tier mismatch must never reattach, or a higher-tier request could inherit
        # a lower tier's weaker approval policy).
        await _create(
            approval_store, "wrong-tier", tier=3, action_scope="turn-1", args_hash=args_hash
        )

        found = await approval_store.find_pending_by_scope(
            principal_id="user:ada",
            action="write.external.email.send",
            tier=2,
            action_scope="turn-1",
            args_hash=args_hash,
        )
        assert found is not None and found.request_id == "scoped"

        # The tier=3 row is found under its OWN tier — it's a real, independent row.
        found_tier3 = await approval_store.find_pending_by_scope(
            principal_id="user:ada",
            action="write.external.email.send",
            tier=3,
            action_scope="turn-1",
            args_hash=args_hash,
        )
        assert found_tier3 is not None and found_tier3.request_id == "wrong-tier"

        # No match for a scope that was never used.
        assert (
            await approval_store.find_pending_by_scope(
                principal_id="user:ada",
                action="write.external.email.send",
                tier=2,
                action_scope="turn-9",
                args_hash=args_hash,
            )
            is None
        )

        # No match for a DIFFERENT args_hash under the SAME (principal, action,
        # tier, scope) — the Argus P1 pin at the store/find layer.
        assert (
            await approval_store.find_pending_by_scope(
                principal_id="user:ada",
                action="write.external.email.send",
                tier=2,
                action_scope="turn-1",
                args_hash=canonical_args_hash({"tool": "different"}),
            )
            is None
        )

        # A terminal decision removes the row from the scoped-pending lookup.
        await approval_store.decide(
            "scoped",
            "rejected",
            approver="alice",
            now=_NOW,
            event=_decision_event("scoped", ApprovalDecision.rejected),
            evidence_row=_evidence("scoped", outcome="rejected", approver="alice"),
        )
        assert (
            await approval_store.find_pending_by_scope(
                principal_id="user:ada",
                action="write.external.email.send",
                tier=2,
                action_scope="turn-1",
                args_hash=args_hash,
            )
            is None
        )

    asyncio.run(scenario())


def test_pending_scope_unique_index_blocks_duplicate_insert(tmp_path: Any) -> None:
    """The partial unique index is the atomic half of the gate's claim-or-find loop:
    a second PENDING row for the same (principal, action, tier, scope, args_hash)
    key is rejected at the SQL layer; a terminal row leaves the index (a re-request
    can insert again); NULL-scope rows are entirely unconstrained (legacy path); a
    DIFFERENT tier is a genuinely different key and is never blocked (Codex review
    finding C1 — tier must be part of the index, not just (principal, action,
    scope)); a DIFFERENT args_hash is likewise a genuinely different key and is
    never blocked (Argus re-check P1, round 2)."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'scope.db'}")
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    args_hash = canonical_args_hash({"tool": "write.external.email.send"})
    try:
        asyncio.run(_create(store, "r1", action_scope="turn-1", args_hash=args_hash))
        with pytest.raises(IntegrityError):
            asyncio.run(_create(store, "r2", action_scope="turn-1", args_hash=args_hash))

        # A DIFFERENT tier for the same (principal, action, scope, args_hash) is
        # NOT blocked — it's a distinct request that must get its own row, never
        # silently reattach to (and inherit the approval policy of) the tier=2 row
        # above.
        asyncio.run(_create(store, "r-tier3", tier=3, action_scope="turn-1", args_hash=args_hash))

        # A DIFFERENT args_hash for the same (principal, action, tier, scope) is
        # likewise NOT blocked — two genuinely distinct concurrent calls must get
        # two independent rows (Argus re-check P1).
        other_hash = canonical_args_hash({"tool": "different"})
        asyncio.run(_create(store, "r-other-args", action_scope="turn-1", args_hash=other_hash))

        # After the first row goes terminal, a THIRD create with the same
        # quintuple succeeds (terminal rows fall outside the partial index's
        # WHERE clause).
        asyncio.run(
            store.decide(
                "r1",
                "rejected",
                approver="alice",
                now=_NOW,
                event=_decision_event("r1", ApprovalDecision.rejected),
                evidence_row=_evidence("r1", outcome="rejected", approver="alice"),
            )
        )
        asyncio.run(_create(store, "r3", action_scope="turn-1", args_hash=args_hash))

        # Two NULL-scope rows for the identical (principal, action) both succeed —
        # the legacy path is entirely unconstrained by this index.
        asyncio.run(_create(store, "n1"))
        asyncio.run(_create(store, "n2"))

        with engine.connect() as conn:
            index_names = {
                ix["name"] for ix in sa.inspect(conn).get_indexes("ne_approval_requests")
            }
        assert PENDING_SCOPE_INDEX_NAME in index_names
    finally:
        engine.dispose()


def test_ensure_schema_upgrades_legacy_approvals_table() -> None:
    """Mirrors test_outbox_edge_cases.py's NE-070 legacy-upgrade test exactly: a
    pre-unit ``ne_approval_requests`` (no ``action_scope`` column) gets the column
    + the partial unique index added additively and idempotently."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE ne_approval_requests ("
                "request_id TEXT PRIMARY KEY, action TEXT NOT NULL, tier INTEGER NOT NULL, "
                "principal_id TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, "
                "requested_at TIMESTAMP NOT NULL, sla_deadline TIMESTAMP NOT NULL, "
                "expires_at TIMESTAMP NOT NULL, escalated_at TIMESTAMP, "
                "first_approver TEXT, second_approver TEXT, decided_reason TEXT, "
                "decided_at TIMESTAMP, "
                "CONSTRAINT ck_approval_named_approver CHECK ("
                "status != 'approved' OR (first_approver IS NOT NULL AND first_approver != '' "
                "AND (tier < 3 OR (second_approver IS NOT NULL AND second_approver != ''))))"
                ")"
            )
        )
    before = {c["name"] for c in sa.inspect(engine).get_columns("ne_approval_requests")}
    assert "action_scope" not in before
    assert "args_hash" not in before

    ensure_approval_schema(engine)
    after = {c["name"] for c in sa.inspect(engine).get_columns("ne_approval_requests")}
    assert "action_scope" in after
    assert "args_hash" in after

    with engine.connect() as conn:
        index_sql = conn.execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND name = 'uq_approval_pending_scope'"
            )
        ).scalar()
    assert index_sql is not None and "args_hash" in index_sql

    # Idempotent second call is a no-op (no duplicate-column / duplicate-index error).
    ensure_approval_schema(engine)

    store = SqlAlchemyApprovalStore(engine)
    args_hash = canonical_args_hash({"tool": "write.external.email.send"})

    async def scenario() -> None:
        await _create(store, "r1", action_scope="turn-1", args_hash=args_hash)
        found = await store.find_pending_by_scope(
            principal_id="user:ada",
            action="write.external.email.send",
            tier=2,
            action_scope="turn-1",
            args_hash=args_hash,
        )
        assert found is not None and found.request_id == "r1"

    asyncio.run(scenario())
    # The index landed on the UPGRADED table too.
    with pytest.raises(IntegrityError):
        asyncio.run(_create(store, "r2", action_scope="turn-1", args_hash=args_hash))
    engine.dispose()


def test_ensure_schema_widens_round1_index(tmp_path: Any) -> None:
    """A round-1-shaped database (``action_scope`` present, but the OLD 4-column
    index and NO ``args_hash`` column) must be upgraded IN PLACE by a plain
    ``ensure_approval_schema`` call: the ``args_hash`` column is added, and the
    stale 4-column index is DROPPED and REPLACED by the 5-column definition —
    plain ``CREATE ... IF NOT EXISTS`` alone would leave the old, narrower index
    silently in place under the same name (Argus re-check P1, round 2)."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'round1.db'}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE ne_approval_requests ("
                "request_id TEXT PRIMARY KEY, action TEXT NOT NULL, tier INTEGER NOT NULL, "
                "principal_id TEXT NOT NULL, payload TEXT NOT NULL, "
                "action_scope TEXT, status TEXT NOT NULL, "
                "requested_at TIMESTAMP NOT NULL, sla_deadline TIMESTAMP NOT NULL, "
                "expires_at TIMESTAMP NOT NULL, escalated_at TIMESTAMP, "
                "first_approver TEXT, second_approver TEXT, decided_reason TEXT, "
                "decided_at TIMESTAMP, "
                "CONSTRAINT ck_approval_named_approver CHECK ("
                "status != 'approved' OR (first_approver IS NOT NULL AND first_approver != '' "
                "AND (tier < 3 OR (second_approver IS NOT NULL AND second_approver != ''))))"
                ")"
            )
        )
        # The exact round-1 (pre-args_hash) index definition.
        conn.execute(
            sa.text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_pending_scope "
                "ON ne_approval_requests (principal_id, action, tier, action_scope) "
                "WHERE status IN ('pending', 'pending_second') AND action_scope IS NOT NULL"
            )
        )
    before = {c["name"] for c in sa.inspect(engine).get_columns("ne_approval_requests")}
    assert "action_scope" in before
    assert "args_hash" not in before
    with engine.connect() as conn:
        before_index_sql = conn.execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND name = 'uq_approval_pending_scope'"
            )
        ).scalar()
    assert before_index_sql is not None and "args_hash" not in before_index_sql

    ensure_approval_schema(engine)

    after = {c["name"] for c in sa.inspect(engine).get_columns("ne_approval_requests")}
    assert "args_hash" in after
    with engine.connect() as conn:
        after_index_sql = conn.execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND name = 'uq_approval_pending_scope'"
            )
        ).scalar()
    assert after_index_sql is not None
    for column in ("principal_id", "action", "tier", "action_scope", "args_hash"):
        assert column in after_index_sql
    assert "args_hash IS NOT NULL" in after_index_sql
    engine.dispose()
