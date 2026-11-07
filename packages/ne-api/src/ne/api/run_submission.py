# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed API boundary for accepted canonical Run submissions.

The :class:`ne.runtime.run_store.AgentRunStore` implements the transaction
contract in this module. Sequentially calling its older
``create_run``/``transition``/``link_attempt`` methods would leave a Run without
its idempotency claim, exact target or enqueue outbox after a process failure;
the production routes therefore require this all-or-none seam and never synthesize
it from those primitives.

Everything before that transaction is still real: the authenticated principal,
current visible Agent definition, boot-frozen platform configuration and live
PRE-011E capability surface are revalidated for every request.  The transaction
command carries those exact values so the store can compare them with the
Conversation/Run row under the same lock that claims the idempotency key and
inserts the content-minimal outbox record.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from ne.api.agent_catalog import AgentCatalog
from ne.api.agent_manifest import ExecutionStrategy
from ne.api.agent_preflight import AgentCapabilityPreflightService
from ne.api.model_selection import (
    AuthorizedModelSelection,
    ModelSelectionError,
    ModelSelectionService,
)
from ne.api.run_credential_floor import (
    credential_too_short_detail,
    run_credential_floor_seconds,
)
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.types import Lane, Principal
from ne.runtime.agent import AgentExecutionBinding
from ne.runtime.correlation import (
    current_run_requested_model,
    current_run_requested_model_lane,
)
from ne.runtime.run_store import RunSubmissionConflictError, RunSubmissionNotFoundError
from ne.runtime.source_results import require_authenticated_context
from ne.runtime.workflow_execution_profile import WorkflowExecutionProfile

__all__ = [
    "RUN_SUBMISSION_STATE_KEY",
    "AcceptedRunSubmission",
    "CancelRunCommand",
    "RetryRunCommand",
    "RunCredentialTooShort",
    "RunSubmissionConflict",
    "RunSubmissionNotFound",
    "RunSubmissionService",
    "RunSubmissionTransactions",
    "RunSubmissionUnavailable",
    "StartRunCommand",
    "compose_run_submission_service",
]

RUN_SUBMISSION_STATE_KEY = "ne_run_submission_service"

RunSubmissionAction = Literal["start", "cancel", "retry"]
RunSubmissionStatus = Literal["submitted", "queued", "cancelling"]


class RunSubmissionNotFound(RuntimeError):
    """The principal-owned target or current snapshot was not found."""


class RunSubmissionConflict(RuntimeError):
    """An idempotency, version, configuration or lifecycle check conflicted."""


class RunCredentialTooShort(RunSubmissionConflict):
    """The submitting bearer cannot outlast the Run it is asking to start.

    A ``RunSubmissionConflict`` by inheritance ON PURPOSE: any handler that has
    not been taught about this case still REFUSES, which is the fail-closed
    direction. Handlers that have been taught about it catch this class FIRST
    and answer 401 with :attr:`detail` rather than the generic opaque 409 --
    the caller's remedy is to renew a credential, and a 409 tells them nothing
    they can act on.
    """

    def __init__(self, *, remaining_seconds: int, required_seconds: int) -> None:
        self.remaining_seconds = remaining_seconds
        self.required_seconds = required_seconds
        self.detail = credential_too_short_detail(
            remaining_seconds=remaining_seconds,
            required_seconds=required_seconds,
        )
        super().__init__(self.detail)


class RunSubmissionUnavailable(RuntimeError):
    """The accepted-submission transaction cannot currently be completed."""


@dataclass(frozen=True, slots=True)
class StartRunCommand:
    """One atomic Run + attempt + idempotency claim + enqueue-outbox insert."""

    idempotency_key: str
    request_hash: str
    conversation_id: str
    expected_version: int
    owner_principal_id: str
    department: str
    conversation_agent_name: str
    agent_id: str
    agent_version: int
    strategy: ExecutionStrategy
    config_hash: str
    capability_snapshot_hash: str
    capability_snapshot_receipt_id: str
    agent_definition_hash: str
    model_policy_id: str
    authorized_model: str
    authorized_model_lane: Lane
    model_authorization_receipt_hash: str
    target_input_budget: int
    input_plaintext: bytes
    authenticated_context: AuthenticatedRequestContext
    requested_model: str | None = None
    requested_model_lane: Lane | None = None


@dataclass(frozen=True, slots=True)
class CancelRunCommand:
    """One atomic owner/version check + cancel intent + control-outbox insert."""

    idempotency_key: str
    request_hash: str
    run_id: str
    expected_version: int
    owner_principal_id: str
    department: str
    agent_id: str
    agent_version: int
    strategy: ExecutionStrategy
    config_hash: str
    capability_snapshot_hash: str
    capability_snapshot_receipt_id: str


@dataclass(frozen=True, slots=True)
class RetryRunCommand:
    """One atomic owner/version check + new attempt + enqueue-outbox insert."""

    idempotency_key: str
    request_hash: str
    run_id: str
    expected_version: int
    owner_principal_id: str
    department: str
    agent_id: str
    agent_version: int
    strategy: ExecutionStrategy
    config_hash: str
    capability_snapshot_hash: str
    capability_snapshot_receipt_id: str
    authenticated_context: AuthenticatedRequestContext
    requested_model: str | None = None
    requested_model_lane: Lane | None = None


@dataclass(frozen=True, slots=True)
class AcceptedRunSubmission:
    """Safe projection returned only after the owning transaction commits."""

    action: RunSubmissionAction
    run_id: str
    attempt_id: str
    version: int
    status: RunSubmissionStatus
    enqueue_id: str
    replayed: bool


@runtime_checkable
class RunSubmissionTransactions(Protocol):
    """Runtime-owned atomic accepted-submission operations.

    Each method MUST claim-or-load ``(owner_principal_id, action,
    idempotency_key)`` and compare ``request_hash`` in the same database
    transaction as every Run/attempt/version mutation and its durable outbox
    insert.  An exact replay returns the original identifiers with
    ``replayed=True``; key reuse with a different hash raises
    :class:`RunSubmissionConflict`.  Owner mismatch and absence are the same
    :class:`RunSubmissionNotFound`.
    """

    async def admit_start(self, command: StartRunCommand) -> AcceptedRunSubmission: ...

    async def admit_cancel(self, command: CancelRunCommand) -> AcceptedRunSubmission: ...

    async def admit_retry(self, command: RetryRunCommand) -> AcceptedRunSubmission: ...


@dataclass(frozen=True, slots=True)
class _ValidatedSnapshot:
    department: str
    agent_version: int
    strategy: ExecutionStrategy
    config_hash: str
    capability_hash: str
    receipt_id: str
    definition_hash: str
    model_policy_id: str
    agent_max_input_tokens: int
    #: Seconds of credential this Run must still hold to be admitted --
    #: derived from the agent's own enforced wall clock. See
    #: :mod:`ne.api.run_credential_floor`.
    credential_floor_seconds: int
    workflow_profile: WorkflowExecutionProfile | None


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _department(principal: Principal) -> str:
    value = principal.claims.get("department")
    if not isinstance(value, str) or not value.strip():
        raise RunSubmissionConflict("principal department is unavailable")
    return value


class RunSubmissionService:
    """Revalidate request authority, then cross exactly one atomic boundary."""

    def __init__(
        self,
        *,
        transactions: RunSubmissionTransactions,
        agent_catalog: AgentCatalog,
        agent_preflight: AgentCapabilityPreflightService,
        config_hash: str,
        model_selection: ModelSelectionService | None = None,
        execution_bindings: Mapping[str, AgentExecutionBinding] | None = None,
    ) -> None:
        self._transactions = transactions
        self._agent_catalog = agent_catalog
        self._agent_preflight = agent_preflight
        self._model_selection = model_selection
        self._execution_bindings = execution_bindings
        self._config_hash = config_hash

    def _required_model_selection(self) -> ModelSelectionService:
        if self._model_selection is None:
            raise RunSubmissionConflict("requested model policy is unavailable")
        return self._model_selection

    async def _validate_snapshot(
        self,
        *,
        principal: Principal,
        agent_id: str,
        agent_version: int,
        strategy: ExecutionStrategy,
        config_hash: str,
        capability_hash: str,
    ) -> _ValidatedSnapshot:
        department = _department(principal)
        catalog_row = self._agent_catalog.get_visible(agent_id, principal)
        if catalog_row is None:
            raise RunSubmissionNotFound
        definition = catalog_row.definition
        if agent_version != definition.version:
            raise RunSubmissionConflict("Agent version changed")
        if strategy not in definition.allowed_strategies:
            raise RunSubmissionConflict("execution strategy is unavailable")
        if config_hash != self._config_hash:
            raise RunSubmissionConflict("platform configuration changed")

        preflight = await self._agent_preflight.preflight(
            agent_id=agent_id,
            principal=principal,
            presented_capability_hash=capability_hash,
        )
        if preflight is None:
            # Stale capability hashes deliberately remain byte-equivalent to a
            # hidden or unknown Agent, matching the PRE-011E read join.
            raise RunSubmissionNotFound
        capabilities = preflight.capabilities
        if (
            not catalog_row.available
            or not preflight.available
            or capabilities is None
            or preflight.agent_id != definition.agent_id
            or preflight.agent_version != definition.version
            or preflight.definition_hash != definition.definition_hash
            or capabilities.agent_version != definition.version
            or capabilities.agent_definition_hash != definition.definition_hash
            or capabilities.capability_hash != capability_hash
        ):
            raise RunSubmissionConflict("Agent capability snapshot is unavailable")

        receipt_digest = _canonical_hash(
            {
                "domain": "ne.accepted-run-capability-snapshot.v1",
                "principal_id": principal.id,
                "department": department,
                "agent_id": definition.agent_id,
                "agent_version": definition.version,
                "agent_definition_hash": definition.definition_hash,
                "strategy": strategy,
                "config_hash": config_hash,
                "capability_hash": capability_hash,
            }
        )
        binding = (
            None if self._execution_bindings is None else self._execution_bindings.get(agent_id)
        )
        if self._execution_bindings is not None and (
            binding is None
            or binding.agent_version != definition.version
            or binding.strategy.value != strategy
        ):
            raise RunSubmissionConflict("Agent execution binding is unavailable")
        if strategy == "deterministic" and binding is None:
            raise RunSubmissionConflict("deterministic workflow profile is unavailable")
        return _ValidatedSnapshot(
            department=department,
            agent_version=definition.version,
            strategy=strategy,
            config_hash=config_hash,
            capability_hash=capability_hash,
            receipt_id=f"capability:v1:{receipt_digest}",
            definition_hash=definition.definition_hash,
            model_policy_id=definition.model_policy_id,
            agent_max_input_tokens=definition.budgets.max_input_tokens,
            credential_floor_seconds=run_credential_floor_seconds(definition.budgets),
            workflow_profile=None if binding is None else binding.workflow_profile,
        )

    @staticmethod
    def _authenticated_context(
        principal: Principal,
        department: str,
        supplied: AuthenticatedRequestContext | None,
        *,
        floor_seconds: int,
    ) -> AuthenticatedRequestContext:
        """Re-verify the caller's context AND that it can outlast the Run.

        ``floor_seconds`` is the remaining-lifetime floor derived in
        :mod:`ne.api.run_credential_floor` from the agent's own enforced wall
        clock. Admission freezes ``context.expires_at`` into the Run
        (``run_store._insert_authenticated_context``) and the worker re-checks
        that frozen value before every protected disclosure, so a context that
        cannot cover the wall clock the runner will hold this Run to is one we
        already know will be killed mid-flight.

        ⭐ THE ZERO-MARGIN CHECK BELOW STAYS. It is not made redundant by the
        floor and removing it would leave the guard depending on a policy number
        for a correctness property: a floor is a deployment judgement and could
        in principle be configured to nothing, whereas "the credential has
        already expired" is never admissible. The two are separate refusals and
        the second is the one that must survive any mutation of the first.

        ⚠ EXACT FOR AGENTIC RUNS, A LOWER BOUND FOR DETERMINISTIC ONES. The
        derivation is the agentic wall clock; a deterministic workflow profile
        is not held to ``max_steps * _SECONDS_PER_STEP`` in the same way, so its
        floor is a floor rather than a proof. That is strictly more protection
        than the zero margin it replaces and is not claimed to be more.
        """

        context = require_authenticated_context() if supplied is None else supplied
        # ONE clock read for both refusals: two `time.time()` calls could
        # straddle a second boundary and report a `remaining` that disagrees
        # with the expiry check immediately above it.
        now = int(time.time())
        if (
            context.principal_id != principal.id
            or context.department != department
            or context.authorization_groups != tuple(sorted(set(principal.groups)))
            or context.expires_at <= now
        ):
            raise RunSubmissionConflict("authenticated request context changed")
        remaining = context.expires_at - now
        if remaining < floor_seconds:
            # Equality admits: a credential holding exactly the enforced wall
            # clock plus the dispatch allowance covers every instant the Run is
            # permitted to occupy, so only a STRICTLY shorter one is doomed.
            raise RunCredentialTooShort(
                remaining_seconds=remaining,
                required_seconds=floor_seconds,
            )
        return context

    async def _authorize_model(
        self,
        *,
        principal: Principal,
        snapshot: _ValidatedSnapshot,
        requested_model: str | None,
        requested_model_lane: Lane | None,
        idempotency_key: str,
    ) -> tuple[AuthorizedModelSelection, str]:
        model_selection = self._required_model_selection()
        trace_id = _canonical_hash(
            {
                "domain": "ne.agent-run-model-selection-trace.v1",
                "principal_id": principal.id,
                "idempotency_key": idempotency_key,
                "model_policy_id": snapshot.model_policy_id,
            }
        )[:32]
        try:
            if requested_model is None:
                authorized = await model_selection.authorize_auto(
                    task=snapshot.model_policy_id,
                    principal=principal,
                    trace_id=trace_id,
                )
            else:
                authorized = await model_selection.authorize(
                    task=snapshot.model_policy_id,
                    requested_model=requested_model,
                    principal=principal,
                    trace_id=trace_id,
                )
        except ModelSelectionError as exc:
            raise RunSubmissionConflict("requested model policy is unavailable") from exc
        if requested_model_lane is not None and requested_model_lane is not authorized.lane:
            raise RunSubmissionConflict("client model lane differs from server authority")
        receipt_hash = _canonical_hash(
            {
                "domain": "ne.agent-run-model-authorization.v1",
                "principal_id": principal.id,
                "department": snapshot.department,
                "agent_definition_hash": snapshot.definition_hash,
                "model_policy_id": snapshot.model_policy_id,
                "request_mode": "auto" if requested_model is None else "explicit",
                "requested_model": requested_model,
                "authorized_model": authorized.model,
                "authorized_lane": authorized.lane.value,
                "observed_at": authorized.observed_at.isoformat(),
                "policy_effect": authorized.policy_effect,
                "policy_version": authorized.policy_version,
                "config_hash": snapshot.config_hash,
            }
        )
        return authorized, receipt_hash

    async def start(
        self,
        *,
        principal: Principal,
        agent_id: str,
        conversation_id: str,
        expected_version: int,
        agent_version: int,
        strategy: ExecutionStrategy,
        config_hash: str,
        capability_hash: str,
        input_text: str,
        idempotency_key: str,
        requested_model: str | None = None,
        requested_model_lane: Lane | None = None,
        authenticated_context: AuthenticatedRequestContext | None = None,
    ) -> AcceptedRunSubmission:
        command = await self.prepare_start(
            principal=principal,
            agent_id=agent_id,
            conversation_id=conversation_id,
            expected_version=expected_version,
            agent_version=agent_version,
            strategy=strategy,
            config_hash=config_hash,
            capability_hash=capability_hash,
            input_text=input_text,
            idempotency_key=idempotency_key,
            requested_model=requested_model,
            requested_model_lane=requested_model_lane,
            authenticated_context=authenticated_context,
        )
        try:
            return await self._transactions.admit_start(command)
        except RunSubmissionNotFoundError as exc:
            raise RunSubmissionNotFound from exc
        except RunSubmissionConflictError as exc:
            raise RunSubmissionConflict from exc

    async def prepare_start(
        self,
        *,
        principal: Principal,
        agent_id: str,
        conversation_id: str,
        expected_version: int,
        agent_version: int,
        strategy: ExecutionStrategy,
        config_hash: str,
        capability_hash: str,
        input_text: str,
        idempotency_key: str,
        requested_model: str | None = None,
        requested_model_lane: Lane | None = None,
        authenticated_context: AuthenticatedRequestContext | None = None,
    ) -> StartRunCommand:
        """Revalidate and freeze one start command without creating a Run.

        PRE-010G's default-Ask activation must add the canonical turn target in
        the *same* transaction as Run admission.  Calling :meth:`start` first
        would create a pre-target Run and make that all-or-none guarantee
        impossible.  This preparation seam performs the identical current
        catalog/config/011E checks and content hashing while deliberately
        crossing no storage or enqueue boundary.
        """
        if requested_model is None:
            requested_model = current_run_requested_model()
        if requested_model_lane is None:
            requested_model_lane = current_run_requested_model_lane()
        if requested_model is None and requested_model_lane is not None:
            raise RunSubmissionConflict("requested model identity is incomplete")
        if requested_model is not None and not requested_model.strip():
            raise RunSubmissionConflict("requested model identity is incomplete")
        snapshot = await self._validate_snapshot(
            principal=principal,
            agent_id=agent_id,
            agent_version=agent_version,
            strategy=strategy,
            config_hash=config_hash,
            capability_hash=capability_hash,
        )
        context = self._authenticated_context(
            principal,
            snapshot.department,
            authenticated_context,
            floor_seconds=snapshot.credential_floor_seconds,
        )
        input_plaintext = input_text.encode("utf-8")
        if snapshot.strategy == "deterministic":
            profile = snapshot.workflow_profile
            if profile is None:
                raise RunSubmissionConflict("deterministic workflow profile is unavailable")
            try:
                payload: Any = json.loads(input_text)
                profile.validate_input(payload)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                raise RunSubmissionConflict("deterministic workflow input is invalid") from exc
        authorized, authorization_receipt_hash = await self._authorize_model(
            principal=principal,
            snapshot=snapshot,
            requested_model=requested_model,
            requested_model_lane=requested_model_lane,
            idempotency_key=idempotency_key,
        )
        request_hash = _canonical_hash(
            {
                "action": "start",
                "principal_id": principal.id,
                "department": snapshot.department,
                "conversation_id": conversation_id,
                "expected_version": expected_version,
                "agent_id": agent_id,
                "agent_version": snapshot.agent_version,
                "strategy": snapshot.strategy,
                "config_hash": snapshot.config_hash,
                "capability_snapshot_hash": snapshot.capability_hash,
                "capability_snapshot_receipt_id": snapshot.receipt_id,
                "input_hash": hashlib.sha256(input_plaintext).hexdigest(),
                "requested_model": requested_model,
                "requested_model_lane": (
                    None if requested_model is None else authorized.lane.value
                ),
                "agent_definition_hash": snapshot.definition_hash,
                "model_policy_id": snapshot.model_policy_id,
                "authorized_model": authorized.model,
                "authorized_model_lane": authorized.lane.value,
                "model_authorization_receipt_hash": authorization_receipt_hash,
                "authenticated_context_hash": context.context_hash,
            }
        )
        return StartRunCommand(
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            conversation_id=conversation_id,
            expected_version=expected_version,
            owner_principal_id=principal.id,
            department=snapshot.department,
            conversation_agent_name=agent_id,
            agent_id=agent_id,
            agent_version=snapshot.agent_version,
            strategy=snapshot.strategy,
            config_hash=snapshot.config_hash,
            capability_snapshot_hash=snapshot.capability_hash,
            capability_snapshot_receipt_id=snapshot.receipt_id,
            agent_definition_hash=snapshot.definition_hash,
            model_policy_id=snapshot.model_policy_id,
            authorized_model=authorized.model,
            authorized_model_lane=authorized.lane,
            model_authorization_receipt_hash=authorization_receipt_hash,
            target_input_budget=min(
                self._required_model_selection().input_budget(authorized.model),
                snapshot.agent_max_input_tokens,
            ),
            input_plaintext=input_plaintext,
            authenticated_context=context,
            requested_model=requested_model,
            requested_model_lane=None if requested_model is None else authorized.lane,
        )

    async def cancel(
        self,
        *,
        principal: Principal,
        agent_id: str,
        run_id: str,
        expected_version: int,
        agent_version: int,
        strategy: ExecutionStrategy,
        config_hash: str,
        capability_hash: str,
        idempotency_key: str,
    ) -> AcceptedRunSubmission:
        snapshot = await self._validate_snapshot(
            principal=principal,
            agent_id=agent_id,
            agent_version=agent_version,
            strategy=strategy,
            config_hash=config_hash,
            capability_hash=capability_hash,
        )
        request_hash = self._control_hash(
            action="cancel",
            principal=principal,
            agent_id=agent_id,
            run_id=run_id,
            expected_version=expected_version,
            snapshot=snapshot,
        )
        try:
            return await self._transactions.admit_cancel(
                CancelRunCommand(
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    run_id=run_id,
                    expected_version=expected_version,
                    owner_principal_id=principal.id,
                    department=snapshot.department,
                    agent_id=agent_id,
                    agent_version=snapshot.agent_version,
                    strategy=snapshot.strategy,
                    config_hash=snapshot.config_hash,
                    capability_snapshot_hash=snapshot.capability_hash,
                    capability_snapshot_receipt_id=snapshot.receipt_id,
                )
            )
        except RunSubmissionNotFoundError as exc:
            raise RunSubmissionNotFound from exc
        except RunSubmissionConflictError as exc:
            raise RunSubmissionConflict from exc

    async def retry(
        self,
        *,
        principal: Principal,
        agent_id: str,
        run_id: str,
        expected_version: int,
        agent_version: int,
        strategy: ExecutionStrategy,
        config_hash: str,
        capability_hash: str,
        idempotency_key: str,
        authenticated_context: AuthenticatedRequestContext | None = None,
    ) -> AcceptedRunSubmission:
        snapshot = await self._validate_snapshot(
            principal=principal,
            agent_id=agent_id,
            agent_version=agent_version,
            strategy=strategy,
            config_hash=config_hash,
            capability_hash=capability_hash,
        )
        context = self._authenticated_context(
            principal,
            snapshot.department,
            authenticated_context,
            floor_seconds=snapshot.credential_floor_seconds,
        )
        request_hash = self._control_hash(
            action="retry",
            principal=principal,
            agent_id=agent_id,
            run_id=run_id,
            expected_version=expected_version,
            snapshot=snapshot,
            authenticated_context=context,
        )
        try:
            return await self._transactions.admit_retry(
                RetryRunCommand(
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    run_id=run_id,
                    expected_version=expected_version,
                    owner_principal_id=principal.id,
                    department=snapshot.department,
                    agent_id=agent_id,
                    agent_version=snapshot.agent_version,
                    strategy=snapshot.strategy,
                    config_hash=snapshot.config_hash,
                    capability_snapshot_hash=snapshot.capability_hash,
                    capability_snapshot_receipt_id=snapshot.receipt_id,
                    authenticated_context=context,
                )
            )
        except RunSubmissionNotFoundError as exc:
            raise RunSubmissionNotFound from exc
        except RunSubmissionConflictError as exc:
            raise RunSubmissionConflict from exc

    @staticmethod
    def _control_hash(
        *,
        action: Literal["cancel", "retry"],
        principal: Principal,
        agent_id: str,
        run_id: str,
        expected_version: int,
        snapshot: _ValidatedSnapshot,
        authenticated_context: AuthenticatedRequestContext | None = None,
    ) -> str:
        return _canonical_hash(
            {
                "action": action,
                "principal_id": principal.id,
                "department": snapshot.department,
                "agent_id": agent_id,
                "run_id": run_id,
                "expected_version": expected_version,
                "agent_version": snapshot.agent_version,
                "strategy": snapshot.strategy,
                "config_hash": snapshot.config_hash,
                "capability_snapshot_hash": snapshot.capability_hash,
                "capability_snapshot_receipt_id": snapshot.receipt_id,
                "authenticated_context_hash": (
                    None if authenticated_context is None else authenticated_context.context_hash
                ),
            }
        )


def compose_run_submission_service(
    *,
    transactions: object,
    agent_catalog: AgentCatalog,
    agent_preflight: AgentCapabilityPreflightService | None,
    model_selection: ModelSelectionService | None = None,
    execution_bindings: Mapping[str, AgentExecutionBinding] | None = None,
    config_hash: str,
) -> RunSubmissionService | None:
    """Compose only when the real runtime implements the atomic contract."""
    if (
        agent_preflight is None
        or model_selection is None
        or not isinstance(transactions, RunSubmissionTransactions)
    ):
        return None
    return RunSubmissionService(
        transactions=transactions,
        agent_catalog=agent_catalog,
        agent_preflight=agent_preflight,
        model_selection=model_selection,
        execution_bindings=execution_bindings,
        config_hash=config_hash,
    )
