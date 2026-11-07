# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-Postgres proofs for the PRE-009A Run store's concurrent claim paths.

Env-gated on ``NE_TEST_POSTGRES_URL`` and PROMOTED into the direct and pooled
live-Postgres CI lanes, which fail the job if any node here skips or collects
zero. In-memory SQLite cannot stand in for these: its global writer lock
serializes the transactions whose interleaving is exactly what is under test, so
a SQLite "concurrency" proof passes against the broken implementation too.

Every claim proof runs genuinely concurrent workers on separate connections and
stores, released together from one :class:`threading.Barrier`, and asserts BOTH
that no worker raised (no leaked ``IntegrityError``) and that the persisted rows
are exactly the winner's.

:func:`test_unlocked_max_plus_one_event_sequence_duplicates_under_the_same_harness`
is the positive control for the sequence proof: it reimplements the pre-fix
``max(sequence) + 1`` allocation under the identical harness and shows it
DUPLICATES, so a green sequence proof cannot be green merely because the harness
never produced a race.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy import event
from sqlalchemy.exc import DatabaseError, IntegrityError, ProgrammingError

from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import SourceAuthorityStamp
from ne.events import new_approval_event
from ne.runtime.approval_store import (
    SqlAlchemyApprovalStore,
    approval_metadata,
    ensure_approval_schema,
    ne_approval_evidence,
    ne_approval_requests,
)
from ne.runtime.private_payloads import (
    AGENT_RUN_TURN_TARGET_KIND,
    CanonicalTargetTuple,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    RunPrivatePayloadCipher,
    StaticPrivatePayloadKeys,
)
from ne.runtime.run_checkpoint import (
    RunApprovalCheckpointStore,
    ne_agent_run_approval_audit_outbox,
    ne_agent_run_approval_joins,
    ne_agent_run_approval_resume_intents,
    ne_agent_run_continuation_checkpoints,
)
from ne.runtime.run_store import (
    LEGACY_TRANSPORT_GENERATION,
    OWNERLESS_RETIREMENT_REASON,
    AgentRunStore,
    ClaimDeclaration,
    EvidenceDeclaration,
    EvidenceSetDeclaration,
    ProtectedSliceUnavailable,
    RunAuditDecisionKind,
    RunState,
    RunSubmissionConflictError,
    ensure_run_schema,
    ne_agent_claim_verifier_receipts,
    ne_agent_delivery_receipts,
    ne_agent_evidence_records,
    ne_agent_evidence_sets,
    ne_agent_pending_source_tool_results,
    ne_agent_protected_source_invocations,
    ne_agent_protected_turn_contents,
    ne_agent_run_attempt_links,
    ne_agent_run_audit_receipts,
    ne_agent_run_claims,
    ne_agent_run_enqueue_outbox,
    ne_agent_run_events,
    ne_agent_run_inputs,
    ne_agent_run_submissions,
    ne_agent_runs,
)
from ne.runtime.run_workflow import RunWorkflowStateStore, ensure_run_workflow_schema
from ne.runtime.store import (
    SqlAlchemySessionStore,
    ensure_runtime_schema,
    metadata,
    ne_agent_conversation_turns,
    ne_agent_session_messages,
)
from ne.schema import ApprovalDecision

from .test_run_finalizer import _prove_source_delivery_uses_exact_evidence_projection_readers
from .test_run_store import _create_run, _stamp, _verified_evidence_set

_URL = os.environ.get("NE_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="live Postgres not configured (set NE_TEST_POSTGRES_URL); pending promoted proof",
)

WORKERS = 8
# Per-round detection of a lost claim arbitration measured at 24/25; see _race_claims.
CLAIM_RACE_ROUNDS = 10


@pytest.fixture
def pg_engine() -> Iterator[sa.Engine]:
    assert _URL is not None
    engine = sa.create_engine(_URL, pool_size=WORKERS * 2, max_overflow=WORKERS * 2)
    metadata.drop_all(engine)
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    try:
        yield engine
    finally:
        metadata.drop_all(engine)
        engine.dispose()


def _run_store(engine: sa.Engine) -> AgentRunStore:
    return AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="pg-k1", keys={"pg-k1": b"p" * 32})
        ),
    )


def _prove_run_approval_wait_converges_and_rolls_back_atomically_on_postgres(
    pg_engine: sa.Engine,
) -> None:
    ensure_approval_schema(pg_engine)
    sessions = SqlAlchemySessionStore(pg_engine)
    runs = _run_store(pg_engine)
    approvals = SqlAlchemyApprovalStore(pg_engine)
    checkpoints = RunApprovalCheckpointStore(runs, approvals)

    async def seed_running() -> tuple[str, str]:
        _, run = await _create_run(sessions, runs)
        await runs.transition(
            run_id=run.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED
        )
        await runs.transition(run_id=run.run_id, expected=RunState.QUEUED, target=RunState.RUNNING)
        attempt = await runs.link_attempt(run_id=run.run_id)
        return run.run_id, attempt.attempt_id

    def kwargs(run_id: str, attempt_id: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        return {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "step_id": "step-approval",
            "tool_call_id": "tool-call-approval",
            "action": "write.external.email.send",
            "action_hash": "a" * 64,
            "tier": 2,
            "principal_id": "alice",
            "policy_hash": "b" * 64,
            "invocation_plaintext": b"POSTGRES private approval sentinel",
            "source_stamps": (_stamp(),),
            "stamp_receipt_id": "opaque-receipt",
            "stamp_receipt_hash": "c" * 64,
            "requested_at": now,
            "sla_deadline": now + timedelta(hours=1),
            "expires_at": now + timedelta(hours=2),
            "event": new_approval_event(
                approval_id="allocated-inside",
                tier=2,
                decision=ApprovalDecision.requested,
                subject_ref="tool:write.external.email.send",
                actor="alice",
            ),
        }

    async def scenario() -> tuple[str, str]:
        run_id, attempt_id = await seed_running()
        winners = await asyncio.gather(
            *(checkpoints.create_wait(**kwargs(run_id, attempt_id)) for _ in range(WORKERS))
        )
        assert len({winner.approval_id for winner in winners}) == 1

        rollback_run_id, rollback_attempt_id = await seed_running()

        def fail_outbox(
            _conn: sa.Connection,
            clauseelement: Any,
            _multiparams: Any,
            _params: Any,
            _execution_options: Any,
        ) -> None:
            if getattr(clauseelement, "table", None) is ne_agent_run_approval_audit_outbox:
                raise RuntimeError("forced approval outbox failure")

        event.listen(pg_engine, "before_execute", fail_outbox)
        try:
            with pytest.raises(RuntimeError, match="forced approval outbox failure"):
                await checkpoints.create_wait(**kwargs(rollback_run_id, rollback_attempt_id))
        finally:
            event.remove(pg_engine, "before_execute", fail_outbox)
        return run_id, rollback_run_id

    run_id, rollback_run_id = asyncio.run(scenario())
    with pg_engine.connect() as conn:
        for table in (
            ne_approval_requests,
            ne_agent_protected_source_invocations,
            ne_agent_run_continuation_checkpoints,
            ne_agent_run_approval_joins,
            ne_agent_run_approval_audit_outbox,
        ):
            assert (
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(table)
                    .where(
                        table.c.run_id == run_id
                        if "run_id" in table.c
                        else table.c.request_id
                        == sa.select(ne_agent_run_approval_joins.c.approval_id)
                        .where(ne_agent_run_approval_joins.c.run_id == run_id)
                        .scalar_subquery()
                    )
                ).scalar_one()
                == 1
            )
        assert (
            conn.execute(
                sa.select(ne_agent_runs.c.status).where(ne_agent_runs.c.run_id == run_id)
            ).scalar_one()
            == RunState.WAITING_APPROVAL.value
        )
        assert (
            conn.execute(
                sa.select(ne_agent_runs.c.status).where(ne_agent_runs.c.run_id == rollback_run_id)
            ).scalar_one()
            == RunState.RUNNING.value
        )
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_run_approval_joins)
                .where(ne_agent_run_approval_joins.c.run_id == rollback_run_id)
            ).scalar_one()
            == 0
        )
    approval_metadata.drop_all(pg_engine)


def _prove_accepted_submission_converges_and_rolls_back_atomically_on_postgres(
    pg_engine: sa.Engine,
) -> None:
    sessions = SqlAlchemySessionStore(pg_engine)
    runs = _run_store(pg_engine)

    async def scenario() -> tuple[str, str]:
        conversation = await sessions.create_conversation("assistant", "claims", "alice")
        command = SimpleNamespace(
            idempotency_key="same-start",
            request_hash="a" * 64,
            conversation_id=conversation.conversation_id,
            expected_version=0,
            owner_principal_id="alice",
            department="claims",
            conversation_agent_name="assistant",
            agent_id="agt_01",
            agent_version=7,
            strategy="agentic",
            config_hash="1" * 64,
            capability_snapshot_hash="2" * 64,
            capability_snapshot_receipt_id="cap-receipt-1",
            input_plaintext=b"PRIVATE postgres admission sentinel",
            authenticated_context=AuthenticatedRequestContext(
                principal_id="alice",
                authorization_groups=(),
                department="claims",
                issuer="https://issuer.example.test",
                expires_at=4_102_444_800,
            ),
        )
        accepted = await asyncio.gather(*(runs.admit_start(command) for _ in range(WORKERS)))
        assert len({(row.run_id, row.attempt_id, row.enqueue_id) for row in accepted}) == 1
        assert sum(row.replayed for row in accepted) == WORKERS - 1
        with pytest.raises(RunSubmissionConflictError):
            await runs.admit_start(SimpleNamespace(**{**vars(command), "request_hash": "b" * 64}))

        rollback_conversation = await sessions.create_conversation("assistant", "claims", "alice")
        rollback = SimpleNamespace(
            **{
                **vars(command),
                "conversation_id": rollback_conversation.conversation_id,
                "idempotency_key": "rollback-start",
                "request_hash": "c" * 64,
            }
        )

        def fail_enqueue(
            _conn: sa.Connection,
            clauseelement: Any,
            _multiparams: Any,
            _params: Any,
            _execution_options: Any,
        ) -> None:
            if getattr(clauseelement, "table", None) is ne_agent_run_enqueue_outbox:
                raise RuntimeError("forced enqueue failure")

        event.listen(pg_engine, "before_execute", fail_enqueue)
        try:
            with pytest.raises(RuntimeError, match="forced enqueue failure"):
                await runs.admit_start(rollback)
        finally:
            event.remove(pg_engine, "before_execute", fail_enqueue)
        return accepted[0].run_id, rollback_conversation.conversation_id

    run_id, rollback_conversation_id = asyncio.run(scenario())
    with pg_engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(ne_agent_runs)).scalar_one() == 1
        assert (
            conn.execute(sa.select(sa.func.count()).select_from(ne_agent_run_inputs)).scalar_one()
            == 1
        )
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_run_submissions)
            ).scalar_one()
            == 1
        )
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_run_enqueue_outbox)
            ).scalar_one()
            == 1
        )
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_runs)
                .where(ne_agent_runs.c.conversation_id == rollback_conversation_id)
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                sa.select(ne_agent_runs.c.run_id).where(ne_agent_runs.c.run_id == run_id)
            ).scalar_one()
            == run_id
        )


def _race(worker: Callable[[int], None], *, workers: int = WORKERS) -> list[BaseException]:
    """Release ``workers`` threads together; return whatever they raised."""
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def wrapped(index: int) -> None:
        barrier.wait()
        try:
            worker(index)
        except BaseException as exc:  # the point of the proof is exactly what escaped
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def _race_claims(
    build: Callable[[int, int], Callable[[], Awaitable[Any]]],
    *,
    workers: int = WORKERS,
    rounds: int = CLAIM_RACE_ROUNDS,
) -> tuple[list[Any], list[BaseException]]:
    """Race ``workers`` claimants per round, with ALL setup done before the barrier.

    ``build(round, index)`` returns a zero-argument coroutine factory; it and the
    thread's event loop are constructed BEFORE ``barrier.wait()``.

    That ordering is load-bearing, and it was measured, not assumed. With the event
    loop and store built AFTER the barrier, entry into the claim spread ~6 ms across
    eight threads while the claim transaction itself commits in well under 1 ms — so
    each thread simply read the previous winner and a lock-free SELECT-then-INSERT
    passed this proof unharmed. With setup hoisted, the same mutant raises
    ``UniqueViolation`` in 24 of 25 rounds; ``rounds`` turns that into a certainty.
    :func:`test_an_unarbitrated_select_then_insert_raises_under_the_same_harness`
    is the standing control that this harness still produces the interleaving.
    """
    results: list[Any] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    for round_index in range(rounds):
        barrier = threading.Barrier(workers)

        def run(
            index: int,
            round_index: int = round_index,
            barrier: threading.Barrier = barrier,
        ) -> None:
            loop = asyncio.new_event_loop()
            factory: Callable[[], Awaitable[Any]] | None = None
            try:
                factory = build(round_index, index)
            except BaseException as exc:
                with lock:
                    errors.append(exc)
            try:
                barrier.wait()  # every thread reaches this, so a failure cannot deadlock
                if factory is None:
                    return
                value = loop.run_until_complete(factory())
                with lock:
                    results.append(value)
            except BaseException as exc:
                with lock:
                    errors.append(exc)
            finally:
                loop.close()

        threads = [threading.Thread(target=run, args=(index,)) for index in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    return results, errors


def _seed_run(engine: sa.Engine, *, owner: str = "alice") -> tuple[str, str]:
    """Create one Conversation-owned Run plus its first attempt; return both ids."""
    sessions = SqlAlchemySessionStore(engine)
    runs = _run_store(engine)

    async def scenario() -> tuple[str, str]:
        conversation = await sessions.create_conversation("assistant", "claims", owner)
        run = await runs.create_run(
            conversation_id=conversation.conversation_id,
            owner_principal_id=owner,
            department="claims",
            conversation_agent_name="assistant",
            agent_id="agt_01",
            agent_version=7,
            strategy="agentic",
            config_hash="1" * 64,
            capability_snapshot_hash="2" * 64,
            capability_snapshot_receipt_id="cap-receipt-1",
            input_plaintext=b"private prompt",
        )
        attempt = await runs.link_attempt(run_id=run.run_id)
        return run.run_id, attempt.attempt_id

    return asyncio.run(scenario())


def _declaration(*, tool_call_id: str, read_ordinal: int) -> EvidenceDeclaration:
    return EvidenceDeclaration(
        turn_id="turn-1",
        step_id="step-1",
        tool_call_id=tool_call_id,
        read_ordinal=read_ordinal,
        content_hash="3" * 64,
        safe_event_detail={"evidence_status": "located"},
        locator_plaintext=b"locator sentinel",
        pending_result_plaintext=b"protected tool result sentinel",
        source_stamps=(_stamp(),),
    )


def _stamps() -> tuple[SourceAuthorityStamp, ...]:
    return (_stamp(),)


def test_schema_upgrade_is_advisory_locked_and_idempotent(pg_engine: sa.Engine) -> None:
    errors = _race(lambda _index: ensure_run_schema(pg_engine), workers=6)
    assert errors == []
    tables = set(sa.inspect(pg_engine).get_table_names())
    assert {
        "ne_agent_runs",
        "ne_agent_run_inputs",
        "ne_agent_run_events",
        "ne_agent_run_event_seq",
        "ne_agent_evidence_records",
        "ne_agent_run_claims",
        "ne_agent_run_audit_receipts",
    } <= tables
    _prove_accepted_submission_converges_and_rolls_back_atomically_on_postgres(pg_engine)
    _prove_run_approval_wait_converges_and_rolls_back_atomically_on_postgres(pg_engine)
    _prove_concurrent_approval_decision_is_atomic_and_restart_recoverable(pg_engine)


def _prove_concurrent_approval_decision_is_atomic_and_restart_recoverable(
    pg_engine: sa.Engine,
) -> None:
    """One contender authors the exact decision/checkpoint/resume commit."""

    ensure_approval_schema(pg_engine)
    ensure_run_workflow_schema(pg_engine)
    sessions = SqlAlchemySessionStore(pg_engine)
    runs = _run_store(pg_engine)
    checkpoints = RunApprovalCheckpointStore(runs, SqlAlchemyApprovalStore(pg_engine))
    lifecycle = RunWorkflowStateStore(pg_engine, run_store=runs)

    async def park() -> tuple[str, str]:
        conversation = await sessions.create_conversation("assistant", "claims", "alice")
        accepted = await runs.admit_start(
            SimpleNamespace(
                idempotency_key="approval-winner",
                request_hash="9" * 64,
                conversation_id=conversation.conversation_id,
                expected_version=0,
                owner_principal_id="alice",
                department="claims",
                conversation_agent_name="assistant",
                agent_id="agt_01",
                agent_version=7,
                strategy="agentic",
                config_hash="1" * 64,
                capability_snapshot_hash="2" * 64,
                capability_snapshot_receipt_id="cap-approval-winner",
                input_plaintext=b"PRIVATE postgres approval input",
                authenticated_context=AuthenticatedRequestContext(
                    principal_id="alice",
                    authorization_groups=(),
                    department="claims",
                    issuer="https://issuer.example.test",
                    expires_at=4_102_444_800,
                ),
            )
        )
        lifecycle.claim(
            dispatch_id=accepted.enqueue_id,
            run_id=accepted.run_id,
            owner_principal_id="alice",
            config_hash="1" * 64,
        )
        now = datetime.now(UTC)
        joined = await checkpoints.create_wait(
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            step_id="step-approval-winner",
            tool_call_id="tool-approval-winner",
            action="write.external.email.send",
            action_hash="a" * 64,
            tier=2,
            principal_id="alice",
            policy_hash="b" * 64,
            invocation_plaintext=b'{"subject":"protected"}',
            source_stamps=(_stamp(),),
            stamp_receipt_id="stamp-approval-winner",
            stamp_receipt_hash="c" * 64,
            requested_at=now,
            sla_deadline=now + timedelta(hours=1),
            expires_at=now + timedelta(hours=2),
            event=new_approval_event(
                approval_id="allocated-inside",
                tier=2,
                decision=ApprovalDecision.requested,
                subject_ref="tool:write.external.email.send",
                actor="alice",
            ),
        )
        return joined.approval_id, accepted.run_id

    approval_id, run_id = asyncio.run(park())
    winners: list[bool] = []
    winner_lock = threading.Lock()

    def decide(_index: int) -> None:
        result = asyncio.run(
            RunApprovalCheckpointStore(
                _run_store(pg_engine), SqlAlchemyApprovalStore(pg_engine)
            ).decide(
                approval_id,
                status="approved",
                expected_action_hash="a" * 64,
                principal_id="alice",
                policy_hash="b" * 64,
                approver="approver:one",
                reason=None,
                decided_at=datetime.now(UTC),
                event=new_approval_event(
                    approval_id="resolved-through-join",
                    tier=2,
                    decision=ApprovalDecision.approved,
                    subject_ref="tool:write.external.email.send",
                    actor="approver:one",
                ),
            )
        )
        with winner_lock:
            winners.append(result)

    assert _race(decide) == []
    assert winners.count(True) == 1
    assert winners.count(False) == WORKERS - 1

    assert _URL is not None
    restarted = sa.create_engine(_URL, pool_size=2, max_overflow=2)
    try:
        restart_checkpoints = RunApprovalCheckpointStore(
            _run_store(restarted), SqlAlchemyApprovalStore(restarted)
        )
        resume = asyncio.run(restart_checkpoints.load_resume_request(approval_id))
        assert resume.run_id == run_id
        assert resume.decision == "approved"
        assert asyncio.run(restart_checkpoints.load_pending_resume_requests()) == (resume,)
        with restarted.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_approval_evidence)
                    .where(ne_approval_evidence.c.request_id == approval_id)
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_agent_run_approval_resume_intents)
                    .where(ne_agent_run_approval_resume_intents.c.approval_id == approval_id)
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    sa.select(ne_agent_run_continuation_checkpoints.c.status).where(
                        ne_agent_run_continuation_checkpoints.c.run_id == run_id
                    )
                ).scalar_one()
                == "approved"
            )
    finally:
        restarted.dispose()


def test_concurrent_run_and_same_predecessor_allocation_are_serialized(
    pg_engine: sa.Engine,
) -> None:
    sessions = SqlAlchemySessionStore(pg_engine)
    conversation = asyncio.run(sessions.create_conversation("assistant", "claims", "alice"))
    values: list[int] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        run = asyncio.run(
            _run_store(pg_engine).create_run(
                conversation_id=conversation.conversation_id,
                owner_principal_id="alice",
                department="claims",
                conversation_agent_name="assistant",
                agent_id="agt_01",
                agent_version=1,
                strategy="agentic",
                config_hash="1" * 64,
                capability_snapshot_hash=f"{index + 1:064x}",
                capability_snapshot_receipt_id=f"cap-{index}",
                input_plaintext=f"private prompt {index}".encode(),
            )
        )
        with lock:
            values.append(run.sequence)

    assert _race(worker) == []
    assert sorted(values) == list(range(1, WORKERS + 1))
    with pg_engine.connect() as conn:
        persisted = list(
            conn.execute(
                sa.select(ne_agent_runs.c.sequence).order_by(ne_agent_runs.c.sequence)
            ).scalars()
        )
    assert persisted == list(range(1, WORKERS + 1))
    retry_runs = [_seed_run(pg_engine) for _ in range(CLAIM_RACE_ROUNDS)]

    def build(round_index: int, _index: int) -> Callable[[], Awaitable[Any]]:
        run_id, predecessor_attempt_id = retry_runs[round_index]
        store = _run_store(pg_engine)

        def call() -> Awaitable[Any]:
            return store.link_attempt(run_id=run_id, predecessor_attempt_id=predecessor_attempt_id)

        return call

    successors, errors = _race_claims(build)
    assert len(successors) == CLAIM_RACE_ROUNDS
    assert len(errors) == (WORKERS - 1) * CLAIM_RACE_ROUNDS
    assert all(
        isinstance(error, ValueError)
        and "attempt predecessor is not the exact current Run attempt" in str(error)
        for error in errors
    )
    assert {successor.run_id for successor in successors} == {
        run_id for run_id, _predecessor_attempt_id in retry_runs
    }
    assert all(successor.attempt_number == 2 for successor in successors)
    successor = successors[0]
    run_id = successor.run_id
    where = ne_agent_run_attempt_links.c.attempt_id == successor.attempt_id
    with (
        pg_engine.begin() as conn,
        pytest.raises(DatabaseError, match="Run attempt lineage is immutable"),
    ):
        conn.execute(ne_agent_run_attempt_links.update().where(where).values(attempt_number=3))
    with (
        pg_engine.begin() as conn,
        pytest.raises(DatabaseError, match="Run attempt lineage is immutable"),
    ):
        conn.execute(ne_agent_run_attempt_links.delete().where(where))
    with pg_engine.connect() as conn:
        row = conn.execute(sa.select(ne_agent_run_attempt_links).where(where)).one()
    assert str(row.run_id) == run_id
    assert int(row.attempt_number) == 2


def test_concurrent_distinct_evidence_claims_allocate_unique_gapless_event_sequences(
    pg_engine: sa.Engine,
) -> None:
    run_id, attempt_id = _seed_run(pg_engine)

    def worker(index: int) -> None:
        asyncio.run(
            _run_store(pg_engine).claim_evidence(
                run_id=run_id,
                owner_principal_id="alice",
                attempt_id=attempt_id,
                declaration=_declaration(tool_call_id=f"tool-{index}", read_ordinal=index),
            )
        )

    assert _race(worker) == []
    with pg_engine.connect() as conn:
        sequences = list(
            conn.execute(
                sa.select(ne_agent_run_events.c.sequence)
                .where(ne_agent_run_events.c.run_id == run_id)
                .order_by(ne_agent_run_events.c.sequence)
            ).scalars()
        )
    # Contiguous, gapless AND unique: two writers must not both pick max + 1.
    assert sequences == list(range(1, WORKERS + 1))


def test_unlocked_max_plus_one_event_sequence_duplicates_under_the_same_harness(
    pg_engine: sa.Engine,
) -> None:
    """Positive control: the pre-fix allocation loses where the counter claim wins."""
    red = sa.Table(
        "pre009a_red_control_events",
        sa.MetaData(),
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.String, nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
    )
    red.metadata.drop_all(pg_engine)
    red.metadata.create_all(pg_engine)
    try:

        def worker(_index: int) -> None:
            for _ in range(25):
                with pg_engine.begin() as conn:
                    current = conn.execute(
                        sa.select(sa.func.max(red.c.sequence)).where(red.c.run_id == "red")
                    ).scalar()
                    conn.execute(
                        red.insert().values(
                            run_id="red", sequence=1 if current is None else int(current) + 1
                        )
                    )

        assert _race(worker) == []
        with pg_engine.connect() as conn:
            sequences = list(
                conn.execute(sa.select(red.c.sequence).where(red.c.run_id == "red")).scalars()
            )
        assert len(sequences) > len(set(sequences))
    finally:
        red.metadata.drop_all(pg_engine)


def test_concurrent_same_invocation_evidence_claims_yield_one_record_without_integrity_error(
    pg_engine: sa.Engine,
) -> None:
    run_id, attempt_id = _seed_run(pg_engine)

    def build(round_index: int, _index: int) -> Callable[[], Awaitable[Any]]:
        store = _run_store(pg_engine)
        declaration = _declaration(tool_call_id=f"tool-shared-{round_index}", read_ordinal=0)

        def call() -> Awaitable[Any]:
            return store.claim_evidence(
                run_id=run_id,
                owner_principal_id="alice",
                attempt_id=attempt_id,
                declaration=declaration,
            )

        return call

    records, errors = _race_claims(build)
    assert errors == []
    assert not [error for error in errors if isinstance(error, IntegrityError)]
    assert len(records) == WORKERS * CLAIM_RACE_ROUNDS
    by_key: dict[str, set[Any]] = {}
    for record in records:
        by_key.setdefault(record.invocation_key, set()).add(record)
    assert len(by_key) == CLAIM_RACE_ROUNDS
    assert all(len(values) == 1 for values in by_key.values())
    with pg_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_evidence_records)
            ).scalar_one()
            == CLAIM_RACE_ROUNDS
        )
        # Every loser's speculative protected row and RunEvent rolled back with it.
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_pending_source_tool_results)
            ).scalar_one()
            == CLAIM_RACE_ROUNDS
        )
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_run_events)
                .where(ne_agent_run_events.c.run_id == run_id)
            ).scalar_one()
            == CLAIM_RACE_ROUNDS
        )
    _assert_concurrent_same_verified_set_yields_one_complete_atomic_winner(pg_engine)


def _assert_concurrent_same_verified_set_yields_one_complete_atomic_winner(
    pg_engine: sa.Engine,
) -> None:
    run_id, attempt_id = _seed_run(pg_engine)
    second = _stamp().model_copy(
        update={"authority_receipt_id": "receipt-b", "authority_receipt_hash": "e" * 64}
    )

    def build(round_index: int, _index: int) -> Callable[[], Awaitable[Any]]:
        store = _run_store(pg_engine)
        base = _verified_evidence_set((_stamp(), second))
        declaration = EvidenceSetDeclaration(
            **{
                **base.__dict__,
                "tool_call_id": f"tool-set-{round_index}",
            }
        )

        def call() -> Awaitable[Any]:
            return store.claim_evidence_set(
                run_id=run_id,
                owner_principal_id="alice",
                attempt_id=attempt_id,
                declaration=declaration,
            )

        return call

    records, errors = _race_claims(build)
    assert errors == []
    assert len(records) == WORKERS * CLAIM_RACE_ROUNDS
    by_key: dict[str, set[Any]] = {}
    for record in records:
        by_key.setdefault(record.set_key, set()).add(record)
    assert len(by_key) == CLAIM_RACE_ROUNDS
    assert all(len(values) == 1 for values in by_key.values())
    assert all(len(next(iter(values)).evidence) == 2 for values in by_key.values())
    with pg_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_evidence_sets)
                .where(ne_agent_evidence_sets.c.run_id == run_id)
            ).scalar_one()
            == CLAIM_RACE_ROUNDS
        )
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_evidence_records)
                .where(ne_agent_evidence_records.c.run_id == run_id)
            ).scalar_one()
            == CLAIM_RACE_ROUNDS * 2
        )
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_pending_source_tool_results)
                .where(ne_agent_pending_source_tool_results.c.run_id == run_id)
            ).scalar_one()
            == CLAIM_RACE_ROUNDS
        )
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_run_events)
                .where(ne_agent_run_events.c.run_id == run_id)
            ).scalar_one()
            == CLAIM_RACE_ROUNDS * 3
        )


def test_concurrent_same_target_protected_payload_claims_yield_one_row_and_one_winner(
    pg_engine: sa.Engine,
) -> None:
    run_id, _attempt_id = _seed_run(pg_engine)

    def build(round_index: int, _index: int) -> Callable[[], Awaitable[Any]]:
        store = _run_store(pg_engine)

        def call() -> Awaitable[Any]:
            return store.claim_protected_payload(
                domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                run_id=run_id,
                owner_principal_id="alice",
                target_kind="conversation_turn",
                target_binding=("conv-1", f"attach-{round_index}", "attempt-1", "input"),
                content_status=ProtectedContentStatus.SOURCE_FREE,
                plaintext=b"one exact protected payload",
            )

        return call

    refs, errors = _race_claims(build)
    assert errors == []
    assert len(refs) == WORKERS * CLAIM_RACE_ROUNDS
    # One winning row per round, and every racer in a round received exactly it.
    by_target: dict[str, set[str]] = {}
    for ref in refs:
        by_target.setdefault(ref.target_hash, set()).add(ref.row_id)
    assert len(by_target) == CLAIM_RACE_ROUNDS
    assert all(len(ids) == 1 for ids in by_target.values())
    with pg_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_protected_turn_contents)
            ).scalar_one()
            == CLAIM_RACE_ROUNDS
        )


def test_an_unarbitrated_select_then_insert_raises_under_the_same_harness(
    pg_engine: sa.Engine,
) -> None:
    """Positive control: this harness DOES produce the losing interleaving.

    Without it, every arbitration proof above is satisfiable by a harness whose
    threads never actually overlap — which is exactly how an earlier draft of this
    file passed against a lock-free SELECT-then-INSERT.
    """
    red = sa.Table(
        "pre009a_red_control_claims",
        sa.MetaData(),
        sa.Column("target_hash", sa.String, nullable=False, unique=True),
        sa.Column("row_id", sa.String, nullable=False),
    )
    red.metadata.drop_all(pg_engine)
    red.metadata.create_all(pg_engine)
    try:

        def build(round_index: int, index: int) -> Callable[[], Awaitable[Any]]:
            target = f"target-{round_index}"

            async def call() -> None:
                with pg_engine.begin() as conn:
                    prior = conn.execute(
                        sa.select(red.c.row_id).where(red.c.target_hash == target)
                    ).one_or_none()
                    if prior is None:
                        conn.execute(red.insert().values(target_hash=target, row_id=f"row-{index}"))

            return call

        _results, errors = _race_claims(build)
        assert [error for error in errors if isinstance(error, IntegrityError)]
    finally:
        red.metadata.drop_all(pg_engine)


def test_concurrent_same_delivery_address_yields_one_receipt_bound_to_one_committed_event(
    pg_engine: sa.Engine,
) -> None:
    run_id, attempt_id = _seed_run(pg_engine)

    def build(round_index: int, _index: int) -> Callable[[], Awaitable[Any]]:
        store = _run_store(pg_engine)

        def call() -> Awaitable[Any]:
            return store.claim_delivery(
                run_id=run_id,
                owner_principal_id="alice",
                delivery_kind="tool_result_served",
                protected_ref=f"protected-row-{round_index}",
                protected_hash="9" * 64,
                source_stamps=_stamps(),
                receiving_attempt_id=attempt_id,
            )

        return call

    receipts, errors = _race_claims(build)
    assert errors == []
    assert len(set(receipts)) == CLAIM_RACE_ROUNDS
    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                ne_agent_delivery_receipts.c.delivery_receipt_id,
                ne_agent_delivery_receipts.c.run_id,
                ne_agent_delivery_receipts.c.owner_principal_id,
                ne_agent_delivery_receipts.c.receiving_attempt_id,
                ne_agent_run_events.c.event_id,
                ne_agent_run_events.c.run_id.label("event_run_id"),
                ne_agent_run_events.c.attempt_id.label("event_attempt_id"),
                ne_agent_run_events.c.sequence,
            ).join(
                ne_agent_run_events,
                ne_agent_run_events.c.event_id == ne_agent_delivery_receipts.c.event_id,
            )
        ).all()
        surviving_events = conn.execute(
            sa.select(sa.func.count())
            .select_from(ne_agent_run_events)
            .where(ne_agent_run_events.c.run_id == run_id)
        ).scalar_one()
    # Exactly one RunEvent per winning receipt: every loser's event rolled back with
    # it, so no orphan event and no burnt sequence.
    assert surviving_events == CLAIM_RACE_ROUNDS
    assert len(rows) == CLAIM_RACE_ROUNDS
    assert {str(row.delivery_receipt_id) for row in rows} == set(receipts)
    for row in rows:
        assert str(row.event_run_id) == run_id == str(row.run_id)
        assert str(row.event_attempt_id) == attempt_id == str(row.receiving_attempt_id)
        assert str(row.owner_principal_id) == "alice"
    assert sorted(int(row.sequence) for row in rows) == list(range(1, CLAIM_RACE_ROUNDS + 1))


def _prove_concurrent_same_claim_verifier_intent_replays_one_exact_event(
    pg_engine: sa.Engine,
) -> None:
    run_id, attempt_id = _seed_run(pg_engine)

    def build(round_index: int, _index: int) -> Callable[[], Awaitable[Any]]:
        store = _run_store(pg_engine)

        def call() -> Awaitable[Any]:
            return store.claim_run_claim_verifier(
                run_id=run_id,
                owner_principal_id="alice",
                attempt_id=attempt_id,
                claim_key=f"claim-{round_index}",
                claim_content_hash="4" * 64,
                served_output_hash="5" * 64,
                span_start_byte=0,
                span_end_byte=17,
                verifier_method="ne.grounding.claim",
                verifier_version="v1/ng2",
                result_hash="6" * 64,
            )

        return call

    records, errors = _race_claims(build)
    assert errors == []
    by_receipt: dict[str, set[str]] = {}
    for record in records:
        by_receipt.setdefault(record.verifier_receipt_id, set()).add(record.event_id)
    assert len(by_receipt) == CLAIM_RACE_ROUNDS
    assert all(len(event_ids) == 1 for event_ids in by_receipt.values())
    with pg_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_run_events)
                .where(ne_agent_run_events.c.event_kind == "claim.verifier")
            ).scalar_one()
            == CLAIM_RACE_ROUNDS
        )

    with pytest.raises(ValueError, match="changed its exact intent"):
        asyncio.run(
            _run_store(pg_engine).claim_run_claim_verifier(
                run_id=run_id,
                owner_principal_id="alice",
                attempt_id=attempt_id,
                claim_key="claim-0",
                claim_content_hash="4" * 64,
                served_output_hash="5" * 64,
                span_start_byte=1,
                span_end_byte=17,
                verifier_method="ne.grounding.claim",
                verifier_version="v1/ng2",
                result_hash="6" * 64,
            )
        )


def test_delivery_receipt_row_referencing_a_foreign_run_event_is_rejected_by_postgres(
    pg_engine: sa.Engine,
) -> None:
    """The receipt/event binding is a database constraint, not a caller convention."""
    run_id, attempt_id = _seed_run(pg_engine)
    other_run_id, other_attempt_id = _seed_run(pg_engine, owner="bob")
    runs = _run_store(pg_engine)
    # One RunEvent on THIS Run that no delivery receipt references yet, plus one on
    # a DIFFERENT Run — the two arms of "missing or foreign".
    own = asyncio.run(
        runs.claim_evidence(
            run_id=run_id,
            owner_principal_id="alice",
            attempt_id=attempt_id,
            declaration=_declaration(tool_call_id="tool-own", read_ordinal=0),
        )
    )
    foreign = asyncio.run(
        runs.claim_evidence(
            run_id=other_run_id,
            owner_principal_id="bob",
            attempt_id=other_attempt_id,
            declaration=_declaration(tool_call_id="tool-foreign", read_ordinal=0),
        )
    )

    def insert_receipt(event_id: str) -> None:
        with pg_engine.begin() as conn:
            conn.execute(
                ne_agent_delivery_receipts.insert().values(
                    delivery_receipt_id="receipt-probe",
                    delivery_key="k" * 64,
                    intent_hash="i" * 64,
                    run_id=run_id,
                    owner_principal_id="alice",
                    delivery_kind="tool_result_served",
                    decision="served",
                    protected_ref="protected-row-probe",
                    protected_hash="7" * 64,
                    stamp_set_hash="6" * 64,
                    receiving_attempt_id=attempt_id,
                    event_id=event_id,
                    created_at=sa.func.now(),
                )
            )

    with pytest.raises(IntegrityError, match="fk_delivery_run_event"):
        insert_receipt("event-that-does-not-exist")
    with pytest.raises(IntegrityError, match="fk_delivery_run_event"):
        insert_receipt(foreign.producing_event_id)
    # Positive control: the SAME statement with THIS Run's own event is accepted, so
    # the two rejections above are that foreign key firing, not an unrelated defect.
    insert_receipt(own.producing_event_id)
    with pg_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(ne_agent_delivery_receipts.c.event_id).where(
                    ne_agent_delivery_receipts.c.delivery_receipt_id == "receipt-probe"
                )
            ).scalar_one()
            == own.producing_event_id
        )


def test_concurrent_same_audit_address_yields_one_receipt_without_integrity_error(
    pg_engine: sa.Engine,
) -> None:
    run_id, attempt_id = _seed_run(pg_engine)
    runs = _run_store(pg_engine)
    events = [
        asyncio.run(
            runs.claim_evidence(
                run_id=run_id,
                owner_principal_id="alice",
                attempt_id=attempt_id,
                declaration=_declaration(tool_call_id=f"tool-audit-{index}", read_ordinal=index),
            )
        ).producing_event_id
        for index in range(CLAIM_RACE_ROUNDS)
    ]

    def build(round_index: int, _index: int) -> Callable[[], Awaitable[Any]]:
        store = _run_store(pg_engine)

        def call() -> Awaitable[Any]:
            return store.record_audit_receipt(
                run_id=run_id,
                attempt_id=attempt_id,
                run_event_id=events[round_index],
                decision_kind=RunAuditDecisionKind.EVIDENCE,
                audit_event_id=f"audit-event-{round_index}",
                audit_sequence=42,
                audit_record_hash="e" * 64,
                config_hash="1" * 64,
                owner_principal_id="alice",
                step_id="step-1",
                tool_call_id=f"tool-audit-{round_index}",
            )

        return call

    receipts, errors = _race_claims(build)
    assert errors == []
    assert len(receipts) == WORKERS * CLAIM_RACE_ROUNDS
    assert len(set(receipts)) == CLAIM_RACE_ROUNDS
    with pg_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_run_audit_receipts)
            ).scalar_one()
            == CLAIM_RACE_ROUNDS
        )
    second_attempt = asyncio.run(
        runs.link_attempt(run_id=run_id, predecessor_attempt_id=attempt_id)
    )
    with (
        pg_engine.begin() as conn,
        pytest.raises(IntegrityError, match="fk_run_audit_exact_attempt_event"),
    ):
        conn.execute(
            ne_agent_run_audit_receipts.insert().values(
                run_audit_receipt_id="mismatched-attempt-event",
                intent_key="a" * 64,
                intent_hash="b" * 64,
                run_id=run_id,
                attempt_id=second_attempt.attempt_id,
                run_event_id=events[0],
                step_id="step-1",
                tool_call_id="tool-audit-0",
                approval_id=None,
                decision_kind=RunAuditDecisionKind.EVIDENCE.value,
                audit_event_id="audit-mismatched-attempt-event",
                audit_sequence=41,
                audit_record_hash="c" * 64,
                config_hash="d" * 64,
                created_at=sa.func.now(),
            )
        )


def test_half_filled_run_bound_delivery_shape_is_rejected_by_postgres(
    pg_engine: sa.Engine,
) -> None:
    """Run-bound and standalone are the only two shapes the database will store."""
    run_id, attempt_id = _seed_run(pg_engine)
    runs = _run_store(pg_engine)
    bound = asyncio.run(
        runs.claim_delivery(
            run_id=run_id,
            owner_principal_id="alice",
            delivery_kind="tool_result_served",
            protected_ref="protected-row-bound",
            protected_hash="9" * 64,
            source_stamps=_stamps(),
            receiving_attempt_id=attempt_id,
        )
    )
    assert bound
    with pg_engine.connect() as conn:
        event_id = conn.execute(
            sa.select(ne_agent_run_events.c.event_id).where(ne_agent_run_events.c.run_id == run_id)
        ).scalar_one()

    def insert(**overrides: object) -> None:
        values: dict[str, object] = {
            "delivery_receipt_id": f"probe-{len(overrides)}-{overrides.get('run_id')}",
            "delivery_key": "k" * 64,
            "intent_hash": "i" * 64,
            "run_id": None,
            "owner_principal_id": "alice",
            "delivery_kind": "direct_result_served",
            "decision": "served",
            "protected_ref": "protected-row-probe",
            "protected_hash": "7" * 64,
            "stamp_set_hash": "6" * 64,
            "receiving_attempt_id": None,
            "event_id": None,
            "created_at": sa.func.now(),
        }
        values.update(overrides)
        with pg_engine.begin() as conn:
            conn.execute(ne_agent_delivery_receipts.insert().values(**values))

    with pytest.raises(IntegrityError, match="ck_delivery_run_bound_shape"):
        insert(run_id=run_id, delivery_key="a" * 64)  # Run without attempt or event
    with pytest.raises(IntegrityError, match="ck_delivery_run_bound_shape"):
        insert(event_id=str(event_id), delivery_key="b" * 64)  # event without its Run
    with pytest.raises(IntegrityError, match="ck_delivery_run_bound_shape"):
        insert(
            run_id=run_id, receiving_attempt_id=attempt_id, delivery_key="c" * 64
        )  # Run and attempt without the event
    # Positive control: the all-null standalone shape IS accepted, so the three
    # rejections are the shape check firing rather than a broken insert.
    insert(delivery_key="d" * 64)
    with pg_engine.connect() as conn:
        standalone = conn.execute(
            sa.select(ne_agent_delivery_receipts).where(
                ne_agent_delivery_receipts.c.delivery_key == "d" * 64
            )
        ).one()
    assert standalone.run_id is None
    assert standalone.receiving_attempt_id is None
    assert standalone.event_id is None


def test_evidence_producing_event_must_belong_to_the_same_run_on_postgres(
    pg_engine: sa.Engine,
) -> None:
    """The exact-Run composite join, enforced by the database rather than a convention."""
    run_id, attempt_id = _seed_run(pg_engine)
    other_run_id, other_attempt_id = _seed_run(pg_engine, owner="bob")
    runs = _run_store(pg_engine)
    mine = asyncio.run(
        runs.claim_evidence(
            run_id=run_id,
            owner_principal_id="alice",
            attempt_id=attempt_id,
            declaration=_declaration(tool_call_id="tool-mine", read_ordinal=0),
        )
    )
    foreign = asyncio.run(
        runs.claim_evidence(
            run_id=other_run_id,
            owner_principal_id="bob",
            attempt_id=other_attempt_id,
            declaration=_declaration(tool_call_id="tool-foreign", read_ordinal=0),
        )
    )
    columns = {
        item["name"]: item["constrained_columns"]
        for item in sa.inspect(pg_engine).get_foreign_keys("ne_agent_evidence_records")
    }
    assert "fk_evidence_event" in columns
    # The join is composite: event id ALONE cannot satisfy it.
    assert sorted(columns["fk_evidence_event"]) == ["producing_event_id", "run_id"]
    assert mine.producing_event_id != foreign.producing_event_id


def test_legacy_protection_guards_reject_half_migrated_shapes_on_postgres(
    pg_engine: sa.Engine,
) -> None:
    sessions = SqlAlchemySessionStore(pg_engine)
    conversation = asyncio.run(sessions.create_conversation("assistant", "claims", "alice"))
    asyncio.run(
        sessions.append_conversation_turn(
            conversation.conversation_id,
            "alice",
            "claims",
            "assistant",
            [ModelRequest(parts=[UserPromptPart(content="PLAINTEXT-SENTINEL")])],
            served_output="SERVED-SENTINEL",
            trace_id="trace-1",
            attachment_id="attachment-1",
            attempt_id="attempt-1",
        )
    )
    where = ne_agent_conversation_turns.c.conversation_id == conversation.conversation_id
    with pg_engine.begin() as conn, pytest.raises(DatabaseError, match="partial protected tuple"):
        conn.execute(
            ne_agent_conversation_turns.update()
            .where(where)
            .values(private_context="", protected_input_ref="r")
        )
    with (
        pg_engine.begin() as conn,
        pytest.raises(DatabaseError, match="protected row still carries plaintext"),
    ):
        conn.execute(
            ne_agent_conversation_turns.update()
            .where(where)
            .values(
                input_status="source_free",
                protected_input_ref="r",
                protected_input_hash="a" * 64,
            )
        )
    # Positive control: the complete, plaintext-cleared shape IS accepted.
    with pg_engine.begin() as conn:
        conn.execute(
            ne_agent_conversation_turns.update()
            .where(where)
            .values(
                private_context="",
                input_status="source_free",
                protected_input_ref="r",
                protected_input_hash="a" * 64,
            )
        )
    with pg_engine.connect() as conn:
        row = conn.execute(sa.select(ne_agent_conversation_turns).where(where)).one()
    assert row.protected_input_ref == "r"
    assert row.private_context == ""
    _assert_ownerless_retirement_is_exact_and_frozen(pg_engine)


def _assert_ownerless_retirement_is_exact_and_frozen(
    pg_engine: sa.Engine,
) -> None:
    sessions = SqlAlchemySessionStore(pg_engine)
    runs = _run_store(pg_engine)

    invalid_receipts: list[dict[str, object]] = [
        {"legacy_transport_retired": False},
        {"legacy_transport_generation": "agent-transport-v0"},
        {"legacy_transport_reason": "invented_reason"},
        {"legacy_transport_fence": "a" * 63},
        {"legacy_transport_fence": "A" * 64},
        {"legacy_transport_fence": "z" * 64},
        {"payload": "PLAINTEXT-MUST-NOT-SURVIVE"},
        {"principal_id": "user:invented"},
        {"trace_id": "trace-invented"},
        {"content_status": "legacy_unavailable"},
        {"protected_content_ref": "foreign-ref"},
        {"protected_content_hash": "b" * 64},
    ]

    async def seed_invalid(index: int) -> str:
        session_id = await sessions.open_session(f"invalid-{index}", f"legacy-{index}")
        await sessions.append_turn(
            session_id,
            [ModelRequest(parts=[UserPromptPart(content="OWNERLESS-PG-SENTINEL")])],
        )
        return session_id

    for index, invalid in enumerate(invalid_receipts):
        invalid_session = asyncio.run(seed_invalid(index))
        receipt: dict[str, object] = {
            "legacy_transport_retired": True,
            "legacy_transport_generation": LEGACY_TRANSPORT_GENERATION,
            "legacy_transport_reason": OWNERLESS_RETIREMENT_REASON,
            "legacy_transport_fence": "a" * 64,
            "payload": "",
            "principal_id": None,
            "trace_id": None,
            "content_status": None,
            "protected_content_ref": None,
            "protected_content_hash": None,
        }
        receipt.update(invalid)
        with (
            pg_engine.begin() as conn,
            pytest.raises(
                DatabaseError,
                match=r"invalid legacy retirement receipt|partial protected tuple",
            ),
        ):
            conn.execute(
                ne_agent_session_messages.update()
                .where(ne_agent_session_messages.c.session_id == invalid_session)
                .values(**receipt)
            )

    async def scenario() -> str:
        session_id = await sessions.open_session("assistant", "legacy")
        await sessions.append_turn(
            session_id,
            [ModelRequest(parts=[UserPromptPart(content="OWNERLESS-PG-SENTINEL")])],
        )
        kwargs = {
            "session_id": session_id,
            "sequence": 1,
            "generation": LEGACY_TRANSPORT_GENERATION,
            "reason": OWNERLESS_RETIREMENT_REASON,
            "retirement_fence": "7" * 64,
        }
        await runs.retire_ownerless_session_message(**kwargs)  # type: ignore[arg-type]
        await runs.retire_ownerless_session_message(**kwargs)  # exact replay
        return session_id

    session_id = asyncio.run(scenario())
    where = ne_agent_session_messages.c.session_id == session_id
    with pg_engine.connect() as conn:
        row = conn.execute(sa.select(ne_agent_session_messages).where(where)).one()
    assert row.payload == ""
    assert row.principal_id is None and row.trace_id is None
    assert row.content_status is None
    assert row.protected_content_ref is None
    assert row.protected_content_hash is None
    assert (
        row.legacy_transport_retired,
        row.legacy_transport_generation,
        row.legacy_transport_reason,
        row.legacy_transport_fence,
    ) == (
        True,
        LEGACY_TRANSPORT_GENERATION,
        OWNERLESS_RETIREMENT_REASON,
        "7" * 64,
    )
    with (
        pg_engine.begin() as conn,
        pytest.raises(DatabaseError, match="legacy retirement receipt is immutable"),
    ):
        conn.execute(
            ne_agent_session_messages.update().where(where).values(legacy_transport_fence="8" * 64)
        )


def test_delivery_receiving_attempt_must_belong_to_the_same_run_on_postgres(
    pg_engine: sa.Engine,
) -> None:
    """Kills DEMOTING fk_delivery_receiving_attempt to a single column.

    A control that names an attempt id existing nowhere is satisfied by a
    single-column foreign key too, so it cannot tell a composite from a scalar.
    This one uses an attempt that genuinely EXISTS — on a different Run — which
    only the composite (receiving_attempt_id, run_id) rejects.
    """
    run_id, attempt_id = _seed_run(pg_engine)
    other_run_id, other_attempt_id = _seed_run(pg_engine, owner="bob")
    assert other_run_id != run_id
    runs = _run_store(pg_engine)
    # An event on THIS Run that no receipt references yet, so the probe below can
    # fail only on the attempt binding rather than on the unique event_id.
    own = asyncio.run(
        runs.claim_evidence(
            run_id=run_id,
            owner_principal_id="alice",
            attempt_id=attempt_id,
            declaration=_declaration(tool_call_id="tool-own", read_ordinal=0),
        )
    )
    event_id = own.producing_event_id
    with pg_engine.connect() as conn:
        foreign_attempt_exists = conn.execute(
            sa.select(sa.func.count())
            .select_from(ne_agent_run_attempt_links)
            .where(ne_agent_run_attempt_links.c.attempt_id == other_attempt_id)
        ).scalar_one()
    assert foreign_attempt_exists == 1  # the attempt is real; only its Run is wrong
    with (
        pytest.raises(IntegrityError, match="fk_delivery_receiving_attempt"),
        pg_engine.begin() as conn,
    ):
        conn.execute(
            ne_agent_delivery_receipts.insert().values(
                delivery_receipt_id="probe-foreign-attempt",
                delivery_key="f" * 64,
                intent_hash="i" * 64,
                run_id=run_id,
                owner_principal_id="alice",
                delivery_kind="tool_result_served",
                decision="served",
                protected_ref="protected-row-probe",
                protected_hash="7" * 64,
                stamp_set_hash="6" * 64,
                receiving_attempt_id=other_attempt_id,
                event_id=str(event_id),
                created_at=sa.func.now(),
            )
        )


def test_claim_verifier_receipt_event_must_belong_to_the_same_run_on_postgres(
    pg_engine: sa.Engine,
) -> None:
    """Same discipline for the verifier join: a real event, but another Run's."""
    run_id, attempt_id = _seed_run(pg_engine)
    other_run_id, other_attempt_id = _seed_run(pg_engine, owner="bob")
    runs = _run_store(pg_engine)
    foreign = asyncio.run(
        runs.claim_evidence(
            run_id=other_run_id,
            owner_principal_id="bob",
            attempt_id=other_attempt_id,
            declaration=_declaration(tool_call_id="tool-foreign", read_ordinal=0),
        )
    )
    mine = asyncio.run(
        runs.claim_evidence(
            run_id=run_id,
            owner_principal_id="alice",
            attempt_id=attempt_id,
            declaration=_declaration(tool_call_id="tool-mine", read_ordinal=0),
        )
    )

    def insert_verifier(event_id: str, receipt_id: str) -> None:
        with pg_engine.begin() as conn:
            conn.execute(
                ne_agent_claim_verifier_receipts.insert().values(
                    verifier_receipt_id=receipt_id,
                    run_id=run_id,
                    claim_key="claim-1",
                    claim_content_hash="6" * 64,
                    served_output_hash="5" * 64,
                    span_start_byte=0,
                    span_end_byte=4,
                    verifier_method="exact",
                    verifier_version="1",
                    result_hash="4" * 64,
                    event_id=event_id,
                    receipt_hash=receipt_id.ljust(64, "0"),
                    created_at=sa.func.now(),
                )
            )

    with pytest.raises(IntegrityError, match="fk_claim_verifier_run_event"):
        insert_verifier(foreign.producing_event_id, "verifier-foreign")
    # Positive control: the SAME insert with this Run's own event is accepted.
    insert_verifier(mine.producing_event_id, "verifier-own")
    with pg_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_claim_verifier_receipts)
                .where(ne_agent_claim_verifier_receipts.c.run_id == run_id)
            ).scalar_one()
            == 1
        )


# --- A1: canonical tuple + RunClaim binding, proven by the database -------------


def _tuple_for(run_id: str, conversation_id: str, attempt_id: str, **overrides: object):
    base: dict[str, object] = {
        "conversation_id": conversation_id,
        "run_id": run_id,
        "turn_id": "turn-1",
        "turn_sequence": 1,
        "attachment_id": "attachment-1",
        "attempt_id": attempt_id,
    }
    return CanonicalTargetTuple(**{**base, **overrides})  # type: ignore[arg-type]


def _seed_run_with_conversation(pg_engine: sa.Engine, *, owner: str = "alice"):
    """Return (conversation_id, run_id, attempt_id) for a real owned Run."""
    sessions = SqlAlchemySessionStore(pg_engine)
    runs = _run_store(pg_engine)

    async def scenario():
        conversation = await sessions.create_conversation("assistant", "claims", owner)
        run = await runs.create_run(
            conversation_id=conversation.conversation_id,
            owner_principal_id=owner,
            department="claims",
            conversation_agent_name="assistant",
            agent_id="agt_01",
            agent_version=7,
            strategy="agentic",
            config_hash="1" * 64,
            capability_snapshot_hash="2" * 64,
            capability_snapshot_receipt_id="cap-receipt-1",
            input_plaintext=b"private prompt",
        )
        attempt = await runs.link_attempt(run_id=run.run_id)
        return conversation.conversation_id, run.run_id, attempt.attempt_id

    return asyncio.run(scenario())


def test_the_tuple_discriminator_is_enforced_by_postgres_in_both_directions(
    pg_engine: sa.Engine,
) -> None:
    """A tupled row may not omit a component; a non-tupled row may not carry one.

    SQLite enforces this too, but only Postgres is the deployment target, and the
    check constraint is generated from the leaf's TUPLED_TARGET_KINDS rather than
    restated — so this proves the generated SQL is actually what the server applies.
    """
    conversation_id, run_id, attempt_id = _seed_run_with_conversation(pg_engine)
    table = ne_agent_protected_turn_contents

    def insert(**overrides: object) -> None:
        values: dict[str, object] = {
            "turn_content_id": f"probe-{overrides.get('target_hash', 'x')}",
            "run_id": None,
            "owner_principal_id": "alice",
            "target_kind": AGENT_RUN_TURN_TARGET_KIND,
            "target_hash": "a" * 64,
            "content_status": ProtectedContentStatus.SOURCE_FREE.value,
            "conversation_id": None,
            "turn_id": None,
            "turn_sequence": None,
            "attachment_id": None,
            "attempt_id": None,
            "stamp_set_hash": None,
            "created_at": sa.func.now(),
            "cipher_version": 1,
            "plaintext_hash": "b" * 64,
            "binding_hash": "c" * 64,
            "key_id": "pg-k1",
            "payload_nonce": "n",
            "payload_ciphertext": "ct",
            "key_nonce": "kn",
            "encrypted_dek": "dek",
        }
        values.update(overrides)
        with pg_engine.begin() as conn:
            conn.execute(table.insert().values(**values))

    # (a) a TUPLED kind with no tuple at all
    with pytest.raises(IntegrityError, match="ck_protected_turn_contents_target_tuple"):
        insert(target_hash="1" * 64)
    # (b) a TUPLED kind missing exactly one component
    with pytest.raises(IntegrityError, match="ck_protected_turn_contents_target_tuple"):
        insert(
            target_hash="2" * 64,
            run_id=run_id,
            conversation_id=conversation_id,
            turn_id="turn-1",
            turn_sequence=1,
            attachment_id="attachment-1",
            # attempt_id deliberately omitted
        )
    # (c) a NON-tupled (legacy) kind smuggling a component in
    with pytest.raises(IntegrityError, match="ck_protected_turn_contents_target_tuple"):
        insert(target_hash="3" * 64, target_kind="conversation_turn", turn_id="turn-1")
    # Positive controls: the complete tupled shape and the empty legacy shape are
    # both accepted, so the three rejections are the discriminator, not a dead table.
    insert(
        target_hash="4" * 64,
        run_id=run_id,
        conversation_id=conversation_id,
        turn_id="turn-1",
        turn_sequence=1,
        attachment_id="attachment-1",
        attempt_id=attempt_id,
    )
    insert(target_hash="5" * 64, target_kind="conversation_turn")
    with pg_engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 2


def test_a_run_claim_cannot_reference_another_turns_protected_row_on_postgres(
    pg_engine: sa.Engine,
) -> None:
    """The RunClaim -> protected-row binding is enforced by the exact target guard.

    The foreign row EXISTS and is owned by the same principal — only its turn
    differs — so the guard must refuse it before the claim can be persisted.
    """
    conversation_id, run_id, attempt_id = _seed_run_with_conversation(pg_engine)
    runs = _run_store(pg_engine)
    mine = _tuple_for(run_id, conversation_id, attempt_id)
    other = _tuple_for(run_id, conversation_id, attempt_id, turn_id="turn-2")

    async def claim(target):
        return await runs.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=target.run_id,
            owner_principal_id="alice",
            target_kind=AGENT_RUN_TURN_TARGET_KIND,
            target=target,
            content_status=ProtectedContentStatus.SOURCE_FREE,
            plaintext=b"the assistant said this exactly",
        )

    own = asyncio.run(claim(mine))
    foreign = asyncio.run(claim(other))
    assert own.row_id != foreign.row_id

    def insert_claim(protected_ref: str, claim_id: str) -> None:
        with pg_engine.begin() as conn:
            conn.execute(
                ne_agent_run_claims.insert().values(
                    claim_id=claim_id,
                    run_id=run_id,
                    turn_id=mine.turn_id,
                    owner_principal_id="alice",
                    agent_version=7,
                    conversation_id=conversation_id,
                    turn_sequence=mine.turn_sequence,
                    attachment_id=mine.attachment_id,
                    attempt_id=attempt_id,
                    claim_key="claim-1",
                    claim_index=0,
                    claim_verdict="unavailable",
                    display_state="protected_source_free_slice",
                    protected_ref=protected_ref,
                    served_output_hash="5" * 64,
                    span_start_byte=0,
                    span_end_byte=4,
                    claim_content_hash="6" * 64,
                    association_status="association_unavailable",
                    created_at=sa.func.now(),
                )
            )

    with pytest.raises(
        ProgrammingError, match="Run claim protected target is mismatched"
    ) as refusal:
        insert_claim(foreign.row_id, "claim-foreign")
    assert "ne_require_run_claim_protected_target" in str(refusal.value)
    # Positive control: THIS turn's own row is accepted by the same statement.
    insert_claim(own.row_id, "claim-own")
    with pg_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(ne_agent_run_claims.c.protected_ref).where(
                    ne_agent_run_claims.c.claim_id == "claim-own"
                )
            ).scalar_one()
            == own.row_id
        )


def test_source_free_claim_slice_round_trips_against_postgres(pg_engine: sa.Engine) -> None:
    """The narrow slice resolves on the real backend, and still refuses a foreign owner."""
    conversation_id, run_id, attempt_id = _seed_run_with_conversation(pg_engine)
    runs = _run_store(pg_engine)
    target = _tuple_for(run_id, conversation_id, attempt_id)
    plaintext = b"the assistant said this exactly"
    span = (4, 13)

    async def scenario() -> bytes:
        protected = await runs.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=run_id,
            owner_principal_id="alice",
            target_kind=AGENT_RUN_TURN_TARGET_KIND,
            target=target,
            content_status=ProtectedContentStatus.SOURCE_FREE,
            plaintext=plaintext,
        )
        await runs.transition(run_id=run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED)
        await runs.transition(run_id=run_id, expected=RunState.QUEUED, target=RunState.RUNNING)
        claim = ClaimDeclaration(
            claim_key="claim-1",
            claim_index=0,
            claim_verdict="unavailable",
            display_state="protected_source_free_slice",
            protected_ref=protected.row_id,
            served_output_hash="5" * 64,
            span_start_byte=span[0],
            span_end_byte=span[1],
            claim_content_hash=hashlib.sha256(plaintext[span[0] : span[1]]).hexdigest(),
            association_status="association_unavailable",
        )
        (claim_id,) = await runs.commit_terminal_claims(
            run_id=run_id,
            owner_principal_id="alice",
            turn_id=target.turn_id,
            attempt_id=attempt_id,
            target=target,
            claims=(claim,),
            terminal_status=RunState.COMPLETED,
        )
        with pytest.raises(ProtectedSliceUnavailable):
            await runs.materialize_source_free_claim_slice(
                claim_id=claim_id,
                owner_principal_id="mallory",
                target=target,
                protected_ref=protected.row_id,
                served_output_hash="5" * 64,
            )
        return await runs.materialize_source_free_claim_slice(
            claim_id=claim_id,
            owner_principal_id="alice",
            target=target,
            protected_ref=protected.row_id,
            served_output_hash="5" * 64,
        )

    assert asyncio.run(scenario()) == plaintext[span[0] : span[1]]


def test_the_content_status_to_stamp_relationship_is_enforced_by_postgres(
    pg_engine: sa.Engine,
) -> None:
    """The legal status<->stamp pairing is a database constraint, not only a guard.

    The Python guard in `claim_protected_payload` is the first line; this proves a
    direct writer that bypasses it still cannot persist source-bearing bytes under a
    source-free status.
    """
    _seed_run_with_conversation(pg_engine)
    table = ne_agent_protected_turn_contents

    def insert(status: str, stamp_set_hash: str | None, key: str) -> None:
        with pg_engine.begin() as conn:
            conn.execute(
                table.insert().values(
                    turn_content_id=f"probe-{key}",
                    run_id=None,
                    owner_principal_id="alice",
                    target_kind="conversation_turn",
                    target_hash=key * 64,
                    content_status=status,
                    conversation_id=None,
                    turn_id=None,
                    turn_sequence=None,
                    attachment_id=None,
                    attempt_id=None,
                    stamp_set_hash=stamp_set_hash,
                    created_at=sa.func.now(),
                    cipher_version=1,
                    plaintext_hash="b" * 64,
                    binding_hash="c" * 64,
                    key_id="pg-k1",
                    payload_nonce="n",
                    payload_ciphertext="ct",
                    key_nonce="kn",
                    encrypted_dek="dek",
                )
            )

    match = "ck_protected_turn_contents_content_status"
    # source-free / legacy-unavailable may NOT carry an authority stamp set
    with pytest.raises(IntegrityError, match=match):
        insert(ProtectedContentStatus.SOURCE_FREE.value, "d" * 64, "1")
    with pytest.raises(IntegrityError, match=match):
        insert(ProtectedContentStatus.LEGACY_UNAVAILABLE.value, "d" * 64, "2")
    # a source-bound container MUST carry one
    with pytest.raises(IntegrityError, match=match):
        insert(ProtectedContentStatus.SOURCE_BOUND_CONTAINER.value, None, "3")
    # the status vocabulary is CLOSED
    with pytest.raises(IntegrityError, match=match):
        insert("source_free_ish", None, "4")
    # and this table always requires one
    with pytest.raises(IntegrityError, match=match):
        insert(None, None, "5")  # type: ignore[arg-type]
    # Positive controls: both legal pairings are accepted.
    insert(ProtectedContentStatus.SOURCE_FREE.value, None, "6")
    insert(ProtectedContentStatus.SOURCE_BOUND_CONTAINER.value, "d" * 64, "7")
    with pg_engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 2


def test_evidence_projection_readers_are_exact_against_postgres(
    pg_engine: sa.Engine, tmp_path: Path
) -> None:
    _prove_concurrent_same_claim_verifier_intent_replays_one_exact_event(pg_engine)
    metadata.drop_all(pg_engine)
    ensure_runtime_schema(pg_engine)
    ensure_run_schema(pg_engine)
    _prove_source_delivery_uses_exact_evidence_projection_readers(tmp_path, engine=pg_engine)


def test_pre_consume_continuation_constraint_is_migrated_at_boot_on_postgres(
    pg_engine: sa.Engine,
) -> None:
    """The PRODUCTION upgrade path: a pre-`consumed` Postgres constraint migrates at boot.

    The SQLite twin (``test_run_store.py``) proves the same contract, but this
    fixture creates the CURRENT schema — so without the regression step below,
    ``ensure_continuation_status_compatibility`` early-returns and the Postgres
    ALTER branch, the one production actually executes, would never run in any
    test (Sol cross-model finding, 2026-08-29). Here the constraint is put back
    into the exact shape every pre-fix database holds, with a live parked row,
    and boot must repair it in place: constraint widened, row preserved, the
    fence value writable, and a second boot a no-op.

    CLEANUP IS LOAD-BEARING, hence the try/finally. ``create_wait`` below also
    inserts a PENDING run-bound ``ne_approval_requests`` row (dict-shaped
    ``protected_invocation_ref`` disclosure, 2h expiry) into the SHARED lane
    database, and this test — unlike the ``_prove_run_approval_wait_converges``
    precedent above — never decides it. The live-postgres lane later runs the
    cross-process HITL proof (``test_worker_hitl_live.py``) against the SAME
    database; it approves ``list_pending()[0]`` through the STANDALONE gate,
    and a leaked run-bound row lands there first (``requested_at`` ascending)
    and dies in ``_assert_protected_arguments_current`` with
    ``MalformedProtectedReferenceError`` — a run-bound reference may only be
    decided through its own checkpoint coordinator. The failure is this test's
    hygiene, not that guard's over-reach, so the approval tables are dropped on
    every exit path (the in-file ``approval_metadata.drop_all`` precedent).
    """

    ensure_approval_schema(pg_engine)
    try:
        sessions = SqlAlchemySessionStore(pg_engine)
        runs = _run_store(pg_engine)
        checkpoints = RunApprovalCheckpointStore(runs, SqlAlchemyApprovalStore(pg_engine))

        async def seed() -> str:
            _, run = await _create_run(sessions, runs)
            await runs.transition(
                run_id=run.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED
            )
            await runs.transition(
                run_id=run.run_id, expected=RunState.QUEUED, target=RunState.RUNNING
            )
            attempt = await runs.link_attempt(run_id=run.run_id)
            now = datetime.now(UTC)
            joined = await checkpoints.create_wait(
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                step_id="step-consume-migration",
                tool_call_id="tool-call-consume-migration",
                action="write.external.email.send",
                action_hash="a" * 64,
                tier=2,
                principal_id="alice",
                policy_hash="b" * 64,
                invocation_plaintext=b"POSTGRES private migration sentinel",
                source_stamps=(_stamp(),),
                stamp_receipt_id="opaque-receipt-consume-migration",
                stamp_receipt_hash="c" * 64,
                requested_at=now,
                sla_deadline=now + timedelta(hours=1),
                expires_at=now + timedelta(hours=2),
                event=new_approval_event(
                    approval_id="allocated-inside",
                    tier=2,
                    decision=ApprovalDecision.requested,
                    subject_ref="tool:write.external.email.send",
                    actor="alice",
                ),
            )
            return joined.checkpoint_id

        checkpoint_id = asyncio.run(seed())

        table = ne_agent_run_continuation_checkpoints
        # Regress to the exact PRE-consume constraint every pre-fix database holds.
        with pg_engine.begin() as conn:
            conn.exec_driver_sql(
                f'ALTER TABLE {table.name} DROP CONSTRAINT "ck_run_continuation_status"'
            )
            conn.exec_driver_sql(
                f"ALTER TABLE {table.name} ADD CONSTRAINT ck_run_continuation_status "
                "CHECK (status IN ('pending','approved','resumed','rejected','expired',"
                "'source_withheld'))"
            )

        # Precondition: the regressed database refuses `consumed` outright — the
        # exact failure the first resume after an unmigrated deploy would hit.
        with pg_engine.connect() as conn, pytest.raises(IntegrityError), conn.begin():
            conn.execute(
                table.update()
                .where(table.c.checkpoint_id == checkpoint_id)
                .values(status="consumed")
            )

        # Boot. The Postgres branch of the compatibility migration must swap the
        # constraint in place with the seeded row untouched.
        ensure_run_schema(pg_engine)

        with pg_engine.connect() as conn:
            checks = {
                str(item.get("name")): "".join(str(item.get("sqltext") or "").lower().split())
                for item in sa.inspect(conn).get_check_constraints(table.name)
            }
            assert "'consumed'" in checks["ck_run_continuation_status"], (
                "boot must admit the consume fence on an existing Postgres database; "
                "the first approval resume dies on its own CAS otherwise"
            )
            survivor = conn.execute(sa.select(table)).one()
            assert (str(survivor.checkpoint_id), str(survivor.status)) == (checkpoint_id, "pending")

        # The fence value itself is now writable (schema probe, reverted)...
        with pg_engine.begin() as conn:
            conn.execute(
                table.update()
                .where(table.c.checkpoint_id == checkpoint_id)
                .values(status="consumed")
            )
            conn.execute(
                table.update()
                .where(table.c.checkpoint_id == checkpoint_id)
                .values(status="pending")
            )
        # ...and boot stays idempotent on the repaired database.
        ensure_run_schema(pg_engine)
    finally:
        # Never leak the parked PENDING approval row into the shared lane DB
        # (see the docstring); mirrors the wait-converge prove's teardown.
        approval_metadata.drop_all(pg_engine)


def test_pre_resume_outcome_constraint_is_migrated_at_boot_on_postgres(
    pg_engine: sa.Engine,
) -> None:
    """The PRODUCTION upgrade path for POST_DEMO item 4's widened terminal outcome.

    An adversarial pass caught that this migration had ZERO coverage, and it was
    right: every test database is built by ``metadata.create_all``, which already
    carries the WIDENED check, so the compatibility block finds
    ``approved_effect_complete`` and breaks immediately. The twelve-line DDL — the
    only code a real upgrade executes — ran in no test in this repo.

    That is the same trap a Sol cross-model finding removed from
    ``ensure_continuation_status_compatibility`` on 2026-08-29, reproduced on the
    same table family two days later. This mirrors that test's shape: put the
    constraint back into the exact narrow form every pre-fix database holds, boot,
    and require the repair.

    HONEST LIMIT: this asserts the CONSTRAINT, not a live row write. The lifecycle
    outbox is foreign-key bound to a real run and attempt, so writing a row here
    would mean seeding a whole run to test a DDL string. What the reviewer's gap
    was about — that the ALTER never executes — is closed exactly.
    """
    from ne.runtime.run_store import ensure_run_schema, ne_agent_run_lifecycle_outbox

    narrow = (
        f"ALTER TABLE {ne_agent_run_lifecycle_outbox.name} "
        "ADD CONSTRAINT ck_run_lifecycle_outcome_shape CHECK ("
        "(terminal_status = 'completed' AND ("
        "(output_verdict = 'refused' AND outcome = 'refused') OR "
        "(output_verdict != 'refused' AND outcome = 'execution_complete'))) OR "
        "(terminal_status = 'failed' AND outcome = 'failed') OR "
        "(terminal_status = 'cancelled' AND outcome = 'cancelled') OR "
        "(terminal_status = 'expired' AND outcome = 'expired') OR "
        "(terminal_status = 'context_exhausted' AND outcome = 'context_exhausted') OR "
        "(terminal_status = 'budget_exhausted' AND outcome = 'budget_exhausted'))"
    )

    def outcome_check(conn: sa.Connection) -> str:
        for item in sa.inspect(conn).get_check_constraints(ne_agent_run_lifecycle_outbox.name):
            if str(item.get("name")) == "ck_run_lifecycle_outcome_shape":
                return str(item.get("sqltext") or "")
        raise AssertionError("ck_run_lifecycle_outcome_shape is missing entirely")

    ensure_run_schema(pg_engine)

    # REGRESS to the exact pre-fix shape, which is what makes the ALTER reachable.
    with pg_engine.begin() as conn:
        conn.exec_driver_sql(
            f"ALTER TABLE {ne_agent_run_lifecycle_outbox.name} "
            'DROP CONSTRAINT "ck_run_lifecycle_outcome_shape"'
        )
        conn.exec_driver_sql(narrow)
    with pg_engine.connect() as conn:
        assert "approved_effect_complete" not in outcome_check(conn), (
            "the regression step did not actually narrow the constraint, so the "
            "assertions below would pass without the migration ever running"
        )

    # BOOT — the only place the twelve-line DDL executes.
    ensure_run_schema(pg_engine)
    with pg_engine.connect() as conn:
        widened = outcome_check(conn)
    assert "approved_effect_complete" in widened, (
        "boot must widen the terminal-outcome constraint on an existing database; "
        "a resumed run's finalizer dies on this CHECK otherwise — the exact defect "
        "the migration exists to remove"
    )
    assert "execution_complete" in widened, (
        "widening must ADD a value, never replace the one every ordinary run uses"
    )

    # And the constraint is VALIDATED, not left NOT VALID. This is the half a first
    # attempt got wrong: `ADD CONSTRAINT ... NOT VALID` holds ACCESS EXCLUSIVE until
    # its transaction commits, so validating inside the boot pass blocked writers
    # exactly as a plain ADD would have. VALIDATE now runs in its own transaction
    # afterwards — and if that step were dropped, the constraint would sit NOT VALID
    # forever, enforcing new rows but never attesting the history.
    with pg_engine.connect() as conn:
        pending = conn.exec_driver_sql(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = 'ck_run_lifecycle_outcome_shape'"
        ).scalar_one()
    assert pending is True, "boot must VALIDATE the widened constraint, not leave it NOT VALID"

    # A database with NO such constraint — one predating it, or one an operator
    # dropped during an incident — gets it back at boot.
    #
    # ⚠️ HONEST LABEL: this asserts an OBSERVED end state, and it is NOT mutation-
    # discriminating for the compatibility block above. An adversarial pass argued
    # that block silently no-ops when the constraint is absent, leaving the table
    # unconstrained. I restructured it find-then-act and then MEASURED the claim:
    # with the old loop form deliberately restored, a reboot STILL restored the
    # constraint — so something else in `ensure_run_schema` re-adds it and the
    # finding does not hold. The restructure stays because find-then-act says what
    # it does; it is a readability change, not the fix it was written as. This test
    # documents the end state a database actually reaches, and says plainly that it
    # would pass without that block.
    with pg_engine.begin() as conn:
        conn.exec_driver_sql(
            f"ALTER TABLE {ne_agent_run_lifecycle_outbox.name} "
            'DROP CONSTRAINT "ck_run_lifecycle_outcome_shape"'
        )
    ensure_run_schema(pg_engine)
    with pg_engine.connect() as conn:
        assert "approved_effect_complete" in outcome_check(conn), (
            "boot must ADD the outcome-shape constraint when it is absent, not "
            "silently leave the table unconstrained"
        )

    # Idempotent: a second boot leaves the repaired constraint alone.
    ensure_run_schema(pg_engine)
    with pg_engine.connect() as conn:
        assert outcome_check(conn) == widened
