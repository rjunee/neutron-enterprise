# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Closed inference-target lifecycle contracts (ADR-0021, additive in 1.18.0).

This module is the only value surface shared by the unprivileged API client and
the confined host operator.  Its absence of executable paths, digests, command
arguments, environments, endpoints, credentials, and free-form error text is a
security property: those values belong exclusively to the operator's closed,
server-owned manifest and must never cross this boundary.

The three mutation methods are deliberately distinct Protocol methods rather
than one arbitrary operation dispatcher.  Their request types pin the operation
with ``Literal`` so a caller cannot send ``stop`` through ``start`` (or vice
versa).  ``describe`` is a safe manifest handshake: it projects only opaque
target/adapter/resource identifiers, declared capabilities, and the SHA-256 of
the full private operator manifest.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# Opaque, bounded identifiers only.  In particular, slash and backslash are not
# admitted, so none of these carrier types can double as a relative/absolute path
# or URL.  A colon supports typed resource ids such as ``compose:vllm-cpu``.
SafeInferenceId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._:-]*$",
    ),
]
ManifestHash = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]


class InferenceTargetOperation(StrEnum):
    """The complete lifecycle operation set exposed by the host boundary."""

    STATUS = "status"
    START = "start"
    STOP = "stop"


class InferenceTargetState(StrEnum):
    """A target's persisted, provider-neutral lifecycle state."""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    ERROR = "error"
    UNKNOWN = "unknown"


class InferenceOperatorCode(StrEnum):
    """Closed, log-safe operator outcomes; never raw runner/provider detail."""

    OBSERVED = "observed"
    ACCEPTED = "accepted"
    ALREADY_PROVEN = "already-proven"
    TARGET_UNKNOWN = "target-unknown"
    OPERATION_UNSUPPORTED = "operation-unsupported"
    TRANSITION_CONFLICT = "transition-conflict"
    MANIFEST_MISMATCH = "manifest-mismatch"
    SCRIPT_REFUSED = "script-refused"
    TRANSPORT_UNAVAILABLE = "transport-unavailable"
    AIR_GAP_REFUSED = "air-gap-refused"
    DEADLINE_EXCEEDED = "deadline-exceeded"
    INTERRUPTED = "interrupted"
    READINESS_FAILED = "readiness-failed"
    PROVIDER_UNAVAILABLE = "provider-unavailable"
    COMPOSE_REFUSED = "compose-refused"
    INTERNAL_ERROR = "internal-error"


class _ClosedModel(BaseModel):
    """Frozen, reject-by-default wire base."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class InferenceManifestTarget(_ClosedModel):
    """Safe public projection of one closed operator-manifest target.

    ``resource_id`` is an opaque typed identifier, not a host path, endpoint, or
    provider payload.  The operator owns the mapping from these identifiers to
    every executable detail.
    """

    target_id: SafeInferenceId
    adapter_id: SafeInferenceId
    resource_id: SafeInferenceId
    capabilities: tuple[InferenceTargetOperation, ...] = Field(min_length=1)

    @field_validator("capabilities")
    @classmethod
    def _capabilities_are_unique_and_observable(
        cls, value: tuple[InferenceTargetOperation, ...]
    ) -> tuple[InferenceTargetOperation, ...]:
        if len(value) != len(set(value)):
            raise ValueError("capabilities must not contain duplicates")
        if InferenceTargetOperation.STATUS not in value:
            raise ValueError("every inference target must support status")
        return value


class InferenceOperatorManifest(_ClosedModel):
    """Safe handshake for the operator's private, closed manifest."""

    protocol_version: Literal[1] = 1
    manifest_hash: ManifestHash
    targets: tuple[InferenceManifestTarget, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _target_ids_are_unique(self) -> InferenceOperatorManifest:
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("operator manifest target ids must be unique")
        return self


class InferenceStatusRequest(_ClosedModel):
    """Observe one registered target without requesting a mutation."""

    protocol_version: Literal[1] = 1
    target_id: SafeInferenceId
    operation: Literal[InferenceTargetOperation.STATUS] = InferenceTargetOperation.STATUS
    transition_id: SafeInferenceId | None = None


class InferenceStartRequest(_ClosedModel):
    """Request a target-scoped start under an API-minted transition id."""

    protocol_version: Literal[1] = 1
    target_id: SafeInferenceId
    operation: Literal[InferenceTargetOperation.START] = InferenceTargetOperation.START
    transition_id: SafeInferenceId


class InferenceStopRequest(_ClosedModel):
    """Request a target-scoped stop under an API-minted transition id."""

    protocol_version: Literal[1] = 1
    target_id: SafeInferenceId
    operation: Literal[InferenceTargetOperation.STOP] = InferenceTargetOperation.STOP
    transition_id: SafeInferenceId


class InferenceOperatorObservation(_ClosedModel):
    """One safe operator observation or transition acknowledgement.

    There is intentionally no detail/message field.  Consumers branch only on
    the closed ``code`` and ``state`` enums; raw child/provider output stays on
    the privileged side and is discarded there.
    """

    protocol_version: Literal[1] = 1
    target_id: SafeInferenceId
    adapter_id: SafeInferenceId | None
    resource_id: SafeInferenceId | None
    operation: InferenceTargetOperation
    transition_id: SafeInferenceId | None = None
    state: InferenceTargetState
    code: InferenceOperatorCode
    manifest_hash: ManifestHash
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _observed_at_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("observed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _closed_code_has_consistent_shape(self) -> InferenceOperatorObservation:
        if self.code is InferenceOperatorCode.TARGET_UNKNOWN:
            if self.state is not InferenceTargetState.UNKNOWN:
                raise ValueError("target-unknown observations must have unknown state")
            if self.adapter_id is not None or self.resource_id is not None:
                raise ValueError("target-unknown observations cannot name manifest resources")
        elif self.adapter_id is None or self.resource_id is None:
            raise ValueError("known-target observations must name adapter and resource ids")
        if self.code is InferenceOperatorCode.ACCEPTED:
            if self.transition_id is None:
                raise ValueError("accepted transitions must carry a transition id")
            expected_state = {
                InferenceTargetOperation.START: InferenceTargetState.STARTING,
                InferenceTargetOperation.STOP: InferenceTargetState.STOPPING,
            }.get(self.operation)
            if expected_state is None or self.state is not expected_state:
                raise ValueError("accepted transition state must match its mutation operation")
        return self


class InferenceTargetOperator(Protocol):
    """Confined target lifecycle seam implemented by the host transport client.

    An implementation may communicate with a privileged local service, but it
    accepts only the closed request models above.  It never accepts an
    executable, path, digest, argv, environment, endpoint, credential, or raw
    provider value.
    """

    async def describe(self) -> InferenceOperatorManifest: ...

    async def status(self, request: InferenceStatusRequest) -> InferenceOperatorObservation: ...

    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation: ...

    async def stop(self, request: InferenceStopRequest) -> InferenceOperatorObservation: ...


__all__ = [
    "InferenceManifestTarget",
    "InferenceOperatorCode",
    "InferenceOperatorManifest",
    "InferenceOperatorObservation",
    "InferenceStartRequest",
    "InferenceStatusRequest",
    "InferenceStopRequest",
    "InferenceTargetOperation",
    "InferenceTargetOperator",
    "InferenceTargetState",
    "ManifestHash",
    "SafeInferenceId",
]
