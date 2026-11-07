# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-Postgres concurrency proof for the approval gate (NE-070H, race 3).

Env-gated on ``NE_TEST_POSTGRES_URL``. A concurrent approve vs expiry transition
must converge to EXACTLY ONE terminal row state, and the expiry-driving worker must
report the ACTUAL terminal state — never the pre-fix wrong EXPIRED when a concurrent
approve actually won the store's totally-ordered terminal CAS.

The interleave is driven with two gates over ONE shared PG engine, each with its own
store + sink: gate A's clock sits PAST expiry (so ``await_outcome`` drives the expiry
CAS); gate B's clock sits BEFORE expiry (so ``approve`` drives the grant CAS). Both
race on the same row. The loop asserts observed interleaves rather than sleep-tuning,
and requires at least one approve-win iteration (else the interleave was not
exercised).
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from ne.contracts.governance import ApprovalStatus
from ne.runtime.approval_store import (
    SqlAlchemyApprovalStore,
    approval_metadata,
    ensure_approval_schema,
    ne_approval_requests,
)
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.errors import ApprovalAlreadyDecidedError

from .conftest import RecordingAuditSink, approval_request, approval_settings

_URL = os.environ.get("NE_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="live Postgres not configured (set NE_TEST_POSTGRES_URL); "
    "pending-live-verification ledger",
)


@pytest.fixture
def pg_engine() -> Iterator[sa.Engine]:
    assert _URL is not None
    engine = sa.create_engine(_URL, pool_size=20, max_overflow=20)
    approval_metadata.drop_all(engine)
    ensure_approval_schema(engine)
    try:
        yield engine
    finally:
        approval_metadata.drop_all(engine)
        engine.dispose()


def test_concurrent_approve_vs_expiry_single_terminal_state(pg_engine: sa.Engine) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    settings = approval_settings(expiry_s=1000.0, poll_interval_s=0.0)

    def gate_with(clock_offset: float) -> DurableApprovalGate:
        return DurableApprovalGate(
            store=SqlAlchemyApprovalStore(pg_engine),
            sink=RecordingAuditSink(),
            settings=settings,
            clock=lambda: base + timedelta(seconds=clock_offset),
        )

    setup_gate = gate_with(0.0)  # submits at base; expires_at = base + 1000
    gate_a = gate_with(2000.0)  # PAST expiry: await_outcome drives the expiry CAS
    gate_b = gate_with(10.0)  # BEFORE expiry: approve drives the grant CAS

    approve_wins = 0
    for _ in range(30):
        rid = asyncio.run(setup_gate.submit(approval_request("write.external.email.send", 2)))
        holder = _race_once(gate_a, gate_b, rid)

        with pg_engine.connect() as conn:
            status = conn.execute(
                sa.select(ne_approval_requests.c.status).where(
                    ne_approval_requests.c.request_id == rid
                )
            ).scalar_one()
        assert status in ("approved", "rejected", "expired")  # exactly one terminal state

        if holder.get("b_ok"):
            # B's approve won the CAS ⇒ the row is approved ⇒ A must report APPROVED,
            # never the pre-fix wrong EXPIRED.
            assert status == "approved"
            a_outcome = holder["a_outcome"]
            assert a_outcome.status is ApprovalStatus.APPROVED  # type: ignore[union-attr]
            assert a_outcome.approver == "alice"  # type: ignore[union-attr]
            approve_wins += 1
        else:
            # A's expiry won ⇒ the row is expired and B was correctly rejected.
            assert status == "expired"

    assert approve_wins >= 1, "interleave not exercised: no approve-win iteration observed"


def _race_once(
    gate_a: DurableApprovalGate, gate_b: DurableApprovalGate, rid: str
) -> dict[str, object]:
    """Barrier-race gate A's expiry-driving await_outcome vs gate B's approve on ``rid``."""
    barrier = threading.Barrier(2)
    holder: dict[str, object] = {}

    def run_expiry() -> None:
        barrier.wait()
        holder["a_outcome"] = asyncio.run(gate_a.await_outcome(rid))

    def run_approve() -> None:
        barrier.wait()
        try:
            holder["b_outcome"] = asyncio.run(gate_b.approve(rid, approver="alice"))
            holder["b_ok"] = True
        except ApprovalAlreadyDecidedError:
            holder["b_ok"] = False

    ta = threading.Thread(target=run_expiry)
    tb = threading.Thread(target=run_approve)
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    return holder
