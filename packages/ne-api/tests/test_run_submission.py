# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-010A accepted-submission API boundary controls."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ne.api.agent_catalog import AgentCatalog
from ne.api.agent_preflight import (
    AgentCapabilityPreflight,
    AgentCapabilityPreflightService,
    AgentCapabilitySet,
)
from ne.api.deps import api_state, bind_authenticated_request_context, current_principal
from ne.api.model_selection import (
    AuthorizedModelSelection,
    ModelSelectionService,
    UnknownModelSelection,
)
from ne.api.routers import runs as runs_router
from ne.api.run_submission import (
    RUN_SUBMISSION_STATE_KEY,
    AcceptedRunSubmission,
    CancelRunCommand,
    RetryRunCommand,
    RunSubmissionConflict,
    RunSubmissionNotFound,
    RunSubmissionService,
    RunSubmissionStatus,
    StartRunCommand,
    compose_run_submission_service,
)
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.types import Lane, Principal
from ne.runtime.agent import AgentExecutionBinding
from ne.runtime.source_results import authenticated_request_scope
from ne.runtime.workflow_execution_profile import (
    MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
    ExecutionStrategy,
)

AGENT_ID = "agt_01k3meridiananalyst000000000"
AGENT_VERSION = 7
DEFINITION_HASH = "d" * 64
CAPABILITY_HASH = "c" * 64
CONFIG_HASH = "f" * 64
OWNER = Principal(
    id="user:ada",
    groups=("role:ops",),
    claims={"department": "operations"},
)
FOREIGN = Principal(
    id="user:bob",
    groups=("role:ops",),
    claims={"department": "operations"},
)


def test_accepted_submission_status_type_contains_only_admission_states() -> None:
    assert set(get_args(RunSubmissionStatus)) == {"submitted", "queued", "cancelling"}


class _Catalog:
    def __init__(self, *, max_input_tokens: int = 24_000, max_steps: int = 40) -> None:
        self.definition = SimpleNamespace(
            agent_id=AGENT_ID,
            version=AGENT_VERSION,
            definition_hash=DEFINITION_HASH,
            model_policy_id="agent.meridian-analyst.v2",
            # `max_steps` mirrors the real meridian analyst (40), so the
            # derived run-credential floor here is the deployed 420 s.
            budgets=SimpleNamespace(max_input_tokens=max_input_tokens, max_steps=max_steps),
            allowed_strategies=("agentic", "deterministic"),
        )
        self.row = SimpleNamespace(
            definition=self.definition,
            available=True,
            availability_reasons=(),
        )

    def get_visible(self, agent_id: str, principal: Principal) -> Any:
        if agent_id != AGENT_ID or principal.id == "user:hidden":
            return None
        return self.row


class _Preflight:
    def __init__(self) -> None:
        self.current_hash = CAPABILITY_HASH
        self.calls: list[Principal] = []

    async def preflight(
        self,
        *,
        agent_id: str,
        principal: Principal,
        presented_capability_hash: str | None = None,
    ) -> AgentCapabilityPreflight | None:
        self.calls.append(principal)
        if agent_id != AGENT_ID:
            return None
        # Mirrors the real PRE-011E service: no presented hash is a current
        # preflight read, while a presented hash is the exact submit-time join.
        if presented_capability_hash is not None and presented_capability_hash != self.current_hash:
            return None
        return AgentCapabilityPreflight(
            agent_id=AGENT_ID,
            agent_version=AGENT_VERSION,
            definition_hash=DEFINITION_HASH,
            available=True,
            availability_reasons=(),
            capabilities=AgentCapabilitySet(
                agent_version=AGENT_VERSION,
                agent_definition_hash=DEFINITION_HASH,
                tools=(),
                connectors=(),
                capability_hash=self.current_hash,
            ),
        )


class _ModelSelection:
    def __init__(self, *, input_budget: int = 4096) -> None:
        self._input_budget = input_budget

    async def authorize_auto(self, **_kwargs: Any) -> AuthorizedModelSelection:
        return await self.authorize(requested_model="private-primary")

    async def authorize(
        self,
        *,
        requested_model: str,
        **_kwargs: Any,
    ) -> AuthorizedModelSelection:
        if requested_model not in {"private-primary", "frontier-primary"}:
            raise UnknownModelSelection
        lane = Lane.FRONTIER_API if requested_model == "frontier-primary" else Lane.PRIVATE_VLLM
        return AuthorizedModelSelection(
            model=requested_model,
            lane=lane,
            observed_at=datetime(2026, 8, 23, tzinfo=UTC),
            policy_effect="allow",
            policy_version="test-v1",
        )

    def input_budget(self, _model: str) -> int:
        return self._input_budget


class _AtomicTransactions:
    """Contract double: one call owns idempotency, version, Run and outbox."""

    def __init__(self) -> None:
        self.calls: list[StartRunCommand | CancelRunCommand | RetryRunCommand] = []
        self.accepted: dict[tuple[str, str, str], tuple[str, AcceptedRunSubmission]] = {}
        self.run_owner = {"run-owned": OWNER.id}
        self.run_version = {"run-owned": 1}

    def _claim(
        self,
        *,
        action: str,
        command: StartRunCommand | CancelRunCommand | RetryRunCommand,
        result: AcceptedRunSubmission,
    ) -> AcceptedRunSubmission:
        self.calls.append(command)
        key = (command.owner_principal_id, action, command.idempotency_key)
        prior = self.accepted.get(key)
        if prior is not None:
            prior_hash, prior_result = prior
            if prior_hash != command.request_hash:
                raise RunSubmissionConflict("idempotency key changed request")
            return replace(prior_result, replayed=True)
        self.accepted[key] = (command.request_hash, result)
        return result

    async def admit_start(self, command: StartRunCommand) -> AcceptedRunSubmission:
        return self._claim(
            action="start",
            command=command,
            result=AcceptedRunSubmission(
                action="start",
                run_id="run-new",
                attempt_id="attempt-new",
                version=1,
                status="submitted",
                enqueue_id="outbox-start",
                replayed=False,
            ),
        )

    def _owned_version(self, command: CancelRunCommand | RetryRunCommand) -> None:
        if self.run_owner.get(command.run_id) != command.owner_principal_id:
            raise RunSubmissionNotFound
        if self.run_version[command.run_id] != command.expected_version:
            raise RunSubmissionConflict("stale version")

    async def admit_cancel(self, command: CancelRunCommand) -> AcceptedRunSubmission:
        key = (command.owner_principal_id, "cancel", command.idempotency_key)
        if key not in self.accepted:
            self._owned_version(command)
        result = self._claim(
            action="cancel",
            command=command,
            result=AcceptedRunSubmission(
                action="cancel",
                run_id=command.run_id,
                attempt_id="attempt-owned",
                version=2,
                status="cancelling",
                enqueue_id="outbox-cancel",
                replayed=False,
            ),
        )
        self.run_version[command.run_id] = 2
        return result

    async def admit_retry(self, command: RetryRunCommand) -> AcceptedRunSubmission:
        key = (command.owner_principal_id, "retry", command.idempotency_key)
        if key not in self.accepted:
            self._owned_version(command)
        result = self._claim(
            action="retry",
            command=command,
            result=AcceptedRunSubmission(
                action="retry",
                run_id=command.run_id,
                attempt_id="attempt-retry",
                version=2,
                status="queued",
                enqueue_id="outbox-retry",
                replayed=False,
            ),
        )
        self.run_version[command.run_id] = 2
        return result


def _service(
    transactions: _AtomicTransactions | None = None,
    *,
    agent_input_budget: int = 24_000,
    model_input_budget: int = 4096,
    execution_bindings: dict[str, AgentExecutionBinding] | None = None,
    agent_max_steps: int = 40,
) -> tuple[RunSubmissionService, _AtomicTransactions, _Preflight]:
    transactions = transactions or _AtomicTransactions()
    preflight = _Preflight()
    service = RunSubmissionService(
        transactions=transactions,
        agent_catalog=cast(
            AgentCatalog,
            _Catalog(max_input_tokens=agent_input_budget, max_steps=agent_max_steps),
        ),
        agent_preflight=cast(AgentCapabilityPreflightService, preflight),
        model_selection=cast(
            ModelSelectionService,
            _ModelSelection(input_budget=model_input_budget),
        ),
        execution_bindings=execution_bindings,
        config_hash=CONFIG_HASH,
    )
    return service, transactions, preflight


def test_prepare_start_preserves_the_exact_deterministic_admission_budget() -> None:
    binding = AgentExecutionBinding(
        agent_id=AGENT_ID,
        agent_version=AGENT_VERSION,
        strategy=ExecutionStrategy.DETERMINISTIC,
        workflow_profile=MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
    )
    service, transactions, _preflight = _service(
        agent_input_budget=8210,
        model_input_budget=32_768,
        execution_bindings={AGENT_ID: binding},
    )

    for max_subjects in range(1, 6):
        input_text = f'{{"max_subjects":{max_subjects}}}'
        command = asyncio.run(
            service.prepare_start(
                principal=OWNER,
                agent_id=AGENT_ID,
                conversation_id=f"hardship-{max_subjects}",
                expected_version=0,
                agent_version=AGENT_VERSION,
                strategy="deterministic",
                config_hash=CONFIG_HASH,
                capability_hash=CAPABILITY_HASH,
                input_text=input_text,
                idempotency_key=f"hardship-{max_subjects}",
                authenticated_context=AuthenticatedRequestContext(
                    principal_id=OWNER.id,
                    authorization_groups=tuple(sorted(set(OWNER.groups))),
                    department="operations",
                    issuer="https://issuer.example.test",
                    expires_at=4_102_444_800,
                ),
            )
        )
        assert command.input_plaintext == input_text.encode("utf-8")
        assert command.target_input_budget == 8210

    assert transactions.calls == []


def _client(
    service: RunSubmissionService,
    principal: Principal = OWNER,
    *,
    dispatches: list[Any] | None = None,
) -> TestClient:
    context = AuthenticatedRequestContext(
        principal_id=principal.id,
        authorization_groups=tuple(sorted(set(principal.groups))),
        department=cast(str, principal.claims["department"]),
        issuer="https://issuer.example.test",
        expires_at=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
    )

    class _DispatchStore:
        async def load_workflow_request(self, **kwargs: Any) -> Any:
            return SimpleNamespace(**kwargs)

    class _Enqueuer:
        async def enqueue(self, request: Any) -> None:
            if dispatches is not None:
                dispatches.append(request)

    async def bind_context() -> AsyncIterator[None]:
        with authenticated_request_scope(context):
            yield

    app = FastAPI()
    app.include_router(runs_router.router)
    app.dependency_overrides[api_state] = lambda: SimpleNamespace(
        agent_run_store=_DispatchStore(), agent_run_enqueuer=_Enqueuer()
    )
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[bind_authenticated_request_context] = bind_context
    setattr(app.state, RUN_SUBMISSION_STATE_KEY, service)
    return TestClient(app)


def _start_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "conversation_id": "conversation-owned",
        "expected_version": 0,
        "agent_version": AGENT_VERSION,
        "strategy": "agentic",
        "config_hash": CONFIG_HASH,
        "capability_snapshot_hash": CAPABILITY_HASH,
        "input": "PRIVATE accepted Run input",
    }
    body.update(overrides)
    return body


def _control_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "expected_version": 1,
        "agent_version": AGENT_VERSION,
        "strategy": "agentic",
        "config_hash": CONFIG_HASH,
        "capability_snapshot_hash": CAPABILITY_HASH,
    }
    body.update(overrides)
    return body


def test_start_duplicate_key_replays_one_atomic_admission_and_changed_body_conflicts() -> None:
    service, transactions, _preflight = _service()
    client = _client(service)
    headers = {"Idempotency-Key": "start-once"}

    first = client.post(f"/v1/agents/{AGENT_ID}/runs", headers=headers, json=_start_body())
    replay = client.post(f"/v1/agents/{AGENT_ID}/runs", headers=headers, json=_start_body())
    changed = client.post(
        f"/v1/agents/{AGENT_ID}/runs",
        headers=headers,
        json=_start_body(input="different input"),
    )

    assert first.status_code == replay.status_code == 202
    assert first.headers["cache-control"] == "private, no-store"
    assert first.json() == {
        "action": "start",
        "run_id": "run-new",
        "attempt_id": "attempt-new",
        "version": 1,
        "status": "submitted",
        "enqueue_id": "outbox-start",
        "replayed": False,
    }
    assert replay.json() == {**first.json(), "replayed": True}
    assert changed.status_code == 409
    assert changed.json() == {"detail": "request conflicts with current Run state"}
    assert len(transactions.calls) == 3
    command = cast(StartRunCommand, transactions.calls[0])
    assert command.owner_principal_id == OWNER.id
    assert command.input_plaintext == b"PRIVATE accepted Run input"
    assert command.capability_snapshot_hash == CAPABILITY_HASH
    assert command.capability_snapshot_receipt_id.startswith("capability:v1:")
    assert command.agent_definition_hash == DEFINITION_HASH
    assert command.model_policy_id == "agent.meridian-analyst.v2"
    assert command.authorized_model == "private-primary"
    assert command.authorized_model_lane is Lane.PRIVATE_VLLM
    assert len(command.model_authorization_receipt_hash) == 64
    assert command.target_input_budget == 4096
    assert len(command.request_hash) == 64


def test_start_and_exact_replay_dispatch_the_same_durable_workflow_target() -> None:
    service, _transactions, _preflight = _service()
    dispatches: list[Any] = []
    client = _client(service, dispatches=dispatches)
    headers = {"Idempotency-Key": "dispatch-recovery"}
    first = client.post(f"/v1/agents/{AGENT_ID}/runs", headers=headers, json=_start_body())
    replay = client.post(f"/v1/agents/{AGENT_ID}/runs", headers=headers, json=_start_body())
    assert first.status_code == replay.status_code == 202
    assert replay.json()["replayed"] is True
    assert len(dispatches) == 2
    assert vars(dispatches[0]) == vars(dispatches[1])
    assert dispatches[0].enqueue_id == first.json()["enqueue_id"]
    assert dispatches[0].run_id == first.json()["run_id"]
    assert dispatches[0].attempt_id == first.json()["attempt_id"]


@pytest.mark.parametrize(
    "changed",
    [
        {"agent_version": AGENT_VERSION + 1},
        {"strategy": "deterministic", "config_hash": "0" * 64},
    ],
)
def test_stale_agent_or_config_never_reaches_transaction(
    changed: dict[str, Any],
) -> None:
    service, transactions, _preflight = _service()
    response = _client(service).post(
        f"/v1/agents/{AGENT_ID}/runs",
        headers={"Idempotency-Key": "stale-snapshot"},
        json=_start_body(**changed),
    )
    assert response.status_code == 409
    assert transactions.calls == []


def test_stale_capability_snapshot_is_byte_equivalent_to_hidden_agent() -> None:
    service, transactions, _preflight = _service()
    client = _client(service)
    stale = client.post(
        f"/v1/agents/{AGENT_ID}/runs",
        headers={"Idempotency-Key": "stale-snapshot"},
        json=_start_body(capability_snapshot_hash="0" * 64),
    )
    hidden = client.post(
        "/v1/agents/agt_hidden00000000000000000000/runs",
        headers={"Idempotency-Key": "hidden-agent"},
        json=_start_body(),
    )
    assert (stale.status_code, stale.content, stale.headers["cache-control"]) == (
        hidden.status_code,
        hidden.content,
        hidden.headers["cache-control"],
    )
    assert stale.status_code == 404
    assert transactions.calls == []


def test_same_role_foreign_principal_cannot_cancel_an_owned_run() -> None:
    service, transactions, preflight = _service()
    foreign = _client(service, FOREIGN).post(
        f"/v1/agents/{AGENT_ID}/runs/run-owned/cancel",
        headers={"Idempotency-Key": "foreign-cancel"},
        json=_control_body(),
    )
    random = _client(service, FOREIGN).post(
        f"/v1/agents/{AGENT_ID}/runs/run-random/cancel",
        headers={"Idempotency-Key": "random-cancel"},
        json=_control_body(),
    )
    assert (foreign.status_code, foreign.content, foreign.headers["cache-control"]) == (
        random.status_code,
        random.content,
        random.headers["cache-control"],
    )
    assert foreign.status_code == 404
    assert all(call.owner_principal_id == FOREIGN.id for call in transactions.calls)
    assert all(principal.id == FOREIGN.id for principal in preflight.calls)


def test_cancel_and_retry_carry_optimistic_version_and_distinct_outbox_projection() -> None:
    cancel_service, cancel_transactions, _ = _service()
    cancel = _client(cancel_service).post(
        f"/v1/agents/{AGENT_ID}/runs/run-owned/cancel",
        headers={"Idempotency-Key": "cancel-once"},
        json=_control_body(),
    )
    retry_service, retry_transactions, _ = _service()
    retry = _client(retry_service).post(
        f"/v1/agents/{AGENT_ID}/runs/run-owned/retry",
        headers={"Idempotency-Key": "retry-once"},
        json=_control_body(),
    )
    assert cancel.status_code == retry.status_code == 202
    assert cancel.json()["status"] == "cancelling"
    assert retry.json()["status"] == "queued"
    assert cancel.json()["enqueue_id"] != retry.json()["enqueue_id"]
    cancel_command = cast(CancelRunCommand, cancel_transactions.calls[0])
    retry_command = cast(RetryRunCommand, retry_transactions.calls[0])
    assert cancel_command.expected_version == retry_command.expected_version == 1
    assert cancel_command.request_hash != retry_command.request_hash


def test_start_idempotency_intent_includes_requested_model_and_lane() -> None:
    first_service, first_transactions, _ = _service()
    second_service, second_transactions, _ = _service()
    first = _client(first_service).post(
        f"/v1/agents/{AGENT_ID}/runs",
        headers={"Idempotency-Key": "model-intent"},
        json=_start_body(requested_model="frontier-primary", requested_model_lane="frontier-api"),
    )
    second = _client(second_service).post(
        f"/v1/agents/{AGENT_ID}/runs",
        headers={"Idempotency-Key": "model-intent"},
        json=_start_body(requested_model="private-primary", requested_model_lane="private-vllm"),
    )
    assert first.status_code == second.status_code == 202
    first_command = cast(StartRunCommand, first_transactions.calls[0])
    second_command = cast(StartRunCommand, second_transactions.calls[0])
    assert first_command.request_hash != second_command.request_hash
    assert (first_command.requested_model, first_command.requested_model_lane) == (
        "frontier-primary",
        Lane.FRONTIER_API,
    )


def test_server_derives_lane_and_unknown_or_client_mismatched_model_never_admits() -> None:
    service, transactions, _ = _service()
    client = _client(service)
    derived = client.post(
        f"/v1/agents/{AGENT_ID}/runs",
        headers={"Idempotency-Key": "derived-lane"},
        json=_start_body(requested_model="frontier-primary"),
    )
    unknown = client.post(
        f"/v1/agents/{AGENT_ID}/runs",
        headers={"Idempotency-Key": "unknown-model"},
        json=_start_body(requested_model="invented-model"),
    )
    mismatched = client.post(
        f"/v1/agents/{AGENT_ID}/runs",
        headers={"Idempotency-Key": "mismatched-lane"},
        json=_start_body(
            requested_model="frontier-primary",
            requested_model_lane="private-vllm",
        ),
    )

    assert derived.status_code == 202
    command = cast(StartRunCommand, transactions.calls[0])
    assert command.requested_model_lane is Lane.FRONTIER_API
    assert unknown.status_code == mismatched.status_code == 409
    assert len(transactions.calls) == 1


@pytest.mark.parametrize("input_text", ("not-json", '{"max_subjects":"three"}'))
def test_deterministic_input_is_validated_against_frozen_profile_before_admission(
    input_text: str,
) -> None:
    transactions = _AtomicTransactions()
    binding = AgentExecutionBinding(
        agent_id=AGENT_ID,
        agent_version=AGENT_VERSION,
        strategy=ExecutionStrategy.DETERMINISTIC,
        workflow_profile=MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
    )
    service = RunSubmissionService(
        transactions=transactions,
        agent_catalog=cast(AgentCatalog, _Catalog()),
        agent_preflight=cast(AgentCapabilityPreflightService, _Preflight()),
        model_selection=cast(ModelSelectionService, _ModelSelection()),
        execution_bindings={AGENT_ID: binding},
        config_hash=CONFIG_HASH,
    )
    with pytest.raises(RunSubmissionConflict, match="workflow input is invalid"):
        asyncio.run(
            service.prepare_start(
                principal=OWNER,
                agent_id=AGENT_ID,
                conversation_id="conversation-owned",
                expected_version=0,
                agent_version=AGENT_VERSION,
                strategy="deterministic",
                config_hash=CONFIG_HASH,
                capability_hash=CAPABILITY_HASH,
                input_text=input_text,
                idempotency_key="deterministic-invalid",
                authenticated_context=AuthenticatedRequestContext(
                    principal_id=OWNER.id,
                    authorization_groups=tuple(sorted(set(OWNER.groups))),
                    department="operations",
                    issuer="https://issuer.example.test",
                    expires_at=4_102_444_800,
                ),
            )
        )
    assert transactions.calls == []


def test_composition_requires_the_complete_atomic_transaction_contract() -> None:
    catalog = cast(AgentCatalog, _Catalog())
    preflight = cast(AgentCapabilityPreflightService, _Preflight())
    model_selection = cast(ModelSelectionService, _ModelSelection())
    assert (
        compose_run_submission_service(
            transactions=SimpleNamespace(admit_start=lambda _command: None),
            agent_catalog=catalog,
            agent_preflight=preflight,
            model_selection=model_selection,
            config_hash=CONFIG_HASH,
        )
        is None
    )
    assert isinstance(
        compose_run_submission_service(
            transactions=_AtomicTransactions(),
            agent_catalog=catalog,
            agent_preflight=preflight,
            model_selection=model_selection,
            config_hash=CONFIG_HASH,
        ),
        RunSubmissionService,
    )


def test_missing_idempotency_key_is_rejected_before_submission() -> None:
    service, transactions, _ = _service()
    response = _client(service).post(
        f"/v1/agents/{AGENT_ID}/runs",
        json=_start_body(),
    )
    assert response.status_code == 422
    assert transactions.calls == []
