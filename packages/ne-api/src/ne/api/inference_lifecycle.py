# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Governed inference-target lifecycle state and confined operator client.

The API owns policy, durable intent, idempotency, confirmation and audit.  The
host operator owns every executable detail.  This module crosses that boundary
only with the closed ADR-0021 contract models; no command, path, argv, env,
endpoint, image digest, provider payload or credential is accepted from HTTP.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import secrets
import stat
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError

from ne.audit.store import append_record
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
    SafeInferenceId,
)
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Lane, Principal
from ne.events import new_audit_event
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.errors import (
    ActionValidationError,
    ApprovalAlreadyDecidedError,
    ApprovalRequestNotFoundError,
)
from ne.runtime.tools import ToolBinding
from ne.schema import AuditOutcome

LIFECYCLE_ACTION = "write.internal.model.lifecycle.transition"
LIFECYCLE_ARG_POLICY = ToolArgPolicy(
    args={
        "target_id": ArgSpec(kind=ToolArgKind.identifier, required=True, max_length=128),
        "desired_state": ArgSpec(kind=ToolArgKind.identifier, required=True, max_length=16),
        "expected_version": ArgSpec(kind=ToolArgKind.number, required=True),
        "idempotency_key": ArgSpec(kind=ToolArgKind.identifier, required=True, max_length=128),
        "confirmation_token": ArgSpec(kind=ToolArgKind.text, required=False, max_length=128),
    }
)


class InferenceTargetConfig(BaseModel):
    """Public, config-hashed projection of one closed operator target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: SafeInferenceId
    display_name: str = Field(min_length=1, max_length=96)
    adapter_id: SafeInferenceId
    aliases: tuple[SafeInferenceId, ...] = Field(min_length=1)
    capabilities: tuple[InferenceTargetOperation, ...] = Field(min_length=1)
    # Host adapters may need a bounded one-token inference probe before they can
    # prove readiness (the demo CPU/GPU registries use 100s/120s). Keep this
    # finite while leaving enough headroom for slower private deployments.
    status_deadline_s: float = Field(default=5.0, gt=0, le=300)
    start_deadline_s: float = Field(default=300.0, gt=0, le=3600)
    stop_deadline_s: float = Field(default=300.0, gt=0, le=3600)
    reconcile_cadence_s: float = Field(default=15.0, gt=0, le=3600)
    cooldown_s: float = Field(default=10.0, ge=0, le=3600)
    secret_refs: tuple[SafeInferenceId, ...] = ()
    resource_id: SafeInferenceId
    provider_control: bool = False

    @field_validator("aliases", "capabilities", "secret_refs")
    @classmethod
    def _unique(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        if len(value) != len(set(value)):
            raise ValueError("values must be unique")
        return value

    @model_validator(mode="after")
    def _observable(self) -> InferenceTargetConfig:
        if InferenceTargetOperation.STATUS not in self.capabilities:
            raise ValueError("every target must support status")
        return self


class InferenceLifecycleConfig(BaseModel):
    """Closed boot registry plus local transport and confirmation policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    targets: tuple[InferenceTargetConfig, ...] = ()
    operator_socket: str = ""
    operator_uid: int = Field(default=0, ge=0)
    operator_gid: int = Field(default=10001, ge=0)
    operator_parent_mode: int = Field(default=0o750, ge=0, le=0o777)
    operator_socket_mode: int = Field(default=0o660, ge=0, le=0o777)
    confirmation_ttl_s: float = Field(default=120.0, gt=0, le=900)
    pending_reservation_ttl_s: float = Field(default=60.0, ge=30.0, le=300.0)

    @model_validator(mode="after")
    def _registry_is_unambiguous(self) -> InferenceLifecycleConfig:
        target_ids = [target.target_id for target in self.targets]
        aliases = [alias for target in self.targets for alias in target.aliases]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("inference target ids must be unique")
        if len(aliases) != len(set(aliases)):
            raise ValueError("inference target aliases must be unique")
        if self.operator_socket and not Path(self.operator_socket).is_absolute():
            raise ValueError("operator_socket must be an absolute Unix-domain socket path")
        return self

    @property
    def registry_hash(self) -> str:
        payload = [target.model_dump(mode="json") for target in self.targets]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class LifecycleUnavailable(ActionValidationError):
    """The confined operator is absent or failed its exact manifest handshake."""


class LifecycleConflict(ActionValidationError):
    """The requested transition conflicts with durable target state."""


class LifecycleConfirmationRefused(ActionValidationError):
    """A destructive stop lacked a current, bound, single-use confirmation."""


_REQUEST_PRINCIPAL: ContextVar[Principal | None] = ContextVar(
    "ne_api_inference_lifecycle_request_principal", default=None
)


@contextmanager
def inference_lifecycle_request_principal(principal: Principal):
    """Bind the HTTP-authenticated principal across validation, HITL, and execution."""
    token = _REQUEST_PRINCIPAL.set(principal)
    try:
        yield
    finally:
        _REQUEST_PRINCIPAL.reset(token)


def current_inference_lifecycle_request_principal() -> Principal | None:
    """Return only the router-bound, already-authenticated lifecycle requester."""
    return _REQUEST_PRINCIPAL.get()


class UdsInferenceTargetOperator(InferenceTargetOperator):
    """Strict local-only HTTP transport for the closed ADR-0021 protocol."""

    def __init__(
        self,
        socket_path: str,
        *,
        expected_uid: int = 0,
        expected_gid: int = 10001,
        expected_parent_mode: int = 0o750,
        expected_socket_mode: int = 0o660,
    ) -> None:
        path = Path(socket_path)
        if not path.is_absolute():
            raise ValueError("operator socket path must be absolute")
        self._socket_path = path
        self._expected_uid = expected_uid
        self._expected_gid = expected_gid
        self._expected_parent_mode = expected_parent_mode
        self._expected_socket_mode = expected_socket_mode
        self._trusted_identity: tuple[int, int] | None = None
        self._trusted_socket_fd: int | None = None
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(path)),
            base_url="http://neutron-inference-operator",
            # The lifecycle service owns the operation-specific total deadline.
            # A second transport deadline here would reject a legitimate slow
            # status before the configured governed deadline expires.
            timeout=None,
        )

    @property
    def is_available(self) -> bool:
        try:
            self._endpoint_identity()
            return True
        except OSError:
            return False

    def _endpoint_identity(self) -> tuple[int, int]:
        """Authenticate the protected local pathname and pin its socket inode.

        httpx does not expose the connected server's ``SO_PEERCRED``. The host
        service therefore authenticates this API peer, while this client trusts
        only a root-owned, exact-group/mode, non-symlink socket inside the
        equivalently protected canonical parent and rechecks dev/inode around
        every request.
        """
        parent = self._socket_path.parent
        if parent.resolve(strict=True) != parent:
            raise OSError("operator socket parent must be canonical and non-symlinked")
        parent_stat = parent.lstat()
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise OSError("operator socket parent is not a directory")
        if (
            parent_stat.st_uid != self._expected_uid
            or parent_stat.st_gid != self._expected_gid
            or stat.S_IMODE(parent_stat.st_mode) != self._expected_parent_mode
        ):
            raise OSError("operator socket parent ownership or mode is unsafe")
        socket_stat = self._socket_path.lstat()
        if not stat.S_ISSOCK(socket_stat.st_mode):
            raise OSError("operator endpoint is not a non-symlink socket")
        if (
            socket_stat.st_uid != self._expected_uid
            or socket_stat.st_gid != self._expected_gid
            or stat.S_IMODE(socket_stat.st_mode) != self._expected_socket_mode
        ):
            raise OSError("operator socket ownership or mode is unsafe")
        identity = (socket_stat.st_dev, socket_stat.st_ino)
        pinned = self._trusted_socket_fd
        o_path = getattr(os, "O_PATH", None)
        if pinned is None and isinstance(o_path, int):
            flags = o_path | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            candidate = os.open(self._socket_path, flags)
            try:
                pinned_stat = os.fstat(candidate)
                if (
                    (pinned_stat.st_dev, pinned_stat.st_ino) != identity
                    or not stat.S_ISSOCK(pinned_stat.st_mode)
                    or pinned_stat.st_uid != self._expected_uid
                    or pinned_stat.st_gid != self._expected_gid
                    or stat.S_IMODE(pinned_stat.st_mode) != self._expected_socket_mode
                ):
                    raise OSError("operator socket changed while trust was established")
            except BaseException:
                os.close(candidate)
                raise
            self._trusted_socket_fd = candidate
            pinned = candidate
        if pinned is not None:
            pinned_stat = os.fstat(pinned)
            if (pinned_stat.st_dev, pinned_stat.st_ino) != identity:
                raise OSError("operator socket identity changed after trust was established")
        if self._trusted_identity is None:
            self._trusted_identity = identity
        elif self._trusted_identity != identity:
            raise OSError("operator socket identity changed after trust was established")
        return identity

    def _recheck_endpoint(self, identity: tuple[int, int]) -> None:
        if self._endpoint_identity() != identity:
            raise OSError("operator socket was replaced during the request")

    async def _get(self, path: str, model: type[BaseModel]) -> BaseModel:
        identity = self._endpoint_identity()
        try:
            response = await self._client.get(path)
            response.raise_for_status()
            return model.model_validate(response.json())
        finally:
            self._recheck_endpoint(identity)

    async def _post(self, path: str, request: BaseModel) -> InferenceOperatorObservation:
        identity = self._endpoint_identity()
        try:
            response = await self._client.post(path, json=request.model_dump(mode="json"))
            response.raise_for_status()
            return InferenceOperatorObservation.model_validate(response.json())
        finally:
            self._recheck_endpoint(identity)

    async def describe(self) -> InferenceOperatorManifest:
        return InferenceOperatorManifest.model_validate(
            (await self._get("/v1/manifest", InferenceOperatorManifest)).model_dump()
        )

    async def status(self, request: InferenceStatusRequest) -> InferenceOperatorObservation:
        return await self._post("/v1/status", request)

    async def start(self, request: InferenceStartRequest) -> InferenceOperatorObservation:
        return await self._post("/v1/start", request)

    async def stop(self, request: InferenceStopRequest) -> InferenceOperatorObservation:
        return await self._post("/v1/stop", request)

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        finally:
            if self._trusted_socket_fd is not None:
                os.close(self._trusted_socket_fd)
                self._trusted_socket_fd = None


_metadata = sa.MetaData()
inference_target_state = sa.Table(
    "ne_inference_target_state",
    _metadata,
    sa.Column("target_id", sa.String, primary_key=True),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("desired_state", sa.String, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("transition_id", sa.String, nullable=True),
    sa.Column("idempotency_key", sa.String, nullable=True),
    sa.Column("request_hash", sa.String, nullable=True),
    sa.Column("code", sa.String, nullable=False),
    sa.Column("manifest_hash", sa.String, nullable=True),
    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("actor", sa.String, nullable=True),
    sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
    sa.Column("operation_lease_until", sa.DateTime(timezone=True), nullable=True),
)
inference_confirmations = sa.Table(
    "ne_inference_confirmations",
    _metadata,
    sa.Column("token_hash", sa.String, primary_key=True),
    sa.Column("target_id", sa.String, nullable=False),
    sa.Column("principal_id", sa.String, nullable=False),
    sa.Column("expected_version", sa.Integer, nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
)
inference_transition_idempotency = sa.Table(
    "ne_inference_transition_idempotency",
    _metadata,
    sa.Column("target_id", sa.String, primary_key=True),
    sa.Column("idempotency_key", sa.String, primary_key=True),
    sa.Column("request_hash", sa.String, nullable=False),
    sa.Column("transition_id", sa.String, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("desired_state", sa.String, nullable=False),
    sa.Column("code", sa.String, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("manifest_hash", sa.String, nullable=True),
    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)
inference_pending_changes = sa.Table(
    "ne_inference_pending_changes",
    _metadata,
    sa.Column("reservation_id", sa.String, primary_key=True),
    sa.Column("target_id", sa.String, nullable=False, unique=True),
    sa.Column("request_id", sa.String, nullable=True, unique=True),
    sa.Column("requested_by", sa.String, nullable=False),
    sa.Column("desired_state", sa.String, nullable=False),
    sa.Column("expected_version", sa.Integer, nullable=False),
    sa.Column("idempotency_key", sa.String, nullable=False),
    sa.Column("request_hash", sa.String, nullable=False),
    sa.Column("owner_instance_id", sa.String, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class LifecycleState:
    target_id: str
    state: InferenceTargetState
    desired_state: InferenceTargetState
    version: int
    transition_id: str | None
    code: InferenceOperatorCode
    manifest_hash: str | None
    observed_at: datetime | None
    updated_at: datetime
    cooldown_until: datetime | None
    idempotency_key: str | None
    request_hash: str | None


def ensure_inference_lifecycle_schema(engine: sa.Engine) -> None:
    _metadata.create_all(
        engine,
        tables=[
            inference_target_state,
            inference_confirmations,
            inference_transition_idempotency,
            inference_pending_changes,
        ],
    )


class InferenceLifecycleService:
    """Durable target state and the sole caller of the confined operator seam."""

    def __init__(
        self,
        *,
        engine: sa.Engine,
        config: InferenceLifecycleConfig,
        operator: InferenceTargetOperator | None,
        air_gap_provider: Callable[[], bool],
        config_hash: str,
        model_lanes: Mapping[str, Lane] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        self.config = config
        self._operator = operator
        self._air_gap_provider = air_gap_provider
        self._config_hash = config_hash
        self._clock = clock
        self._targets = {target.target_id: target for target in config.targets}
        self._aliases = {
            alias: target.target_id for target in config.targets for alias in target.aliases
        }
        self._handshake_hash: str | None = None
        self._instance_id = secrets.token_hex(16)
        if model_lanes is not None:
            managed_aliases = set(self._aliases)
            unknown = managed_aliases - set(model_lanes)
            incompatible = {
                alias
                for alias in managed_aliases
                if model_lanes.get(alias) is not Lane.PRIVATE_VLLM
            }
            if unknown:
                raise ValueError(
                    "inference lifecycle aliases are absent from the gateway registry: "
                    + ", ".join(sorted(unknown))
                )
            if incompatible:
                raise ValueError(
                    "inference lifecycle aliases must name private-vllm gateway models: "
                    + ", ".join(sorted(incompatible))
                )
        ensure_inference_lifecycle_schema(engine)
        now = self._now()
        for target in config.targets:
            try:
                with engine.begin() as conn:
                    existing = conn.execute(
                        sa.select(inference_target_state.c.target_id).where(
                            inference_target_state.c.target_id == target.target_id
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        continue
                    conn.execute(
                        inference_target_state.insert().values(
                            target_id=target.target_id,
                            state=InferenceTargetState.UNKNOWN.value,
                            desired_state=InferenceTargetState.STOPPED.value,
                            version=0,
                            transition_id=None,
                            idempotency_key=None,
                            request_hash=None,
                            code=InferenceOperatorCode.TRANSPORT_UNAVAILABLE.value,
                            manifest_hash=None,
                            observed_at=None,
                            updated_at=now,
                            actor=None,
                            cooldown_until=None,
                            operation_lease_until=None,
                        )
                    )
            except IntegrityError:
                # A sibling API replica can win the initial target insert
                # between this replica's SELECT and INSERT. The unique target
                # key is authoritative; verify the committed winner rather than
                # failing healthy concurrent startup or overwriting live state.
                with engine.connect() as conn:
                    winner = conn.execute(
                        sa.select(inference_target_state.c.target_id).where(
                            inference_target_state.c.target_id == target.target_id
                        )
                    ).scalar_one_or_none()
                if winner != target.target_id:
                    raise

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("lifecycle clock must be timezone-aware")
        return now

    def target(self, target_id: str) -> InferenceTargetConfig:
        try:
            return self._targets[target_id]
        except KeyError as exc:
            raise LifecycleConflict("unknown inference target") from exc

    def _state_from_row(self, row: sa.Row) -> LifecycleState:
        def _aware(value: datetime | None) -> datetime | None:
            if value is not None and value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value

        return LifecycleState(
            target_id=str(row.target_id),
            state=InferenceTargetState(row.state),
            desired_state=InferenceTargetState(row.desired_state),
            version=int(row.version),
            transition_id=row.transition_id,
            code=InferenceOperatorCode(row.code),
            manifest_hash=row.manifest_hash,
            observed_at=_aware(row.observed_at),
            updated_at=_aware(row.updated_at) or self._now(),
            cooldown_until=_aware(row.cooldown_until),
            idempotency_key=row.idempotency_key,
            request_hash=row.request_hash,
        )

    def states(self) -> tuple[LifecycleState, ...]:
        # Durable rows for targets removed from a later boot registry are
        # historical evidence, not active lifecycle membership. Preserve them
        # without reconciling, advertising, or allowing them to poison current
        # registry targets.
        if not self._targets:
            return ()
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(inference_target_state)
                .where(inference_target_state.c.target_id.in_(tuple(self._targets)))
                .order_by(inference_target_state.c.target_id)
            ).all()
        return tuple(self._state_from_row(row) for row in rows)

    def state(self, target_id: str) -> LifecycleState:
        self.target(target_id)
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(inference_target_state).where(
                    inference_target_state.c.target_id == target_id
                )
            ).one()
        return self._state_from_row(row)

    def availability(self, alias: str) -> tuple[bool, str | None]:
        target_id = self._aliases.get(alias)
        if target_id is None:
            return True, None
        try:
            row = self.state(target_id)
        except Exception:
            return False, "lifecycle-state-unavailable"
        target = self.target(target_id)
        if target.provider_control and self._air_gap():
            return False, "lifecycle-air-gap-refused"
        if row.state is InferenceTargetState.READY:
            if row.observed_at is None:
                return False, "lifecycle-unobserved"
            age_s = (self._now() - row.observed_at).total_seconds()
            if age_s < 0 or age_s > target.reconcile_cadence_s * 2:
                return False, "lifecycle-stale"
            return True, None
        return False, f"lifecycle-{row.state.value}"

    @property
    def transport_available(self) -> bool:
        if self._operator is None:
            return False
        available = getattr(self._operator, "is_available", True)
        return available if isinstance(available, bool) else False

    def _air_gap(self) -> bool:
        try:
            value = self._air_gap_provider()
            return value if isinstance(value, bool) else True
        except Exception:
            return True

    def _mark_unknown(self, code: InferenceOperatorCode) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                inference_target_state.update()
                .where(
                    inference_target_state.c.state.not_in(
                        (
                            InferenceTargetState.STARTING.value,
                            InferenceTargetState.STOPPING.value,
                        )
                    )
                )
                .values(
                    state=InferenceTargetState.UNKNOWN.value,
                    code=code.value,
                    updated_at=self._now(),
                )
            )

    @staticmethod
    def _remaining(deadline_at: float) -> float:
        remaining = deadline_at - anyio.current_time()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    @staticmethod
    def _same_transition(row: LifecycleState) -> tuple[sa.ColumnElement[bool], ...]:
        transition_match = (
            inference_target_state.c.transition_id.is_(None)
            if row.transition_id is None
            else inference_target_state.c.transition_id == row.transition_id
        )
        return (
            inference_target_state.c.target_id == row.target_id,
            inference_target_state.c.version == row.version,
            transition_match,
        )

    def _mark_target_if_current(
        self,
        row: LifecycleState,
        code: InferenceOperatorCode,
        *,
        state: InferenceTargetState = InferenceTargetState.UNKNOWN,
        advance_version: bool = False,
    ) -> bool:
        changed = row.state is not state or row.code is not code
        next_version = row.version + 1 if changed or advance_version else row.version
        values: dict[str, object] = {
            "state": state.value,
            "code": code.value,
            "updated_at": self._now(),
            "version": next_version,
        }
        if state in (InferenceTargetState.ERROR, InferenceTargetState.UNKNOWN):
            values["operation_lease_until"] = None
        with self._engine.begin() as conn:
            durable = conn.execute(
                sa.select(inference_target_state.c.actor).where(*self._same_transition(row))
            ).first()
            if durable is None:
                return False
            result = conn.execute(
                inference_target_state.update().where(*self._same_transition(row)).values(**values)
            )
            if (
                result.rowcount == 1
                and changed
                and row.state in (InferenceTargetState.STARTING, InferenceTargetState.STOPPING)
                and row.transition_id is not None
                and row.idempotency_key is not None
                and row.request_hash is not None
            ):
                conn.execute(
                    inference_transition_idempotency.update()
                    .where(
                        inference_transition_idempotency.c.target_id == row.target_id,
                        inference_transition_idempotency.c.transition_id == row.transition_id,
                        inference_transition_idempotency.c.idempotency_key == row.idempotency_key,
                        inference_transition_idempotency.c.request_hash == row.request_hash,
                        inference_transition_idempotency.c.version == row.version,
                    )
                    .values(
                        state=state.value,
                        code=code.value,
                        version=next_version,
                        updated_at=self._now(),
                    )
                )
                self._audit_lifecycle(
                    action="inference.lifecycle.failed",
                    outcome=AuditOutcome.error,
                    actor=(
                        str(durable.actor)
                        if durable.actor is not None
                        else "system:inference-reconciler"
                    ),
                    target_id=row.target_id,
                    transition_id=row.transition_id,
                    old_state=row.state,
                    desired_state=row.desired_state,
                    observed_state=state,
                    code=code,
                    manifest_hash=row.manifest_hash,
                    conn=conn,
                )
        return result.rowcount == 1

    async def handshake(self, *, deadline_at: float) -> str:
        if not self.transport_available or self._operator is None:
            raise LifecycleUnavailable("inference operator transport is unavailable")
        try:
            with anyio.fail_after(self._remaining(deadline_at)):
                manifest = await self._operator.describe()
        except TimeoutError:
            self._handshake_hash = None
            raise
        except Exception as exc:
            self._handshake_hash = None
            raise LifecycleUnavailable("inference operator transport is unavailable") from exc
        expected = {
            target.target_id: InferenceManifestTarget(
                target_id=target.target_id,
                adapter_id=target.adapter_id,
                resource_id=target.resource_id,
                capabilities=target.capabilities,
            )
            for target in self.config.targets
        }
        actual = {target.target_id: target for target in manifest.targets}
        if actual != expected:
            self._handshake_hash = None
            self._mark_unknown(InferenceOperatorCode.MANIFEST_MISMATCH)
            raise LifecycleUnavailable("inference operator manifest does not match boot registry")
        self._handshake_hash = manifest.manifest_hash
        return manifest.manifest_hash

    def cooldown_active(self, row: LifecycleState) -> bool:
        return row.cooldown_until is not None and row.cooldown_until > self._now()

    def _terminal_proof_is_current(
        self,
        target: InferenceTargetConfig,
        row: LifecycleState,
    ) -> bool:
        if row.state not in (InferenceTargetState.READY, InferenceTargetState.STOPPED):
            return False
        if target.provider_control and self._air_gap():
            return False
        if not self.transport_available or self._handshake_hash is None:
            return False
        if row.manifest_hash != self._handshake_hash or row.observed_at is None:
            return False
        age_s = (self._now() - row.observed_at).total_seconds()
        return 0 <= age_s <= target.reconcile_cadence_s * 2

    def available_actions(
        self, target: InferenceTargetConfig, row: LifecycleState
    ) -> tuple[Literal["start", "stop"], ...]:
        """Return only actions that current server truth can actually admit."""
        if not self.transport_available or self._handshake_hash is None:
            return ()
        if target.provider_control and self._air_gap():
            return ()
        if self.cooldown_active(row):
            return ()
        if row.state in (InferenceTargetState.STARTING, InferenceTargetState.STOPPING):
            return ()
        actions: list[Literal["start", "stop"]] = []
        if (
            row.state is not InferenceTargetState.READY
            and InferenceTargetOperation.START in target.capabilities
        ):
            actions.append("start")
        if (
            row.state is not InferenceTargetState.STOPPED
            and InferenceTargetOperation.STOP in target.capabilities
        ):
            actions.append("stop")
        return tuple(actions)

    def reserve_pending_change(
        self,
        *,
        target_id: str,
        requested_by: str,
        desired_state: str,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[str, str | None, bool]:
        """Atomically reserve one pre-HITL request per target.

        An identical concurrent request converges on the existing reservation;
        any different request is refused before another approval can be created.
        """
        self.target(target_id)
        desired = InferenceTargetState(desired_state)
        if desired not in (InferenceTargetState.READY, InferenceTargetState.STOPPED):
            raise LifecycleConflict("pending lifecycle desired state is invalid")

        def _existing_result(existing: sa.Row) -> tuple[str, str | None, bool]:
            if (
                existing.requested_by != requested_by
                or existing.desired_state != desired.value
                or existing.expected_version != expected_version
                or existing.idempotency_key != idempotency_key
                or existing.request_hash != request_hash
            ):
                raise LifecycleConflict("target already has a pending lifecycle request")
            return str(existing.reservation_id), existing.request_id, False

        # SELECT-then-INSERT alone is not cross-replica safe. The unique target
        # constraint is the durable arbiter; a racing loser retries and joins or
        # refuses the committed winner instead of leaking IntegrityError/500.
        for _attempt in range(3):
            now = self._now()
            reservation_id = secrets.token_hex(16)
            try:
                with self._engine.begin() as conn:
                    existing = conn.execute(
                        sa.select(inference_pending_changes).where(
                            inference_pending_changes.c.target_id == target_id
                        )
                    ).first()
                    if existing is not None:
                        expires_at = existing.expires_at
                        if expires_at.tzinfo is None:
                            expires_at = expires_at.replace(tzinfo=UTC)
                        if existing.request_id is not None and expires_at <= now:
                            raise LifecycleConflict(
                                "pending lifecycle approval owner expired; recovery is required"
                            )
                        if existing.request_id is None and expires_at <= now:
                            deleted = conn.execute(
                                inference_pending_changes.delete().where(
                                    inference_pending_changes.c.reservation_id
                                    == existing.reservation_id,
                                    inference_pending_changes.c.request_id.is_(None),
                                    inference_pending_changes.c.expires_at <= now,
                                )
                            )
                            if deleted.rowcount != 1:
                                continue
                        else:
                            return _existing_result(existing)
                    conn.execute(
                        inference_pending_changes.insert().values(
                            reservation_id=reservation_id,
                            target_id=target_id,
                            request_id=None,
                            requested_by=requested_by,
                            desired_state=desired.value,
                            expected_version=expected_version,
                            idempotency_key=idempotency_key,
                            request_hash=request_hash,
                            owner_instance_id=self._instance_id,
                            created_at=now,
                            expires_at=now
                            + timedelta(seconds=self.config.pending_reservation_ttl_s),
                        )
                    )
                return reservation_id, None, True
            except IntegrityError:
                # A competing replica committed the unique target reservation.
                # Retry in a fresh transaction so its exact request can be
                # compared without trusting backend-specific error text.
                continue
        with self._engine.connect() as conn:
            winner = conn.execute(
                sa.select(inference_pending_changes).where(
                    inference_pending_changes.c.target_id == target_id
                )
            ).first()
        if winner is None:
            raise LifecycleConflict("pending lifecycle reservation changed concurrently; retry")
        return _existing_result(winner)

    def attach_pending_request(self, *, reservation_id: str, request_id: str) -> None:
        with self._engine.begin() as conn:
            result = conn.execute(
                inference_pending_changes.update()
                .where(
                    inference_pending_changes.c.reservation_id == reservation_id,
                    inference_pending_changes.c.request_id.is_(None),
                )
                .values(request_id=request_id)
            )
            if result.rowcount == 1:
                return
            existing = conn.execute(
                sa.select(inference_pending_changes.c.request_id).where(
                    inference_pending_changes.c.reservation_id == reservation_id
                )
            ).scalar_one_or_none()
        if existing != request_id:
            raise LifecycleConflict("pending lifecycle reservation changed concurrently")

    def pending_reservation_request(self, reservation_id: str) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(inference_pending_changes.c.request_id).where(
                    inference_pending_changes.c.reservation_id == reservation_id
                )
            ).first()
        return None if row is None else row.request_id

    def clear_pending_reservation(self, reservation_id: str) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                inference_pending_changes.delete().where(
                    inference_pending_changes.c.reservation_id == reservation_id
                )
            )
        return result.rowcount == 1

    def terminalize_pending_approval(self, request_id: str, *, failed: bool = False) -> bool:
        """Atomically clear and audit one rejected/expired lifecycle approval."""
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(inference_pending_changes).where(
                    inference_pending_changes.c.request_id == request_id
                )
            ).first()
            if row is None:
                return False
            deleted = conn.execute(
                inference_pending_changes.delete().where(
                    inference_pending_changes.c.reservation_id == row.reservation_id,
                    inference_pending_changes.c.request_id == request_id,
                )
            )
            if deleted.rowcount != 1:
                return False
            self.audit_refused(
                actor=str(row.requested_by),
                target_id=str(row.target_id),
                desired_state=str(row.desired_state),
                failed=failed,
                conn=conn,
            )
        return True

    def renew_owned_pending_reservations(self) -> None:
        """Heartbeat only this replica's live pre-execution reservations."""
        now = self._now()
        with self._engine.begin() as conn:
            conn.execute(
                inference_pending_changes.update()
                .where(
                    inference_pending_changes.c.owner_instance_id == self._instance_id,
                    inference_pending_changes.c.request_id.is_not(None),
                )
                .values(expires_at=now + timedelta(seconds=self.config.pending_reservation_ttl_s))
            )

    async def recover_expired_pending_approvals(self, approval_gate: Any) -> None:
        """Terminalize approvals whose owning API replica stopped heartbeating.

        Host mutation is never replayed. A durable CAS lease lets exactly one
        replica claim each orphan, reject it through the real approval gate,
        record lifecycle refusal/failure evidence, and release the target.
        """
        now = self._now()
        with self._engine.connect() as conn:
            expired = conn.execute(
                sa.select(inference_pending_changes).where(
                    inference_pending_changes.c.request_id.is_not(None),
                    inference_pending_changes.c.expires_at <= now,
                )
            ).all()
        for row in expired:
            with self._engine.begin() as conn:
                claimed = conn.execute(
                    inference_pending_changes.update()
                    .where(
                        inference_pending_changes.c.reservation_id == row.reservation_id,
                        inference_pending_changes.c.request_id == row.request_id,
                        inference_pending_changes.c.expires_at <= now,
                    )
                    .values(
                        owner_instance_id=self._instance_id,
                        expires_at=now + timedelta(seconds=self.config.pending_reservation_ttl_s),
                    )
                )
            if claimed.rowcount != 1:
                continue
            try:
                prior = self.idempotent_retry_state(
                    target_id=str(row.target_id),
                    desired_state=str(row.desired_state),
                    expected_version=int(row.expected_version),
                    idempotency_key=str(row.idempotency_key),
                )
            except LifecycleConflict:
                prior = None
            if prior is not None and prior.state not in (
                InferenceTargetState.STARTING,
                InferenceTargetState.STOPPING,
            ):
                # The governed handler durably finished and only the route's
                # reservation cleanup was lost. Preserve that exact terminal
                # result and release the stale display/single-flight record;
                # never fence, audit, or replay a completed host mutation.
                self.clear_pending_reservation(str(row.reservation_id))
                continue
            # Fence the expired owner before awaiting the approval store. A
            # waiter that wakes on an already-approved request still carries
            # the pre-approval expected version and therefore cannot reach the
            # host mutation CAS after this durable version advance.
            current = self.state(str(row.target_id))
            fenced = self._mark_target_if_current(
                current,
                InferenceOperatorCode.INTERRUPTED,
                state=InferenceTargetState.ERROR,
                advance_version=True,
            )
            if not fenced:
                continue
            failed = False
            try:
                await approval_gate.reject(
                    str(row.request_id),
                    approver="system:inference-lifecycle",
                    reason="owning API replica stopped before lifecycle execution",
                )
            except (ApprovalAlreadyDecidedError, ApprovalRequestNotFoundError):
                # The durable approval may already be terminal (including an
                # approval landed after the waiter died). Never replay the host
                # mutation; preserve that gate truth and record lifecycle failure.
                failed = True
            self.terminalize_pending_approval(str(row.request_id), failed=failed)

    def pending_changes(self, active_request_ids: set[str]) -> dict[str, Mapping[str, str]]:
        with self._engine.connect() as conn:
            if not active_request_ids:
                return {}
            rows = conn.execute(
                sa.select(inference_pending_changes).where(
                    inference_pending_changes.c.request_id.in_(active_request_ids)
                )
            ).all()
        return {
            str(row.target_id): {
                "request_id": str(row.request_id),
                "requested_by": str(row.requested_by),
                "desired_state": str(row.desired_state),
            }
            for row in rows
        }

    def _audit_lifecycle(
        self,
        *,
        action: str,
        outcome: AuditOutcome,
        actor: str,
        target_id: str,
        transition_id: str | None,
        old_state: InferenceTargetState | None,
        desired_state: InferenceTargetState | None,
        observed_state: InferenceTargetState | None,
        code: InferenceOperatorCode | None,
        manifest_hash: str | None = None,
        conn: sa.Connection | None = None,
    ) -> None:
        detail = {
            "target_id": target_id,
            "transition_id": transition_id,
            "old_state": old_state.value if old_state is not None else None,
            "desired_state": desired_state.value if desired_state is not None else None,
            "observed_state": observed_state.value if observed_state is not None else None,
            "code": code.value if code is not None else None,
        }
        provenance = {"registry_hash": self.config.registry_hash}
        if manifest_hash is not None:
            provenance["manifest_hash"] = manifest_hash
        event = new_audit_event(
            action=action,
            resource=f"inference-target:{target_id}",
            outcome=outcome,
            actor=actor,
            detail=json.dumps(detail),
            provenance=provenance,
            config_hash=self._config_hash,
            occurred_at=self._now(),
        )
        if conn is not None:
            append_record(conn, event)
        else:
            with self._engine.begin() as owned_conn:
                append_record(owned_conn, event)

    def audit_requested(self, *, actor: str, target_id: str, desired_state: str) -> None:
        old = self.state(target_id) if target_id in self._targets else None
        desired = (
            InferenceTargetState(desired_state) if desired_state in ("ready", "stopped") else None
        )
        self._audit_lifecycle(
            action="inference.lifecycle.requested",
            outcome=AuditOutcome.success,
            actor=actor,
            target_id=target_id,
            transition_id=old.transition_id if old is not None else None,
            old_state=old.state if old is not None else None,
            desired_state=desired,
            observed_state=old.state if old is not None else None,
            code=old.code if old is not None else None,
            manifest_hash=old.manifest_hash if old is not None else None,
        )

    def audit_refused(
        self,
        *,
        actor: str,
        target_id: str,
        desired_state: str,
        failed: bool = False,
        conn: sa.Connection | None = None,
    ) -> None:
        old = self.state(target_id) if target_id in self._targets else None
        desired = (
            InferenceTargetState(desired_state) if desired_state in ("ready", "stopped") else None
        )
        self._audit_lifecycle(
            action="inference.lifecycle.failed" if failed else "inference.lifecycle.refused",
            outcome=AuditOutcome.error if failed else AuditOutcome.denied,
            actor=actor,
            target_id=target_id,
            transition_id=old.transition_id if old is not None else None,
            old_state=old.state if old is not None else None,
            desired_state=desired,
            observed_state=old.state if old is not None else None,
            code=old.code if old is not None else None,
            manifest_hash=old.manifest_hash if old is not None else None,
            conn=conn,
        )

    def issue_stop_confirmation(
        self, *, target_id: str, principal_id: str, expected_version: int
    ) -> tuple[str, datetime]:
        row = self.state(target_id)
        if row.version != expected_version:
            raise LifecycleConflict("target version changed; refresh before confirming stop")
        raw = secrets.token_urlsafe(32)
        expires = self._now() + timedelta(seconds=self.config.confirmation_ttl_s)
        with self._engine.begin() as conn:
            conn.execute(
                inference_confirmations.insert().values(
                    token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                    target_id=target_id,
                    principal_id=principal_id,
                    expected_version=expected_version,
                    expires_at=expires,
                    used_at=None,
                )
            )
        return raw, expires

    def validate_stop_confirmation(
        self,
        *,
        token: str | None,
        target_id: str,
        principal_id: str,
        expected_version: int,
    ) -> None:
        """Validate a stop nonce without consuming it before HITL."""
        if token is None:
            raise LifecycleConfirmationRefused("stop requires a confirmation token")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(inference_confirmations.c.token_hash).where(
                    inference_confirmations.c.token_hash == token_hash,
                    inference_confirmations.c.target_id == target_id,
                    inference_confirmations.c.principal_id == principal_id,
                    inference_confirmations.c.expected_version == expected_version,
                    inference_confirmations.c.used_at.is_(None),
                    inference_confirmations.c.expires_at >= self._now(),
                )
            ).first()
        if row is None:
            raise LifecycleConfirmationRefused(
                "stop confirmation is invalid, expired, stale, or already used"
            )

    def preflight(
        self,
        *,
        target_id: str,
        desired_state: str,
        expected_version: int,
        idempotency_key: str | None = None,
        require_verified_manifest: bool = True,
    ) -> None:
        target = self.target(target_id)
        desired = InferenceTargetState(desired_state)
        operation = (
            InferenceTargetOperation.START
            if desired is InferenceTargetState.READY
            else InferenceTargetOperation.STOP
        )
        if desired not in (InferenceTargetState.READY, InferenceTargetState.STOPPED):
            raise LifecycleConflict("desired_state must be ready or stopped")
        if operation not in target.capabilities:
            raise LifecycleConflict("target does not support requested transition")
        row = self.state(target_id)
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "target_id": target_id,
                    "desired_state": desired_state,
                    "expected_version": expected_version,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if idempotency_key is not None and row.idempotency_key == idempotency_key:
            if row.request_hash != request_hash:
                raise LifecycleConflict("idempotency key was already used for a different request")
            return
        if row.state is desired:
            if not self._terminal_proof_is_current(target, row):
                raise LifecycleUnavailable("terminal target state is not currently proven")
            return
        if row.version != expected_version:
            raise LifecycleConflict("target version changed; refresh and retry")
        if row.cooldown_until is not None and row.cooldown_until > self._now():
            raise LifecycleConflict("target transition cooldown is active")
        if row.state in (InferenceTargetState.STARTING, InferenceTargetState.STOPPING):
            raise LifecycleConflict("target already has a transition in flight")
        if target.provider_control and self._air_gap():
            raise LifecycleUnavailable(
                "provider-controlled target is unavailable while AIR_GAP is engaged"
            )
        if not self.transport_available:
            raise LifecycleUnavailable("inference operator transport is unavailable")
        if require_verified_manifest and self._handshake_hash is None:
            raise LifecycleUnavailable("inference operator manifest is not currently verified")

    def idempotent_retry_state(
        self,
        *,
        target_id: str,
        desired_state: str,
        expected_version: int,
        idempotency_key: str,
    ) -> LifecycleState | None:
        """Return an exact prior request without admitting another mutation."""
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "target_id": target_id,
                    "desired_state": desired_state,
                    "expected_version": expected_version,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self._engine.connect() as conn:
            prior = conn.execute(
                sa.select(inference_transition_idempotency).where(
                    inference_transition_idempotency.c.target_id == target_id,
                    inference_transition_idempotency.c.idempotency_key == idempotency_key,
                )
            ).first()
        if prior is None:
            return None
        if prior.request_hash != request_hash:
            raise LifecycleConflict("idempotency key was already used for a different request")
        return self._idempotency_state_from_row(prior)

    def already_proven_state(
        self,
        *,
        target_id: str,
        desired_state: str,
        actor: str,
    ) -> LifecycleState | None:
        """Return a current proven terminal state without governance or mutation."""
        desired = InferenceTargetState(desired_state)
        row = self.state(target_id)
        if row.state is not desired:
            return None
        target = self.target(target_id)
        if not self._terminal_proof_is_current(target, row):
            raise LifecycleUnavailable("terminal target state is not currently proven")
        self._audit_lifecycle(
            action="inference.lifecycle.completed",
            outcome=AuditOutcome.success,
            actor=actor,
            target_id=target_id,
            transition_id=row.transition_id,
            old_state=row.state,
            desired_state=desired,
            observed_state=row.state,
            code=InferenceOperatorCode.ALREADY_PROVEN,
            manifest_hash=row.manifest_hash,
        )
        return row

    def _idempotency_state_from_row(self, prior: sa.Row) -> LifecycleState:
        observed_at = prior.observed_at
        updated_at = prior.updated_at
        cooldown_until = prior.cooldown_until
        if observed_at is not None and observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        if cooldown_until is not None and cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=UTC)
        return LifecycleState(
            target_id=str(prior.target_id),
            state=InferenceTargetState(prior.state),
            desired_state=InferenceTargetState(prior.desired_state),
            version=int(prior.version),
            transition_id=str(prior.transition_id),
            code=InferenceOperatorCode(prior.code),
            manifest_hash=prior.manifest_hash,
            observed_at=observed_at,
            updated_at=updated_at,
            cooldown_until=cooldown_until,
            idempotency_key=str(prior.idempotency_key),
            request_hash=str(prior.request_hash),
        )

    def _consume_confirmation(
        self,
        conn: sa.Connection,
        *,
        token: str,
        target_id: str,
        principal_id: str,
        version: int,
        now: datetime,
    ) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        result = conn.execute(
            inference_confirmations.update()
            .where(
                inference_confirmations.c.token_hash == token_hash,
                inference_confirmations.c.target_id == target_id,
                inference_confirmations.c.principal_id == principal_id,
                inference_confirmations.c.expected_version == version,
                inference_confirmations.c.used_at.is_(None),
                inference_confirmations.c.expires_at >= now,
            )
            .values(used_at=now)
        )
        if result.rowcount != 1:
            raise LifecycleConfirmationRefused(
                "stop confirmation is invalid, expired, stale, or already used"
            )

    @staticmethod
    def _observation_matches(
        observation: InferenceOperatorObservation,
        *,
        target: InferenceTargetConfig,
        operation: InferenceTargetOperation,
        transition_id: str | None,
        manifest_hash: str,
    ) -> bool:
        return (
            observation.target_id == target.target_id
            and observation.adapter_id == target.adapter_id
            and observation.resource_id == target.resource_id
            and observation.operation is operation
            and observation.transition_id == transition_id
            and observation.manifest_hash == manifest_hash
        )

    @staticmethod
    def _transition_observation_is_coherent(
        observation: InferenceOperatorObservation,
        *,
        desired: InferenceTargetState,
    ) -> bool:
        if observation.code is InferenceOperatorCode.ACCEPTED:
            return observation.state in (
                InferenceTargetState.STARTING,
                InferenceTargetState.STOPPING,
            )
        if observation.code in (
            InferenceOperatorCode.OBSERVED,
            InferenceOperatorCode.ALREADY_PROVEN,
        ):
            return observation.state is desired
        return observation.state in (
            InferenceTargetState.ERROR,
            InferenceTargetState.UNKNOWN,
        )

    async def transition(
        self,
        *,
        target_id: str,
        desired_state: str,
        expected_version: int,
        idempotency_key: str,
        confirmation_token: str | None,
        actor: str,
    ) -> LifecycleState:
        prior = self.idempotent_retry_state(
            target_id=target_id,
            desired_state=desired_state,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        if prior is not None:
            if prior.state in (InferenceTargetState.ERROR, InferenceTargetState.UNKNOWN):
                raise LifecycleUnavailable("the prior idempotent transition failed")
            return prior
        try:
            self.preflight(
                target_id=target_id,
                desired_state=desired_state,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                require_verified_manifest=False,
            )
        except ActionValidationError:
            self.audit_refused(
                actor=actor,
                target_id=target_id,
                desired_state=desired_state,
            )
            raise
        target = self.target(target_id)
        desired = InferenceTargetState(desired_state)
        operation = (
            InferenceTargetOperation.START
            if desired is InferenceTargetState.READY
            else InferenceTargetOperation.STOP
        )
        deadline = (
            target.start_deadline_s
            if operation is InferenceTargetOperation.START
            else target.stop_deadline_s
        )
        deadline_at = anyio.current_time() + deadline
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "target_id": target_id,
                    "desired_state": desired_state,
                    "expected_version": expected_version,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        existing = self.state(target_id)
        if existing.idempotency_key == idempotency_key:
            if existing.request_hash != request_hash:
                raise LifecycleConflict("idempotency key was already used for a different request")
            if existing.state in (InferenceTargetState.ERROR, InferenceTargetState.UNKNOWN):
                raise LifecycleUnavailable("the prior idempotent transition failed")
            return existing
        if existing.state is desired:
            if not self._terminal_proof_is_current(target, existing):
                raise LifecycleUnavailable("terminal target state is not currently proven")
            return existing
        try:
            await self.handshake(deadline_at=deadline_at)
        except TimeoutError as exc:
            self._mark_target_if_current(existing, InferenceOperatorCode.DEADLINE_EXCEEDED)
            self.audit_refused(
                actor=actor,
                target_id=target_id,
                desired_state=desired_state,
                failed=True,
            )
            raise LifecycleUnavailable("operator handshake deadline exceeded") from exc
        except LifecycleUnavailable:
            self._mark_target_if_current(existing, InferenceOperatorCode.TRANSPORT_UNAVAILABLE)
            self.audit_refused(
                actor=actor,
                target_id=target_id,
                desired_state=desired_state,
                failed=True,
            )
            raise
        operator = self._operator
        if operator is None:  # narrowed after the fail-closed handshake
            raise LifecycleUnavailable("inference operator transport is unavailable")
        now = self._now()
        operation_lease_until = now + timedelta(seconds=self._remaining(deadline_at))
        transition_id = secrets.token_hex(16)
        transitional = (
            InferenceTargetState.STARTING
            if operation is InferenceTargetOperation.START
            else InferenceTargetState.STOPPING
        )
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(inference_target_state)
                .where(inference_target_state.c.target_id == target_id)
                .with_for_update()
            ).one()
            prior_row = conn.execute(
                sa.select(inference_transition_idempotency).where(
                    inference_transition_idempotency.c.target_id == target_id,
                    inference_transition_idempotency.c.idempotency_key == idempotency_key,
                )
            ).first()
            if prior_row is not None:
                if prior_row.request_hash != request_hash:
                    raise LifecycleConflict(
                        "idempotency key was already used for a different request"
                    )
                prior_state = InferenceTargetState(prior_row.state)
                if prior_state in (InferenceTargetState.ERROR, InferenceTargetState.UNKNOWN):
                    raise LifecycleUnavailable("the prior idempotent transition failed")
                return self._idempotency_state_from_row(prior_row)
            if InferenceTargetState(row.state) is desired:
                return self._state_from_row(row)
            if operation is InferenceTargetOperation.STOP:
                if confirmation_token is None:
                    raise LifecycleConfirmationRefused("stop requires a confirmation token")
                self._consume_confirmation(
                    conn,
                    token=confirmation_token,
                    target_id=target_id,
                    principal_id=actor,
                    version=expected_version,
                    now=now,
                )
            result = conn.execute(
                inference_target_state.update()
                .where(
                    inference_target_state.c.target_id == target_id,
                    inference_target_state.c.version == expected_version,
                )
                .values(
                    state=transitional.value,
                    desired_state=desired.value,
                    version=expected_version + 1,
                    transition_id=transition_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    code=InferenceOperatorCode.ACCEPTED.value,
                    updated_at=now,
                    actor=actor,
                    operation_lease_until=operation_lease_until,
                )
            )
            if result.rowcount != 1:
                raise LifecycleConflict("target changed concurrently; refresh and retry")
            conn.execute(
                inference_transition_idempotency.insert().values(
                    target_id=target_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    transition_id=transition_id,
                    state=transitional.value,
                    desired_state=desired.value,
                    code=InferenceOperatorCode.ACCEPTED.value,
                    version=expected_version + 1,
                    manifest_hash=self._handshake_hash,
                    observed_at=None,
                    cooldown_until=None,
                    updated_at=now,
                )
            )
            append_record(
                conn,
                new_audit_event(
                    action="inference.lifecycle.started",
                    resource=f"inference-target:{target_id}",
                    outcome=AuditOutcome.success,
                    actor=actor,
                    detail=json.dumps(
                        {
                            "target_id": target_id,
                            "old_state": existing.state.value,
                            "desired_state": desired.value,
                            "observed_state": transitional.value,
                            "code": InferenceOperatorCode.ACCEPTED.value,
                            "version": expected_version + 1,
                            "transition_id": transition_id,
                        }
                    ),
                    provenance={
                        "registry_hash": self.config.registry_hash,
                        "manifest_hash": self._handshake_hash or "0" * 64,
                    },
                    config_hash=self._config_hash,
                    occurred_at=now,
                ),
            )
        cancelled: BaseException | None = None
        try:
            with anyio.fail_after(self._remaining(deadline_at)):
                if operation is InferenceTargetOperation.START:
                    observation = await operator.start(
                        InferenceStartRequest(target_id=target_id, transition_id=transition_id)
                    )
                else:
                    observation = await operator.stop(
                        InferenceStopRequest(target_id=target_id, transition_id=transition_id)
                    )
        except TimeoutError:
            observation = InferenceOperatorObservation(
                target_id=target_id,
                adapter_id=target.adapter_id,
                resource_id=target.resource_id,
                operation=operation,
                transition_id=transition_id,
                state=InferenceTargetState.ERROR,
                code=InferenceOperatorCode.DEADLINE_EXCEEDED,
                manifest_hash=self._handshake_hash or "0" * 64,
                observed_at=self._now(),
            )
        except anyio.get_cancelled_exc_class() as exc:
            cancelled = exc
            observation = InferenceOperatorObservation(
                target_id=target_id,
                adapter_id=target.adapter_id,
                resource_id=target.resource_id,
                operation=operation,
                transition_id=transition_id,
                state=InferenceTargetState.ERROR,
                code=InferenceOperatorCode.INTERRUPTED,
                manifest_hash=self._handshake_hash or "0" * 64,
                observed_at=self._now(),
            )
        except Exception:
            observation = InferenceOperatorObservation(
                target_id=target_id,
                adapter_id=target.adapter_id,
                resource_id=target.resource_id,
                operation=operation,
                transition_id=transition_id,
                state=InferenceTargetState.ERROR,
                code=InferenceOperatorCode.TRANSPORT_UNAVAILABLE,
                manifest_hash=self._handshake_hash or "0" * 64,
                observed_at=self._now(),
            )
        invalid_observation = not (
            self._observation_matches(
                observation,
                target=target,
                operation=operation,
                transition_id=transition_id,
                manifest_hash=self._handshake_hash or "0" * 64,
            )
            and self._transition_observation_is_coherent(observation, desired=desired)
        )
        if invalid_observation:
            observation = InferenceOperatorObservation(
                target_id=target_id,
                adapter_id=target.adapter_id,
                resource_id=target.resource_id,
                operation=operation,
                transition_id=transition_id,
                state=InferenceTargetState.UNKNOWN,
                code=InferenceOperatorCode.MANIFEST_MISMATCH,
                manifest_hash=self._handshake_hash or "0" * 64,
                observed_at=self._now(),
            )
        finished = self._now()
        terminal_cooldown = (
            finished + timedelta(seconds=target.cooldown_s)
            if observation.state in (InferenceTargetState.READY, InferenceTargetState.STOPPED)
            else None
        )
        with self._engine.begin() as conn:
            result = conn.execute(
                inference_target_state.update()
                .where(
                    inference_target_state.c.target_id == target_id,
                    inference_target_state.c.transition_id == transition_id,
                    inference_target_state.c.version == expected_version + 1,
                )
                .values(
                    state=observation.state.value,
                    code=observation.code.value,
                    manifest_hash=observation.manifest_hash,
                    observed_at=observation.observed_at,
                    cooldown_until=terminal_cooldown,
                    operation_lease_until=None,
                    updated_at=finished,
                )
            )
            if result.rowcount != 1:
                raise LifecycleConflict("transition result lost its durable compare-and-swap")
            conn.execute(
                inference_transition_idempotency.update()
                .where(
                    inference_transition_idempotency.c.target_id == target_id,
                    inference_transition_idempotency.c.idempotency_key == idempotency_key,
                    inference_transition_idempotency.c.transition_id == transition_id,
                )
                .values(
                    state=observation.state.value,
                    code=observation.code.value,
                    manifest_hash=observation.manifest_hash,
                    observed_at=observation.observed_at,
                    cooldown_until=terminal_cooldown,
                    updated_at=finished,
                )
            )
            if observation.state not in (
                InferenceTargetState.STARTING,
                InferenceTargetState.STOPPING,
            ):
                failed = invalid_observation or observation.state in (
                    InferenceTargetState.ERROR,
                    InferenceTargetState.UNKNOWN,
                )
                append_record(
                    conn,
                    new_audit_event(
                        action=(
                            "inference.lifecycle.failed"
                            if failed
                            else "inference.lifecycle.completed"
                        ),
                        resource=f"inference-target:{target_id}",
                        outcome=AuditOutcome.error if failed else AuditOutcome.success,
                        actor=actor,
                        detail=json.dumps(
                            {
                                "target_id": target_id,
                                "old_state": existing.state.value,
                                "desired_state": desired.value,
                                "observed_state": observation.state.value,
                                "code": observation.code.value,
                                "version": expected_version + 1,
                                "transition_id": transition_id,
                            }
                        ),
                        provenance={
                            "registry_hash": self.config.registry_hash,
                            "manifest_hash": observation.manifest_hash,
                        },
                        config_hash=self._config_hash,
                        occurred_at=finished,
                    ),
                )
        if cancelled is not None:
            raise cancelled
        if invalid_observation:
            raise LifecycleUnavailable("operator observation does not match requested transition")
        if observation.state in (InferenceTargetState.ERROR, InferenceTargetState.UNKNOWN):
            raise LifecycleUnavailable("operator did not prove the requested transition")
        return self.state(target_id)

    async def reconcile(self, target_id: str) -> LifecycleState:
        target = self.target(target_id)
        row = self.state(target_id)
        with self._engine.connect() as conn:
            lease_row = conn.execute(
                sa.select(
                    inference_target_state.c.operation_lease_until,
                    inference_target_state.c.actor,
                ).where(*self._same_transition(row))
            ).first()
        operation_lease_until = None if lease_row is None else lease_row.operation_lease_until
        transition_actor = None if lease_row is None else lease_row.actor
        if operation_lease_until is not None and operation_lease_until.tzinfo is None:
            operation_lease_until = operation_lease_until.replace(tzinfo=UTC)
        if operation_lease_until is not None and operation_lease_until > self._now():
            return row
        if target.provider_control and self._air_gap():
            self._mark_target_if_current(row, InferenceOperatorCode.AIR_GAP_REFUSED)
            return self.state(target_id)
        deadline_at = anyio.current_time() + target.status_deadline_s
        try:
            manifest_hash = await self.handshake(deadline_at=deadline_at)
            operator = self._operator
            if operator is None:  # narrowed after the fail-closed handshake
                raise LifecycleUnavailable("inference operator transport is unavailable")
            with anyio.fail_after(self._remaining(deadline_at)):
                observation = await operator.status(
                    InferenceStatusRequest(target_id=target_id, transition_id=row.transition_id)
                )
        except anyio.get_cancelled_exc_class():
            self._mark_target_if_current(
                row,
                InferenceOperatorCode.INTERRUPTED,
                state=InferenceTargetState.ERROR,
            )
            raise
        except TimeoutError as exc:
            self._mark_target_if_current(
                row,
                InferenceOperatorCode.DEADLINE_EXCEEDED,
                state=InferenceTargetState.ERROR,
            )
            raise LifecycleUnavailable("operator status deadline exceeded") from exc
        except LifecycleUnavailable:
            self._mark_target_if_current(row, InferenceOperatorCode.TRANSPORT_UNAVAILABLE)
            raise
        except Exception as exc:
            self._mark_target_if_current(row, InferenceOperatorCode.TRANSPORT_UNAVAILABLE)
            raise LifecycleUnavailable("operator status is unavailable") from exc
        if not self._observation_matches(
            observation,
            target=target,
            operation=InferenceTargetOperation.STATUS,
            transition_id=row.transition_id,
            manifest_hash=manifest_hash,
        ):
            self._mark_target_if_current(row, InferenceOperatorCode.MANIFEST_MISMATCH)
            raise LifecycleUnavailable("operator status does not match boot registry")
        now = self._now()
        persisted_state = observation.state
        persisted_code = observation.code
        if (
            row.state in (InferenceTargetState.STARTING, InferenceTargetState.STOPPING)
            and observation.state in (InferenceTargetState.READY, InferenceTargetState.STOPPED)
            and observation.state is not row.desired_state
        ):
            # After a replica crashes between the durable STARTING/STOPPING CAS
            # and the host call, STATUS can truthfully report the unchanged
            # opposite terminal state. That is a failed transition, not a
            # successful idempotent result.
            persisted_state = InferenceTargetState.ERROR
            persisted_code = InferenceOperatorCode.INTERRUPTED
        changed = persisted_state is not row.state or persisted_code is not row.code
        next_version = row.version + 1 if changed else row.version
        terminal_cooldown = row.cooldown_until
        if row.state in (InferenceTargetState.STARTING, InferenceTargetState.STOPPING):
            terminal_cooldown = (
                now + timedelta(seconds=target.cooldown_s)
                if persisted_state in (InferenceTargetState.READY, InferenceTargetState.STOPPED)
                else None
            )
        with self._engine.begin() as conn:
            result = conn.execute(
                inference_target_state.update()
                .where(*self._same_transition(row))
                .values(
                    state=persisted_state.value,
                    code=persisted_code.value,
                    manifest_hash=observation.manifest_hash,
                    observed_at=observation.observed_at,
                    cooldown_until=terminal_cooldown,
                    operation_lease_until=None,
                    updated_at=now,
                    version=next_version,
                )
            )
            if (
                result.rowcount == 1
                and row.state in (InferenceTargetState.STARTING, InferenceTargetState.STOPPING)
                and row.transition_id is not None
                and row.idempotency_key is not None
                and row.request_hash is not None
            ):
                conn.execute(
                    inference_transition_idempotency.update()
                    .where(
                        inference_transition_idempotency.c.target_id == row.target_id,
                        inference_transition_idempotency.c.transition_id == row.transition_id,
                        inference_transition_idempotency.c.idempotency_key == row.idempotency_key,
                        inference_transition_idempotency.c.request_hash == row.request_hash,
                        inference_transition_idempotency.c.version == row.version,
                    )
                    .values(
                        state=persisted_state.value,
                        code=persisted_code.value,
                        version=next_version,
                        manifest_hash=observation.manifest_hash,
                        observed_at=observation.observed_at,
                        cooldown_until=terminal_cooldown,
                        updated_at=now,
                    )
                )
                if persisted_state not in (
                    InferenceTargetState.STARTING,
                    InferenceTargetState.STOPPING,
                ):
                    failed = persisted_state in (
                        InferenceTargetState.ERROR,
                        InferenceTargetState.UNKNOWN,
                    )
                    append_record(
                        conn,
                        new_audit_event(
                            action=(
                                "inference.lifecycle.failed"
                                if failed
                                else "inference.lifecycle.completed"
                            ),
                            resource=f"inference-target:{row.target_id}",
                            outcome=AuditOutcome.error if failed else AuditOutcome.success,
                            actor=(
                                str(transition_actor)
                                if transition_actor is not None
                                else "system:inference-reconciler"
                            ),
                            detail=json.dumps(
                                {
                                    "target_id": row.target_id,
                                    "old_state": row.state.value,
                                    "desired_state": row.desired_state.value,
                                    "observed_state": persisted_state.value,
                                    "code": persisted_code.value,
                                    "version": next_version,
                                    "transition_id": row.transition_id,
                                }
                            ),
                            provenance={
                                "registry_hash": self.config.registry_hash,
                                "manifest_hash": observation.manifest_hash,
                            },
                            config_hash=self._config_hash,
                            occurred_at=now,
                        ),
                    )
        if result.rowcount != 1:
            return self.state(target_id)
        return self.state(target_id)

    async def reconcile_due(self) -> tuple[LifecycleState, ...]:
        """Reconcile only stale targets, using STATUS and never replaying a mutation."""
        now = self._now()
        out: list[LifecycleState] = []
        for row in self.states():
            target = self.target(row.target_id)
            age_s = None if row.observed_at is None else (now - row.observed_at).total_seconds()
            due = age_s is None or age_s < 0 or age_s >= target.reconcile_cadence_s
            if not due:
                out.append(row)
                continue
            try:
                out.append(await self.reconcile(row.target_id))
            except LifecycleUnavailable:
                out.append(self.state(row.target_id))
        return tuple(out)

    async def run_reconciler(self, *, approval_gate: Any | None = None) -> None:
        """Continuously converge stale durable state through STATUS-only reads."""
        if not self.config.targets:
            return
        cadence = min(
            min(target.reconcile_cadence_s for target in self.config.targets),
            self.config.pending_reservation_ttl_s / 3,
        )
        while True:
            try:
                self.renew_owned_pending_reservations()
                if approval_gate is not None:
                    await self.recover_expired_pending_approvals(approval_gate)
                await self.reconcile_due()
            except anyio.get_cancelled_exc_class():
                raise
            except Exception:
                # One unexpected target or storage failure cannot silently kill the
                # production convergence task. Target-scoped normal failures are
                # already persisted by ``reconcile`` and consumed by ``reconcile_due``.
                pass
            await anyio.sleep(cadence)

    async def aclose(self) -> None:
        close = getattr(self._operator, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


class _LifecycleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_id: SafeInferenceId
    desired_state: Literal["ready", "stopped"]
    expected_version: int = Field(ge=0)
    idempotency_key: SafeInferenceId
    confirmation_token: str | None = Field(default=None, min_length=16, max_length=128)


def build_inference_lifecycle_binding(
    service: InferenceLifecycleService, principal_provider: Callable[[], Principal | None]
) -> ToolBinding:
    def _parse(args: Mapping[str, object]) -> _LifecycleArgs:
        try:
            return _LifecycleArgs.model_validate(dict(args))
        except Exception as exc:
            raise LifecycleConflict("invalid lifecycle transition request") from exc

    def _validate(args: Mapping[str, object]) -> None:
        principal = current_inference_lifecycle_request_principal()
        if principal is None:
            raise LifecycleConfirmationRefused(
                "no verified request principal bound to lifecycle validation"
            )
        parsed = _parse(args)
        target_id = str(parsed.target_id)
        desired_state = parsed.desired_state
        try:
            service.preflight(
                target_id=parsed.target_id,
                desired_state=parsed.desired_state,
                expected_version=parsed.expected_version,
                idempotency_key=parsed.idempotency_key,
            )
            if parsed.desired_state == "stopped":
                service.validate_stop_confirmation(
                    token=parsed.confirmation_token,
                    target_id=parsed.target_id,
                    principal_id=principal.id,
                    expected_version=parsed.expected_version,
                )
        except ActionValidationError:
            service.audit_refused(
                actor=principal.id,
                target_id=target_id,
                desired_state=desired_state,
            )
            raise

    async def _handler(args: Mapping[str, object]) -> str:
        parsed = _parse(args)
        principal = principal_provider()
        if principal is None:
            raise LifecycleConflict("no verified principal bound to lifecycle action")
        request_principal = current_inference_lifecycle_request_principal()
        if request_principal is None or request_principal.id != principal.id:
            raise LifecycleConflict("lifecycle request principal changed before execution")
        row = await service.transition(
            target_id=parsed.target_id,
            desired_state=parsed.desired_state,
            expected_version=parsed.expected_version,
            idempotency_key=parsed.idempotency_key,
            confirmation_token=parsed.confirmation_token,
            actor=principal.id,
        )
        return json.dumps(
            {
                "target_id": row.target_id,
                "state": row.state.value,
                "version": row.version,
                "code": row.code.value,
            }
        )

    return ToolBinding(
        spec=ToolSpec(
            name=LIFECYCLE_ACTION,
            description=(
                "Start or stop one boot-registered inference target through the "
                "confined host operator."
            ),
            tier=ApprovalTier.TIER_2,
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "desired_state": {"enum": ["ready", "stopped"]},
                    "expected_version": {"type": "integer"},
                    "idempotency_key": {"type": "string"},
                    "confirmation_token": {"type": "string"},
                },
                "required": ["target_id", "desired_state", "expected_version", "idempotency_key"],
            },
        ),
        handler=_handler,
        validate_args=_validate,
        extract_audit_detail=lambda args, result: {
            "target_id": str(args.get("target_id", "")),
            "desired_state": str(args.get("desired_state", "")),
            "expected_version": str(args.get("expected_version", "")),
        },
    )
