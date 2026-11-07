# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Discriminating backend tests for the ADR-0021 inference lifecycle seam."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from pydantic import BaseModel, ValidationError
from sqlalchemy import event
from starlette.applications import Starlette

from ne.api.app import _lifespan_factory
from ne.api.inference_lifecycle import (
    InferenceLifecycleConfig,
    InferenceLifecycleService,
    InferenceTargetConfig,
    LifecycleConfirmationRefused,
    LifecycleConflict,
    LifecycleUnavailable,
    UdsInferenceTargetOperator,
    ensure_inference_lifecycle_schema,
    inference_target_state,
    inference_transition_idempotency,
)
from ne.audit import ensure_audit_schema
from ne.contracts.inference import (
    InferenceManifestTarget,
    InferenceOperatorCode,
    InferenceOperatorManifest,
    InferenceOperatorObservation,
    InferenceStartRequest,
    InferenceStatusRequest,
    InferenceStopRequest,
    InferenceTargetOperation,
    InferenceTargetState,
)
from ne.runtime import ApprovalAlreadyDecidedError

NOW = datetime(2026, 8, 20, 18, 0, tzinfo=UTC)
MANIFEST_HASH = "a" * 64


def target(*, provider_control: bool = False) -> InferenceTargetConfig:
    return InferenceTargetConfig(
        target_id="cpu-vllm",
        display_name="CPU vLLM",
        adapter_id="compose-v1",
        aliases=("private-default",),
        capabilities=(
            InferenceTargetOperation.STATUS,
            InferenceTargetOperation.START,
            InferenceTargetOperation.STOP,
        ),
        resource_id="compose:vllm-cpu",
        provider_control=provider_control,
        cooldown_s=0,
    )


class FakeOperator:
    def __init__(self, *, manifest_target: InferenceManifestTarget | None = None) -> None:
        self.target = manifest_target or InferenceManifestTarget(
            target_id="cpu-vllm",
            adapter_id="compose-v1",
            resource_id="compose:vllm-cpu",
            capabilities=(
                InferenceTargetOperation.STATUS,
                InferenceTargetOperation.START,
                InferenceTargetOperation.STOP,
            ),
        )
        self.describe_count = 0
        self.start_count = 0
        self.stop_count = 0
        self.status_count = 0
        self.state = InferenceTargetState.STOPPED

    async def describe(self) -> InferenceOperatorManifest:
        self.describe_count += 1
        return InferenceOperatorManifest(manifest_hash=MANIFEST_HASH, targets=(self.target,))

    def _observation(
        self,
        operation: InferenceTargetOperation,
        transition_id: str | None,
    ) -> InferenceOperatorObservation:
        return InferenceOperatorObservation(
            target_id=self.target.target_id,
            adapter_id=self.target.adapter_id,
            resource_id=self.target.resource_id,
            operation=operation,
            transition_id=transition_id,
            state=self.state,
            code=InferenceOperatorCode.ALREADY_PROVEN,
            manifest_hash=MANIFEST_HASH,
            observed_at=NOW,
        )

    async def status(self, request: InferenceStatusRequest) -> InferenceOperatorObservation:
        self.status_count += 1
        return self._observation(InferenceTargetOperation.STATUS, request.transition_id)

    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation:
        self.start_count += 1
        self.state = InferenceTargetState.READY
        return self._observation(InferenceTargetOperation.START, request.transition_id)

    async def stop(self, request: InferenceStopRequest) -> InferenceOperatorObservation:
        self.stop_count += 1
        self.state = InferenceTargetState.STOPPED
        return self._observation(InferenceTargetOperation.STOP, request.transition_id)


class BarrierOperator(FakeOperator):
    """Release two concurrent handshakes together so only durable CAS can win."""

    def __init__(self) -> None:
        super().__init__()
        self._both = asyncio.Event()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def describe(self) -> InferenceOperatorManifest:
        self.describe_count += 1
        if self.describe_count == 2:
            self._both.set()
        await self._both.wait()
        return InferenceOperatorManifest(manifest_hash=MANIFEST_HASH, targets=(self.target,))

    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation:
        self.start_count += 1
        self.started.set()
        await self.release.wait()
        self.state = InferenceTargetState.READY
        return self._observation(InferenceTargetOperation.START, request.transition_id)


class PausedHandshakeOperator(FakeOperator):
    def __init__(self) -> None:
        super().__init__()
        self.describe_called = asyncio.Event()
        self.release_describe = asyncio.Event()

    async def describe(self) -> InferenceOperatorManifest:
        self.describe_count += 1
        self.describe_called.set()
        await self.release_describe.wait()
        return InferenceOperatorManifest(manifest_hash=MANIFEST_HASH, targets=(self.target,))


class WrongObservationOperator(FakeOperator):
    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation:
        self.start_count += 1
        return InferenceOperatorObservation(
            target_id=self.target.target_id,
            adapter_id="wrong-adapter",
            resource_id=self.target.resource_id,
            operation=InferenceTargetOperation.START,
            transition_id=request.transition_id,
            state=InferenceTargetState.READY,
            code=InferenceOperatorCode.ALREADY_PROVEN,
            manifest_hash=MANIFEST_HASH,
            observed_at=NOW,
        )


class BlockingOperator(FakeOperator):
    def __init__(self) -> None:
        super().__init__()
        self.called = asyncio.Event()
        self.block = asyncio.Event()

    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation:
        self.start_count += 1
        self.called.set()
        await self.block.wait()
        return self._observation(InferenceTargetOperation.START, request.transition_id)


class FailedOperator(FakeOperator):
    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation:
        self.start_count += 1
        raise RuntimeError("private operator detail must not cross the seam")


class ToggleStatusFailureOperator(FakeOperator):
    def __init__(self) -> None:
        super().__init__()
        self.fail_status = False

    async def status(self, request: InferenceStatusRequest) -> InferenceOperatorObservation:
        if self.fail_status:
            self.status_count += 1
            raise RuntimeError("private status failure")
        return await super().status(request)


class ContradictoryOperator(FakeOperator):
    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation:
        self.start_count += 1
        return InferenceOperatorObservation(
            target_id=self.target.target_id,
            adapter_id=self.target.adapter_id,
            resource_id=self.target.resource_id,
            operation=InferenceTargetOperation.START,
            transition_id=request.transition_id,
            state=InferenceTargetState.STOPPED,
            code=InferenceOperatorCode.ALREADY_PROVEN,
            manifest_hash=MANIFEST_HASH,
            observed_at=NOW,
        )


class BlockingStatusOperator(FakeOperator):
    def __init__(self) -> None:
        super().__init__()
        self.status_called = asyncio.Event()
        self.release_status = asyncio.Event()

    async def status(self, request: InferenceStatusRequest) -> InferenceOperatorObservation:
        self.status_count += 1
        # Capture the STATUS result before the concurrent mutation. Returning a
        # freshly assembled observation after release would make the test unable
        # to distinguish a stale overwrite from a correct CAS refusal.
        observation = self._observation(InferenceTargetOperation.STATUS, request.transition_id)
        self.status_called.set()
        await self.release_status.wait()
        return observation


class AcceptedThenStatusOperator(FakeOperator):
    def __init__(self, *, interrupted: bool = False, observed_at: datetime = NOW) -> None:
        super().__init__()
        self.interrupted = interrupted
        self.observed_at = observed_at

    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation:
        self.start_count += 1
        return InferenceOperatorObservation(
            target_id=self.target.target_id,
            adapter_id=self.target.adapter_id,
            resource_id=self.target.resource_id,
            operation=InferenceTargetOperation.START,
            transition_id=request.transition_id,
            state=InferenceTargetState.STARTING,
            code=InferenceOperatorCode.ACCEPTED,
            manifest_hash=MANIFEST_HASH,
            observed_at=self.observed_at,
        )

    async def status(self, request: InferenceStatusRequest) -> InferenceOperatorObservation:
        self.status_count += 1
        state = (
            InferenceTargetState.STOPPED
            if self.start_count == 0
            else (InferenceTargetState.ERROR if self.interrupted else InferenceTargetState.READY)
        )
        code = (
            InferenceOperatorCode.INTERRUPTED
            if self.start_count > 0 and self.interrupted
            else InferenceOperatorCode.OBSERVED
        )
        return InferenceOperatorObservation(
            target_id=self.target.target_id,
            adapter_id=self.target.adapter_id,
            resource_id=self.target.resource_id,
            operation=InferenceTargetOperation.STATUS,
            transition_id=request.transition_id,
            state=state,
            code=code,
            manifest_hash=MANIFEST_HASH,
            observed_at=self.observed_at,
        )


class AcceptedThenFailingStatusOperator(AcceptedThenStatusOperator):
    async def status(self, request: InferenceStatusRequest) -> InferenceOperatorObservation:
        self.status_count += 1
        raise RuntimeError("private status failure")


class ConcurrentTerminalStatusOperator(AcceptedThenStatusOperator):
    def __init__(self) -> None:
        super().__init__()
        self.both_status_calls = asyncio.Event()
        self.release_status = asyncio.Event()

    async def status(self, request: InferenceStatusRequest) -> InferenceOperatorObservation:
        self.status_count += 1
        if self.status_count == 2:
            self.both_status_calls.set()
        await self.both_status_calls.wait()
        await self.release_status.wait()
        return InferenceOperatorObservation(
            target_id=self.target.target_id,
            adapter_id=self.target.adapter_id,
            resource_id=self.target.resource_id,
            operation=InferenceTargetOperation.STATUS,
            transition_id=request.transition_id,
            state=InferenceTargetState.READY,
            code=InferenceOperatorCode.OBSERVED,
            manifest_hash=MANIFEST_HASH,
            observed_at=NOW,
        )


class SlowTerminalStartOperator(FakeOperator):
    def __init__(self) -> None:
        super().__init__()
        self.start_called = asyncio.Event()
        self.release_start = asyncio.Event()

    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation:
        self.start_count += 1
        self.start_called.set()
        await self.release_start.wait()
        self.state = InferenceTargetState.READY
        return self._observation(InferenceTargetOperation.START, request.transition_id)


class PartialStatusFailureOperator:
    def __init__(self, targets: tuple[InferenceManifestTarget, ...]) -> None:
        self.targets = {item.target_id: item for item in targets}
        self.status_calls: list[str] = []

    async def describe(self) -> InferenceOperatorManifest:
        return InferenceOperatorManifest(
            manifest_hash=MANIFEST_HASH,
            targets=tuple(self.targets.values()),
        )

    async def status(self, request: InferenceStatusRequest) -> InferenceOperatorObservation:
        self.status_calls.append(request.target_id)
        if request.target_id == "cpu-vllm":
            raise RuntimeError("target-local status failure")
        item = self.targets[request.target_id]
        return InferenceOperatorObservation(
            target_id=item.target_id,
            adapter_id=item.adapter_id,
            resource_id=item.resource_id,
            operation=InferenceTargetOperation.STATUS,
            transition_id=request.transition_id,
            state=InferenceTargetState.READY,
            code=InferenceOperatorCode.OBSERVED,
            manifest_hash=MANIFEST_HASH,
            observed_at=NOW,
        )

    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation:
        raise AssertionError("reconciliation must not start a target")

    async def stop(self, request: InferenceStopRequest) -> InferenceOperatorObservation:
        raise AssertionError("reconciliation must not stop a target")


class RecoveryApprovalGate:
    def __init__(self) -> None:
        self.status = "pending"
        self.rejections: list[tuple[str, str, str]] = []

    async def reject(self, request_id: str, *, approver: str, reason: str):
        if self.status != "pending":
            raise ApprovalAlreadyDecidedError(request_id)
        self.status = "rejected"
        self.rejections.append((request_id, approver, reason))
        return object()

    async def approve(self, request_id: str) -> None:
        if self.status != "pending":
            raise ApprovalAlreadyDecidedError(request_id)
        self.status = "approved"


def service(
    tmp_path,
    *,
    operator: FakeOperator | None,
    provider_control: bool = False,
    air_gap: bool = False,
) -> InferenceLifecycleService:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    ensure_audit_schema(engine)
    return InferenceLifecycleService(
        engine=engine,
        config=InferenceLifecycleConfig(targets=(target(provider_control=provider_control),)),
        operator=operator,
        air_gap_provider=lambda: air_gap,
        config_hash="b" * 64,
        clock=lambda: NOW,
    )


def shared_service(
    engine: sa.Engine,
    *,
    operator: FakeOperator,
    config: InferenceLifecycleConfig | None = None,
    clock: Callable[[], datetime] = lambda: NOW,
) -> InferenceLifecycleService:
    ensure_audit_schema(engine)
    return InferenceLifecycleService(
        engine=engine,
        config=config or InferenceLifecycleConfig(targets=(target(),)),
        operator=operator,
        air_gap_provider=lambda: False,
        config_hash="b" * 64,
        clock=clock,
    )


def test_registry_rejects_executable_fields_and_alias_collisions() -> None:
    with pytest.raises(ValidationError):
        InferenceTargetConfig.model_validate(
            {**target().model_dump(), "command": "docker compose up"}
        )
    with pytest.raises(ValidationError, match="aliases must be unique"):
        InferenceLifecycleConfig(
            targets=(
                target(),
                target().model_copy(
                    update={"target_id": "gpu-vllm", "resource_id": "provider:gpu"}
                ),
            )
        )
    with pytest.raises(ValueError, match="absolute"):
        UdsInferenceTargetOperator("relative.sock")


@pytest.mark.anyio
async def test_concurrent_replica_boot_converges_on_one_target_seed(tmp_path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'concurrent-seed.db'}")
    ensure_audit_schema(engine)
    ensure_inference_lifecycle_schema(engine)
    barrier = threading.Barrier(2)
    insert_count = 0
    count_lock = threading.Lock()

    def hold_first_two_inserts(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        nonlocal insert_count
        if not statement.startswith("INSERT INTO ne_inference_target_state"):
            return
        with count_lock:
            insert_count += 1
            should_wait = insert_count <= 2
        if should_wait:
            barrier.wait(timeout=5)

    event.listen(engine, "before_cursor_execute", hold_first_two_inserts)
    try:
        results = await asyncio.gather(
            asyncio.to_thread(shared_service, engine, operator=FakeOperator()),
            asyncio.to_thread(shared_service, engine, operator=FakeOperator()),
            return_exceptions=True,
        )
    finally:
        event.remove(engine, "before_cursor_execute", hold_first_two_inserts)
    assert all(not isinstance(result, BaseException) for result in results)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(inference_target_state).where(
                inference_target_state.c.target_id == "cpu-vllm"
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].version == 0
    assert rows[0].state == InferenceTargetState.UNKNOWN.value


@pytest.mark.anyio
async def test_removed_registry_target_is_preserved_but_not_reconciled_after_restart(
    tmp_path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'retired-target.db'}")
    retired = target().model_copy(
        update={
            "target_id": "retired-vllm",
            "aliases": ("retired-default",),
            "resource_id": "compose:retired-vllm",
        }
    )
    shared_service(
        engine,
        operator=FakeOperator(),
        config=InferenceLifecycleConfig(targets=(target(), retired)),
    )
    operator = FakeOperator()
    restarted = shared_service(
        engine,
        operator=operator,
        config=InferenceLifecycleConfig(targets=(target(),)),
    )

    assert [row.target_id for row in restarted.states()] == ["cpu-vllm"]
    reconciled = await restarted.reconcile_due()
    assert [row.target_id for row in reconciled] == ["cpu-vllm"]
    assert operator.status_count == 1
    with engine.connect() as conn:
        durable_ids = set(conn.execute(sa.select(inference_target_state.c.target_id)).scalars())
    assert durable_ids == {"cpu-vllm", "retired-vllm"}


@pytest.mark.anyio
async def test_future_observation_is_unavailable_and_reconciles_immediately(tmp_path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'future-observation.db'}")
    operator = FakeOperator()
    operator.state = InferenceTargetState.READY
    lifecycle = shared_service(engine, operator=operator)
    future = NOW + timedelta(days=1)
    with engine.begin() as conn:
        conn.execute(
            inference_target_state.update()
            .where(inference_target_state.c.target_id == "cpu-vllm")
            .values(
                state=InferenceTargetState.READY.value,
                desired_state=InferenceTargetState.READY.value,
                manifest_hash=MANIFEST_HASH,
                observed_at=future,
                updated_at=future,
            )
        )

    assert lifecycle.availability("private-default") == (False, "lifecycle-stale")
    reconciled = await lifecycle.reconcile_due()
    assert len(reconciled) == 1
    assert reconciled[0].state is InferenceTargetState.READY
    assert reconciled[0].observed_at == NOW
    assert operator.status_count == 1
    assert lifecycle.availability("private-default") == (True, None)


def test_status_deadline_accepts_host_probe_budgets_but_remains_bounded() -> None:
    # The closed host registry uses 100s for CPU and 120s for GPU because a
    # one-token readiness probe can legitimately take about 90 seconds.
    for deadline in (100, 120, 300):
        configured = InferenceTargetConfig.model_validate(
            {**target().model_dump(), "status_deadline_s": deadline}
        )
        assert configured.status_deadline_s == deadline
    with pytest.raises(ValidationError, match="less than or equal to 300"):
        InferenceTargetConfig.model_validate(
            {**target().model_dump(), "status_deadline_s": 300.001}
        )


@pytest.mark.anyio
async def test_missing_transport_is_read_only_and_start_is_503_class(tmp_path) -> None:
    lifecycle = service(tmp_path, operator=None)
    assert lifecycle.availability("private-default") == (False, "lifecycle-unknown")
    with pytest.raises(LifecycleUnavailable, match="transport"):
        lifecycle.preflight(target_id="cpu-vllm", desired_state="ready", expected_version=0)


@pytest.mark.anyio
async def test_configured_but_absent_uds_is_still_read_only(tmp_path) -> None:
    operator = UdsInferenceTargetOperator(str(tmp_path / "missing.sock"))
    lifecycle = service(tmp_path, operator=operator)  # type: ignore[arg-type]
    assert lifecycle.transport_available is False
    with pytest.raises(LifecycleUnavailable, match="transport"):
        lifecycle.preflight(target_id="cpu-vllm", desired_state="ready", expected_version=0)


@pytest.mark.anyio
async def test_manifest_mismatch_refuses_before_mutation(tmp_path) -> None:
    operator = FakeOperator(
        manifest_target=InferenceManifestTarget(
            target_id="cpu-vllm",
            adapter_id="wrong-adapter",
            resource_id="compose:vllm-cpu",
            capabilities=(InferenceTargetOperation.STATUS,),
        )
    )
    lifecycle = service(tmp_path, operator=operator)
    with pytest.raises(LifecycleUnavailable, match="manifest"):
        await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="start-once",
            confirmation_token=None,
            actor="user:ops",
        )
    assert operator.start_count == 0
    assert lifecycle.state("cpu-vllm").version == 0


@pytest.mark.anyio
async def test_wrong_operator_observation_is_persisted_unknown_and_refused(tmp_path) -> None:
    operator = WrongObservationOperator()
    lifecycle = service(tmp_path, operator=operator)
    with pytest.raises(LifecycleUnavailable, match="observation"):
        await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="start-wrong-observation",
            confirmation_token=None,
            actor="user:ops",
        )
    row = lifecycle.state("cpu-vllm")
    assert row.state is InferenceTargetState.UNKNOWN
    assert row.code is InferenceOperatorCode.MANIFEST_MISMATCH


@pytest.mark.anyio
async def test_cancelled_transition_is_persisted_interrupted_before_propagation(tmp_path) -> None:
    operator = BlockingOperator()
    lifecycle = service(tmp_path, operator=operator)
    task = asyncio.create_task(
        lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="cancelled-start",
            confirmation_token=None,
            actor="user:ops",
        )
    )
    await operator.called.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = lifecycle.state("cpu-vllm")
    assert row.state is InferenceTargetState.ERROR
    assert row.code is InferenceOperatorCode.INTERRUPTED
    assert row.version == 1


@pytest.mark.anyio
async def test_operator_failure_is_persisted_error_and_not_reported_as_success(tmp_path) -> None:
    operator = FailedOperator()
    lifecycle = service(tmp_path, operator=operator)
    with pytest.raises(LifecycleUnavailable, match="did not prove"):
        await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="failed-start",
            confirmation_token=None,
            actor="user:ops",
        )
    row = lifecycle.state("cpu-vllm")
    assert row.state is InferenceTargetState.ERROR
    assert row.code is InferenceOperatorCode.TRANSPORT_UNAVAILABLE
    assert row.version == 1

    with pytest.raises(LifecycleUnavailable, match="prior idempotent transition failed"):
        await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="failed-start",
            confirmation_token=None,
            actor="user:ops",
        )
    assert operator.start_count == 1


@pytest.mark.anyio
async def test_contradictory_success_observation_is_failed_closed(tmp_path) -> None:
    operator = ContradictoryOperator()
    lifecycle = service(tmp_path, operator=operator)
    with pytest.raises(LifecycleUnavailable, match="observation"):
        await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="contradictory-start",
            confirmation_token=None,
            actor="user:ops",
        )
    row = lifecycle.state("cpu-vllm")
    assert row.state is InferenceTargetState.UNKNOWN
    assert row.code is InferenceOperatorCode.MANIFEST_MISMATCH
    assert operator.start_count == 1


@pytest.mark.anyio
async def test_start_and_stop_are_durable_idempotent_and_stop_confirmation_is_bound(
    tmp_path,
) -> None:
    operator = FakeOperator()
    lifecycle = service(tmp_path, operator=operator)
    started = await lifecycle.transition(
        target_id="cpu-vllm",
        desired_state="ready",
        expected_version=0,
        idempotency_key="start-once",
        confirmation_token=None,
        actor="user:ops",
    )
    assert started.state is InferenceTargetState.READY
    assert started.version == 1
    assert operator.start_count == 1

    retried = await lifecycle.transition(
        target_id="cpu-vllm",
        desired_state="ready",
        expected_version=0,
        idempotency_key="start-once",
        confirmation_token=None,
        actor="user:ops",
    )
    assert retried == started
    assert operator.start_count == 1

    wrong_token, _ = lifecycle.issue_stop_confirmation(
        target_id="cpu-vllm", principal_id="user:other", expected_version=1
    )
    with pytest.raises(LifecycleConfirmationRefused):
        await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="stopped",
            expected_version=1,
            idempotency_key="stop-wrong-principal",
            confirmation_token=wrong_token,
            actor="user:ops",
        )
    assert operator.stop_count == 0

    token, _ = lifecycle.issue_stop_confirmation(
        target_id="cpu-vllm", principal_id="user:ops", expected_version=1
    )
    stopped = await lifecycle.transition(
        target_id="cpu-vllm",
        desired_state="stopped",
        expected_version=1,
        idempotency_key="stop-once",
        confirmation_token=token,
        actor="user:ops",
    )
    assert stopped.state is InferenceTargetState.STOPPED
    assert stopped.version == 2
    assert operator.stop_count == 1
    assert (
        await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="stopped",
            expected_version=1,
            idempotency_key="stop-once",
            confirmation_token=token,
            actor="user:ops",
        )
    ) == stopped
    assert operator.stop_count == 1

    historical_start = await lifecycle.transition(
        target_id="cpu-vllm",
        desired_state="ready",
        expected_version=0,
        idempotency_key="start-once",
        confirmation_token=None,
        actor="user:ops",
    )
    assert historical_start.state is InferenceTargetState.READY
    assert historical_start.version == 1
    assert operator.start_count == 1
    with pytest.raises(LifecycleConflict, match="different request"):
        await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="stopped",
            expected_version=2,
            idempotency_key="start-once",
            confirmation_token=token,
            actor="user:ops",
        )
    assert operator.stop_count == 1


@pytest.mark.anyio
async def test_air_gap_refuses_provider_target_before_handshake_or_call(tmp_path) -> None:
    operator = FakeOperator()
    lifecycle = service(tmp_path, operator=operator, provider_control=True, air_gap=True)
    with pytest.raises(LifecycleUnavailable, match="AIR_GAP"):
        lifecycle.preflight(target_id="cpu-vllm", desired_state="ready", expected_version=0)
    assert operator.describe_count == operator.start_count == 0


@pytest.mark.anyio
async def test_air_gap_invalidates_already_proven_provider_terminal_state(tmp_path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'airgap-proven.db'}")
    operator = FakeOperator()
    operator.state = InferenceTargetState.READY
    air_gap = [False]
    lifecycle = InferenceLifecycleService(
        engine=engine,
        config=InferenceLifecycleConfig(targets=(target(provider_control=True),)),
        operator=operator,
        air_gap_provider=lambda: air_gap[0],
        config_hash="b" * 64,
        clock=lambda: NOW,
    )
    ensure_audit_schema(engine)
    ready = await lifecycle.reconcile("cpu-vllm")
    assert (
        lifecycle.already_proven_state(
            target_id="cpu-vllm", desired_state="ready", actor="user:ops"
        )
        == ready
    )

    air_gap[0] = True
    with pytest.raises(LifecycleUnavailable, match="not currently proven"):
        lifecycle.already_proven_state(
            target_id="cpu-vllm", desired_state="ready", actor="user:ops"
        )
    assert operator.start_count == operator.stop_count == 0


@pytest.mark.anyio
async def test_concurrent_different_transitions_have_exactly_one_cas_winner(tmp_path) -> None:
    operator = BarrierOperator()
    lifecycle = service(tmp_path, operator=operator)

    async def start(key: str):
        return await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key=key,
            confirmation_token=None,
            actor=f"user:{key}",
        )

    first = asyncio.create_task(start("first"))
    second = asyncio.create_task(start("second"))
    await operator.started.wait()
    await asyncio.sleep(0)
    operator.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert sum(isinstance(result, LifecycleConflict) for result in results) == 1, results
    assert operator.start_count == 1
    assert lifecycle.state("cpu-vllm").version == 1


@pytest.mark.anyio
async def test_reconcile_is_status_only_and_updates_availability(tmp_path) -> None:
    operator = FakeOperator()
    operator.state = InferenceTargetState.READY
    lifecycle = service(tmp_path, operator=operator)
    row = await lifecycle.reconcile("cpu-vllm")
    assert row.state is InferenceTargetState.READY
    assert row.version == 1
    assert lifecycle.availability("private-default") == (True, None)
    assert operator.status_count == 1
    assert operator.start_count == operator.stop_count == 0


@pytest.mark.anyio
async def test_reconcile_state_change_invalidates_version_bound_confirmation(tmp_path) -> None:
    operator = FakeOperator()
    lifecycle = service(tmp_path, operator=operator)
    token, _ = lifecycle.issue_stop_confirmation(
        target_id="cpu-vllm", principal_id="user:ops", expected_version=0
    )
    operator.state = InferenceTargetState.READY
    row = await lifecycle.reconcile("cpu-vllm")
    assert row.version == 1
    with pytest.raises(LifecycleConfirmationRefused):
        lifecycle.validate_stop_confirmation(
            token=token,
            target_id="cpu-vllm",
            principal_id="user:ops",
            expected_version=1,
        )


@pytest.mark.anyio
async def test_status_failure_advances_version_and_rejects_stale_stop_confirmation(
    tmp_path,
) -> None:
    operator = ToggleStatusFailureOperator()
    operator.state = InferenceTargetState.READY
    lifecycle = service(tmp_path, operator=operator)
    ready = await lifecycle.reconcile("cpu-vllm")
    token, _ = lifecycle.issue_stop_confirmation(
        target_id="cpu-vllm",
        principal_id="user:ops",
        expected_version=ready.version,
    )
    operator.fail_status = True
    with pytest.raises(LifecycleUnavailable, match="status is unavailable"):
        await lifecycle.reconcile("cpu-vllm")
    failed = lifecycle.state("cpu-vllm")
    assert failed.state is InferenceTargetState.UNKNOWN
    assert failed.code is InferenceOperatorCode.TRANSPORT_UNAVAILABLE
    assert failed.version == ready.version + 1
    with pytest.raises(LifecycleConflict, match="version changed"):
        await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="stopped",
            expected_version=ready.version,
            idempotency_key="stale-stop-after-status-failure",
            confirmation_token=token,
            actor="user:ops",
        )
    assert operator.stop_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_reconcile_updates_exact_idempotency_result_after_accepted_transition(
    tmp_path, interrupted: bool
) -> None:
    operator = AcceptedThenStatusOperator(interrupted=interrupted)
    lifecycle = service(tmp_path, operator=operator)
    accepted = await lifecycle.transition(
        target_id="cpu-vllm",
        desired_state="ready",
        expected_version=0,
        idempotency_key="accepted-start",
        confirmation_token=None,
        actor="user:ops",
    )
    assert accepted.state is InferenceTargetState.STARTING
    assert accepted.code is InferenceOperatorCode.ACCEPTED

    terminal = await lifecycle.reconcile("cpu-vllm")
    if interrupted:
        assert terminal.state is InferenceTargetState.ERROR
        assert terminal.code is InferenceOperatorCode.INTERRUPTED
        with pytest.raises(LifecycleUnavailable, match="prior idempotent transition failed"):
            await lifecycle.transition(
                target_id="cpu-vllm",
                desired_state="ready",
                expected_version=0,
                idempotency_key="accepted-start",
                confirmation_token=None,
                actor="user:ops",
            )
    else:
        retried = await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="accepted-start",
            confirmation_token=None,
            actor="user:ops",
        )
        assert terminal.state is InferenceTargetState.READY
        assert retried == terminal
    assert terminal.version == 2
    assert operator.start_count == 1


@pytest.mark.anyio
async def test_accepted_status_failure_finalizes_idempotency_and_failed_audit(
    tmp_path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'accepted-status-failure.db'}")
    operator = AcceptedThenFailingStatusOperator()
    lifecycle = shared_service(engine, operator=operator)
    accepted = await lifecycle.transition(
        target_id="cpu-vllm",
        desired_state="ready",
        expected_version=0,
        idempotency_key="accepted-status-failure",
        confirmation_token=None,
        actor="user:ops",
    )
    assert accepted.state is InferenceTargetState.STARTING

    with pytest.raises(LifecycleUnavailable, match="status is unavailable"):
        await lifecycle.reconcile("cpu-vllm")
    failed = lifecycle.state("cpu-vllm")
    assert failed.state is InferenceTargetState.UNKNOWN
    assert failed.code is InferenceOperatorCode.TRANSPORT_UNAVAILABLE
    with pytest.raises(LifecycleUnavailable, match="prior idempotent transition failed"):
        await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="accepted-status-failure",
            confirmation_token=None,
            actor="user:ops",
        )
    with engine.connect() as conn:
        actions = list(
            conn.execute(
                sa.text(
                    "SELECT action FROM ne_audit_log WHERE action LIKE "
                    "'inference.lifecycle.%' ORDER BY seq"
                )
            ).scalars()
        )
    assert actions == ["inference.lifecycle.started", "inference.lifecycle.failed"]


@pytest.mark.anyio
async def test_reconcile_does_not_invalidate_active_host_transition(tmp_path) -> None:
    operator = SlowTerminalStartOperator()
    lifecycle = service(tmp_path, operator=operator)
    transition = asyncio.create_task(
        lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="slow-start",
            confirmation_token=None,
            actor="user:ops",
        )
    )
    await operator.start_called.wait()
    in_flight = await lifecycle.reconcile("cpu-vllm")
    assert in_flight.state is InferenceTargetState.STARTING
    assert in_flight.version == 1
    assert operator.status_count == 0
    operator.release_start.set()
    terminal = await transition
    assert terminal.state is InferenceTargetState.READY
    assert terminal.version == 1
    assert operator.start_count == 1
    assert (
        await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="slow-start",
            confirmation_token=None,
            actor="user:ops",
        )
    ) == terminal


@pytest.mark.anyio
async def test_durable_operation_lease_blocks_cross_replica_reconcile(tmp_path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'shared.db'}")
    operator = SlowTerminalStartOperator()
    owner = shared_service(engine, operator=operator)
    other_replica = shared_service(engine, operator=operator)
    transition = asyncio.create_task(
        owner.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="cross-replica-start",
            confirmation_token=None,
            actor="user:ops",
        )
    )
    await operator.start_called.wait()

    in_flight = await other_replica.reconcile("cpu-vllm")
    assert in_flight.state is InferenceTargetState.STARTING
    assert in_flight.code is InferenceOperatorCode.ACCEPTED
    assert in_flight.version == 1
    assert operator.status_count == 0

    operator.release_start.set()
    terminal = await transition
    assert terminal.state is InferenceTargetState.READY
    assert terminal.version == 1
    assert other_replica.state("cpu-vllm") == terminal
    assert operator.start_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("identical", [True, False])
async def test_pending_reservation_unique_race_converges_or_conflicts(
    tmp_path, identical: bool
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'reservations.db'}")
    first = shared_service(engine, operator=FakeOperator())
    second = shared_service(engine, operator=FakeOperator())
    barrier = threading.Barrier(2)
    insert_count = 0
    count_lock = threading.Lock()

    def hold_first_two_inserts(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        nonlocal insert_count
        if not statement.startswith("INSERT INTO ne_inference_pending_changes"):
            return
        with count_lock:
            insert_count += 1
            should_wait = insert_count <= 2
        if should_wait:
            barrier.wait(timeout=5)

    event.listen(engine, "before_cursor_execute", hold_first_two_inserts)
    base = {
        "target_id": "cpu-vllm",
        "requested_by": "user:ops",
        "desired_state": "ready",
        "expected_version": 0,
        "idempotency_key": "same" if identical else "first",
        "request_hash": "1" * 64,
    }
    other = dict(base)
    if not identical:
        other.update(idempotency_key="second", request_hash="2" * 64)
    try:
        results = await asyncio.gather(
            asyncio.to_thread(first.reserve_pending_change, **base),
            asyncio.to_thread(second.reserve_pending_change, **other),
            return_exceptions=True,
        )
    finally:
        event.remove(engine, "before_cursor_execute", hold_first_two_inserts)

    if identical:
        assert all(not isinstance(result, BaseException) for result in results)
        admitted = [result for result in results if not isinstance(result, BaseException)]
        assert admitted[0][0] == admitted[1][0]
        assert sorted(result[2] for result in admitted) == [False, True]
    else:
        assert sum(isinstance(result, LifecycleConflict) for result in results) == 1
        admitted = [result for result in results if not isinstance(result, BaseException)]
        assert len(admitted) == 1
        assert admitted[0][2] is True


def test_unattached_pending_reservation_expires_and_is_reclaimed_after_restart(tmp_path) -> None:
    current = [NOW]
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'reservation-restart.db'}")
    config = InferenceLifecycleConfig(targets=(target(),), pending_reservation_ttl_s=30)
    first = shared_service(engine, operator=FakeOperator(), config=config, clock=lambda: current[0])
    reservation, request_id, owns = first.reserve_pending_change(
        target_id="cpu-vllm",
        requested_by="user:ops",
        desired_state="ready",
        expected_version=0,
        idempotency_key="crashed",
        request_hash="1" * 64,
    )
    assert request_id is None and owns is True

    restarted = shared_service(
        engine, operator=FakeOperator(), config=config, clock=lambda: current[0]
    )
    joined = restarted.reserve_pending_change(
        target_id="cpu-vllm",
        requested_by="user:ops",
        desired_state="ready",
        expected_version=0,
        idempotency_key="crashed",
        request_hash="1" * 64,
    )
    assert joined == (reservation, None, False)

    current[0] += timedelta(seconds=20)
    first.renew_owned_pending_reservations()
    current[0] += timedelta(seconds=11)
    recovered = restarted.reserve_pending_change(
        target_id="cpu-vllm",
        requested_by="user:ops",
        desired_state="ready",
        expected_version=0,
        idempotency_key="crashed",
        request_hash="1" * 64,
    )
    assert recovered[0] != reservation
    assert recovered[1:] == (None, True)

    current[0] += timedelta(seconds=31)
    replacement = first.reserve_pending_change(
        target_id="cpu-vllm",
        requested_by="user:ops",
        desired_state="ready",
        expected_version=0,
        idempotency_key="replacement",
        request_hash="2" * 64,
    )
    assert replacement[0] != recovered[0]
    assert replacement[1:] == (None, True)


@pytest.mark.anyio
@pytest.mark.parametrize("approved_after_restart", [False, True])
async def test_expired_attached_approval_is_terminal_without_host_replay(
    tmp_path, approved_after_restart: bool
) -> None:
    current = [NOW]
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'attached-restart.db'}")
    config = InferenceLifecycleConfig(targets=(target(),), pending_reservation_ttl_s=30)
    operator = FakeOperator()
    owner = shared_service(engine, operator=operator, config=config, clock=lambda: current[0])
    reservation, _, owns = owner.reserve_pending_change(
        target_id="cpu-vllm",
        requested_by="user:ops",
        desired_state="ready",
        expected_version=0,
        idempotency_key="parked-before-crash",
        request_hash="1" * 64,
    )
    assert owns is True
    owner.attach_pending_request(reservation_id=reservation, request_id="approval-1")

    current[0] += timedelta(seconds=31)
    restarted = shared_service(engine, operator=operator, config=config, clock=lambda: current[0])
    gate = RecoveryApprovalGate()
    if approved_after_restart:
        await gate.approve("approval-1")
    await restarted.recover_expired_pending_approvals(gate)

    if approved_after_restart:
        assert gate.status == "approved"
        assert gate.rejections == []
    else:
        assert gate.status == "rejected"
        assert gate.rejections == [
            (
                "approval-1",
                "system:inference-lifecycle",
                "owning API replica stopped before lifecycle execution",
            )
        ]
    with pytest.raises(ApprovalAlreadyDecidedError):
        await gate.approve("approval-1")
    assert restarted.pending_reservation_request(reservation) is None
    state = restarted.state("cpu-vllm")
    assert state.state is InferenceTargetState.ERROR
    assert state.code is InferenceOperatorCode.INTERRUPTED
    assert operator.start_count == 0
    assert operator.stop_count == 0
    with engine.connect() as conn:
        action = conn.execute(
            sa.text(
                "SELECT action FROM ne_audit_log WHERE action IN "
                "('inference.lifecycle.refused', 'inference.lifecycle.failed')"
            )
        ).scalar_one()
    assert action == (
        "inference.lifecycle.failed" if approved_after_restart else "inference.lifecycle.refused"
    )


@pytest.mark.anyio
async def test_expired_reservation_after_terminal_transition_clears_without_poisoning(
    tmp_path,
) -> None:
    current = [NOW]
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'terminal-cleanup-crash.db'}")
    config = InferenceLifecycleConfig(targets=(target(),), pending_reservation_ttl_s=30)
    operator = FakeOperator()
    owner = shared_service(engine, operator=operator, config=config, clock=lambda: current[0])
    reservation, _, owns = owner.reserve_pending_change(
        target_id="cpu-vllm",
        requested_by="user:ops",
        desired_state="ready",
        expected_version=0,
        idempotency_key="completed-before-cleanup",
        request_hash="1" * 64,
    )
    assert owns is True
    owner.attach_pending_request(reservation_id=reservation, request_id="approval-1")
    gate = RecoveryApprovalGate()
    await gate.approve("approval-1")
    completed = await owner.transition(
        target_id="cpu-vllm",
        desired_state="ready",
        expected_version=0,
        idempotency_key="completed-before-cleanup",
        confirmation_token=None,
        actor="user:ops",
    )
    assert completed.state is InferenceTargetState.READY
    assert operator.start_count == 1

    current[0] += timedelta(seconds=31)
    restarted = shared_service(engine, operator=operator, config=config, clock=lambda: current[0])
    await restarted.recover_expired_pending_approvals(gate)
    assert restarted.state("cpu-vllm") == completed
    assert restarted.pending_reservation_request(reservation) is None
    assert gate.status == "approved"
    assert operator.start_count == 1
    with engine.connect() as conn:
        actions = list(
            conn.execute(
                sa.text(
                    "SELECT action FROM ne_audit_log WHERE action LIKE "
                    "'inference.lifecycle.%' ORDER BY seq"
                )
            ).scalars()
        )
    assert actions == ["inference.lifecycle.started", "inference.lifecycle.completed"]


@pytest.mark.anyio
async def test_expired_approved_owner_is_fenced_before_stale_executor_can_call_host(
    tmp_path,
) -> None:
    current = [NOW]
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'approved-owner-fence.db'}")
    config = InferenceLifecycleConfig(targets=(target(),), pending_reservation_ttl_s=30)
    operator = PausedHandshakeOperator()
    owner = shared_service(engine, operator=operator, config=config, clock=lambda: current[0])
    reservation, _, owns = owner.reserve_pending_change(
        target_id="cpu-vllm",
        requested_by="user:ops",
        desired_state="ready",
        expected_version=0,
        idempotency_key="approved-stale-owner",
        request_hash="1" * 64,
    )
    assert owns is True
    owner.attach_pending_request(reservation_id=reservation, request_id="approval-1")
    stale_executor = asyncio.create_task(
        owner.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="approved-stale-owner",
            confirmation_token=None,
            actor="user:ops",
        )
    )
    await operator.describe_called.wait()

    gate = RecoveryApprovalGate()
    await gate.approve("approval-1")
    current[0] += timedelta(seconds=31)
    restarted = shared_service(engine, operator=operator, config=config, clock=lambda: current[0])
    await restarted.recover_expired_pending_approvals(gate)
    fenced = restarted.state("cpu-vllm")
    assert fenced.state is InferenceTargetState.ERROR
    assert fenced.code is InferenceOperatorCode.INTERRUPTED
    assert fenced.version == 1

    operator.release_describe.set()
    with pytest.raises(LifecycleConflict):
        await stale_executor
    assert operator.start_count == 0
    assert restarted.pending_reservation_request(reservation) is None


@pytest.mark.anyio
async def test_crash_before_host_call_reconciles_opposite_terminal_as_failed(tmp_path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'before-host-crash.db'}")
    operator = FakeOperator()
    lifecycle = shared_service(engine, operator=operator)
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "target_id": "cpu-vllm",
                "desired_state": "ready",
                "expected_version": 0,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with engine.begin() as conn:
        conn.execute(
            inference_target_state.update()
            .where(inference_target_state.c.target_id == "cpu-vllm")
            .values(
                state=InferenceTargetState.STARTING.value,
                desired_state=InferenceTargetState.READY.value,
                version=1,
                transition_id="crashed-transition",
                idempotency_key="crashed-before-host",
                request_hash=request_hash,
                code=InferenceOperatorCode.ACCEPTED.value,
                actor="user:ops",
                operation_lease_until=NOW - timedelta(seconds=1),
                updated_at=NOW - timedelta(seconds=1),
            )
        )
        conn.execute(
            inference_transition_idempotency.insert().values(
                target_id="cpu-vllm",
                idempotency_key="crashed-before-host",
                request_hash=request_hash,
                transition_id="crashed-transition",
                state=InferenceTargetState.STARTING.value,
                desired_state=InferenceTargetState.READY.value,
                code=InferenceOperatorCode.ACCEPTED.value,
                version=1,
                manifest_hash=MANIFEST_HASH,
                observed_at=None,
                cooldown_until=None,
                updated_at=NOW - timedelta(seconds=1),
            )
        )

    recovered = await lifecycle.reconcile("cpu-vllm")
    assert recovered.state is InferenceTargetState.ERROR
    assert recovered.code is InferenceOperatorCode.INTERRUPTED
    assert recovered.version == 2
    with pytest.raises(LifecycleUnavailable, match="prior idempotent transition failed"):
        await lifecycle.transition(
            target_id="cpu-vllm",
            desired_state="ready",
            expected_version=0,
            idempotency_key="crashed-before-host",
            confirmation_token=None,
            actor="user:ops",
        )
    assert operator.start_count == 0


@pytest.mark.anyio
async def test_async_terminal_cooldown_and_audit_begin_at_terminal_proof(tmp_path) -> None:
    current = [NOW]
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'accepted-terminal.db'}")
    operator = AcceptedThenStatusOperator(observed_at=NOW)
    configured_target = target().model_copy(update={"cooldown_s": 15})
    lifecycle = shared_service(
        engine,
        operator=operator,
        config=InferenceLifecycleConfig(targets=(configured_target,)),
        clock=lambda: current[0],
    )
    accepted = await lifecycle.transition(
        target_id="cpu-vllm",
        desired_state="ready",
        expected_version=0,
        idempotency_key="accepted-audit",
        confirmation_token=None,
        actor="user:ops",
    )
    assert accepted.state is InferenceTargetState.STARTING
    assert accepted.cooldown_until is None
    with engine.connect() as conn:
        before = list(
            conn.execute(
                sa.text(
                    "SELECT action FROM ne_audit_log WHERE action LIKE "
                    "'inference.lifecycle.%' ORDER BY seq"
                )
            ).scalars()
        )
    assert before == ["inference.lifecycle.started"]

    current[0] += timedelta(seconds=60)
    terminal = await lifecycle.reconcile("cpu-vllm")
    assert terminal.state is InferenceTargetState.READY
    assert terminal.cooldown_until == current[0] + timedelta(seconds=15)
    assert lifecycle.available_actions(configured_target, terminal) == ()
    await lifecycle.reconcile("cpu-vllm")
    with engine.connect() as conn:
        after = list(
            conn.execute(
                sa.text(
                    "SELECT action FROM ne_audit_log WHERE action LIKE "
                    "'inference.lifecycle.%' ORDER BY seq"
                )
            ).scalars()
        )
    assert after == ["inference.lifecycle.started", "inference.lifecycle.completed"]

    current[0] += timedelta(seconds=16)
    assert lifecycle.available_actions(configured_target, lifecycle.state("cpu-vllm")) == ("stop",)


@pytest.mark.anyio
async def test_concurrent_replicas_append_one_terminal_transition_audit(tmp_path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'terminal-audit-cas.db'}")
    operator = ConcurrentTerminalStatusOperator()
    owner = shared_service(engine, operator=operator)
    other = shared_service(engine, operator=operator)
    accepted = await owner.transition(
        target_id="cpu-vllm",
        desired_state="ready",
        expected_version=0,
        idempotency_key="one-terminal-audit",
        confirmation_token=None,
        actor="user:ops",
    )
    assert accepted.state is InferenceTargetState.STARTING

    first = asyncio.create_task(owner.reconcile("cpu-vllm"))
    second = asyncio.create_task(other.reconcile("cpu-vllm"))
    await operator.both_status_calls.wait()
    operator.release_status.set()
    rows = await asyncio.gather(first, second)
    assert all(row.state is InferenceTargetState.READY for row in rows)
    with engine.connect() as conn:
        actions = list(
            conn.execute(
                sa.text(
                    "SELECT action FROM ne_audit_log WHERE action LIKE "
                    "'inference.lifecycle.%' ORDER BY seq"
                )
            ).scalars()
        )
    assert actions == ["inference.lifecycle.started", "inference.lifecycle.completed"]


@pytest.mark.anyio
async def test_stale_status_cannot_overwrite_a_newer_transition(tmp_path) -> None:
    operator = BlockingStatusOperator()
    lifecycle = service(tmp_path, operator=operator)
    reconcile = asyncio.create_task(lifecycle.reconcile("cpu-vllm"))
    await operator.status_called.wait()
    started = await lifecycle.transition(
        target_id="cpu-vllm",
        desired_state="ready",
        expected_version=0,
        idempotency_key="start-while-status-stale",
        confirmation_token=None,
        actor="user:ops",
    )
    operator.release_status.set()
    reconciled = await reconcile
    assert started.state is InferenceTargetState.READY
    assert reconciled.state is InferenceTargetState.READY
    assert lifecycle.state("cpu-vllm").version == 1
    assert operator.start_count == 1


@pytest.mark.anyio
async def test_one_status_failure_does_not_abort_or_poison_sibling_target(tmp_path) -> None:
    first = target()
    second = target().model_copy(
        update={
            "target_id": "gpu-vllm",
            "display_name": "Private GPU",
            "adapter_id": "provider-v1",
            "aliases": ("private-gpu",),
            "resource_id": "provider:private-gpu",
        }
    )
    manifest_targets = tuple(
        InferenceManifestTarget(
            target_id=item.target_id,
            adapter_id=item.adapter_id,
            resource_id=item.resource_id,
            capabilities=item.capabilities,
        )
        for item in (first, second)
    )
    operator = PartialStatusFailureOperator(manifest_targets)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'multi-audit.db'}")
    ensure_audit_schema(engine)
    lifecycle = InferenceLifecycleService(
        engine=engine,
        config=InferenceLifecycleConfig(targets=(first, second)),
        operator=operator,
        air_gap_provider=lambda: False,
        config_hash="b" * 64,
        clock=lambda: NOW,
    )
    rows = {row.target_id: row for row in await lifecycle.reconcile_due()}
    assert rows["cpu-vllm"].state is InferenceTargetState.UNKNOWN
    assert rows["cpu-vllm"].code is InferenceOperatorCode.TRANSPORT_UNAVAILABLE
    assert rows["gpu-vllm"].state is InferenceTargetState.READY
    assert rows["gpu-vllm"].code is InferenceOperatorCode.OBSERVED
    assert operator.status_calls == ["cpu-vllm", "gpu-vllm"]


@pytest.mark.anyio
async def test_production_lifespan_reconciler_converges_without_mutation(tmp_path) -> None:
    operator = FakeOperator()
    operator.state = InferenceTargetState.READY
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'lifespan-audit.db'}")
    ensure_audit_schema(engine)
    lifecycle = InferenceLifecycleService(
        engine=engine,
        config=InferenceLifecycleConfig(
            targets=(target().model_copy(update={"reconcile_cadence_s": 0.02}),)
        ),
        operator=operator,
        air_gap_provider=lambda: False,
        config_hash="b" * 64,
        clock=lambda: NOW,
    )
    from ne.contracts import freeze_config_hash

    class _TestConfig(BaseModel):
        value: str

    frozen_hash = freeze_config_hash(_TestConfig(value="lifespan"))
    lifespan = _lifespan_factory(
        frozen_hash,
        [],
        inference_lifecycle=lifecycle,
    )
    async with lifespan(Starlette()):
        deadline = asyncio.get_running_loop().time() + 1
        while operator.status_count == 0 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert lifecycle.state("cpu-vllm").state is InferenceTargetState.READY
        assert operator.status_count >= 1
        assert operator.start_count == operator.stop_count == 0
