# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""HTTP composition proof for governed inference lifecycle controls."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from ne.api.inference_lifecycle import (
    LIFECYCLE_ACTION,
    InferenceLifecycleConfig,
    inference_pending_changes,
    inference_target_state,
)
from ne.contracts.governance import DecisionEffect, PolicyDecision
from ne.contracts.inference import InferenceOperatorCode, InferenceTargetState
from ne.contracts.types import Lane
from ne.gateway.config import ModelDef, TaskRoute
from ne.runtime.config import ApprovalSettings

from .conftest import AppBuilder, get_state, run
from .test_inference_lifecycle import (
    AcceptedThenStatusOperator,
    FailedOperator,
    FakeOperator,
    target,
)


def managed_model() -> ModelDef:
    return ModelDef(
        name="private-default",
        litellm_model="hosted_vllm/qwen",
        provider="hosted_vllm",
        lane=Lane.PRIVATE_VLLM,
        api_base="http://vllm:8000/v1",
        context_window=32768,
    )


async def wait_for_lifecycle_action(client, headers, action: str) -> dict:
    deadline = asyncio.get_running_loop().time() + 3
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get("/api/v1/models/lifecycle", headers=headers)
        assert response.status_code == 200, response.text
        target_body = response.json()["targets"][0]
        if action in target_body["available_actions"]:
            return target_body
        await asyncio.sleep(0.02)
    raise AssertionError(f"lifecycle action {action!r} never became available")


def test_lifecycle_alias_typo_fails_boot_instead_of_leaving_real_model_unmanaged(
    make_app: AppBuilder,
) -> None:
    mistyped = target().model_copy(update={"aliases": ("private-defualt",)})
    with pytest.raises(ValueError, match="absent from the gateway registry"):
        make_app(
            ops_roles=("ops",),
            inference_lifecycle=InferenceLifecycleConfig(targets=(mistyped,)),
            gateway_models=(managed_model(),),
        )


def test_partial_rollout_is_read_only_and_transition_returns_503(
    make_app: AppBuilder,
) -> None:
    harness = make_app(
        ops_roles=("ops",),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
        gateway_models=(managed_model(),),
    )

    async def flow() -> None:
        lifecycle = get_state(harness.app).inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        async with harness.client() as client:
            headers = harness.bearer("user:ops", groups=("ops",))
            read = await client.get("/api/v1/models/lifecycle", headers=headers)
            assert read.status_code == 200, read.text
            body = read.json()
            assert body["transport_available"] is False
            assert body["targets"][0]["state"] == "unknown"
            assert body["targets"][0]["available_actions"] == []

            write = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition",
                headers=headers,
                json={
                    "desired_state": "ready",
                    "expected_version": 0,
                    "idempotency_key": "start-once",
                },
            )
            assert write.status_code == 503, write.text
            assert "transport" in write.json()["detail"]

    run(flow())


def test_start_is_tier_two_and_cannot_reach_operator_before_approval(
    make_app: AppBuilder,
) -> None:
    operator = FakeOperator()
    harness = make_app(
        ops_roles=("ops",),
        approver_roles=("approver",),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
        inference_operator=operator,
        gateway_models=(managed_model(),),
    )

    async def flow() -> None:
        lifecycle = get_state(harness.app).inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        async with harness.client() as client:
            headers = harness.bearer("user:ops", groups=("ops",))
            view = await wait_for_lifecycle_action(client, headers, "start")
            response = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition",
                headers=headers,
                json={
                    "desired_state": "ready",
                    "expected_version": view["version"],
                    "idempotency_key": "start-once",
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "pending"
            assert operator.start_count == 0

    run(flow())


@pytest.mark.parametrize(
    ("desired_state", "operator_state"),
    [
        ("ready", InferenceTargetState.READY),
        ("stopped", InferenceTargetState.STOPPED),
    ],
)
def test_fresh_key_for_already_proven_terminal_state_is_applied_without_hitl_or_host(
    make_app: AppBuilder,
    desired_state: str,
    operator_state: InferenceTargetState,
) -> None:
    operator = FakeOperator()
    operator.state = operator_state
    harness = make_app(
        ops_roles=("ops",),
        approver_roles=("approver",),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
        inference_operator=operator,
        gateway_models=(managed_model(),),
    )

    async def flow() -> None:
        lifecycle = get_state(harness.app).inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        now = datetime.now(UTC)
        engine = sa.create_engine(harness.audit_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    inference_target_state.update()
                    .where(inference_target_state.c.target_id == "cpu-vllm")
                    .values(observed_at=now, updated_at=now)
                )
        finally:
            engine.dispose()
        row = lifecycle.state("cpu-vllm")
        assert row.state is operator_state
        ops = harness.bearer("user:ops", groups=("ops",))
        approver = harness.bearer("user:approver", groups=("approver",))
        async with harness.client() as client:
            response = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition",
                headers=ops,
                json={
                    "desired_state": desired_state,
                    "expected_version": row.version,
                    "idempotency_key": f"already-{desired_state}",
                },
            )
            assert response.status_code == 200, response.text
            assert response.json() == {"status": "applied", "request_id": None}
            queue = await client.get("/api/v1/approvals/pending", headers=approver)
            assert [
                item for item in queue.json()["approvals"] if item["action"] == LIFECYCLE_ACTION
            ] == []
        assert operator.start_count == 0
        assert operator.stop_count == 0

    run(flow())


@pytest.mark.parametrize("unproven", ["stale", "transport-absent"])
def test_already_ready_fails_closed_when_terminal_proof_is_not_current(
    make_app: AppBuilder,
    unproven: str,
) -> None:
    operator = FakeOperator()
    operator.state = InferenceTargetState.READY
    harness = make_app(
        ops_roles=("ops",),
        approver_roles=("approver",),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
        inference_operator=operator,
        gateway_models=(managed_model(),),
    )

    async def flow() -> None:
        lifecycle = get_state(harness.app).inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        now = datetime.now(UTC)
        observed_at = (
            now - timedelta(seconds=target().reconcile_cadence_s * 3)
            if unproven == "stale"
            else now
        )
        engine = sa.create_engine(harness.audit_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    inference_target_state.update()
                    .where(inference_target_state.c.target_id == "cpu-vllm")
                    .values(observed_at=observed_at, updated_at=observed_at)
                )
        finally:
            engine.dispose()
        if unproven == "transport-absent":
            object.__setattr__(lifecycle, "_operator", None)
        row = lifecycle.state("cpu-vllm")
        ops = harness.bearer("user:ops", groups=("ops",))
        approver = harness.bearer("user:approver", groups=("approver",))
        async with harness.client() as client:
            response = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition",
                headers=ops,
                json={
                    "desired_state": "ready",
                    "expected_version": row.version,
                    "idempotency_key": f"unproven-{unproven}",
                },
            )
            assert response.status_code == 503, response.text
            assert response.json()["detail"] == "terminal target state is not currently proven"
            queue = await client.get("/api/v1/approvals/pending", headers=approver)
            assert [
                item for item in queue.json()["approvals"] if item["action"] == LIFECYCLE_ACTION
            ] == []
        assert operator.start_count == 0
        assert operator.stop_count == 0

    run(flow())


def test_pending_lifecycle_request_is_single_flight_before_approval(
    make_app: AppBuilder,
) -> None:
    operator = FakeOperator()
    harness = make_app(
        ops_roles=("ops",),
        approver_roles=("approver",),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
        inference_operator=operator,
        gateway_models=(managed_model(),),
    )

    async def flow() -> None:
        lifecycle = get_state(harness.app).inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        ops = harness.bearer("user:ops", groups=("ops",))
        approver = harness.bearer("user:approver", groups=("approver",))
        body = {
            "desired_state": "ready",
            "expected_version": lifecycle.state("cpu-vllm").version,
            "idempotency_key": "single-flight-start",
        }
        async with harness.client() as client:
            first = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition", headers=ops, json=body
            )
            assert first.status_code == 200, first.text
            assert first.json()["status"] == "pending"
            request_id = first.json()["request_id"]

            identical = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition", headers=ops, json=body
            )
            assert identical.status_code == 200, identical.text
            assert identical.json() == {"status": "pending", "request_id": request_id}

            conflicting = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition",
                headers=ops,
                json={**body, "idempotency_key": "different-start"},
            )
            assert conflicting.status_code == 409, conflicting.text
            queue = await client.get("/api/v1/approvals/pending", headers=approver)
            matches = [
                item for item in queue.json()["approvals"] if item["action"] == LIFECYCLE_ACTION
            ]
            assert [item["request_id"] for item in matches] == [request_id]
            assert operator.start_count == 0
            rejected = await client.post(
                f"/api/v1/approvals/{request_id}/reject",
                headers=approver,
                json={"reason": "not now"},
            )
            assert rejected.status_code == 200, rejected.text
            for _ in range(100):
                view = await client.get("/api/v1/models/lifecycle", headers=ops)
                if view.json()["targets"][0]["pending_change"] is None:
                    break
                await asyncio.sleep(0.01)
            replacement = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition",
                headers=ops,
                json={**body, "idempotency_key": "after-rejection"},
            )
            assert replacement.status_code == 200, replacement.text
            assert replacement.json()["status"] == "pending"

    run(flow())


def test_exact_durable_approval_repairs_crash_before_reservation_attach(
    make_app: AppBuilder,
) -> None:
    operator = FakeOperator()
    harness = make_app(
        ops_roles=("ops",),
        approver_roles=("approver",),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
        inference_operator=operator,
        gateway_models=(managed_model(),),
    )

    async def flow() -> None:
        lifecycle = get_state(harness.app).inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        ops = harness.bearer("user:ops", groups=("ops",))
        approver = harness.bearer("user:approver", groups=("approver",))
        body = {
            "desired_state": "ready",
            "expected_version": lifecycle.state("cpu-vllm").version,
            "idempotency_key": "repair-unattached",
        }
        async with harness.client() as client:
            first = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition", headers=ops, json=body
            )
            assert first.status_code == 200, first.text
            request_id = first.json()["request_id"]
            engine = sa.create_engine(harness.audit_url)
            try:
                with engine.begin() as conn:
                    changed = conn.execute(
                        inference_pending_changes.update()
                        .where(inference_pending_changes.c.request_id == request_id)
                        .values(request_id=None)
                    )
                assert changed.rowcount == 1
            finally:
                engine.dispose()

            repaired = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition", headers=ops, json=body
            )
            assert repaired.status_code == 200, repaired.text
            assert repaired.json() == {"status": "pending", "request_id": request_id}
            engine = sa.create_engine(harness.audit_url)
            try:
                with engine.connect() as conn:
                    attached = conn.execute(
                        sa.select(inference_pending_changes.c.request_id).where(
                            inference_pending_changes.c.target_id == "cpu-vllm"
                        )
                    ).scalar_one()
                assert attached == request_id
            finally:
                engine.dispose()
            queue = await client.get("/api/v1/approvals/pending", headers=approver)
            matches = [
                item for item in queue.json()["approvals"] if item["action"] == LIFECYCLE_ACTION
            ]
            assert [item["request_id"] for item in matches] == [request_id]
            assert operator.start_count == 0
            rejected = await client.post(
                f"/api/v1/approvals/{request_id}/reject",
                headers=approver,
                json={"reason": "test cleanup"},
            )
            assert rejected.status_code == 200, rejected.text

    run(flow())


def test_valid_accepted_transition_response_remains_pending(make_app: AppBuilder) -> None:
    operator = AcceptedThenStatusOperator(observed_at=datetime.now(UTC))
    harness = make_app(
        ops_roles=("ops",),
        approver_roles=("approver",),
        approvals=ApprovalSettings(poll_interval_s=0.001),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
        inference_operator=operator,
        gateway_models=(managed_model(),),
    )

    async def flow() -> None:
        lifecycle = get_state(harness.app).inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        ops = harness.bearer("user:ops", groups=("ops",))
        approver = harness.bearer("user:approver", groups=("approver",))
        async with harness.client() as writer, harness.client() as reviewer:
            submitted = asyncio.create_task(
                writer.post(
                    "/api/v1/models/lifecycle/cpu-vllm/transition",
                    headers=ops,
                    json={
                        "desired_state": "ready",
                        "expected_version": lifecycle.state("cpu-vllm").version,
                        "idempotency_key": "accepted-start",
                    },
                )
            )
            pending = None
            for _ in range(1000):
                queue = await reviewer.get("/api/v1/approvals/pending", headers=approver)
                matches = [
                    item for item in queue.json()["approvals"] if item["action"] == LIFECYCLE_ACTION
                ]
                if matches:
                    pending = matches[0]
                    break
                await asyncio.sleep(0)
            assert pending is not None
            response = await asyncio.wait_for(submitted, timeout=3)
            assert response.status_code == 200, response.text
            assert response.json() == {
                "status": "pending",
                "request_id": pending["request_id"],
            }
            approved = await reviewer.post(
                f"/api/v1/approvals/{pending['request_id']}/approve",
                headers=approver,
                json={},
            )
            assert approved.status_code == 200
            deadline = asyncio.get_running_loop().time() + 3
            row = None
            while asyncio.get_running_loop().time() < deadline:
                view = await reviewer.get("/api/v1/models/lifecycle", headers=ops)
                assert view.status_code == 200, view.text
                row = view.json()["targets"][0]
                if row["state"] == "starting" and row["pending_change"] is None:
                    break
                await asyncio.sleep(0.001)
            assert row is not None
            assert row["pending_change"] is None
            assert row["state"] == "starting"
            assert lifecycle.state("cpu-vllm").state is InferenceTargetState.STARTING
            assert operator.start_count == 1

    run(flow())


def test_manifest_mismatch_is_503_before_approval_or_mutation(make_app: AppBuilder) -> None:
    bad_target = target().model_copy(update={"adapter_id": "expected-adapter"})
    operator = FakeOperator()
    harness = make_app(
        ops_roles=("ops",),
        approver_roles=("approver",),
        inference_lifecycle=InferenceLifecycleConfig(targets=(bad_target,)),
        inference_operator=operator,
        gateway_models=(managed_model(),),
    )

    async def flow() -> None:
        lifecycle = get_state(harness.app).inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        async with harness.client() as client:
            headers = harness.bearer("user:ops", groups=("ops",))
            await asyncio.sleep(0.05)
            read = await client.get("/api/v1/models/lifecycle", headers=headers)
            assert read.status_code == 200
            assert read.json()["targets"][0]["available_actions"] == []
            response = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition",
                headers=headers,
                json={
                    "desired_state": "ready",
                    "expected_version": 0,
                    "idempotency_key": "mismatch-start",
                },
            )
            assert response.status_code == 503, response.text
            assert "manifest" in response.json()["detail"]
            assert operator.start_count == operator.stop_count == 0
            engine = sa.create_engine(harness.runtime_url)
            try:
                with engine.connect() as conn:
                    count = conn.execute(
                        sa.text("SELECT COUNT(*) FROM ne_approval_requests")
                    ).scalar_one()
                assert count == 0
            finally:
                engine.dispose()

    run(flow())


def test_stop_without_bound_confirmation_is_rejected_before_approval(
    make_app: AppBuilder,
) -> None:
    operator = FakeOperator()
    operator.state = InferenceTargetState.READY
    harness = make_app(
        ops_roles=("ops",),
        approver_roles=("approver",),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
        inference_operator=operator,
        gateway_models=(managed_model(),),
    )

    async def flow() -> None:
        lifecycle = get_state(harness.app).inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        async with harness.client() as client:
            headers = harness.bearer("user:ops", groups=("ops",))
            response = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition",
                headers=headers,
                json={
                    "desired_state": "stopped",
                    "expected_version": 1,
                    "idempotency_key": "stop-without-confirmation",
                },
            )
            assert response.status_code == 409, response.text
            assert "confirmation" in response.json()["detail"]
            assert operator.stop_count == 0
            engine = sa.create_engine(harness.runtime_url)
            try:
                with engine.connect() as conn:
                    count = conn.execute(
                        sa.text("SELECT COUNT(*) FROM ne_approval_requests")
                    ).scalar_one()
                assert count == 0
            finally:
                engine.dispose()

    run(flow())


@pytest.mark.parametrize("deny_mode", ["approval", "pdp"])
def test_terminal_governance_denial_records_requested_and_refused_once(
    make_app: AppBuilder,
    monkeypatch: pytest.MonkeyPatch,
    deny_mode: str,
) -> None:
    operator = FakeOperator()
    harness = make_app(
        ops_roles=("ops",),
        approver_roles=("approver",),
        approvals=ApprovalSettings(poll_interval_s=0.001),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
        inference_operator=operator,
        gateway_models=(managed_model(),),
    )
    state = get_state(harness.app)
    if deny_mode == "pdp":
        real_pdp = state.executor._pdp  # type: ignore[attr-defined]

        class _DenyLifecycle:
            async def decide(self, request):
                if request.action == LIFECYCLE_ACTION:
                    return PolicyDecision(
                        effect=DecisionEffect.DENY,
                        policy_version="test",
                        reason="denied by test",
                    )
                return await real_pdp.decide(request)

        monkeypatch.setattr(state.executor, "_pdp", _DenyLifecycle())

    async def flow() -> None:
        lifecycle = state.inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        ops = harness.bearer("user:ops", groups=("ops",))
        approver = harness.bearer("user:approver", groups=("approver",))
        async with harness.client() as client:
            submitted = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition",
                headers=ops,
                json={
                    "desired_state": "ready",
                    "expected_version": lifecycle.state("cpu-vllm").version,
                    "idempotency_key": f"{deny_mode}-denied-start",
                },
            )
            if deny_mode == "pdp":
                assert submitted.status_code == 403, submitted.text
            else:
                assert submitted.status_code == 200, submitted.text
                request_id = submitted.json()["request_id"]
                rejected = await client.post(
                    f"/api/v1/approvals/{request_id}/reject",
                    headers=approver,
                    json={"reason": "not now"},
                )
                assert rejected.status_code == 200, rejected.text
            actions: list[str] = []
            for _ in range(100):
                await asyncio.sleep(0.01)
                engine = sa.create_engine(harness.audit_url)
                try:
                    with engine.connect() as conn:
                        actions = list(
                            conn.execute(
                                sa.text(
                                    "SELECT action FROM ne_audit_log "
                                    "WHERE action IN ('inference.lifecycle.requested', "
                                    "'inference.lifecycle.refused') ORDER BY seq"
                                )
                            ).scalars()
                        )
                finally:
                    engine.dispose()
                if actions.count("inference.lifecycle.refused") == 1:
                    break
            assert actions.count("inference.lifecycle.requested") == 1
            assert actions.count("inference.lifecycle.refused") == 1
            assert operator.start_count == 0

    run(flow())


def test_confirmed_stop_persists_pending_link_and_executes_as_exact_principal(
    make_app: AppBuilder,
) -> None:
    operator = FakeOperator()
    operator.state = InferenceTargetState.READY
    harness = make_app(
        ops_roles=("ops",),
        approver_roles=("approver",),
        approvals=ApprovalSettings(poll_interval_s=0.02),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
        inference_operator=operator,
        gateway_models=(managed_model(),),
    )

    async def flow() -> None:
        lifecycle = get_state(harness.app).inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        assert lifecycle.state("cpu-vllm").version == 1
        ops = harness.bearer("user:ops", groups=("ops",))
        approver = harness.bearer("user:approver", groups=("approver",))
        async with harness.client() as client:
            confirmation = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/stop-confirmation",
                headers=ops,
                json={"expected_version": 1},
            )
            assert confirmation.status_code == 200, confirmation.text
            submitted = await client.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition",
                headers=ops,
                json={
                    "desired_state": "stopped",
                    "expected_version": 1,
                    "idempotency_key": "approved-stop",
                    "confirmation_token": confirmation.json()["confirmation_token"],
                },
            )
            assert submitted.status_code == 200, submitted.text
            request_id = submitted.json()["request_id"]
            assert request_id
            assert operator.stop_count == 0

            reloaded = await client.get("/api/v1/models/lifecycle", headers=ops)
            pending = reloaded.json()["targets"][0]["pending_change"]
            assert pending == {
                "request_id": request_id,
                "requested_by": "user:ops",
                "desired_state": "stopped",
            }
            assert reloaded.json()["targets"][0]["available_actions"] == []

            approved = await client.post(
                f"/api/v1/approvals/{request_id}/approve", json={}, headers=approver
            )
            assert approved.status_code == 200, approved.text
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                read = await client.get("/api/v1/models/lifecycle", headers=ops)
                if read.json()["targets"][0]["state"] == "stopped":
                    break
                await asyncio.sleep(0.02)
            assert operator.stop_count == 1
            assert lifecycle.state("cpu-vllm").state is InferenceTargetState.STOPPED

        engine = sa.create_engine(harness.audit_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    sa.text(
                        "SELECT action, actor, payload FROM ne_audit_log "
                        "WHERE action LIKE 'inference.lifecycle.%' ORDER BY seq"
                    )
                ).all()
            actions = [row.action for row in rows]
            assert "inference.lifecycle.requested" in actions
            assert "inference.lifecycle.started" in actions
            assert "inference.lifecycle.completed" in actions
            for row in rows:
                assert row.actor == "user:ops"
                detail = json.loads(json.loads(row.payload)["detail"])
                assert set(detail) == {
                    "target_id",
                    "transition_id",
                    "old_state",
                    "desired_state",
                    "observed_state",
                    "code",
                } or set(detail) == {
                    "target_id",
                    "old_state",
                    "desired_state",
                    "observed_state",
                    "code",
                    "version",
                    "transition_id",
                }
        finally:
            engine.dispose()

    run(flow())


def test_post_approval_version_drift_fails_parked_transition_without_mutation(
    make_app: AppBuilder,
) -> None:
    operator = FakeOperator()
    harness = make_app(
        ops_roles=("ops",),
        approver_roles=("approver",),
        approvals=ApprovalSettings(poll_interval_s=0.01),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
        inference_operator=operator,
        gateway_models=(managed_model(),),
    )

    async def flow() -> None:
        lifecycle = get_state(harness.app).inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        ops = harness.bearer("user:ops", groups=("ops",))
        approver = harness.bearer("user:approver", groups=("approver",))
        async with harness.client() as writer, harness.client() as reviewer:
            submitted = asyncio.create_task(
                writer.post(
                    "/api/v1/models/lifecycle/cpu-vllm/transition",
                    headers=ops,
                    json={
                        "desired_state": "ready",
                        "expected_version": 1,
                        "idempotency_key": "drifted-start",
                    },
                )
            )
            pending = None
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                queue = await reviewer.get("/api/v1/approvals/pending", headers=approver)
                matches = [
                    item for item in queue.json()["approvals"] if item["action"] == LIFECYCLE_ACTION
                ]
                if matches:
                    pending = matches[0]
                    break
                await asyncio.sleep(0.001)
            assert pending is not None
            response = await asyncio.wait_for(submitted, timeout=3)
            assert response.status_code == 200, response.text
            assert response.json() == {
                "status": "pending",
                "request_id": pending["request_id"],
            }
            engine = sa.create_engine(harness.audit_url)
            try:
                with engine.begin() as conn:
                    conn.execute(
                        sa.text(
                            "UPDATE ne_inference_target_state SET version = version + 1 "
                            "WHERE target_id = 'cpu-vllm'"
                        )
                    )
            finally:
                engine.dispose()
            approved = await reviewer.post(
                f"/api/v1/approvals/{pending['request_id']}/approve",
                json={},
                headers=approver,
            )
            assert approved.status_code == 200
            deadline = asyncio.get_running_loop().time() + 3
            error_class = None
            while asyncio.get_running_loop().time() < deadline:
                engine = sa.create_engine(harness.audit_url)
                try:
                    with engine.connect() as conn:
                        payload = conn.execute(
                            sa.text(
                                "SELECT payload FROM ne_audit_log "
                                "WHERE action = 'tool.execute' ORDER BY seq DESC LIMIT 1"
                            )
                        ).scalar_one_or_none()
                finally:
                    engine.dispose()
                if payload is not None:
                    error_class = json.loads(json.loads(payload)["detail"])["error_class"]
                    break
                await asyncio.sleep(0.001)
            assert error_class == "LifecycleConflict"
            row = lifecycle.state("cpu-vllm")
            assert row.version == 2
            assert row.state is InferenceTargetState.STOPPED
            assert operator.start_count == 0

    run(flow())


def test_post_approval_operator_failure_is_durable_and_exact_retry_maps_to_safe_503(
    make_app: AppBuilder,
) -> None:
    operator = FailedOperator()
    harness = make_app(
        ops_roles=("ops",),
        approver_roles=("approver",),
        approvals=ApprovalSettings(poll_interval_s=0.01),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
        inference_operator=operator,
        gateway_models=(managed_model(),),
    )

    async def flow() -> None:
        lifecycle = get_state(harness.app).inference_lifecycle
        assert lifecycle is not None
        await lifecycle.reconcile_due()
        ops = harness.bearer("user:ops", groups=("ops",))
        approver = harness.bearer("user:approver", groups=("approver",))
        async with harness.client() as writer, harness.client() as reviewer:
            transition = {
                "desired_state": "ready",
                "expected_version": 1,
                "idempotency_key": "failed-approved-start",
            }
            submitted = asyncio.create_task(
                writer.post(
                    "/api/v1/models/lifecycle/cpu-vllm/transition",
                    headers=ops,
                    json=transition,
                )
            )
            pending = None
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                queue = await reviewer.get("/api/v1/approvals/pending", headers=approver)
                matches = [
                    item for item in queue.json()["approvals"] if item["action"] == LIFECYCLE_ACTION
                ]
                if matches:
                    pending = matches[0]
                    break
                await asyncio.sleep(0.001)
            assert pending is not None
            response = await submitted
            assert response.status_code == 200, response.text
            assert response.json() == {
                "status": "pending",
                "request_id": pending["request_id"],
            }
            approved = await reviewer.post(
                f"/api/v1/approvals/{pending['request_id']}/approve",
                json={},
                headers=approver,
            )
            assert approved.status_code == 200

            failed = None
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                row = lifecycle.state("cpu-vllm")
                if (
                    row.state is InferenceTargetState.ERROR
                    and row.code is InferenceOperatorCode.TRANSPORT_UNAVAILABLE
                    and operator.start_count == 1
                ):
                    failed = row
                    break
                await asyncio.sleep(0.001)
            assert failed is not None
            assert operator.start_count == 1

            retry = await writer.post(
                "/api/v1/models/lifecycle/cpu-vllm/transition",
                headers=ops,
                json=transition,
            )
            assert retry.status_code == 503, retry.text
            assert retry.json() == {"detail": "the prior idempotent transition failed"}
            assert "private operator detail must not cross the seam" not in retry.text
            assert operator.start_count == 1

    run(flow())


def test_routing_keeps_route_but_marks_lifecycle_stopped_alias_unavailable(
    make_app: AppBuilder,
) -> None:
    model = ModelDef(
        name="private-default",
        litellm_model="hosted_vllm/qwen",
        provider="hosted_vllm",
        lane=Lane.PRIVATE_VLLM,
        api_base="http://vllm:8000/v1",
        context_window=32768,
    )
    harness = make_app(
        ops_roles=("ops",),
        gateway_models=(model,),
        gateway_tasks={"agent": TaskRoute(model=model.name)},
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
    )

    async def flow() -> None:
        async with harness.client() as client:
            response = await client.get(
                "/api/v1/models/routing",
                headers=harness.bearer("user:ops", groups=("ops",)),
            )
            assert response.status_code == 200, response.text
            row = response.json()["tasks"][0]
            assert row["model"] == model.name
            assert row["available"] is False
            assert row["unavailable_reason"] == "lifecycle-unknown"
            assert row["targets"][0]["selectable"] is False
            assert row["targets"][0]["reason"].startswith("task 'agent' already routes")

    run(flow())


def test_omitted_model_refuses_when_managed_deployment_default_is_stopped(
    make_app: AppBuilder,
) -> None:
    model = managed_model()
    harness = make_app(
        gateway_models=(model,),
        gateway_tasks={"agent": TaskRoute(model=model.name)},
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
    )
    headers = harness.bearer("user:ops", department="engineering")

    async def flow() -> None:
        async with harness.client() as client:
            session = await client.post(
                "/api/v1/chat/sessions",
                headers=headers,
                json={"agent_name": "console-chat"},
            )
            assert session.status_code == 200, session.text
            response = await client.post(
                f"/api/v1/chat/sessions/{session.json()['session_id']}/messages",
                headers=headers,
                json={"prompt": "must not attempt the stopped default"},
            )
            assert response.status_code == 409, response.text
            assert response.json() == {"detail": "no model is currently available"}
            assert harness.gateway.requests == []

    run(flow())


def test_omitted_managed_primary_never_silently_selects_available_fallback(
    make_app: AppBuilder,
) -> None:
    primary = managed_model()
    fallback = ModelDef(
        name="private-fallback",
        litellm_model="hosted_vllm/fallback",
        provider="hosted_vllm",
        lane=Lane.PRIVATE_VLLM,
        api_base="http://fallback:8000/v1",
        context_window=32768,
    )

    class _FallbackProbe:
        async def observe(self, models, *, force_refresh=False):
            del models
            del force_refresh
            return [
                SimpleNamespace(
                    model_id=fallback.name,
                    lane=fallback.lane.value,
                    state="ok",
                    checked_at=datetime.now(UTC),
                    serves_configured_model=True,
                )
            ]

    harness = make_app(
        gateway_models=(primary, fallback),
        gateway_tasks={"agent": TaskRoute(model=primary.name, fallbacks=(fallback.name,))},
        serving_probe=_FallbackProbe(),
        inference_lifecycle=InferenceLifecycleConfig(targets=(target(),)),
    )
    headers = harness.bearer("user:ops", department="engineering")

    async def flow() -> None:
        async with harness.client() as client:
            session = await client.post(
                "/api/v1/chat/sessions",
                headers=headers,
                json={"agent_name": "console-chat"},
            )
            response = await client.post(
                f"/api/v1/chat/sessions/{session.json()['session_id']}/messages",
                headers=headers,
                json={"prompt": "must not silently use the fallback"},
            )
            assert response.status_code == 409, response.text
            assert response.json() == {"detail": "no model is currently available"}
            assert harness.gateway.requests == []

    run(flow())
