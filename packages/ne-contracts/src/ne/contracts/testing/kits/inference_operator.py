# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""InferenceTargetOperator conformance kit (ADR-0021).

Bindings subclass this kit and provide a fresh operator plus a test-only mutation
counter.  The counter is necessary to discriminate idempotency: receiving the
same safe response twice would not by itself prove that a provider/Compose
mutation did not run twice.
"""

from __future__ import annotations

import asyncio

from ne.contracts.inference import (
    InferenceOperatorCode,
    InferenceStartRequest,
    InferenceStatusRequest,
    InferenceStopRequest,
    InferenceTargetOperation,
    InferenceTargetOperator,
    InferenceTargetState,
)


class InferenceTargetOperatorContractKit:
    """Subclass to prove the closed lifecycle seam's observable invariants.

    The fresh fixture must expose ``target_id`` with status/start/stop support.
    A successful start stays ``starting`` long enough for the repeated-request
    check to observe it. ``prepare_ready_target`` is a test-only binding hook
    that must place the fresh target in ``ready`` without using this Protocol.
    """

    target_id = "local-vllm-cpu"
    unknown_target_id = "missing-target"

    def create_operator(self) -> InferenceTargetOperator:
        """Return a fresh operator with the target described above."""
        raise NotImplementedError

    def mutation_count(
        self,
        operator: InferenceTargetOperator,
        target_id: str,
        operation: InferenceTargetOperation,
    ) -> int:
        """Return test-observed provider/Compose mutation attempts."""
        raise NotImplementedError

    def prepare_ready_target(self, operator: InferenceTargetOperator) -> None:
        """Place the test fixture in ready state without exercising the wire seam."""
        raise NotImplementedError

    def test_manifest_is_stable_and_declares_closed_capabilities(self) -> None:
        operator = self.create_operator()

        async def scenario() -> None:
            first = await operator.describe()
            second = await operator.describe()
            assert first == second
            targets = {target.target_id: target for target in first.targets}
            assert self.target_id in targets
            assert targets[self.target_id].capabilities == (
                InferenceTargetOperation.STATUS,
                InferenceTargetOperation.START,
                InferenceTargetOperation.STOP,
            )

        asyncio.run(scenario())

    def test_status_returns_matching_safe_observation_without_mutation(self) -> None:
        operator = self.create_operator()

        async def scenario() -> None:
            manifest = await operator.describe()
            result = await operator.status(InferenceStatusRequest(target_id=self.target_id))
            assert result.target_id == self.target_id
            assert result.operation is InferenceTargetOperation.STATUS
            assert result.transition_id is None
            assert result.state is InferenceTargetState.STOPPED
            assert result.code is InferenceOperatorCode.OBSERVED
            assert result.manifest_hash == manifest.manifest_hash
            assert (
                self.mutation_count(operator, self.target_id, InferenceTargetOperation.START) == 0
            )
            assert self.mutation_count(operator, self.target_id, InferenceTargetOperation.STOP) == 0

        asyncio.run(scenario())

    def test_start_correlates_transition_and_runs_once(self) -> None:
        operator = self.create_operator()
        request = InferenceStartRequest(
            target_id=self.target_id,
            transition_id="transition-start-1",
        )

        async def scenario() -> None:
            result = await operator.start(request)
            assert result.target_id == self.target_id
            assert result.operation is InferenceTargetOperation.START
            assert result.transition_id == request.transition_id
            assert result.state is InferenceTargetState.STARTING
            assert result.code is InferenceOperatorCode.ACCEPTED
            assert (
                self.mutation_count(operator, self.target_id, InferenceTargetOperation.START) == 1
            )

        asyncio.run(scenario())

    def test_same_start_transition_is_idempotent(self) -> None:
        operator = self.create_operator()
        request = InferenceStartRequest(
            target_id=self.target_id,
            transition_id="transition-start-retry",
        )

        async def scenario() -> None:
            first = await operator.start(request)
            second = await operator.start(request)
            assert second == first
            assert (
                self.mutation_count(operator, self.target_id, InferenceTargetOperation.START) == 1
            )

        asyncio.run(scenario())

    def test_stop_correlates_transition_and_runs_once(self) -> None:
        operator = self.create_operator()
        self.prepare_ready_target(operator)
        request = InferenceStopRequest(
            target_id=self.target_id,
            transition_id="transition-stop-1",
        )

        async def scenario() -> None:
            first = await operator.stop(request)
            second = await operator.stop(request)
            assert first.target_id == self.target_id
            assert first.operation is InferenceTargetOperation.STOP
            assert first.transition_id == request.transition_id
            assert first.state is InferenceTargetState.STOPPING
            assert first.code is InferenceOperatorCode.ACCEPTED
            assert second == first
            assert self.mutation_count(operator, self.target_id, InferenceTargetOperation.STOP) == 1

        asyncio.run(scenario())

    def test_unknown_target_fails_closed_for_every_operation_without_mutation(self) -> None:
        operator = self.create_operator()

        async def scenario() -> None:
            status = await operator.status(InferenceStatusRequest(target_id=self.unknown_target_id))
            start = await operator.start(
                InferenceStartRequest(
                    target_id=self.unknown_target_id,
                    transition_id="transition-unknown-1",
                )
            )
            stop = await operator.stop(
                InferenceStopRequest(
                    target_id=self.unknown_target_id,
                    transition_id="transition-unknown-2",
                )
            )
            for result, operation in (
                (status, InferenceTargetOperation.STATUS),
                (start, InferenceTargetOperation.START),
                (stop, InferenceTargetOperation.STOP),
            ):
                assert result.target_id == self.unknown_target_id
                assert result.operation is operation
                assert result.state is InferenceTargetState.UNKNOWN
                assert result.code is InferenceOperatorCode.TARGET_UNKNOWN
                assert result.adapter_id is None
                assert result.resource_id is None
            assert (
                self.mutation_count(
                    operator, self.unknown_target_id, InferenceTargetOperation.START
                )
                == 0
            )
            assert (
                self.mutation_count(operator, self.unknown_target_id, InferenceTargetOperation.STOP)
                == 0
            )

        asyncio.run(scenario())


__all__ = ["InferenceTargetOperatorContractKit"]
