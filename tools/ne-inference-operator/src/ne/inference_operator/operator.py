# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Durable, target-scoped lifecycle state machine."""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime

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
from ne.inference_operator.journal import JournalRefused, StateJournal
from ne.inference_operator.manifest import AdapterPolicy, PrivateManifest, RegistryTarget
from ne.inference_operator.runner import PinnedRunner, RunResult


class HostInferenceTargetOperator:
    """Contract implementation backed only by closed host policy."""

    def __init__(
        self,
        manifest: PrivateManifest,
        journal: StateJournal,
        *,
        runner: PinnedRunner | None = None,
        air_gap: bool | Callable[[], bool] = True,
        actor: str = "local-operator",
    ) -> None:
        self._manifest = manifest
        self._journal = journal
        self._runner = runner or PinnedRunner()
        self._air_gap = air_gap if callable(air_gap) else lambda: air_gap
        self._actor = actor[:128]
        self._targets = {target.target_id: target for target in manifest.targets}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._active: dict[str, str] = {}
        self._states: dict[str, InferenceTargetState] = {}
        self._versions: defaultdict[str, int] = defaultdict(int)
        self._last_mutation_at: dict[str, datetime] = {}
        self._status_cache: dict[str, InferenceOperatorObservation] = {}
        self._idempotent: dict[tuple[str, str, str], InferenceOperatorObservation] = {}
        self._transition_operations: dict[tuple[str, str], InferenceTargetOperation] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._journal_failed = False
        self._recover()

    def _provider_refused(self, policy: AdapterPolicy) -> bool:
        if not policy.provider_egress:
            return False
        try:
            return self._air_gap()
        except (Exception, SystemExit):
            return True

    async def _run_adapter(
        self,
        policy: AdapterPolicy,
        operation: InferenceTargetOperation,
        *,
        deadline_s: float,
        transition_id: str | None = None,
    ) -> RunResult:
        if self._provider_refused(policy):
            return RunResult(InferenceOperatorCode.AIR_GAP_REFUSED, None)
        broker_transition = transition_id or f"status-{uuid.uuid4().hex}"
        return await self._runner.run(
            policy,
            operation,
            deadline_s=deadline_s,
            transition_id=broker_transition,
        )

    def _recover(self) -> None:
        pending: dict[str, InferenceOperatorObservation] = {}
        historical_pending: dict[tuple[str, str], InferenceOperatorObservation] = {}
        historical_versions: defaultdict[tuple[str, str], int] = defaultdict(int)
        for record in self._journal.records:
            payload = record.payload
            try:
                target_id = str(payload["target_id"])
                state = InferenceTargetState(payload["state"])
                version = int(payload["version"])
                manifest_hash = str(payload["manifest_hash"])
                observation = InferenceOperatorObservation.model_validate(payload["observation"])
            except (KeyError, TypeError, ValueError) as exc:
                raise JournalRefused("operator journal payload is invalid") from exc
            if (
                observation.target_id != target_id
                or observation.state is not state
                or observation.manifest_hash != manifest_hash
            ):
                raise JournalRefused("operator journal payload contradicts its observation")
            generation_key = (manifest_hash, target_id)
            historical_versions[generation_key] = max(historical_versions[generation_key], version)
            is_accepted = payload.get("phase") == "accepted"
            is_mutation = observation.operation in {
                InferenceTargetOperation.START,
                InferenceTargetOperation.STOP,
            }
            if is_accepted and not is_mutation:
                raise JournalRefused("accepted journal row is not a mutation")
            if is_accepted:
                historical_pending[generation_key] = observation
            elif is_mutation:
                historical_pending.pop(generation_key, None)

            if manifest_hash != self._manifest.manifest_hash:
                continue
            target = self._targets.get(target_id)
            if target is None:
                raise JournalRefused("current journal generation names an unknown target")
            if (
                observation.adapter_id != target.adapter_id
                or observation.resource_id != target.resource_id
            ):
                raise JournalRefused("current journal generation contradicts target policy")
            self._states[target_id] = state
            self._versions[target_id] = max(self._versions[target_id], version)
            if observation.transition_id is not None:
                key = (target_id, observation.operation.value, observation.transition_id)
                self._idempotent[key] = observation
                if observation.operation in {
                    InferenceTargetOperation.START,
                    InferenceTargetOperation.STOP,
                }:
                    transition_key = (target_id, observation.transition_id)
                    prior = self._transition_operations.setdefault(
                        transition_key, observation.operation
                    )
                    if prior is not observation.operation:
                        raise JournalRefused("transition id is bound to conflicting operations")
            if is_accepted:
                self._last_mutation_at[target_id] = observation.observed_at
                pending[target_id] = observation
            elif is_mutation:
                pending.pop(target_id, None)
        for generation_key, previous in tuple(historical_pending.items()):
            if generation_key[0] == self._manifest.manifest_hash:
                continue
            self._persist_historical_interruption(
                previous,
                version=historical_versions[generation_key] + 1,
            )
        for previous in pending.values():
            if previous.transition_id is None:
                raise JournalRefused("accepted mutation has no transition id")
            recovered = self._observation(
                previous.target_id,
                previous.operation,
                InferenceTargetState.UNKNOWN,
                InferenceOperatorCode.INTERRUPTED,
                transition_id=previous.transition_id,
            )
            self._persist(recovered, phase="recovered-interrupted", desired=None)
            key = (previous.target_id, previous.operation.value, previous.transition_id)
            self._idempotent[key] = recovered
            self._transition_operations[(previous.target_id, previous.transition_id)] = (
                previous.operation
            )

    def _persist_historical_interruption(
        self,
        previous: InferenceOperatorObservation,
        *,
        version: int,
    ) -> None:
        """Close an old-generation mutation without projecting it into live state."""
        interrupted = previous.model_copy(
            update={
                "state": InferenceTargetState.UNKNOWN,
                "code": InferenceOperatorCode.INTERRUPTED,
                "observed_at": datetime.now(UTC),
            }
        )
        self._journal.append(
            {
                "actor": self._actor,
                "desired_state": None,
                "manifest_hash": interrupted.manifest_hash,
                "observation": interrupted.model_dump(mode="json"),
                "phase": "stale-generation-interrupted",
                "state": interrupted.state.value,
                "target_id": interrupted.target_id,
                "version": version,
            }
        )

    async def describe(self) -> InferenceOperatorManifest:
        return InferenceOperatorManifest(
            manifest_hash=self._manifest.manifest_hash,
            targets=tuple(
                InferenceManifestTarget(
                    target_id=target.target_id,
                    adapter_id=target.adapter_id,
                    resource_id=target.resource_id,
                    capabilities=target.capabilities,
                )
                for target in self._manifest.targets
            ),
        )

    def _observation(
        self,
        target_id: str,
        operation: InferenceTargetOperation,
        state: InferenceTargetState,
        code: InferenceOperatorCode,
        *,
        transition_id: str | None = None,
    ) -> InferenceOperatorObservation:
        target = self._targets.get(target_id)
        return InferenceOperatorObservation(
            target_id=target_id,
            adapter_id=target.adapter_id if target else None,
            resource_id=target.resource_id if target else None,
            operation=operation,
            transition_id=transition_id,
            state=state,
            code=code,
            manifest_hash=self._manifest.manifest_hash,
            observed_at=datetime.now(UTC),
        )

    def _persist(
        self,
        observation: InferenceOperatorObservation,
        *,
        phase: str,
        desired: InferenceTargetState | None,
    ) -> None:
        target_id = observation.target_id
        version = self._versions[target_id] + 1
        self._journal.append(
            {
                "actor": self._actor,
                "desired_state": desired.value if desired else None,
                "manifest_hash": self._manifest.manifest_hash,
                "observation": observation.model_dump(mode="json"),
                "phase": phase,
                "state": observation.state.value,
                "target_id": target_id,
                "version": version,
            }
        )
        self._versions[target_id] = version
        self._states[target_id] = observation.state

    def _try_persist(
        self,
        observation: InferenceOperatorObservation,
        *,
        phase: str,
        desired: InferenceTargetState | None,
    ) -> bool:
        try:
            self._persist(observation, phase=phase, desired=desired)
        except (JournalRefused, OSError):
            self._journal_failed = True
            return False
        return True

    def _internal_failure(
        self,
        target_id: str,
        operation: InferenceTargetOperation,
        transition_id: str | None,
    ) -> InferenceOperatorObservation:
        return self._observation(
            target_id,
            operation,
            InferenceTargetState.UNKNOWN,
            InferenceOperatorCode.INTERNAL_ERROR,
            transition_id=transition_id,
        )

    async def _run_status(
        self, target: RegistryTarget, *, transition_id: str | None
    ) -> InferenceOperatorObservation:
        # A read cannot cancel or supersede an accepted mutation. Returning the
        # durable pending state also prevents reconciliation from repeatedly
        # launching an expensive completion probe while start/stop owns target.
        if target.target_id in self._active:
            observation = self._observation(
                target.target_id,
                InferenceTargetOperation.STATUS,
                self._states[target.target_id],
                InferenceOperatorCode.OBSERVED,
                transition_id=transition_id,
            )
            if self._try_persist(observation, phase="active-observed", desired=None):
                return observation
            return self._internal_failure(
                target.target_id,
                InferenceTargetOperation.STATUS,
                transition_id,
            )
        policy = self._manifest.adapters[target.adapter_id]
        if self._provider_refused(policy):
            observation = self._observation(
                target.target_id,
                InferenceTargetOperation.STATUS,
                InferenceTargetState.UNKNOWN,
                InferenceOperatorCode.AIR_GAP_REFUSED,
                transition_id=transition_id,
            )
            if self._try_persist(observation, phase="refused", desired=None):
                return observation
            return self._internal_failure(
                target.target_id,
                InferenceTargetOperation.STATUS,
                transition_id,
            )
        cached = self._status_cache.get(target.target_id)
        if (
            cached is not None
            and (datetime.now(UTC) - cached.observed_at).total_seconds() < target.reconcile_s
        ):
            observation = self._observation(
                target.target_id,
                InferenceTargetOperation.STATUS,
                cached.state,
                cached.code,
                transition_id=transition_id,
            )
            if self._try_persist(observation, phase="cached-observed", desired=None):
                return observation
            return self._internal_failure(
                target.target_id,
                InferenceTargetOperation.STATUS,
                transition_id,
            )
        result = await self._run_adapter(
            policy,
            InferenceTargetOperation.STATUS,
            deadline_s=target.deadlines_s[InferenceTargetOperation.STATUS],
            transition_id=transition_id,
        )
        state = self._state_from_status(result)
        observation = self._observation(
            target.target_id,
            InferenceTargetOperation.STATUS,
            state,
            result.code,
            transition_id=transition_id,
        )
        if not self._try_persist(observation, phase="observed", desired=None):
            return self._internal_failure(
                target.target_id,
                InferenceTargetOperation.STATUS,
                transition_id,
            )
        self._status_cache[target.target_id] = observation
        return observation

    @staticmethod
    def _state_from_status(result: RunResult) -> InferenceTargetState:
        if result.code is InferenceOperatorCode.OBSERVED and result.returncode == 0:
            return InferenceTargetState.READY
        if result.code is InferenceOperatorCode.OBSERVED and result.returncode == 3:
            return InferenceTargetState.STOPPED
        if result.code is InferenceOperatorCode.DEADLINE_EXCEEDED:
            return InferenceTargetState.ERROR
        if result.code in {
            InferenceOperatorCode.SCRIPT_REFUSED,
            InferenceOperatorCode.AIR_GAP_REFUSED,
        }:
            return InferenceTargetState.UNKNOWN
        return InferenceTargetState.ERROR

    async def status(self, request: InferenceStatusRequest) -> InferenceOperatorObservation:
        target = self._targets.get(request.target_id)
        if target is None:
            return self._observation(
                request.target_id,
                request.operation,
                InferenceTargetState.UNKNOWN,
                InferenceOperatorCode.TARGET_UNKNOWN,
                transition_id=request.transition_id,
            )
        if self._journal_failed:
            return self._internal_failure(
                request.target_id,
                request.operation,
                request.transition_id,
            )
        if request.operation not in target.capabilities:
            return self._observation(
                request.target_id,
                request.operation,
                self._states.get(request.target_id, InferenceTargetState.UNKNOWN),
                InferenceOperatorCode.OPERATION_UNSUPPORTED,
                transition_id=request.transition_id,
            )
        async with self._locks[request.target_id]:
            return await self._run_status(target, transition_id=request.transition_id)

    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation:
        return await self._transition(
            request.target_id,
            request.operation,
            request.transition_id,
            desired=InferenceTargetState.READY,
            pending=InferenceTargetState.STARTING,
        )

    async def stop(self, request: InferenceStopRequest) -> InferenceOperatorObservation:
        return await self._transition(
            request.target_id,
            request.operation,
            request.transition_id,
            desired=InferenceTargetState.STOPPED,
            pending=InferenceTargetState.STOPPING,
        )

    async def _transition(
        self,
        target_id: str,
        operation: InferenceTargetOperation,
        transition_id: str,
        *,
        desired: InferenceTargetState,
        pending: InferenceTargetState,
    ) -> InferenceOperatorObservation:
        target = self._targets.get(target_id)
        if target is None:
            return self._observation(
                target_id,
                operation,
                InferenceTargetState.UNKNOWN,
                InferenceOperatorCode.TARGET_UNKNOWN,
                transition_id=transition_id,
            )
        if self._journal_failed:
            return self._internal_failure(target_id, operation, transition_id)
        key = (target_id, operation.value, transition_id)
        async with self._locks[target_id]:
            if key in self._idempotent:
                return self._idempotent[key]
            prior_operation = self._transition_operations.get((target_id, transition_id))
            if prior_operation is not None and prior_operation is not operation:
                return self._observation(
                    target_id,
                    operation,
                    self._states.get(target_id, InferenceTargetState.UNKNOWN),
                    InferenceOperatorCode.TRANSITION_CONFLICT,
                    transition_id=transition_id,
                )
            if operation not in target.capabilities:
                return self._observation(
                    target_id,
                    operation,
                    self._states.get(target_id, InferenceTargetState.UNKNOWN),
                    InferenceOperatorCode.OPERATION_UNSUPPORTED,
                    transition_id=transition_id,
                )
            policy = self._manifest.adapters[target.adapter_id]
            if self._provider_refused(policy):
                refused = self._observation(
                    target_id,
                    operation,
                    InferenceTargetState.UNKNOWN,
                    InferenceOperatorCode.AIR_GAP_REFUSED,
                    transition_id=transition_id,
                )
                if not self._try_persist(refused, phase="refused", desired=desired):
                    return self._internal_failure(target_id, operation, transition_id)
                self._idempotent[key] = refused
                self._transition_operations[(target_id, transition_id)] = operation
                return refused
            if target_id in self._active:
                return self._observation(
                    target_id,
                    operation,
                    self._states.get(target_id, InferenceTargetState.UNKNOWN),
                    InferenceOperatorCode.TRANSITION_CONFLICT,
                    transition_id=transition_id,
                )
            last_mutation = self._last_mutation_at.get(target_id)
            if (
                last_mutation is not None
                and (datetime.now(UTC) - last_mutation).total_seconds() < target.cooldown_s
            ):
                return self._observation(
                    target_id,
                    operation,
                    self._states.get(target_id, InferenceTargetState.UNKNOWN),
                    InferenceOperatorCode.TRANSITION_CONFLICT,
                    transition_id=transition_id,
                )
            if self._states.get(target_id) is desired:
                status_result = await self._run_adapter(
                    policy,
                    InferenceTargetOperation.STATUS,
                    deadline_s=target.deadlines_s[InferenceTargetOperation.STATUS],
                    transition_id=transition_id,
                )
                if self._state_from_status(status_result) is desired:
                    proven = self._observation(
                        target_id,
                        operation,
                        desired,
                        InferenceOperatorCode.ALREADY_PROVEN,
                        transition_id=transition_id,
                    )
                    if not self._try_persist(
                        proven,
                        phase="already-proven",
                        desired=desired,
                    ):
                        return self._internal_failure(target_id, operation, transition_id)
                    self._idempotent[key] = proven
                    self._transition_operations[(target_id, transition_id)] = operation
                    return proven
            accepted = self._observation(
                target_id,
                operation,
                pending,
                InferenceOperatorCode.ACCEPTED,
                transition_id=transition_id,
            )
            if not self._try_persist(accepted, phase="accepted", desired=desired):
                return self._internal_failure(target_id, operation, transition_id)
            self._active[target_id] = transition_id
            self._status_cache.pop(target_id, None)
            self._last_mutation_at[target_id] = accepted.observed_at
            self._idempotent[key] = accepted
            self._transition_operations[(target_id, transition_id)] = operation
            task = asyncio.create_task(
                self._complete_transition(target, operation, transition_id, desired)
            )
            self._tasks.add(task)
            task.add_done_callback(self._transition_done)
            # Let the owned task enter the adapter exactly once before the
            # acknowledgement crosses the wire. It still completes outside
            # this target lock, so the accepted state is always observable.
            await asyncio.sleep(0)
            return accepted

    def _transition_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            # Consume unexpected task exceptions. _complete_transition records
            # INTERNAL_ERROR before any exception is allowed to reach here.
            task.exception()

    async def _complete_transition(
        self,
        target: RegistryTarget,
        operation: InferenceTargetOperation,
        transition_id: str,
        desired: InferenceTargetState,
    ) -> None:
        policy = self._manifest.adapters[target.adapter_id]
        cancelled = False
        try:
            result = await self._run_adapter(
                policy,
                operation,
                deadline_s=target.deadlines_s[operation],
                transition_id=transition_id,
            )
        except asyncio.CancelledError:
            result = RunResult(InferenceOperatorCode.INTERRUPTED, None)
            cancelled = True
        except Exception:
            result = RunResult(InferenceOperatorCode.INTERNAL_ERROR, None)
        async with self._locks[target.target_id]:
            # Mutation operations prove success only with exit zero. Exit three
            # is reserved for STATUS meaning "proven stopped" and must never
            # make a START look ready.
            successful = result.code is InferenceOperatorCode.OBSERVED and result.returncode == 0
            if successful:
                state = desired
                code = InferenceOperatorCode.OBSERVED
            elif result.code is InferenceOperatorCode.OBSERVED:
                state = InferenceTargetState.ERROR
                code = InferenceOperatorCode.READINESS_FAILED
            elif result.code in {
                InferenceOperatorCode.SCRIPT_REFUSED,
                InferenceOperatorCode.AIR_GAP_REFUSED,
            }:
                state = InferenceTargetState.UNKNOWN
                code = result.code
            else:
                state = InferenceTargetState.ERROR
                code = result.code
            completed = self._observation(
                target.target_id,
                operation,
                state,
                code,
                transition_id=transition_id,
            )
            key = (target.target_id, operation.value, transition_id)
            try:
                if self._try_persist(completed, phase="completed", desired=desired):
                    self._idempotent[key] = completed
                else:
                    failed = self._internal_failure(
                        target.target_id,
                        operation,
                        transition_id,
                    )
                    self._states[target.target_id] = InferenceTargetState.UNKNOWN
                    self._idempotent[key] = failed
            finally:
                self._status_cache.pop(target.target_id, None)
                self._active.pop(target.target_id, None)
        if cancelled:
            raise asyncio.CancelledError

    async def drain(self) -> None:
        """Wait for owned transition tasks; used by orderly shutdown and tests."""
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


__all__ = ["HostInferenceTargetOperator"]
