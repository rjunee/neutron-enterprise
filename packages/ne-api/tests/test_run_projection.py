# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-009B projection unit controls: scope, cursors, safe detail, and claims."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from ne.api.run_projection import (
    AgentRunProjection,
    CursorUnavailable,
    decode_cursor,
    encode_cursor,
    event_view,
    locate_run_engine,
    locate_table_engine,
    projection_from_state,
    run_view,
)
from ne.audit.store import ensure_audit_schema, ne_audit_log
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.events import new_approval_event
from ne.runtime.approval_store import (
    SqlAlchemyApprovalStore,
    ensure_approval_schema,
    ne_approval_requests,
)
from ne.runtime.errors import AgenticNoProgressError
from ne.runtime.private_payloads import (
    AGENT_RUN_TURN_TARGET_KIND,
    CanonicalTargetTuple,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    RunPrivatePayloadCipher,
    StaticPrivatePayloadKeys,
)
from ne.runtime.run_audit_receipts import (
    ensure_run_audit_intent_schema,
    ne_agent_run_audit_intents,
)
from ne.runtime.run_checkpoint import RunApprovalCheckpointStore
from ne.runtime.run_finalizer import derived_failure_reason
from ne.runtime.run_store import (
    AgentRun,
    AgentRunStore,
    ClaimDeclaration,
    RunEvent,
    RunState,
    ensure_run_schema,
    ne_agent_run_events,
    ne_agent_run_turn_targets,
)
from ne.runtime.run_workflow import (
    ensure_run_workflow_schema,
    ne_agent_run_execution_checkpoints,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.schema import ApprovalDecision

AGENT = "agt_projection"
OTHER_AGENT = "agt_other"
OWNER = "user:ada"
DEPARTMENT = "operations"


def test_locate_run_engine_collapses_same_database_engine_aliases(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'shared-run-store.db'}"
    runtime_engine = sa.create_engine(database_url)
    audit_engine = sa.create_engine(database_url)
    ensure_run_schema(runtime_engine)

    assert locate_run_engine([runtime_engine, audit_engine]) is runtime_engine


def test_locate_run_engine_rejects_distinct_database_owners(tmp_path) -> None:
    first = sa.create_engine(f"sqlite:///{tmp_path / 'first-run-store.db'}")
    second = sa.create_engine(f"sqlite:///{tmp_path / 'second-run-store.db'}")
    ensure_run_schema(first)
    ensure_run_schema(second)

    assert locate_run_engine([first, second]) is None


def _stores() -> tuple[sa.Engine, SqlAlchemySessionStore, AgentRunStore]:
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    ensure_run_workflow_schema(engine)
    ensure_approval_schema(engine)
    cipher = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="projection-k1", keys={"projection-k1": b"k" * 32})
    )
    return engine, SqlAlchemySessionStore(engine), AgentRunStore(engine, cipher)


def _stamp() -> SourceAuthorityStamp:
    source = SourceIdentity(
        deployment_id="deployment-projection",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-projection",
        point=M365SourcePoint(
            graph_tenant_id="tenant-projection",
            site_id="",
            drive_id="drive-projection",
            item_id="item-projection",
        ),
        generation=1,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint",
        descriptor_schema_version=1,
        descriptor_hash="a" * 64,
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    receipt = CurrentSourceAuthorityReceipt(
        receipt_id="authority-receipt-projection",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="etag-projection",
        source_contract=contract,
        contract_epoch=1,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 21, 11, 59, tzinfo=UTC),
        audit_event_id="audit-projection",
        lineage_id="lineage-projection",
        freshness_sla_seconds=Decimal("900"),
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


async def _run(
    sessions: SqlAlchemySessionStore,
    runs: AgentRunStore,
    *,
    owner: str = OWNER,
    department: str = DEPARTMENT,
    agent_id: str = AGENT,
    strategy: str = "agentic",
    input_plaintext: bytes = b"PRIVATE-RUN-INPUT-SENTINEL",
):
    conversation = await sessions.create_conversation(agent_id, department, owner)
    run = await runs.create_run(
        conversation_id=conversation.conversation_id,
        owner_principal_id=owner,
        department=department,
        conversation_agent_name=agent_id,
        agent_id=agent_id,
        agent_version=3,
        strategy=strategy,
        config_hash="1" * 64,
        capability_snapshot_hash="2" * 64,
        capability_snapshot_receipt_id="capability-receipt",
        input_plaintext=input_plaintext,
    )
    return conversation, run


async def _wait_for_approval(
    engine: sa.Engine,
    runs: AgentRunStore,
    run: AgentRun,
    *,
    owner: str,
    attach_target: bool = True,
):
    run_id = run.run_id
    await runs.transition(
        run_id=run_id,
        expected=RunState.SUBMITTED,
        target=RunState.QUEUED,
    )
    await runs.transition(
        run_id=run_id,
        expected=RunState.QUEUED,
        target=RunState.RUNNING,
    )
    attempt = await runs.link_attempt(run_id=run_id)
    now = datetime.now(UTC)
    if attach_target:
        _attach_turn_target(engine, run, attempt.attempt_id, now=now)
    action_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    joined = await RunApprovalCheckpointStore(
        runs,
        SqlAlchemyApprovalStore(engine),
    ).create_wait(
        run_id=run_id,
        attempt_id=attempt.attempt_id,
        step_id=f"step-{run_id}",
        tool_call_id=f"tool-{run_id}",
        action="write.external.email.send",
        action_hash=action_hash,
        tier=2,
        principal_id=owner,
        policy_hash="e" * 64,
        invocation_plaintext=b"PRIVATE-APPROVAL-PAYLOAD",
        source_stamps=(_stamp(),),
        stamp_receipt_id=f"stamp-{run_id}",
        stamp_receipt_hash="f" * 64,
        requested_at=now,
        sla_deadline=now + timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        event=new_approval_event(
            approval_id="allocated-inside-transaction",
            tier=2,
            decision=ApprovalDecision.requested,
            subject_ref="tool:write.external.email.send",
            actor=owner,
            config_hash="1" * 64,
        ),
    )
    waiting = await runs.get_run(run_id=run_id, owner_principal_id=owner)
    assert waiting is not None
    return waiting, joined, attempt


def _attach_turn_target(
    engine: sa.Engine,
    run: AgentRun,
    attempt_id: str,
    *,
    now: datetime | None = None,
) -> None:
    run_id = run.run_id
    with engine.begin() as conn:
        conn.execute(
            ne_agent_run_turn_targets.insert().values(
                run_id=run_id,
                attempt_id=attempt_id,
                conversation_id=run.conversation_id,
                turn_id=f"turn-{run_id}",
                turn_sequence=1,
                attachment_id=f"attachment-{run_id}",
                created_at=now or datetime.now(UTC),
            )
        )


def test_cursor_is_deterministic_tamper_evident_and_resource_bound() -> None:
    at = datetime(2026, 8, 21, 12, 30, tzinfo=UTC)
    encoded = encode_cursor("runs:agent-a", at, "run-a")
    assert encoded == encode_cursor("runs:agent-a", at, "run-a")
    assert decode_cursor(encoded, kind="runs:agent-a") == (at, "run-a")

    for token, kind in ((encoded[:-1] + "A", "runs:agent-a"), (encoded, "runs:agent-b")):
        try:
            decode_cursor(token, kind=kind)
        except CursorUnavailable:
            pass
        else:  # pragma: no cover - makes the refusal an explicit positive control
            raise AssertionError("cursor defect was accepted")


def test_owner_department_and_agent_filters_are_all_required_before_paging() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _visible_conversation, visible = await _run(sessions, runs)
        await _run(sessions, runs, owner="user:foreign")
        await _run(sessions, runs, department="finance")
        await _run(sessions, runs, agent_id=OTHER_AGENT)
        projection = AgentRunProjection(runs, engine)

        page, cursor = await projection.list_runs(
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
            limit=25,
            after=None,
        )
        assert [row.run_id for row in page] == [visible.run_id]
        assert cursor is None
        assert (
            await projection.get_run(
                run_id=visible.run_id,
                owner_principal_id=OWNER,
                department="finance",
                agent_id=AGENT,
            )
            is None
        )
        poisoned = replace(
            visible,
            strategy="PRIVATE-PROMPT-SENTINEL",
            display_label="Run 1 · PRIVATE-PROMPT-SENTINEL",
        )
        safe = run_view(poisoned)
        assert safe.strategy == "unavailable"
        assert safe.display_label.startswith("Run 1 · Unavailable ·")
        assert "PRIVATE-PROMPT-SENTINEL" not in safe.model_dump_json()
        assert (
            await projection.get_run(
                run_id=visible.run_id,
                owner_principal_id=OWNER,
                department=DEPARTMENT,
                agent_id=OTHER_AGENT,
            )
            is None
        )

    asyncio.run(scenario())
    engine.dispose()


def test_served_model_requires_uniform_success_provenance_on_exact_run_trace() -> None:
    engine, _sessions, runs = _stores()
    ensure_audit_schema(engine)
    projection = AgentRunProjection(runs, engine, audit_engine=engine)
    now = datetime.now(UTC)

    def insert(
        seq: int,
        *,
        trace: str,
        response_model: str | None,
        routing: dict[str, str] | None = None,
    ) -> None:
        provenance = {} if response_model is None else {"response_model": response_model}
        provenance.update(routing or {})
        with engine.begin() as conn:
            conn.execute(
                ne_audit_log.insert().values(
                    seq=seq,
                    event_id=f"event-{seq}",
                    event_type="audit",
                    config_hash="a" * 64,
                    actor="gateway",
                    occurred_at=now,
                    kpi_tag=None,
                    trace_id=trace,
                    action="ne.gateway.complete",
                    resource="model",
                    outcome="success",
                    payload={"provenance": provenance},
                    prev_hash="0" * 64,
                    record_hash=f"{seq:064x}",
                )
            )

    insert(1, trace="run-uniform", response_model="served-a")
    insert(2, trace="run-uniform", response_model="served-a")
    insert(3, trace="foreign-run", response_model="served-foreign")
    # The gateway records that WERE these calls carry no routing keys, so the
    # position is unavailable -- NOT "primary". "Primary" asserts that nothing
    # was attempted first, and nothing in this ledger says so.
    assert projection._served_model("run-uniform") == ("served-a", None, None)
    insert(4, trace="run-uniform", response_model="served-b")
    assert projection._served_model("run-uniform") == (None, None, None)
    insert(5, trace="run-missing", response_model=None)
    assert projection._served_model("run-missing") == (None, None, None)
    engine.dispose()


def test_served_routing_reads_the_gateways_own_record_and_refuses_a_split_one() -> None:
    """The routing pair rides the SAME ledger record the served name does."""
    engine, _sessions, runs = _stores()
    ensure_audit_schema(engine)
    projection = AgentRunProjection(runs, engine, audit_engine=engine)
    now = datetime.now(UTC)

    def insert(seq: int, *, trace: str, provenance: dict[str, str]) -> None:
        with engine.begin() as conn:
            conn.execute(
                ne_audit_log.insert().values(
                    seq=seq,
                    event_id=f"event-{seq}",
                    event_type="audit",
                    config_hash="a" * 64,
                    actor="gateway",
                    occurred_at=now,
                    kpi_tag=None,
                    trace_id=trace,
                    action="ne.gateway.complete",
                    resource="model",
                    outcome="success",
                    payload={"provenance": provenance},
                    prev_hash="0" * 64,
                    record_hash=f"{seq:064x}",
                )
            )

    fallback = {
        "response_model": "private-default",
        "route_position": "fallback",
        "fallback_from": "frontier-sonnet",
    }
    insert(1, trace="run-fell-back", provenance=fallback)
    assert projection._served_model("run-fell-back") == (
        "private-default",
        "fallback",
        "frontier-sonnet",
    )

    insert(2, trace="run-primary", provenance={"response_model": "a", "route_position": "primary"})
    assert projection._served_model("run-primary") == ("a", "primary", None)

    # C12 -- PROVIDER TEXT IS NOT A MODEL NAME. A `fallback_from` that is an
    # error message rather than a list of declared names loses the names and
    # keeps the position: the fallback happened, and nothing that could echo the
    # prompt back reaches the reader.
    insert(
        3,
        trace="run-dirty",
        provenance={
            "response_model": "private-default",
            "route_position": "fallback",
            "fallback_from": "litellm.BadRequestError: invalid tool_call_id 'SECRET'",
        },
    )
    assert projection._served_model("run-dirty") == ("private-default", "fallback", None)

    # Two calls on one Run that routed differently have no single position.
    insert(4, trace="run-split", provenance={"response_model": "a", "route_position": "primary"})
    insert(
        5,
        trace="run-split",
        provenance={"response_model": "a", "route_position": "fallback", "fallback_from": "b"},
    )
    assert projection._served_model("run-split") == ("a", None, None)
    engine.dispose()


def test_event_projection_enumerates_only_store_owned_safe_scalars() -> None:
    event = RunEvent(
        event_id="event-1",
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=7,
        event_kind="run.terminal",
        event_status="completed",
        parent_event_id="event-0",
        step_id="step-1",
        safe_detail={
            "turn_id": "turn-1",
            "claim_count": 2,
            "terminal_intent_hash": "a" * 64,
            "outcome": "approved_effect_complete",
            "private_context": "PRIVATE-CONTEXT-SENTINEL",
            "locator": "PRIVATE-LOCATOR-SENTINEL",
        },
        detail_hash="b" * 64,
        created_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    view = event_view(event)
    # POST_DEMO item 4: `outcome` MUST survive the projection. An adversarial pass
    # found that it did not — the finalizer wrote `approved_effect_complete` to the
    # lifecycle outbox, which ne-api never reads, while this allowlist dropped the
    # word from the one event that carries it. The distinction was real in the
    # database and invisible to every consumer, so the item's stated purpose was
    # undelivered. This assertion is the thing that would have caught that.
    assert [(item.key, item.value) for item in view.metadata] == [
        ("turn_id", "turn-1"),
        ("claim_count", 2),
        ("terminal_intent_hash", "a" * 64),
        ("outcome", "approved_effect_complete"),
    ]
    payload = view.model_dump_json()
    assert "PRIVATE-CONTEXT-SENTINEL" not in payload
    assert "PRIVATE-LOCATOR-SENTINEL" not in payload

    # `outcome` is a CLOSED server vocabulary and is re-checked as one at the
    # serving boundary. `ne_agent_run_events.safe_detail` carries no CHECK
    # constraint, so a drifted writer or a partial restore could put prose on this
    # key; without the re-check the timeline would render it as a class the server
    # defined. Same shape as `route_position` and `tool_name` beside it.
    drifted = event_view(
        replace(
            event,
            safe_detail={
                "turn_id": "turn-1",
                "outcome": "DRIFTED-OUTCOME-SENTINEL not a server class",
            },
        )
    )
    assert [item.key for item in drifted.metadata] == ["turn_id"], (
        "an outcome outside the closed vocabulary must be dropped, not served"
    )
    assert "DRIFTED-OUTCOME-SENTINEL" not in drifted.model_dump_json()

    # ...and a NON-STRING on that key is already drift. Without an explicit guard
    # the bool fast-path at the top of `_safe_scalar` returns it verbatim and the
    # vocabulary check never runs. Codex found that hole in the first version.
    typed = event_view(replace(event, safe_detail={"turn_id": "turn-1", "outcome": True}))
    assert [item.key for item in typed.metadata] == ["turn_id"], (
        "a non-string outcome must be dropped, not served through the bool fast-path"
    )

    changed = event_view(
        replace(
            event,
            event_kind="model.changed",
            event_status="changed",
            safe_detail={
                "actor": OWNER,
                "safe_boundary": "between-runs",
                "model_policy_id": "agent.meridian-analyst.v2",
                "before_requested_model": "private-default",
                "before_served_model": "private-default",
                "after_requested_model": "frontier-default",
                "after_authorized_model": "frontier-default",
                "after_authorized_lane": "frontier-api",
                "context_snapshot_hash": "c" * 64,
                "context_fit": "fits",
                "estimated_input_tokens": 1234,
                "target_input_budget": 28000,
                "compaction": "not_required",
                "cache_impact": "reset_expected",
                "authorization_receipt_hash": "d" * 64,
                "before_config_hash": "e" * 64,
                "config_hash": "f" * 64,
                "private_context": "PRIVATE-CONTEXT-SENTINEL",
            },
        )
    )
    assert changed.event_kind == "model.changed"
    assert dict((item.key, item.value) for item in changed.metadata) == {
        "actor": OWNER,
        "safe_boundary": "between-runs",
        "model_policy_id": "agent.meridian-analyst.v2",
        "before_requested_model": "private-default",
        "before_served_model": "private-default",
        "after_requested_model": "frontier-default",
        "after_authorized_model": "frontier-default",
        "after_authorized_lane": "frontier-api",
        "context_snapshot_hash": "c" * 64,
        "context_fit": "fits",
        "estimated_input_tokens": 1234,
        "target_input_budget": 28000,
        "compaction": "not_required",
        "cache_impact": "reset_expected",
        "authorization_receipt_hash": "d" * 64,
        "before_config_hash": "e" * 64,
        "config_hash": "f" * 64,
    }
    assert "PRIVATE-CONTEXT-SENTINEL" not in changed.model_dump_json()

    unknown = event_view(
        replace(
            event,
            event_kind="PRIVATE-EVENT-KIND-SENTINEL",
            event_status="PRIVATE-EVENT-STATUS-SENTINEL",
        )
    )
    assert unknown.event_kind == "event.unavailable"
    assert unknown.event_status == "unavailable"
    assert "PRIVATE-EVENT" not in unknown.model_dump_json()


def test_run_view_states_the_routing_positively_and_never_guesses_it() -> None:
    """A turn served as requested SAYS so; an unrecorded one does not say "primary"."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _conversation, row = await _run(sessions, runs)

        # The whole point of the pair: the console can print "served as
        # requested" from the RECORD instead of inferring it from a null.
        served = run_view(replace(row, route_position="primary", fallback_from=None))
        assert (served.route_position, served.fallback_from) == ("primary", None)

        fell_back = run_view(
            replace(row, route_position="fallback", fallback_from="frontier-sonnet")
        )
        assert (fell_back.route_position, fell_back.fallback_from) == (
            "fallback",
            "frontier-sonnet",
        )

        # A Run that recorded nothing reads as the absence it is. "Primary"
        # would be a positive claim that nothing was attempted first, and this
        # Run does not know that.
        silent = run_view(row)
        assert (silent.route_position, silent.fallback_from) == ("unavailable", None)

        # Names without a fallback is a contradiction; the names lose.
        contradiction = run_view(replace(row, route_position="primary", fallback_from="a"))
        assert (contradiction.route_position, contradiction.fallback_from) == ("primary", None)

        # The fallback still stands when its names do not: something WAS passed
        # over, and no name is invented for it.
        poisoned = run_view(
            replace(
                row,
                route_position="fallback",
                fallback_from="provider said: PRIVATE-PROMPT-SENTINEL",
            )
        )
        assert (poisoned.route_position, poisoned.fallback_from) == ("fallback", None)
        assert "PRIVATE-PROMPT-SENTINEL" not in poisoned.model_dump_json()

        invented = run_view(replace(row, route_position="PRIVATE-POSITION-SENTINEL"))
        assert invented.route_position == "unavailable"
        assert "PRIVATE-POSITION-SENTINEL" not in invented.model_dump_json()

    asyncio.run(scenario())
    engine.dispose()


def _model_served(**detail: object) -> RunEvent:
    return RunEvent(
        event_id="event-1",
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=3,
        event_kind="model.served",
        event_status="served",
        parent_event_id=None,
        step_id="model:0",
        safe_detail={
            "model_policy_id": "agent.meridian-analyst.v2",
            "request_mode": "explicit",
            "requested_model": "frontier-sonnet",
            "served_model": "private-default",
            "served_lane": "private-vllm",
            "authorization_receipt_hash": "d" * 64,
            **detail,
        },
        detail_hash="b" * 64,
        created_at=datetime(2026, 8, 26, tzinfo=UTC),
    )


def test_model_served_states_the_route_position_and_keeps_provider_text_off_the_wire() -> None:
    """`model.served` named WHICH model and never WHY -- these two keys are the why.

    The event this test describes is an INVOLUNTARY PROVIDER FALLBACK, and it is
    NOT a `model.changed`: no actor moved the policy, the chain moved underneath
    an unchanged one. The two events stay separately readable -- `model.changed`
    carries `actor`/`safe_boundary` and before/after models, this carries a
    position and the names passed over, and neither key set appears on the other.
    """
    fell_back = event_view(
        _model_served(route_position="fallback", fallback_from="frontier-sonnet")
    )
    metadata = dict((item.key, item.value) for item in fell_back.metadata)
    assert metadata["route_position"] == "fallback"
    assert metadata["fallback_from"] == "frontier-sonnet"
    # `model.changed`'s own keys are not smuggled in by widening the allowlist.
    assert "actor" not in metadata
    assert "safe_boundary" not in metadata

    served = event_view(_model_served(route_position="primary"))
    positions = dict((item.key, item.value) for item in served.metadata)
    assert positions["route_position"] == "primary"
    # Omitted, not emptied: an empty string here would read as a name.
    assert "fallback_from" not in positions

    # THE ALLOWLIST IS STILL AN ALLOWLIST. Adding two keys did not make the
    # mapping reflective, and a writer that supplies an unnamed key -- however
    # innocuous-looking -- still gets nothing.
    assert "route_reason" not in dict(
        (item.key, item.value)
        for item in event_view(
            _model_served(route_position="primary", route_reason="PRIVATE-REASON-SENTINEL")
        ).metadata
    )

    # C12 -- THE PROVIDER'S ERROR TEXT DOES NOT RIDE THIS KEY. `fallback_from`
    # carries declared model names; an exception string (spaces, quotes, a
    # colon-space, anything that could echo a prompt substring back) fails the
    # projection's own alphabet check and is dropped whole rather than trimmed
    # into something that reads like a name.
    dirty = event_view(
        _model_served(
            route_position="fallback",
            fallback_from="litellm.BadRequestError: invalid tool_call_id 'PRIVATE-PROMPT-SENTINEL'",
        )
    )
    payload = dirty.model_dump_json()
    assert "PRIVATE-PROMPT-SENTINEL" not in payload
    assert "BadRequestError" not in payload
    assert dict((item.key, item.value) for item in dirty.metadata)["route_position"] == "fallback"

    # A position outside the two the writer can produce is not a third position.
    invented = event_view(_model_served(route_position="PRIVATE-POSITION-SENTINEL"))
    assert "route_position" not in dict((item.key, item.value) for item in invented.metadata)
    assert "PRIVATE-POSITION-SENTINEL" not in invented.model_dump_json()


def test_retry_lineage_and_refusal_project_without_a_refused_run_state() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, root = await _run(sessions, runs)
        retry = replace(
            root,
            run_id="retry-run",
            version=4,
            predecessor_run_id=root.run_id,
            root_run_id=root.run_id,
            retry_ordinal=1,
            status=RunState.COMPLETED,
            output_verdict="refused",
            terminal_at=datetime.now(UTC),
        )
        view = run_view(retry)
        assert view.status == "completed"
        assert view.output_verdict == "refused"
        assert view.predecessor_run_id == root.run_id
        assert view.root_run_id == root.run_id
        assert view.retry_ordinal == 1

        assert (
            run_view(
                replace(root, status=RunState.RUNNING, output_verdict="verified")
            ).output_verdict
            is None
        )
        assert (
            run_view(
                replace(
                    root,
                    status=RunState.FAILED,
                    output_verdict="grounded",
                    terminal_at=datetime.now(UTC),
                )
            ).output_verdict
            == "unavailable"
        )
        malformed = run_view(
            replace(
                root,
                predecessor_run_id=root.run_id,
                root_run_id=root.run_id,
                retry_ordinal=2,
            )
        )
        assert (malformed.predecessor_run_id, malformed.root_run_id, malformed.retry_ordinal) == (
            None,
            root.run_id,
            0,
        )

        projection = AgentRunProjection(runs, engine)
        refused = await projection.run_detail(
            run=retry,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        assert refused.claims == ()
        assert refused.claim_mapping_state == "unavailable"

        cancelling = await projection.run_detail(
            run=replace(root, status=RunState.CANCELLING),
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        assert cancelling.claims == ()
        assert cancelling.claim_mapping_state == "mapping_pending"

    asyncio.run(scenario())
    engine.dispose()


def test_run_detail_approval_id_requires_exact_actionable_turn_join() -> None:
    engine, sessions, runs = _stores()
    projection = AgentRunProjection(runs, engine)

    async def detail(run: AgentRun):
        return await projection.run_detail(
            run=run,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )

    async def scenario() -> None:
        _conversation, initial = await _run(sessions, runs)
        waiting, joined, target_attempt = await _wait_for_approval(
            engine,
            runs,
            initial,
            owner=OWNER,
        )
        projected = await detail(waiting)
        assert projected.approval_id == joined.approval_id
        foreign_owner = await projection.run_detail(
            run=waiting,
            owner_principal_id="user:foreign",
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        assert foreign_owner.approval_id is None
        with engine.connect() as conn:
            request = conn.execute(
                sa.select(ne_approval_requests).where(
                    ne_approval_requests.c.request_id == joined.approval_id
                )
            ).one()
        # Canonical Run-bound approvals intentionally do not use the legacy turn binding.
        assert request.turn_attempt_id is None

        # A later attempt must not hide or replace the exact canonical turn target.
        later_attempt = await runs.link_attempt(
            run_id=waiting.run_id,
            predecessor_attempt_id=target_attempt.attempt_id,
        )
        assert later_attempt.attempt_id != target_attempt.attempt_id
        assert (await detail(waiting)).approval_id == joined.approval_id

        with engine.begin() as conn:
            conn.execute(
                ne_approval_requests.update()
                .where(ne_approval_requests.c.request_id == joined.approval_id)
                .values(status="rejected", decided_at=datetime.now(UTC))
            )
        assert (await detail(waiting)).approval_id is None

        _second_conversation, second = await _run(sessions, runs)
        second_waiting, second_joined, _second_attempt = await _wait_for_approval(
            engine,
            runs,
            second,
            owner=OWNER,
        )
        running = await runs.transition(
            run_id=second_waiting.run_id,
            expected=RunState.WAITING_APPROVAL,
            target=RunState.RUNNING,
        )
        assert (await detail(running)).approval_id is None
        assert second_joined.approval_id

        _third_conversation, third = await _run(sessions, runs)
        third_waiting, third_joined, third_attempt = await _wait_for_approval(
            engine,
            runs,
            third,
            owner=OWNER,
            attach_target=False,
        )
        assert (await detail(third_waiting)).approval_id is None
        mismatched_attempt = await runs.link_attempt(
            run_id=third_waiting.run_id,
            predecessor_attempt_id=third_attempt.attempt_id,
        )
        _attach_turn_target(engine, third, mismatched_attempt.attempt_id)
        assert (await detail(third_waiting)).approval_id is None
        assert third_joined.approval_id

        _orphan_conversation, orphan = await _run(sessions, runs)
        await runs.transition(
            run_id=orphan.run_id,
            expected=RunState.SUBMITTED,
            target=RunState.QUEUED,
        )
        await runs.transition(
            run_id=orphan.run_id,
            expected=RunState.QUEUED,
            target=RunState.RUNNING,
        )
        orphan_attempt = await runs.link_attempt(run_id=orphan.run_id)
        _attach_turn_target(engine, orphan, orphan_attempt.attempt_id)
        orphan_waiting = await runs.transition(
            run_id=orphan.run_id,
            expected=RunState.RUNNING,
            target=RunState.WAITING_APPROVAL,
        )
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                ne_approval_requests.insert().values(
                    request_id="approval-without-run-join",
                    action="write.external.email.send",
                    tier=2,
                    principal_id=OWNER,
                    payload="{}",
                    action_scope=orphan.run_id,
                    args_hash="a" * 64,
                    conversation_id=None,
                    turn_department=None,
                    turn_agent_name=None,
                    turn_attachment_id=None,
                    turn_attempt_id=None,
                    turn_trace_id=None,
                    status="pending",
                    requested_at=now,
                    sla_deadline=now + timedelta(minutes=5),
                    expires_at=now + timedelta(hours=1),
                )
            )
        assert (await detail(orphan_waiting)).approval_id is None

    asyncio.run(scenario())
    engine.dispose()


def test_agentic_plan_requires_exact_target_and_contiguous_zero_based_safe_intents() -> None:
    engine, sessions, runs = _stores()
    projection = AgentRunProjection(runs, engine)

    async def scenario() -> None:
        _conversation, run = await _run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        _attach_turn_target(engine, run, attempt.attempt_id)

        def insert(stage: str, detail: object, *, attempt_id: str = attempt.attempt_id) -> None:
            with engine.begin() as conn:
                conn.execute(
                    ne_agent_run_execution_checkpoints.insert().values(
                        checkpoint_id=f"checkpoint-{stage}-{attempt_id}",
                        run_id=run.run_id,
                        attempt_id=attempt_id,
                        stage=stage,
                        intent_hash="a" * 64,
                        safe_detail=json.dumps(detail, separators=(",", ":"), sort_keys=True),
                        created_at=datetime.now(UTC),
                    )
                )

        async def plan():
            detail = await projection.run_detail(
                run=run,
                owner_principal_id=OWNER,
                department=DEPARTMENT,
                agent_id=AGENT,
            )
            return detail.agentic_plan

        first = {"ordinal": 0, "tool": "read.kg", "step_intent": "Find evidence."}
        second = {"ordinal": 1, "tool": "write.note", "step_intent": "Park result."}
        insert("agentic-intent:0", first)
        insert("agentic-intent:1", second)
        assert [step.model_dump() for step in (await plan() or ())] == [
            {**first, "tool_call_id": None},
            {**second, "tool_call_id": None},
        ]

        # THE BINDING IS A SERVED FACT, NOT COUNTER ARITHMETIC. A valid
        # `agentic-call:{ordinal}` checkpoint -- written by the executor frame
        # that minted the id -- attaches the id to exactly its step; a bind
        # naming a DIFFERENT tool, a malformed one, or one for an ordinal the
        # plan does not hold degrades to None for that step and NEVER poisons
        # the plan itself.
        insert(
            "agentic-call:0",
            {"ordinal": 0, "tool": "read.kg", "tool_call_id": "run-tool:tool:0"},
        )
        insert(
            "agentic-call:1",
            {"ordinal": 1, "tool": "MISMATCHED-TOOL", "tool_call_id": "run-tool:tool:1"},
        )
        insert(
            "agentic-call:7",
            {"ordinal": 7, "tool": "read.kg", "tool_call_id": "run-tool:tool:7"},
        )
        bound = await plan() or ()
        assert [step.tool_call_id for step in bound] == ["run-tool:tool:0", None]
        assert [step.model_dump(exclude={"tool_call_id"}) for step in bound] == [first, second]
        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_execution_checkpoints.delete().where(
                    ne_agent_run_execution_checkpoints.c.stage.like("agentic-call:%")
                )
            )

        # A second attempt for the same Run cannot be mixed into the exact turn target.
        later = await runs.link_attempt(
            run_id=run.run_id,
            predecessor_attempt_id=attempt.attempt_id,
        )
        insert(
            "agentic-intent:2",
            {"ordinal": 2, "tool": "read.foreign", "step_intent": "Wrong attempt."},
            attempt_id=later.attempt_id,
        )
        assert await plan() is None
        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_execution_checkpoints.delete().where(
                    ne_agent_run_execution_checkpoints.c.attempt_id == later.attempt_id
                )
            )
        assert len(await plan() or ()) == 2

        # A gap, duplicate ordinal, malformed key set, or stage/detail mismatch poisons all steps.
        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_execution_checkpoints.update()
                .where(ne_agent_run_execution_checkpoints.c.stage == "agentic-intent:1")
                .values(
                    stage="agentic-intent:2",
                    safe_detail=json.dumps(
                        {**second, "ordinal": 2}, separators=(",", ":"), sort_keys=True
                    ),
                )
            )
        assert await plan() is None
        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_execution_checkpoints.update()
                .where(ne_agent_run_execution_checkpoints.c.stage == "agentic-intent:2")
                .values(
                    stage="agentic-intent:1",
                    safe_detail=json.dumps(first, separators=(",", ":"), sort_keys=True),
                )
            )
        assert await plan() is None
        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_execution_checkpoints.update()
                .where(ne_agent_run_execution_checkpoints.c.stage == "agentic-intent:1")
                .values(
                    safe_detail=json.dumps(
                        {**second, "private": "PRIVATE-PLAN-SENTINEL"},
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
            )
        assert await plan() is None

    asyncio.run(scenario())
    engine.dispose()


def test_source_free_claim_materializes_only_its_span_and_source_bound_stays_opaque() -> None:
    engine, sessions, runs = _stores()

    async def seed_claim(*, display_state: str, text: bytes, source_bound: bool = False):
        conversation, run = await _run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        await runs.transition(
            run_id=run.run_id,
            expected=RunState.SUBMITTED,
            target=RunState.QUEUED,
        )
        await runs.transition(
            run_id=run.run_id,
            expected=RunState.QUEUED,
            target=RunState.RUNNING,
        )
        target = CanonicalTargetTuple(
            conversation_id=conversation.conversation_id,
            run_id=run.run_id,
            turn_id="turn-1",
            turn_sequence=1,
            attachment_id="attachment-1",
            attempt_id=attempt.attempt_id,
        )
        protected = await runs.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=run.run_id,
            owner_principal_id=OWNER,
            target_kind=AGENT_RUN_TURN_TARGET_KIND,
            target=target,
            content_status=(
                ProtectedContentStatus.SOURCE_BOUND_CONTAINER
                if source_bound
                else ProtectedContentStatus.SOURCE_FREE
            ),
            plaintext=text,
            source_stamps=(_stamp(),) if source_bound else (),
        )
        start = text.index(b"|") + 1
        end = text.rindex(b"|")
        claim = ClaimDeclaration(
            claim_key="claim-1",
            claim_index=0,
            claim_verdict="unavailable",
            display_state=display_state,  # type: ignore[arg-type]
            protected_ref=protected.row_id,
            served_output_hash="5" * 64,
            span_start_byte=start,
            span_end_byte=end,
            claim_content_hash=hashlib.sha256(text[start:end]).hexdigest(),
            association_status="association_unavailable",
        )
        await runs.commit_terminal_claims(
            run_id=run.run_id,
            owner_principal_id=OWNER,
            turn_id="turn-1",
            attempt_id=attempt.attempt_id,
            target=target,
            claims=(claim,),
            terminal_status=RunState.COMPLETED,
        )
        current = await runs.get_run(run_id=run.run_id, owner_principal_id=OWNER)
        assert current is not None
        return current, text[start:end]

    async def scenario() -> None:
        projection = AgentRunProjection(runs, engine)
        free, expected = await seed_claim(
            display_state="protected_source_free_slice",
            text=b"PREFIX-PRIVATE|SOURCE-FREE-DISPLAY|PRIVATE-SUFFIX",
        )
        free_view = await projection.run_detail(
            run=free,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        assert free_view.claims[0].content_state == "available"
        assert free_view.claims[0].display == expected.decode()
        assert "PREFIX-PRIVATE" not in free_view.model_dump_json()
        assert (
            projection._claim_rows(  # the SQL seam itself is owner-scoped
                free.run_id,
                "user:foreign",
                DEPARTMENT,
                AGENT,
            )
            == ()
        )

        bound, _ = await seed_claim(
            display_state="protected_source_slice",
            text=b"PRIVATE-PREFIX|PRIVATE-SOURCE-BOUND-OUTPUT|PRIVATE-SUFFIX",
            source_bound=True,
        )
        bound_view = await projection.run_detail(
            run=bound,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        claim = bound_view.claims[0]
        assert claim.content_state == "content_withheld"
        assert claim.display is None
        assert claim.locator_hash is None
        assert "PRIVATE-SOURCE-BOUND-OUTPUT" not in bound_view.model_dump_json()

    asyncio.run(scenario())
    engine.dispose()


def test_run_detail_projects_integrity_checked_frozen_profile_for_exact_latest_attempt() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _conversation, run = await _run(
            sessions,
            runs,
            strategy="deterministic",
            input_plaintext=b'{"max_subjects":3}',
        )
        attempt = await runs.link_attempt(run_id=run.run_id)
        profile = await runs.record_execution_profile(
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
            workflow_id="frozen-workflow",
            profile_version=7,
            profile_hash="3" * 64,
            input_schema={
                "type": "object",
                "properties": {"max_subjects": {"type": "integer"}},
                "required": ["max_subjects"],
                "additionalProperties": False,
            },
            ordered_plan=(
                {
                    "ordinal": 1,
                    "step_id": "lookup",
                    "kind": "governed-tool",
                    "action": "Read frozen evidence",
                    "model_role": None,
                },
                {
                    "ordinal": 2,
                    "step_id": "decide",
                    "kind": "deterministic-analysis",
                    "action": "Apply frozen rule",
                    "model_role": None,
                },
            ),
        )
        projection = AgentRunProjection(runs, engine)

        async def receipt():
            detail = await projection.run_detail(
                run=run,
                owner_principal_id=OWNER,
                department=DEPARTMENT,
                agent_id=AGENT,
            )
            return detail.execution_profile_receipt

        view = await receipt()
        assert view is not None
        assert view.profile_receipt_id == profile.profile_receipt_id
        assert view.run_id == run.run_id
        assert view.attempt_id == attempt.attempt_id
        assert view.workflow_id == "frozen-workflow"
        assert view.profile_version == 7
        assert view.profile_hash == "3" * 64
        assert view.typed_input_state == "available"
        assert view.typed_input == {"max_subjects": 3}
        assert [step.step_id for step in view.ordered_plan] == ["lookup", "decide"]

        # Any reorder or dropped member without new receipt hashes fails closed.
        assert (
            projection._verified_execution_plan(
                run,
                attempt.attempt_id,
                replace(profile, ordered_plan=tuple(reversed(profile.ordered_plan))),
            )
            is None
        )
        assert (
            projection._verified_execution_plan(
                run,
                attempt.attempt_id,
                replace(profile, ordered_plan=profile.ordered_plan[:1]),
            )
            is None
        )
        assert await receipt() is not None

        # A newer attempt never inherits the earlier attempt's frozen profile.
        await runs.link_attempt(
            run_id=run.run_id,
            predecessor_attempt_id=attempt.attempt_id,
        )
        assert await receipt() is None

    asyncio.run(scenario())
    engine.dispose()


def test_run_detail_projects_only_exact_frozen_skill_activation() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _conversation, run = await _run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        frozen = await runs.record_skill_activation(
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
            agent_id=run.agent_id,
            agent_version=run.agent_version,
            surface_key=f"run:{run.run_id}:attempt:{attempt.attempt_id}:skills",
            pins=(("frozen-skill", 4, "4" * 64),),
            index_hash="5" * 64,
            surface_hash="6" * 64,
        )
        projection = AgentRunProjection(runs, engine)
        detail = await projection.run_detail(
            run=run,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        view = detail.skill_activation_receipt
        assert view is not None
        assert view.activation_receipt_id == frozen.activation_receipt_id
        assert view.run_id == run.run_id
        assert view.attempt_id == attempt.attempt_id
        assert [pin.model_dump() for pin in view.pins] == [
            {"skill_id": "frozen-skill", "version": 4, "content_hash": "4" * 64}
        ]

        foreign = await _run(sessions, runs)
        foreign_attempt = await runs.link_attempt(run_id=foreign[1].run_id)
        assert (
            await runs.load_skill_activation(
                run_id=run.run_id,
                attempt_id=foreign_attempt.attempt_id,
                owner_principal_id=OWNER,
            )
            is None
        )

    asyncio.run(scenario())
    engine.dispose()


def test_failure_reason_serves_only_the_marked_safe_reason_of_the_exact_attempt() -> None:
    """A failed Run may say WHY - and may never say more than the raiser marked.

    `failure_class` is an OpaqueId, so a Run that died on a governance refusal
    used to be indistinguishable from any other crash and reached the reader as
    the console's own generic line. NE-SELECT-REPROBE added an opt-in
    `safe_reason`, checkpointed as `error_reason`; that is the only thing which
    may cross to a reader, and `error_class` deliberately stays behind.
    """

    engine, sessions, runs = _stores()
    projection = AgentRunProjection(runs, engine)

    async def scenario() -> None:
        _conversation, run = await _run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        _attach_turn_target(engine, run, attempt.attempt_id)
        failed = replace(run, status=RunState.FAILED)

        def insert(detail: object, *, attempt_id: str = attempt.attempt_id) -> None:
            with engine.begin() as conn:
                conn.execute(
                    ne_agent_run_execution_checkpoints.insert().values(
                        checkpoint_id=f"checkpoint-failed-{attempt_id}",
                        run_id=run.run_id,
                        attempt_id=attempt_id,
                        stage="execution_failed",
                        intent_hash="b" * 64,
                        safe_detail=json.dumps(detail, separators=(",", ":"), sort_keys=True),
                        created_at=datetime.now(UTC),
                    )
                )

        async def reason(target: AgentRun = failed) -> str | None:
            detail = await projection.run_detail(
                run=target,
                owner_principal_id=OWNER,
                department=DEPARTMENT,
                agent_id=AGENT,
            )
            return detail.failure_reason

        # NOTHING RECORDED, NOTHING SAID. No checkpoint means no reason - the
        # reader gets the status word, never a sentence this layer invented.
        assert await reason() is None

        # A class alone is NOT a reason. `error_class` is an OpaqueId and must
        # not become prose for a person just because the slot exists.
        insert({"error_class": "ModelSelectionError"})
        assert await reason() is None

        with engine.begin() as conn:
            conn.execute(ne_agent_run_execution_checkpoints.delete())

        # THE MARKED REASON IS SERVED, verbatim.
        insert(
            {
                "error_class": "ModelSelectionError",
                "error_reason": "No current serving evidence for the requested lane.",
            }
        )
        assert await reason() == "No current serving evidence for the requested lane."

        # A WORKING RUN HAS NOT FAILED and says nothing, even with the record
        # present: the read is spent only where a Run could actually have failed.
        assert await reason(replace(run, status=RunState.RUNNING)) is None

        # A SUPERSEDED ATTEMPT MAY NOT SPEAK FOR THE RUN - it would explain the
        # wrong failure.
        with engine.begin() as conn:
            conn.execute(ne_agent_run_execution_checkpoints.delete())
        later = await runs.link_attempt(
            run_id=run.run_id,
            predecessor_attempt_id=attempt.attempt_id,
        )
        insert(
            {"error_class": "ModelSelectionError", "error_reason": "Wrong attempt."},
            attempt_id=later.attempt_id,
        )
        assert await reason() is None

        # Blank, over-long and structurally wrong reasons are all refused rather
        # than half-trusted.
        for bad in ("", "   ", "x" * 257, 17, None, ["a"]):
            with engine.begin() as conn:
                conn.execute(ne_agent_run_execution_checkpoints.delete())
            insert({"error_reason": bad})
            assert await reason() is None

    asyncio.run(scenario())


def test_a_no_progress_stop_reaches_the_reader_as_its_own_stated_sentence() -> None:
    """NE-LOOP-NOPROGRESS end-to-end over the publication seam it depends on.

    The loop's stop is only worth building if the reader is TOLD about it: a run
    that stopped because retrieval kept coming back empty must not arrive as the
    console's generic "the run ended before it produced an answer", which reads
    as a broken deployment. The sentence is written at the raise site and this
    proves it survives the round trip verbatim - so the string asserted here is
    the error's OWN `safe_reason`, never a copy typed into the test.
    """

    error = AgenticNoProgressError(tool="read.knowledge.search", calls=3)
    engine, sessions, runs = _stores()
    projection = AgentRunProjection(runs, engine)

    async def scenario() -> None:
        _conversation, run = await _run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        _attach_turn_target(engine, run, attempt.attempt_id)
        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_execution_checkpoints.insert().values(
                    checkpoint_id=f"checkpoint-no-progress-{attempt.attempt_id}",
                    run_id=run.run_id,
                    attempt_id=attempt.attempt_id,
                    stage="execution_failed",
                    intent_hash="c" * 64,
                    # EXACTLY what run_workflow writes for an exception that
                    # marked a safe reason - built from the error, not retyped.
                    safe_detail=json.dumps(
                        {
                            "error_class": type(error).__name__,
                            "error_reason": error.safe_reason,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    created_at=datetime.now(UTC),
                )
            )
        detail = await projection.run_detail(
            run=replace(run, status=RunState.FAILED),
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        reason = detail.failure_reason
        assert reason == error.safe_reason
        # It names the tool and what was actually measured, which is what lets a
        # reader tell "no evidence exists" from "the system broke". The wording
        # moved once, and for a reason worth keeping here: it used to say
        # "Nothing was retrieved to ground an answer", which was FALSE for a
        # successful read whose result carries no `urn:` ref -- three resolved CRM
        # contacts read as nothing retrieved. The sentence now states what the
        # counter measures, and this asserts against the overstatement too.
        assert reason is not None
        assert "read.knowledge.search" in reason
        assert "had not already been served" in reason
        assert "Nothing was retrieved" not in reason

    asyncio.run(scenario())
    engine.dispose()


def test_a_budget_exhausted_run_serves_the_ceiling_it_recorded_and_nothing_else() -> None:
    """A run that died on a ceiling reads as the ceiling, not as a bare status word.

    `budget_exhausted` is one terminal shared by the token, tool-call, request,
    wall-clock and parked-write ceilings. The projection already reads the failure
    record for the exhausted statuses -- only the producer was missing, so the live
    demo's exhausted run served `failure_reason: null` and the reader was told a
    status word that cannot say which number to change.
    """

    engine, sessions, runs = _stores()
    projection = AgentRunProjection(runs, engine)

    async def scenario() -> None:
        _conversation, run = await _run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        _attach_turn_target(engine, run, attempt.attempt_id)
        exhausted = replace(run, status=RunState.BUDGET_EXHAUSTED)

        async def reason() -> str | None:
            detail = await projection.run_detail(
                run=exhausted,
                owner_principal_id=OWNER,
                department=DEPARTMENT,
                agent_id=AGENT,
            )
            return detail.failure_reason

        # THE CONTROL, first: an exhausted run whose producer recorded no ceiling
        # says nothing. This half is what stops the pin below from passing against
        # a projection that manufactures a sentence for the status word.
        assert await reason() is None

        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_execution_checkpoints.insert().values(
                    checkpoint_id=f"checkpoint-exhausted-{attempt.attempt_id}",
                    run_id=run.run_id,
                    attempt_id=attempt.attempt_id,
                    stage="execution_failed",
                    intent_hash="c" * 64,
                    safe_detail=json.dumps(
                        {"error_reason": "stopped: the turn's 96,650-token budget was exhausted"},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    created_at=datetime.now(UTC),
                )
            )
        assert await reason() == "stopped: the turn's 96,650-token budget was exhausted"

    asyncio.run(scenario())


def _insert_plain_event(
    engine: sa.Engine,
    *,
    run_id: str,
    attempt_id: str,
    sequence: int,
    event_id: str,
    kind: str,
    status: str,
) -> None:
    detail = json.dumps({"dispatch_id": "dispatch-1", "lease_epoch": 1}, sort_keys=True)
    with engine.begin() as conn:
        conn.execute(
            ne_agent_run_events.insert().values(
                event_id=event_id,
                run_id=run_id,
                attempt_id=attempt_id,
                sequence=sequence,
                event_kind=kind,
                event_status=status,
                parent_event_id=None,
                step_id="run-execution",
                safe_detail=detail,
                detail_hash=hashlib.sha256(detail.encode()).hexdigest(),
                created_at=datetime(2026, 8, 26, tzinfo=UTC),
            )
        )


def _insert_intent_event(
    engine: sa.Engine,
    *,
    run_id: str,
    attempt_id: str,
    sequence: int,
    event_id: str,
    audit_event_id: str,
    action: str,
    kind: str = "run.audit.intent",
    decision_kind: str = "state_transition",
    tool_call_id: str | None = "tool-call-7",
    resource: str = "PRIVATE-RESOURCE-SENTINEL",
    payload: dict | None = None,
) -> None:
    """Write one Run event plus the audit intent and chain row that NAME it."""

    now = datetime(2026, 8, 26, tzinfo=UTC)
    detail = json.dumps(
        {
            "audit_action_hash": "a" * 64,
            "audit_resource_hash": "b" * 64,
            "event_intent_hash": "c" * 64,
            "private_context": "PRIVATE-CONTEXT-SENTINEL",
        },
        sort_keys=True,
    )
    with engine.begin() as conn:
        conn.execute(
            ne_agent_run_events.insert().values(
                event_id=event_id,
                run_id=run_id,
                attempt_id=attempt_id,
                sequence=sequence,
                event_kind=kind,
                event_status="receipt_pending",
                parent_event_id=None,
                step_id="run-execution",
                safe_detail=detail,
                detail_hash=hashlib.sha256(detail.encode()).hexdigest(),
                created_at=now,
            )
        )
        conn.execute(
            ne_audit_log.insert().values(
                seq=sequence,
                event_id=audit_event_id,
                event_type="audit",
                config_hash="a" * 64,
                actor="run-worker",
                occurred_at=now,
                kpi_tag=None,
                trace_id=run_id,
                action=action,
                resource=resource,
                outcome="success",
                payload=payload if payload is not None else {"detail": "PRIVATE-PAYLOAD-SENTINEL"},
                prev_hash="0" * 64,
                record_hash=f"{sequence:064x}",
            )
        )
        conn.execute(
            ne_agent_run_audit_intents.insert().values(
                address_hash=f"{sequence:064x}",
                event_intent_hash="c" * 64,
                run_id=run_id,
                attempt_id=attempt_id,
                run_event_id=event_id,
                audit_event_id=audit_event_id,
                step_id="run-execution",
                tool_call_id=tool_call_id,
                approval_id=None,
                decision_kind=decision_kind,
                event_ordinal=0,
                occurred_at=now,
            )
        )


def test_audit_intent_events_project_their_readable_governed_action() -> None:
    engine, sessions, runs = _stores()
    ensure_audit_schema(engine)
    ensure_run_audit_intent_schema(engine)
    projection = AgentRunProjection(runs, engine, audit_engine=engine)

    async def scenario() -> None:
        _, run = await _run(sessions, runs)
        attempt_id = (await runs.link_attempt(run_id=run.run_id)).attempt_id
        # A claimed execution is real recorded work; it was being clamped to
        # "event.unavailable" alongside every other unenumerated kind.
        _insert_plain_event(
            engine,
            run_id=run.run_id,
            attempt_id=attempt_id,
            sequence=1,
            event_id="event-claimed",
            kind="run.execution.claimed",
            status="running",
        )
        _insert_intent_event(
            engine,
            run_id=run.run_id,
            attempt_id=attempt_id,
            sequence=2,
            event_id="event-tool",
            audit_event_id="audit-tool",
            action="run.tool.pre_effect",
        )
        _insert_intent_event(
            engine,
            run_id=run.run_id,
            attempt_id=attempt_id,
            sequence=3,
            event_id="event-foreign",
            audit_event_id="audit-foreign",
            action="ne.gateway.complete",
            decision_kind="PRIVATE-DECISION-SENTINEL",
            tool_call_id=None,
        )

        page = await projection.list_events(
            run=run, owner_principal_id=OWNER, after_sequence=0, limit=50
        )
        assert [item.event_kind for item in page.items] == [
            "run.execution.claimed",
            "run.audit.intent",
            "run.audit.intent",
        ]
        assert page.items[0].event_status == "running"
        # THE CURSOR IS A WATERMARK. It names the last sequence served whenever
        # the page served anything, so a tailing reader (the Ask thread, every
        # two seconds) can resume instead of re-reading the whole log; the end
        # of the log is an EMPTY page, not a null cursor.
        assert page.next_cursor is not None
        assert decode_cursor(page.next_cursor, kind=f"event:{run.run_id}") == (
            run.created_at,
            str(page.items[-1].sequence),
        )
        empty = await projection.list_events(
            run=run,
            owner_principal_id=OWNER,
            after_sequence=page.items[-1].sequence,
            limit=50,
        )
        assert empty.items == ()
        assert empty.next_cursor is None
        named = page.items[1]
        assert named.event_status == "receipt_pending"
        assert dict((item.key, item.value) for item in named.metadata) == {
            "audit_action": "run.tool.pre_effect",
            "decision_kind": "state_transition",
            "tool_call_id": "tool-call-7",
        }
        # An action outside the enumerated Run audit set, and a decision kind the
        # server never defined, are DROPPED -- the join names steps, it does not
        # reflect whatever the chain happens to hold.
        assert page.items[2].metadata == ()
        payload = page.model_dump_json()
        assert "PRIVATE-CONTEXT-SENTINEL" not in payload
        assert "PRIVATE-RESOURCE-SENTINEL" not in payload
        assert "PRIVATE-PAYLOAD-SENTINEL" not in payload
        assert "PRIVATE-DECISION-SENTINEL" not in payload

        # Cursor semantics are untouched by the join.
        first = await projection.list_events(
            run=run, owner_principal_id=OWNER, after_sequence=0, limit=1
        )
        assert first.next_cursor is not None
        assert decode_cursor(first.next_cursor, kind=f"event:{run.run_id}") == (
            run.created_at,
            "1",
        )

    asyncio.run(scenario())
    engine.dispose()


def test_unmapped_event_kinds_still_degrade_even_with_an_audit_intent() -> None:
    engine, sessions, runs = _stores()
    ensure_audit_schema(engine)
    ensure_run_audit_intent_schema(engine)
    projection = AgentRunProjection(runs, engine, audit_engine=engine)

    async def scenario() -> None:
        _, run = await _run(sessions, runs)
        attempt_id = (await runs.link_attempt(run_id=run.run_id)).attempt_id
        _insert_intent_event(
            engine,
            run_id=run.run_id,
            attempt_id=attempt_id,
            sequence=2,
            event_id="event-unknown",
            audit_event_id="audit-unknown",
            action="run.tool.pre_effect",
            kind="PRIVATE-EVENT-KIND-SENTINEL",
        )

        page = await projection.list_events(
            run=run, owner_principal_id=OWNER, after_sequence=0, limit=50
        )
        unknown = page.items[0]
        assert unknown.event_kind == "event.unavailable"
        assert "PRIVATE-EVENT-KIND-SENTINEL" not in page.model_dump_json()

    asyncio.run(scenario())
    engine.dispose()


def test_the_derived_failure_floor_is_servable_by_this_exact_validator() -> None:
    """A failure nobody explained still reaches the reader.

    The runtime now writes a derived reason for any FAILED path that recorded
    none (`RunFinalizer._ensure_failure_reason`). That is only worth anything if
    it survives THIS layer's strict validation, so the floor is checked against
    the real projection rather than trusted to be well-intentioned. This layer
    still invents nothing: it serves what the runtime published, which is why
    the sibling controls above - a class alone is not a reason, an exhausted run
    says only the ceiling it recorded - are untouched by it.
    """

    engine, sessions, runs = _stores()
    projection = AgentRunProjection(runs, engine)

    async def scenario() -> None:
        _conversation, run = await _run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        _attach_turn_target(engine, run, attempt.attempt_id)
        floor = derived_failure_reason("RunEffectSettlementUncertain")
        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_execution_checkpoints.insert().values(
                    checkpoint_id="checkpoint-derived-floor",
                    run_id=run.run_id,
                    attempt_id=attempt.attempt_id,
                    stage="execution_failed",
                    intent_hash="c" * 64,
                    safe_detail=json.dumps(
                        {
                            "error_class": "RunEffectSettlementUncertain",
                            "error_reason": floor,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    created_at=datetime.now(UTC),
                )
            )
        detail = await projection.run_detail(
            run=replace(run, status=RunState.FAILED),
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        assert detail.failure_reason == floor
        assert "RunEffectSettlementUncertain" in floor

    asyncio.run(scenario())
    engine.dispose()


@dataclass
class _ComposedState:
    """The composition root's shape, as `RunProjectionState` declares it."""

    agent_run_store: AgentRunStore | None
    engines: list[sa.Engine]


def test_projection_resolves_its_audit_engine_across_same_database_aliases(tmp_path) -> None:
    """THE DEPLOYED COMPOSITION, which a one-engine unit test cannot see.

    The projection resolved its audit engine by counting candidates and taking
    the single one. This box composes a runtime engine and an audit engine over
    ONE `neutron` database, so two candidates answered `has_table` and the count
    said "ambiguous" -- audit_engine became None, `_audit_actions` returned {},
    and `audit_action` vanished from every event on the live wire while
    `decision_kind` (read from the runtime engine) kept arriving. The feature
    read as half-built rather than unwired, for a whole deploy cycle.

    So the assertion is END-TO-END and over TWO engine objects: one database,
    two pools, and the readable action still reaches the reader. Reverting
    `locate_table_engine` to the old count fails this test at the last line.
    """

    database_url = f"sqlite:///{tmp_path / 'composed-deployment.db'}"
    # TWO POOLS, ONE DATABASE -- exactly what `deps.py` composes in production.
    runtime_engine = sa.create_engine(database_url)
    audit_engine = sa.create_engine(database_url)
    ensure_runtime_schema(runtime_engine)
    ensure_run_schema(runtime_engine)
    ensure_run_workflow_schema(runtime_engine)
    ensure_approval_schema(runtime_engine)
    ensure_run_audit_intent_schema(runtime_engine)
    ensure_audit_schema(audit_engine)
    cipher = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="projection-k1", keys={"projection-k1": b"k" * 32})
    )
    runs = AgentRunStore(runtime_engine, cipher)
    sessions = SqlAlchemySessionStore(runtime_engine)

    # THE PRECONDITION THE OLD RESOLUTION TRIPPED ON, pinned as its own fact:
    # both engines see the audit log, because both address one database.
    assert sa.inspect(runtime_engine).has_table(ne_audit_log.name)
    assert sa.inspect(audit_engine).has_table(ne_audit_log.name)

    projection = projection_from_state(
        _ComposedState(agent_run_store=runs, engines=[runtime_engine, audit_engine])
    )

    async def scenario() -> None:
        _, run = await _run(sessions, runs)
        attempt_id = (await runs.link_attempt(run_id=run.run_id)).attempt_id
        _insert_intent_event(
            runtime_engine,
            run_id=run.run_id,
            attempt_id=attempt_id,
            sequence=1,
            event_id="event-tool",
            audit_event_id="audit-tool",
            action="run.tool.pre_effect",
        )
        page = await projection.list_events(
            run=run, owner_principal_id=OWNER, after_sequence=0, limit=50
        )
        served = dict((item.key, item.value) for item in page.items[0].metadata)
        # `decision_kind` came from the runtime engine and survived the bug; it
        # is asserted so a regression cannot pass by losing BOTH halves.
        assert served["decision_kind"] == "state_transition"
        # THE LINE THAT WOULD HAVE CAUGHT IT.
        assert served["audit_action"] == "run.tool.pre_effect"

    asyncio.run(scenario())
    runtime_engine.dispose()
    audit_engine.dispose()


def test_audit_engine_resolution_still_refuses_two_distinct_databases(tmp_path) -> None:
    """Deduplication is not permissiveness: two REAL databases stay ambiguous.

    Collapsing aliases of one database must not collapse two databases that
    each hold an audit log. There is no right answer there, and picking one
    would put a second deployment's chain behind this projection.
    """

    first = sa.create_engine(f"sqlite:///{tmp_path / 'first-audit.db'}")
    second = sa.create_engine(f"sqlite:///{tmp_path / 'second-audit.db'}")
    ensure_audit_schema(first)
    ensure_audit_schema(second)

    assert locate_table_engine([first, second], ne_audit_log.name) is None
    # And one database seen through three pools is still that one database.
    alias = sa.create_engine(f"sqlite:///{tmp_path / 'first-audit.db'}")
    assert locate_table_engine([first, alias], ne_audit_log.name) is first
    first.dispose()
    second.dispose()
    alias.dispose()


def test_evidence_events_state_class_and_count_and_never_content() -> None:
    """`evidence.produced` serves the writer's typed self-description: kind,
    locator status, assurance class, contributor count. Values outside those
    closed vocabularies -- and every unenumerated key -- are dropped."""

    event = RunEvent(
        event_id="event-evidence",
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=9,
        event_kind="evidence.produced",
        event_status="preterminal",
        parent_event_id=None,
        step_id="run-tool",
        safe_detail={
            "read_kind": "search",
            "evidence_status": "located",
            "evidence_assurance": "direct",
            "contributor_count": 3,
            "chunk_text": "PRIVATE-CHUNK-SENTINEL",
            "locator": "PRIVATE-LOCATOR-SENTINEL",
            "row_anchor": "f" * 64,
        },
        detail_hash="b" * 64,
        created_at=datetime(2026, 8, 26, tzinfo=UTC),
    )
    view = event_view(event)
    assert dict((item.key, item.value) for item in view.metadata) == {
        "read_kind": "search",
        "evidence_status": "located",
        "evidence_assurance": "direct",
        "contributor_count": 3,
    }
    payload = view.model_dump_json()
    assert "PRIVATE-CHUNK-SENTINEL" not in payload
    assert "PRIVATE-LOCATOR-SENTINEL" not in payload
    assert "row_anchor" not in payload

    # A drifted or attacker-shaped stored value fails the closed vocabulary and
    # is dropped, never served as if the server had defined it.
    drifted = event_view(
        replace(
            event,
            event_kind="evidence.verified_row",
            event_status="verified",
            safe_detail={
                "read_kind": "PRIVATE-KIND-SENTINEL",
                "evidence_status": "definitely located, trust me",
                "evidence_assurance": "vibes",
                "contributor_count": -2,
            },
        )
    )
    assert drifted.event_kind == "evidence.verified_row"
    assert drifted.metadata == ()
    assert "PRIVATE-KIND-SENTINEL" not in drifted.model_dump_json()


def test_audit_join_names_the_tool_and_the_pinned_model_from_the_chains_own_record() -> None:
    from ne.api.run_projection import RunEventAudit, _audit_metadata

    # The chain named the act: the row serves the registered tool identifier.
    named = _audit_metadata(
        RunEventAudit(
            audit_action="run.tool.pre_effect",
            decision_kind="state_transition",
            tool_call_id="run-tool:tool:0",
            tool_name="read.knowledge.search",
        )
    )
    assert dict((item.key, item.value) for item in named) == {
        "audit_action": "run.tool.pre_effect",
        "decision_kind": "state_transition",
        "tool_call_id": "run-tool:tool:0",
        "tool_name": "read.knowledge.search",
    }

    # A resource that is not a tool identifier -- provider prose, an argument,
    # a sentence -- is dropped, never truncated into something name-shaped.
    assert not any(
        item.key == "tool_name"
        for item in _audit_metadata(
            RunEventAudit(
                audit_action="run.tool.pre_effect",
                tool_name='rm -rf "/" # PRIVATE-ARGUMENT-SENTINEL',
            )
        )
    )
    # A tool name is served only beside the action that authored it.
    assert not any(
        item.key == "tool_name"
        for item in _audit_metadata(
            RunEventAudit(audit_action="run.model.pre_effect", tool_name="read.knowledge.search")
        )
    )

    # The pinned model name, same discipline.
    model = _audit_metadata(
        RunEventAudit(audit_action="run.model.pre_effect", requested_model="frontier-anthropic")
    )
    assert dict((item.key, item.value) for item in model) == {
        "audit_action": "run.model.pre_effect",
        "requested_model": "frontier-anthropic",
    }
    assert not any(
        item.key == "requested_model"
        for item in _audit_metadata(
            RunEventAudit(
                audit_action="run.model.pre_effect",
                requested_model='error: "no capacity" PRIVATE-PROVIDER-SENTINEL',
            )
        )
    )
    assert _audit_metadata(None) == ()


def test_intent_events_serve_tool_and_model_names_read_from_the_audit_chain() -> None:
    engine, sessions, runs = _stores()
    ensure_audit_schema(engine)
    ensure_run_audit_intent_schema(engine)
    projection = AgentRunProjection(runs, engine, audit_engine=engine)

    async def scenario() -> None:
        _, run = await _run(sessions, runs)
        attempt_id = (await runs.link_attempt(run_id=run.run_id)).attempt_id
        _insert_intent_event(
            engine,
            run_id=run.run_id,
            attempt_id=attempt_id,
            sequence=1,
            event_id="event-tool-named",
            audit_event_id="audit-tool-named",
            action="run.tool.pre_effect",
            tool_call_id="run-tool:tool:0",
            resource="tool:read.knowledge.search",
        )
        _insert_intent_event(
            engine,
            run_id=run.run_id,
            attempt_id=attempt_id,
            sequence=2,
            event_id="event-model-named",
            audit_event_id="audit-model-named",
            action="run.model.pre_effect",
            tool_call_id=None,
            resource="gateway-task:frontier-default",
            payload={"provenance": {"requested_model": "frontier-anthropic"}},
        )
        _insert_intent_event(
            engine,
            run_id=run.run_id,
            attempt_id=attempt_id,
            sequence=3,
            event_id="event-model-anon",
            audit_event_id="audit-model-anon",
            action="run.model.pre_effect",
            tool_call_id=None,
            resource="gateway-task:frontier-default",
            payload={"provenance": {"requested_model": 'error: "PRIVATE-PROVIDER-SENTINEL"'}},
        )

        page = await projection.list_events(
            run=run, owner_principal_id=OWNER, after_sequence=0, limit=50
        )
        tool_row, model_row, anon_row = page.items
        assert dict((item.key, item.value) for item in tool_row.metadata) == {
            "audit_action": "run.tool.pre_effect",
            "decision_kind": "state_transition",
            "tool_call_id": "run-tool:tool:0",
            "tool_name": "read.knowledge.search",
        }
        assert dict((item.key, item.value) for item in model_row.metadata) == {
            "audit_action": "run.model.pre_effect",
            "decision_kind": "state_transition",
            "requested_model": "frontier-anthropic",
        }
        # An un-nameable model call still serves its action -- the honest
        # unknown -- and the provider-shaped string never reaches the wire.
        assert dict((item.key, item.value) for item in anon_row.metadata) == {
            "audit_action": "run.model.pre_effect",
            "decision_kind": "state_transition",
        }
        assert "PRIVATE-PROVIDER-SENTINEL" not in page.model_dump_json()

    asyncio.run(scenario())
    engine.dispose()
