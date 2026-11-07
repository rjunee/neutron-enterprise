# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Closed inference lifecycle types and the ADR-0021 conformance kit."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from ne.contracts.inference import (
    InferenceManifestTarget,
    InferenceOperatorCode,
    InferenceOperatorManifest,
    InferenceOperatorObservation,
    InferenceStartRequest,
    InferenceStatusRequest,
    InferenceStopRequest,
    InferenceTargetOperation,
    InferenceTargetOperator,
    InferenceTargetState,
)
from ne.contracts.testing.kits.inference_operator import InferenceTargetOperatorContractKit

_MANIFEST_HASH = "a" * 64
_OBSERVED_AT = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class _MemoryOperator:
    """Small deterministic binding used only to prove the public kit itself."""

    def __init__(self) -> None:
        self._manifest = InferenceOperatorManifest(
            manifest_hash=_MANIFEST_HASH,
            targets=(
                InferenceManifestTarget(
                    target_id="local-vllm-cpu",
                    adapter_id="compose-service",
                    resource_id="compose:vllm-cpu",
                    capabilities=(
                        InferenceTargetOperation.STATUS,
                        InferenceTargetOperation.START,
                        InferenceTargetOperation.STOP,
                    ),
                ),
            ),
        )
        self._state = InferenceTargetState.STOPPED
        self._mutations: dict[tuple[str, InferenceTargetOperation], int] = {}
        self._replies: dict[
            tuple[str, InferenceTargetOperation, str], InferenceOperatorObservation
        ] = {}

    async def describe(self) -> InferenceOperatorManifest:
        return self._manifest

    def _unknown(
        self, target_id: str, operation: InferenceTargetOperation, transition_id: str | None
    ) -> InferenceOperatorObservation:
        return InferenceOperatorObservation(
            target_id=target_id,
            adapter_id=None,
            resource_id=None,
            operation=operation,
            transition_id=transition_id,
            state=InferenceTargetState.UNKNOWN,
            code=InferenceOperatorCode.TARGET_UNKNOWN,
            manifest_hash=_MANIFEST_HASH,
            observed_at=_OBSERVED_AT,
        )

    def _known(
        self,
        operation: InferenceTargetOperation,
        *,
        transition_id: str | None,
        state: InferenceTargetState,
        code: InferenceOperatorCode,
    ) -> InferenceOperatorObservation:
        return InferenceOperatorObservation(
            target_id="local-vllm-cpu",
            adapter_id="compose-service",
            resource_id="compose:vllm-cpu",
            operation=operation,
            transition_id=transition_id,
            state=state,
            code=code,
            manifest_hash=_MANIFEST_HASH,
            observed_at=_OBSERVED_AT,
        )

    async def status(self, request: InferenceStatusRequest) -> InferenceOperatorObservation:
        if request.target_id != "local-vllm-cpu":
            return self._unknown(request.target_id, request.operation, request.transition_id)
        return self._known(
            request.operation,
            transition_id=request.transition_id,
            state=self._state,
            code=InferenceOperatorCode.OBSERVED,
        )

    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation:
        if request.target_id != "local-vllm-cpu":
            return self._unknown(request.target_id, request.operation, request.transition_id)
        key = (request.target_id, request.operation, request.transition_id)
        if key not in self._replies:
            self._mutations[(request.target_id, request.operation)] = 1 + self._mutations.get(
                (request.target_id, request.operation), 0
            )
            self._state = InferenceTargetState.STARTING
            self._replies[key] = self._known(
                request.operation,
                transition_id=request.transition_id,
                state=self._state,
                code=InferenceOperatorCode.ACCEPTED,
            )
        return self._replies[key]

    async def stop(self, request: InferenceStopRequest) -> InferenceOperatorObservation:
        if request.target_id != "local-vllm-cpu":
            return self._unknown(request.target_id, request.operation, request.transition_id)
        key = (request.target_id, request.operation, request.transition_id)
        if key in self._replies:
            return self._replies[key]
        if self._state is InferenceTargetState.STOPPED:
            self._replies[key] = self._known(
                request.operation,
                transition_id=request.transition_id,
                state=self._state,
                code=InferenceOperatorCode.ALREADY_PROVEN,
            )
        else:
            self._mutations[(request.target_id, request.operation)] = 1 + self._mutations.get(
                (request.target_id, request.operation), 0
            )
            self._state = InferenceTargetState.STOPPING
            self._replies[key] = self._known(
                request.operation,
                transition_id=request.transition_id,
                state=self._state,
                code=InferenceOperatorCode.ACCEPTED,
            )
        return self._replies[key]

    def mutation_count(self, target_id: str, operation: InferenceTargetOperation) -> int:
        return self._mutations.get((target_id, operation), 0)


class TestMemoryInferenceTargetOperatorConformance(InferenceTargetOperatorContractKit):
    def create_operator(self) -> InferenceTargetOperator:
        return _MemoryOperator()

    def mutation_count(
        self,
        operator: InferenceTargetOperator,
        target_id: str,
        operation: InferenceTargetOperation,
    ) -> int:
        assert isinstance(operator, _MemoryOperator)
        return operator.mutation_count(target_id, operation)

    def prepare_ready_target(self, operator: InferenceTargetOperator) -> None:
        assert isinstance(operator, _MemoryOperator)
        operator._state = InferenceTargetState.READY


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (InferenceStatusRequest, {"target_id": "local-vllm-cpu"}),
        (
            InferenceStartRequest,
            {"target_id": "local-vllm-cpu", "transition_id": "transition-1"},
        ),
        (
            InferenceStopRequest,
            {"target_id": "local-vllm-cpu", "transition_id": "transition-1"},
        ),
    ],
)
@pytest.mark.parametrize(
    "forbidden",
    [
        "command",
        "path",
        "digest",
        "argv",
        "env",
        "environment",
        "endpoint",
        "secret",
        "credential",
        "stdout",
        "stderr",
    ],
)
def test_requests_reject_every_executable_or_secret_shaped_extra(
    model: type[BaseModel],
    payload: dict[str, Any],
    forbidden: str,
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate({**payload, forbidden: "attacker-controlled"})


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "/usr/bin/docker",
        "../vast-rig.sh",
        r"C:\\operator.exe",
        "https://provider.invalid/control",
        "$(destroy)",
        "target with spaces",
        "UPPERCASE",
        "x" * 129,
    ],
)
def test_safe_ids_cannot_be_paths_urls_shell_or_unbounded(unsafe_id: str) -> None:
    with pytest.raises(ValidationError):
        InferenceStatusRequest(target_id=unsafe_id)


def test_operation_is_pinned_by_request_type() -> None:
    with pytest.raises(ValidationError):
        InferenceStartRequest.model_validate(
            {
                "target_id": "local-vllm-cpu",
                "transition_id": "transition-1",
                "operation": "stop",
            }
        )
    with pytest.raises(ValidationError):
        InferenceStopRequest.model_validate(
            {
                "target_id": "local-vllm-cpu",
                "transition_id": "transition-1",
                "operation": "start",
            }
        )


def test_manifest_refuses_duplicate_targets_and_capabilities_without_status() -> None:
    target = InferenceManifestTarget(
        target_id="local-vllm-cpu",
        adapter_id="compose-service",
        resource_id="compose:vllm-cpu",
        capabilities=(InferenceTargetOperation.STATUS, InferenceTargetOperation.START),
    )
    with pytest.raises(ValidationError, match="target ids must be unique"):
        InferenceOperatorManifest(manifest_hash=_MANIFEST_HASH, targets=(target, target))
    with pytest.raises(ValidationError, match="must support status"):
        InferenceManifestTarget(
            target_id="private-gpu",
            adapter_id="vast-rig",
            resource_id="rig:private-gpu",
            capabilities=(InferenceTargetOperation.START, InferenceTargetOperation.STOP),
        )
    with pytest.raises(ValidationError, match="must not contain duplicates"):
        InferenceManifestTarget(
            target_id="private-gpu",
            adapter_id="vast-rig",
            resource_id="rig:private-gpu",
            capabilities=(InferenceTargetOperation.STATUS, InferenceTargetOperation.STATUS),
        )


def test_observation_refuses_naive_time_and_inconsistent_unknown_shape() -> None:
    common = {
        "target_id": "local-vllm-cpu",
        "adapter_id": "compose-service",
        "resource_id": "compose:vllm-cpu",
        "operation": InferenceTargetOperation.STATUS,
        "state": InferenceTargetState.STOPPED,
        "code": InferenceOperatorCode.OBSERVED,
        "manifest_hash": _MANIFEST_HASH,
    }
    with pytest.raises(ValidationError, match="timezone-aware"):
        InferenceOperatorObservation(**common, observed_at=datetime(2026, 8, 20, 12, 0))
    with pytest.raises(ValidationError, match="must have unknown state"):
        InferenceOperatorObservation(
            **{**common, "code": InferenceOperatorCode.TARGET_UNKNOWN},
            observed_at=_OBSERVED_AT,
        )


@pytest.mark.parametrize(
    ("operation", "state"),
    [
        (InferenceTargetOperation.START, InferenceTargetState.STOPPING),
        (InferenceTargetOperation.STOP, InferenceTargetState.STARTING),
        (InferenceTargetOperation.STATUS, InferenceTargetState.STARTING),
    ],
)
def test_accepted_observation_state_must_match_mutation_operation(
    operation: InferenceTargetOperation,
    state: InferenceTargetState,
) -> None:
    with pytest.raises(ValidationError, match="must match its mutation operation"):
        InferenceOperatorObservation(
            target_id="local-vllm-cpu",
            adapter_id="compose-service",
            resource_id="compose:vllm-cpu",
            operation=operation,
            transition_id="transition-invalid-accepted-shape",
            state=state,
            code=InferenceOperatorCode.ACCEPTED,
            manifest_hash=_MANIFEST_HASH,
            observed_at=_OBSERVED_AT,
        )


def test_wire_models_have_no_free_form_or_execution_fields() -> None:
    forbidden = {
        "argv",
        "command",
        "credential",
        "detail",
        "digest",
        "endpoint",
        "env",
        "environment",
        "host",
        "message",
        "path",
        "port",
        "provider",
        "secret",
        "stderr",
        "stdout",
        "url",
    }
    models = (
        InferenceManifestTarget,
        InferenceOperatorManifest,
        InferenceStatusRequest,
        InferenceStartRequest,
        InferenceStopRequest,
        InferenceOperatorObservation,
    )
    for model in models:
        assert forbidden.isdisjoint(model.model_fields), model.__name__


def test_operator_is_an_exported_protocol() -> None:
    import ne.contracts as contracts

    assert contracts.InferenceTargetOperator is InferenceTargetOperator
    assert getattr(contracts.InferenceTargetOperator, "_is_protocol", False)
    assert "InferenceTargetOperator" in contracts.__all__
