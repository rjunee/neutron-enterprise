# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-009B principal-owned Run/Event HTTP projection controls."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from ne.api.deps import api_state, current_principal
from ne.api.platform import (
    AGENT_PRIVATE_PAYLOAD_KEK_ENV,
    AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV,
)
from ne.api.routers import runs as runs_router
from ne.api.run_submission import (
    AcceptedRunSubmission,
    RunSubmissionService,
    compose_run_submission_service,
)
from ne.contracts.types import Principal
from ne.runtime.approval_store import ensure_approval_schema
from ne.runtime.private_payloads import (
    AGENT_RUN_TURN_TARGET_KIND,
    CanonicalTargetTuple,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    RunPrivatePayloadCipher,
    StaticPrivatePayloadKeys,
)
from ne.runtime.run_store import (
    AgentRunStore,
    ClaimDeclaration,
    RunState,
    ensure_run_schema,
)
from ne.runtime.run_workflow import (
    ensure_run_workflow_schema,
    ne_agent_run_execution_checkpoints,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .conftest import AppBuilder, get_state, run
from .test_run_projection import _wait_for_approval

AGENT = "agt_projection"
OTHER_AGENT = "agt_other"
OWNER = "user:ada"
DEPARTMENT = "operations"
_PRODUCTION_AGENT = "agt_01k3meridiananalyst000000000"
_REGISTRY = Path(__file__).parents[3] / "deploy/agents/registry.toml"


class _Catalog:
    def get_visible(self, agent_id: str, _principal: Principal):
        return object() if agent_id in {AGENT, OTHER_AGENT} else None


def _environment(principal: Principal):
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    ensure_run_workflow_schema(engine)
    ensure_approval_schema(engine)
    sessions = SqlAlchemySessionStore(engine)
    run_store = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(
                current_key_id="projection-k1", keys={"projection-k1": b"k" * 32}
            )
        ),
    )
    state = SimpleNamespace(
        session_store=sessions,
        agent_run_store=run_store,
        agent_catalog=_Catalog(),
        engines=[engine],
    )
    app = FastAPI()
    app.include_router(runs_router.router)
    app.dependency_overrides[api_state] = lambda: state
    app.dependency_overrides[current_principal] = lambda: principal
    return engine, sessions, run_store, TestClient(app)


async def _create_run(
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


async def _complete_with_source_free_claim(
    engine: sa.Engine,
    sessions: SqlAlchemySessionStore,
    runs: AgentRunStore,
):
    conversation, run = await _create_run(sessions, runs)
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
    text = b"PRIVATE-PREFIX|SAFE CLAIM|PRIVATE-SUFFIX"
    start = text.index(b"|") + 1
    end = text.rindex(b"|")
    protected = await runs.claim_protected_payload(
        domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
        run_id=run.run_id,
        owner_principal_id=OWNER,
        target_kind=AGENT_RUN_TURN_TARGET_KIND,
        target=target,
        content_status=ProtectedContentStatus.SOURCE_FREE,
        plaintext=text,
    )
    # Seed one preterminal persisted event through the store's owning insertion
    # primitive. There is intentionally no public arbitrary-event writer.
    with engine.begin() as conn:
        append_event = runs._append_event
        append_event(
            conn,
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
            event_kind="evidence.produced",
            event_status="preterminal",
            safe_detail={"private_disguised": "PRIVATE-EVENT-DETAIL-SENTINEL"},
            step_id="step-1",
        )
    await runs.commit_terminal_claims(
        run_id=run.run_id,
        owner_principal_id=OWNER,
        turn_id="turn-1",
        attempt_id=attempt.attempt_id,
        target=target,
        claims=(
            ClaimDeclaration(
                claim_key="claim-1",
                claim_index=0,
                claim_verdict="unavailable",
                display_state="protected_source_free_slice",
                protected_ref=protected.row_id,
                served_output_hash="4" * 64,
                span_start_byte=start,
                span_end_byte=end,
                claim_content_hash=hashlib.sha256(text[start:end]).hexdigest(),
                association_status="association_unavailable",
            ),
        ),
        terminal_status=RunState.COMPLETED,
    )
    return conversation, run


def test_run_list_get_and_event_pages_are_private_content_minimal_and_stable() -> None:
    engine, sessions, runs, client = _environment(
        Principal(id=OWNER, claims={"department": DEPARTMENT})
    )

    async def seed():
        completed_conversation, completed = await _complete_with_source_free_claim(
            engine, sessions, runs
        )
        await _create_run(sessions, runs)
        await _create_run(sessions, runs, department="finance")
        await _create_run(sessions, runs, agent_id=OTHER_AGENT)
        await _create_run(sessions, runs, owner="user:foreign")
        return completed_conversation, completed

    completed_conversation, completed = asyncio.run(seed())
    first = client.get(f"/v1/agents/{AGENT}/runs", params={"limit": 1})
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-store"
    assert len(first.json()["items"]) == 1
    assert first.json()["next_cursor"]
    second = client.get(
        f"/v1/agents/{AGENT}/runs",
        params={"limit": 1, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1
    assert second.json()["next_cursor"] is None

    detail = client.get(f"/v1/agents/{AGENT}/runs/{completed.run_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert set(body) == {
        "run_id",
        "conversation_id",
        "agent_id",
        "agent_version",
        "sequence",
        "version",
        "predecessor_run_id",
        "root_run_id",
        "retry_ordinal",
        "display_label",
        "strategy",
        "status",
        "output_verdict",
        "model_policy_id",
        "model_request_mode",
        "context_snapshot_hash",
        "requested_model",
        "requested_model_lane",
        "served_model",
        # WHY `served_model` IS THAT MODEL, and content-minimal for the same
        # reason `served_model` and `requested_model_lane` already are: both are
        # server-declared IDENTIFIERS out of the deployment's own model catalog
        # and its two fixed route positions. Neither is derived from, or varies
        # with, the customer's text.
        #
        # `route_position` is one of exactly three server-chosen words --
        # "primary", "fallback", "unavailable" -- and cannot carry anything
        # else. It is here because a Run that named two different models and no
        # relationship between them made an INVOLUNTARY PROVIDER FALLBACK
        # unreadable from the record, which is the one degrade this product
        # claims cannot be silent.
        #
        # `fallback_from` names the candidates passed over, and is the one field
        # here an attacker-shaped string could plausibly ride. It cannot: the
        # writer admits only declared model names character by character
        # (`run_store._require_route_position`) and the projection re-checks the
        # same alphabet before serving (`run_projection._is_model_name_list`),
        # so a provider error message -- which may echo prompt substrings back
        # -- is dropped rather than truncated into something name-shaped. The
        # provider's error TEXT is deliberately NOT projected; whether it ever
        # may is a separate governance decision this pair does not pre-empt.
        "route_position",
        "fallback_from",
        "output_preview",
        # DETAIL-ONLY, and additive. `output_preview: str | None` cannot tell
        # "produced nothing" apart from "produced, and current source authority
        # no longer authorizes you"; the console rendered both as one blank.
        # Absent from `RunView`, so the list page above is unchanged.
        "output_state",
        "config_hash",
        "capability_snapshot_hash",
        "created_at",
        "updated_at",
        "terminal_at",
        "approval_id",
        # The OPT-IN safe reason a failed Run published. Content-minimal by
        # construction: it is a static string a raiser explicitly marked
        # publishable, never an exception message and never `error_class`,
        # which is an OpaqueId and stays server-side.
        "failure_reason",
        "agentic_plan",
        "execution_profile_receipt",
        "skill_activation_receipt",
        "claims",
        "claim_mapping_state",
        "claims_truncated",
    }
    assert body["conversation_id"] == completed_conversation.conversation_id
    assert body["predecessor_run_id"] is None
    assert body["root_run_id"] == completed.run_id
    assert body["retry_ordinal"] == 0
    assert body["output_verdict"] == "unavailable"
    assert body["approval_id"] is None
    assert body["agentic_plan"] is None
    assert body["execution_profile_receipt"] is None
    assert body["skill_activation_receipt"] is None
    assert body["claims"][0]["display"] == "SAFE CLAIM"
    assert body["claims"][0]["content_state"] == "available"
    serialized = detail.text
    for private in (
        "PRIVATE-RUN-INPUT-SENTINEL",
        "PRIVATE-PREFIX",
        "PRIVATE-SUFFIX",
    ):
        assert private not in serialized

    event_one = client.get(
        f"/v1/agents/{AGENT}/runs/{completed.run_id}/events", params={"limit": 1}
    )
    assert event_one.status_code == 200
    assert len(event_one.json()["items"]) == 1
    assert event_one.json()["next_cursor"]
    assert event_one.json()["items"][0]["metadata"] == []
    assert "PRIVATE-EVENT-DETAIL-SENTINEL" not in event_one.text
    event_two = client.get(
        f"/v1/agents/{AGENT}/runs/{completed.run_id}/events",
        params={"limit": 1, "cursor": event_one.json()["next_cursor"]},
    )
    assert event_two.status_code == 200
    assert event_two.json()["items"][0]["event_kind"] == "run.terminal"
    # A WATERMARK, not an end-of-pages flag: the server mints a cursor for every
    # page it served anything on, so the Ask thread can TAIL this endpoint
    # instead of re-reading a growing log every two seconds. The end of the log
    # is an EMPTY page.
    assert event_two.json()["next_cursor"]
    event_three = client.get(
        f"/v1/agents/{AGENT}/runs/{completed.run_id}/events",
        params={"limit": 1, "cursor": event_two.json()["next_cursor"]},
    )
    assert event_three.status_code == 200
    assert event_three.json()["items"] == []
    assert event_three.json()["next_cursor"] is None
    engine.dispose()


def test_waiting_run_detail_exposes_exact_actionable_approval_id() -> None:
    engine, sessions, runs, client = _environment(
        Principal(id=OWNER, claims={"department": DEPARTMENT})
    )

    async def seed():
        _conversation, initial = await _create_run(sessions, runs)
        waiting, joined, _attempt = await _wait_for_approval(
            engine,
            runs,
            initial,
            owner=OWNER,
        )
        return waiting, joined

    waiting, joined = asyncio.run(seed())
    response = client.get(f"/v1/agents/{AGENT}/runs/{waiting.run_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["approval_id"] == joined.approval_id
    assert "PRIVATE-APPROVAL-PAYLOAD" not in response.text
    engine.dispose()


def test_run_detail_projects_only_exact_persisted_agentic_intent_plan() -> None:
    engine, sessions, runs, client = _environment(
        Principal(id=OWNER, claims={"department": DEPARTMENT})
    )

    async def seed():
        _conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        from .test_run_projection import _attach_turn_target

        _attach_turn_target(engine, run, attempt.attempt_id)
        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_execution_checkpoints.insert().values(
                    checkpoint_id="checkpoint-safe-intent",
                    run_id=run.run_id,
                    attempt_id=attempt.attempt_id,
                    stage="agentic-intent:0",
                    intent_hash="a" * 64,
                    safe_detail=json.dumps(
                        {
                            "ordinal": 0,
                            "tool": "read.kg",
                            "step_intent": "Find governed evidence.",
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    created_at=run.created_at,
                )
            )
        return run

    run = asyncio.run(seed())
    response = client.get(f"/v1/agents/{AGENT}/runs/{run.run_id}")
    assert response.status_code == 200
    assert response.json()["agentic_plan"] == [
        {
            "ordinal": 0,
            "tool": "read.kg",
            "step_intent": "Find governed evidence.",
            # No `agentic-call` binding was recorded for the step, so the wire
            # states the honest unknown rather than a counted-up guess.
            "tool_call_id": None,
        }
    ]
    assert "PRIVATE" not in response.text
    engine.dispose()


def test_run_detail_serves_frozen_profile_input_plan_and_skill_receipts_no_store() -> None:
    engine, sessions, runs, client = _environment(
        Principal(id=OWNER, claims={"department": DEPARTMENT})
    )

    async def seed():
        _deterministic_conversation, deterministic = await _create_run(
            sessions,
            runs,
            strategy="deterministic",
            input_plaintext=b'{"max_subjects":2}',
        )
        deterministic_attempt = await runs.link_attempt(run_id=deterministic.run_id)
        profile = await runs.record_execution_profile(
            run_id=deterministic.run_id,
            attempt_id=deterministic_attempt.attempt_id,
            workflow_id="frozen-workflow",
            profile_version=9,
            profile_hash="3" * 64,
            input_schema={
                "type": "object",
                "properties": {"max_subjects": {"type": "integer"}},
                "required": ["max_subjects"],
            },
            ordered_plan=(
                {
                    "ordinal": 1,
                    "step_id": "frozen-step",
                    "kind": "deterministic-analysis",
                    "action": "Apply the frozen rule",
                    "model_role": None,
                },
            ),
        )
        _agentic_conversation, agentic = await _create_run(sessions, runs)
        agentic_attempt = await runs.link_attempt(run_id=agentic.run_id)
        skill = await runs.record_skill_activation(
            run_id=agentic.run_id,
            attempt_id=agentic_attempt.attempt_id,
            agent_id=agentic.agent_id,
            agent_version=agentic.agent_version,
            surface_key=f"run:{agentic.run_id}:attempt:{agentic_attempt.attempt_id}:skills",
            pins=(("frozen-skill", 5, "4" * 64),),
            index_hash="5" * 64,
            surface_hash="6" * 64,
        )
        return deterministic, deterministic_attempt, profile, agentic, agentic_attempt, skill

    deterministic, deterministic_attempt, profile, agentic, agentic_attempt, skill = asyncio.run(
        seed()
    )
    deterministic_response = client.get(f"/v1/agents/{AGENT}/runs/{deterministic.run_id}")
    assert deterministic_response.status_code == 200
    assert deterministic_response.headers["cache-control"] == "private, no-store"
    execution = deterministic_response.json()["execution_profile_receipt"]
    assert execution == {
        "profile_receipt_id": profile.profile_receipt_id,
        "run_id": deterministic.run_id,
        "attempt_id": deterministic_attempt.attempt_id,
        "workflow_id": "frozen-workflow",
        "profile_version": 9,
        "profile_hash": "3" * 64,
        "input_schema": {
            "type": "object",
            "properties": {"max_subjects": {"type": "integer"}},
            "required": ["max_subjects"],
        },
        "input_schema_hash": profile.input_schema_hash,
        "typed_input_state": "available",
        "typed_input": {"max_subjects": 2},
        "ordered_plan": [
            {
                "ordinal": 1,
                "step_id": "frozen-step",
                "kind": "deterministic-analysis",
                "action": "Apply the frozen rule",
                "model_role": None,
            }
        ],
        "ordered_plan_hash": profile.ordered_plan_hash,
        "receipt_hash": profile.receipt_hash,
        "created_at": profile.created_at.isoformat().replace("+00:00", "Z"),
    }
    assert deterministic_response.json()["skill_activation_receipt"] is None

    skill_response = client.get(f"/v1/agents/{AGENT}/runs/{agentic.run_id}")
    assert skill_response.status_code == 200
    assert skill_response.headers["cache-control"] == "private, no-store"
    activation = skill_response.json()["skill_activation_receipt"]
    assert activation["activation_receipt_id"] == skill.activation_receipt_id
    assert activation["run_id"] == agentic.run_id
    assert activation["attempt_id"] == agentic_attempt.attempt_id
    assert activation["pins"] == [
        {"skill_id": "frozen-skill", "version": 5, "content_hash": "4" * 64}
    ]
    assert skill_response.json()["execution_profile_receipt"] is None
    engine.dispose()


def test_foreign_random_wrong_department_and_wrong_agent_are_byte_equivalent() -> None:
    engine, sessions, runs, client = _environment(
        Principal(
            id="user:bob",
            groups=("role:ops",),
            claims={"department": DEPARTMENT},
        )
    )

    async def seed():
        _conversation, foreign_initial = await _create_run(sessions, runs, owner=OWNER)
        foreign, _joined, _attempt = await _wait_for_approval(
            engine,
            runs,
            foreign_initial,
            owner=OWNER,
        )
        _other_conversation, wrong_department = await _create_run(
            sessions, runs, owner="user:bob", department="finance"
        )
        _wrong_agent_conversation, wrong_agent = await _create_run(
            sessions, runs, owner="user:bob", agent_id=OTHER_AGENT
        )
        return foreign, wrong_department, wrong_agent

    foreign, wrong_department, wrong_agent = asyncio.run(seed())
    random = client.get(f"/v1/agents/{AGENT}/runs/run-random")
    for run_id in (foreign.run_id, wrong_department.run_id, wrong_agent.run_id):
        hidden = client.get(f"/v1/agents/{AGENT}/runs/{run_id}")
        assert (hidden.status_code, hidden.content, hidden.headers["cache-control"]) == (
            random.status_code,
            random.content,
            random.headers["cache-control"],
        )
    engine.dispose()


def test_event_cursor_is_bound_to_its_run_and_write_routes_are_explicit() -> None:
    engine, sessions, runs, client = _environment(
        Principal(id=OWNER, claims={"department": DEPARTMENT})
    )
    _conversation, completed = asyncio.run(_complete_with_source_free_claim(engine, sessions, runs))
    _other_conversation, other = asyncio.run(
        _complete_with_source_free_claim(engine, sessions, runs)
    )
    first = client.get(f"/v1/agents/{AGENT}/runs/{completed.run_id}/events", params={"limit": 1})
    cross_run = client.get(
        f"/v1/agents/{AGENT}/runs/{other.run_id}/events",
        params={"cursor": first.json()["next_cursor"]},
    )
    assert cross_run.status_code == 404

    methods = {
        (route.path, method)
        for route in runs_router.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }
    assert methods == {
        ("/v1/agents/{agent_id}/runs", "GET"),
        ("/v1/agents/{agent_id}/runs", "POST"),
        ("/v1/agents/{agent_id}/runs/{run_id}", "GET"),
        ("/v1/agents/{agent_id}/runs/{run_id}/events", "GET"),
        ("/v1/agents/{agent_id}/runs/{run_id}/cancel", "POST"),
        ("/v1/agents/{agent_id}/runs/{run_id}/retry", "POST"),
    }
    engine.dispose()


def test_accepted_submission_wire_refuses_terminal_or_retired_states() -> None:
    with pytest.raises(HTTPException) as captured:
        runs_router._accepted(
            AcceptedRunSubmission(
                action="start",
                run_id="run-1",
                attempt_id="attempt-1",
                version=1,
                status=cast(Any, "refused"),
                enqueue_id="enqueue-1",
                replayed=False,
            )
        )
    assert getattr(captured.value, "status_code", None) == 503


def test_production_store_composes_atomic_submission_seam(
    make_app: AppBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV, "projection-production-k1")
    monkeypatch.setenv(
        AGENT_PRIVATE_PAYLOAD_KEK_ENV,
        base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode("ascii"),
    )
    harness = make_app(agent_manifest_path=str(_REGISTRY), protected_transport=True)
    state = get_state(harness.app)
    store = state.agent_run_store
    assert store is not None

    async def seed() -> tuple[str, str]:
        _owned_conversation, owned = await _create_run(
            state.session_store,
            store,
            owner=OWNER,
            department=DEPARTMENT,
            agent_id=_PRODUCTION_AGENT,
        )
        _foreign_conversation, foreign = await _create_run(
            state.session_store,
            store,
            owner="user:foreign",
            department=DEPARTMENT,
            agent_id=_PRODUCTION_AGENT,
        )
        return owned.run_id, foreign.run_id

    owned_run_id, foreign_run_id = run(seed())
    headers = harness.bearer(OWNER, department=DEPARTMENT)
    assert isinstance(
        compose_run_submission_service(
            transactions=store,
            agent_catalog=state.agent_catalog,
            agent_preflight=state.agent_preflight,
            model_selection=state.model_selection,
            config_hash=state.config_hash,
        ),
        RunSubmissionService,
    )

    async def exercise() -> None:
        async with harness.client() as client:
            owned = await client.get(
                f"/api/v1/agents/{_PRODUCTION_AGENT}/runs/{owned_run_id}",
                headers=headers,
            )
            hidden = await client.get(
                f"/api/v1/agents/{_PRODUCTION_AGENT}/runs/{foreign_run_id}",
                headers=headers,
            )
            random = await client.get(
                f"/api/v1/agents/{_PRODUCTION_AGENT}/runs/run-random",
                headers=headers,
            )
            assert owned.status_code == 200
            assert owned.headers["cache-control"] == "private, no-store"
            assert (hidden.status_code, hidden.content, hidden.headers["cache-control"]) == (
                random.status_code,
                random.content,
                random.headers["cache-control"],
            )
            assert hidden.status_code == 404

    run(exercise())
