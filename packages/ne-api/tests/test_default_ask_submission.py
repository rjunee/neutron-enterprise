# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-010G G1: dormant default-Ask canonical-Run activation controls."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from starlette.responses import StreamingResponse

from ne.api.agent_catalog import AgentCatalog, CatalogAgent
from ne.api.agent_manifest import load_agent_manifest
from ne.api.agent_preflight import (
    AgentCapabilityPreflight,
    AgentCapabilityPreflightService,
    AgentCapabilitySet,
)
from ne.api.default_ask_submission import (
    DEFAULT_ASK_AGENT_ID,
    DEFAULT_ASK_WIRE_GENERATION,
    DefaultAskAccepted,
    DefaultAskActivationPin,
    DefaultAskAdmissionCommand,
    DefaultAskSubmissionService,
    DefaultAskUnavailable,
    compose_default_ask_submission_service,
    select_default_ask_agent,
)
from ne.api.model_selection import AuthorizedModelSelection, ModelSelectionService
from ne.api.routers import chat
from ne.api.run_credential_floor import RUN_CREDENTIAL_FLOOR_BOUND_SECONDS
from ne.api.run_submission import (
    AcceptedRunSubmission,
    RunSubmissionConflict,
    RunSubmissionNotFound,
    RunSubmissionService,
)
from ne.api.wire import ChatMessageRequest, CreateSessionRequest
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.types import Lane, Principal
from ne.runtime.run_finalizer import RunTurnTarget
from ne.runtime.run_workflow import RunWorkflowRequest
from ne.runtime.source_results import authenticated_request_scope
from ne.runtime.store import PrincipalConversation

AGENT_ID = DEFAULT_ASK_AGENT_ID
AGENT_VERSION = 7
DEFINITION_HASH = "d3" * 32
CAPABILITY_HASH = "c4" * 32
CONFIG_HASH = "f5" * 32
MODEL_POLICY_ID = "agent.meridian-analyst.v2"
ASK_TASK = "chat"
OWNER = Principal(id="user:ada", groups=("role:ops",), claims={"department": "operations"})
REPO_ROOT = Path(__file__).resolve().parents[3]


def _authenticated_context() -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id=OWNER.id,
        authorization_groups=tuple(sorted(set(OWNER.groups))),
        department="operations",
        issuer="https://issuer.example.test",
        expires_at=4_102_444_800,
    )


class _Catalog:
    definition = SimpleNamespace(
        agent_id=AGENT_ID,
        version=AGENT_VERSION,
        definition_hash=DEFINITION_HASH,
        allowed_strategies=("agentic",),
        model_policy_id=MODEL_POLICY_ID,
        budgets=SimpleNamespace(max_input_tokens=4096, max_steps=40),
    )
    row = SimpleNamespace(definition=definition, available=True, availability_reasons=())

    def get_visible(self, agent_id: str, _principal: Principal) -> Any:
        return self.row if agent_id == AGENT_ID else None


class _Preflight:
    current_hash = CAPABILITY_HASH

    async def preflight(
        self,
        *,
        agent_id: str,
        principal: Principal,
        presented_capability_hash: str | None = None,
    ) -> AgentCapabilityPreflight | None:
        del principal
        if agent_id != AGENT_ID or presented_capability_hash != self.current_hash:
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


class _UnusedRunTransactions:
    async def admit_start(self, _command: object) -> AcceptedRunSubmission:
        raise AssertionError("default Ask must not create a pre-target Run")

    async def admit_cancel(self, _command: object) -> AcceptedRunSubmission:
        raise AssertionError

    async def admit_retry(self, _command: object) -> AcceptedRunSubmission:
        raise AssertionError


class _ModelSelection:
    async def authorize_auto(self, *, task: str, **_kwargs: Any) -> AuthorizedModelSelection:
        assert task == MODEL_POLICY_ID
        return AuthorizedModelSelection(
            model="private-primary",
            lane=Lane.PRIVATE_VLLM,
            observed_at=datetime(2026, 8, 23, tzinfo=UTC),
            policy_effect="allow",
            policy_version="default-ask-test-v1",
        )

    async def authorize(
        self, *, task: str, requested_model: str | None = None, **_kwargs: Any
    ) -> AuthorizedModelSelection:
        if requested_model is None:
            return await self.authorize_auto(task=task)
        # An EXPLICIT choice is authorized twice on the canonical path: once at
        # the route under the Ask surface task, once at admission under the
        # Agent's model policy. Both are this one authority.
        assert task in (MODEL_POLICY_ID, ASK_TASK)
        return AuthorizedModelSelection(
            model=requested_model,
            lane=(
                Lane.PRIVATE_VLLM if requested_model.startswith("private") else Lane.FRONTIER_API
            ),
            observed_at=datetime(2026, 8, 23, tzinfo=UTC),
            policy_effect="allow",
            policy_version="default-ask-test-v1",
        )

    def input_budget(self, _model: str) -> int:
        return 4096


def _model_selection() -> ModelSelectionService:
    return cast(ModelSelectionService, _ModelSelection())


class _AtomicDefaultAskTransactions:
    def __init__(self, *, mutation: str | None = None) -> None:
        self.mutation = mutation
        self.calls: list[DefaultAskAdmissionCommand] = []

    async def admit_default_ask(self, command: DefaultAskAdmissionCommand) -> DefaultAskAccepted:
        self.calls.append(command)
        submission = AcceptedRunSubmission(
            action="start",
            run_id="run-canonical",
            attempt_id="attempt-canonical",
            version=1,
            status="submitted",
            enqueue_id="dispatch-canonical",
            replayed=False,
        )
        target = RunTurnTarget(
            conversation_id=command.start.conversation_id,
            run_id="run-canonical",
            turn_id="turn-canonical",
            turn_sequence=1,
            attachment_id="attachment-canonical",
            attempt_id="attempt-canonical",
        )
        request = RunWorkflowRequest(
            dispatch_id=submission.enqueue_id,
            run_id=submission.run_id,
            owner_principal_id=command.start.owner_principal_id,
            config_hash=command.start.config_hash,
            target=target,
            authenticated_context=command.start.authenticated_context,
        )
        if self.mutation == "without_run":
            request = request.model_copy(update={"run_id": "run-absent"})
        elif self.mutation == "wrong_attempt":
            request = request.model_copy(
                update={"target": target.model_copy(update={"attempt_id": "attempt-legacy"})}
            )
        elif self.mutation == "wrong_conversation":
            request = request.model_copy(
                update={
                    "target": target.model_copy(update={"conversation_id": "conversation-legacy"})
                }
            )
        elif self.mutation == "wrong_dispatch":
            request = request.model_copy(update={"dispatch_id": "dispatch-legacy"})
        return DefaultAskAccepted(submission=submission, workflow_request=request)


class _CanonicalAskConversationStore:
    def __init__(self, *, agent_name: str = AGENT_ID) -> None:
        self.agent_name = agent_name
        self.created: list[tuple[str, str, str]] = []

    def _row(self) -> PrincipalConversation:
        now = datetime(2026, 8, 23, tzinfo=UTC)
        return PrincipalConversation(
            conversation_id="conversation-owned",
            owner_principal_id=OWNER.id,
            agent_name=self.agent_name,
            department="operations",
            created_at=now,
            updated_at=now,
            last_sequence=0,
        )

    async def create_conversation(
        self, agent_name: str, department: str, owner_principal_id: str
    ) -> PrincipalConversation:
        self.created.append((agent_name, department, owner_principal_id))
        self.agent_name = agent_name
        return self._row()

    async def get_conversation(
        self, conversation_id: str, owner_principal_id: str, department: str
    ) -> PrincipalConversation | None:
        if (
            conversation_id,
            owner_principal_id,
            department,
        ) != ("conversation-owned", OWNER.id, "operations"):
            return None
        return self._row()


class _ChatPreflight:
    async def preflight(self, *, agent_id: str, principal: Principal) -> AgentCapabilityPreflight:
        assert agent_id == AGENT_ID
        assert principal == OWNER
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
                capability_hash=CAPABILITY_HASH,
            ),
        )


class _RunEnqueuer:
    def __init__(self) -> None:
        self.requests: list[RunWorkflowRequest] = []

    async def enqueue(self, request: RunWorkflowRequest) -> None:
        self.requests.append(request)


def _pin(**changes: Any) -> DefaultAskActivationPin:
    return DefaultAskActivationPin(
        wire_generation=changes.pop("wire_generation", DEFAULT_ASK_WIRE_GENERATION),
        config_hash=changes.pop("config_hash", CONFIG_HASH),
        default_agent_id=changes.pop("default_agent_id", AGENT_ID),
        default_agent_version=changes.pop("default_agent_version", AGENT_VERSION),
        producer_snapshot_hash=changes.pop("producer_snapshot_hash", CAPABILITY_HASH),
        submission_ready=changes.pop("submission_ready", True),
        worker_execution_ready=changes.pop("worker_execution_ready", True),
        finalizer_ready=changes.pop("finalizer_ready", True),
        legacy_attachment_writer=changes.pop("legacy_attachment_writer", "retired"),
        legacy_terminal_writer=changes.pop("legacy_terminal_writer", "retired"),
        legacy_cross_turn_loader=changes.pop("legacy_cross_turn_loader", "retired"),
        **changes,
    )


def _run_submissions() -> RunSubmissionService:
    return RunSubmissionService(
        transactions=cast(Any, _UnusedRunTransactions()),
        agent_catalog=cast(AgentCatalog, _Catalog()),
        agent_preflight=cast(AgentCapabilityPreflightService, _Preflight()),
        model_selection=_model_selection(),
        config_hash=CONFIG_HASH,
    )


def _service(
    transactions: _AtomicDefaultAskTransactions | None = None,
    *,
    pin: DefaultAskActivationPin | None = None,
) -> tuple[DefaultAskSubmissionService, _AtomicDefaultAskTransactions]:
    transactions = transactions or _AtomicDefaultAskTransactions()
    service = compose_default_ask_submission_service(
        run_submissions=_run_submissions(),
        transactions=transactions,
        activation=pin or _pin(),
    )
    assert service is not None
    return service, transactions


async def _submit(service: DefaultAskSubmissionService, **changes: Any) -> DefaultAskAccepted:
    values: dict[str, Any] = {
        "principal": OWNER,
        "conversation_id": "conversation-owned",
        "expected_version": 0,
        "agent_id": AGENT_ID,
        "agent_version": AGENT_VERSION,
        "strategy": "agentic",
        "config_hash": CONFIG_HASH,
        "capability_snapshot_hash": CAPABILITY_HASH,
        "input_text": "private default Ask prompt",
        "idempotency_key": "ask-once",
    }
    values.update(changes)
    with authenticated_request_scope(_authenticated_context()):
        return await service.submit(**values)


def test_pre_g_has_no_service_and_preparation_never_creates_a_run() -> None:
    transactions = _AtomicDefaultAskTransactions()
    assert (
        compose_default_ask_submission_service(
            run_submissions=_run_submissions(),
            transactions=transactions,
            activation=None,
        )
        is None
    )
    assert transactions.calls == []

    # Even the new validation seam is side-effect free: its ordinary Run
    # transaction double raises if called, but preparation still succeeds.
    command = asyncio.run(
        _run_submissions().prepare_start(
            principal=OWNER,
            conversation_id="conversation-owned",
            expected_version=0,
            agent_id=AGENT_ID,
            agent_version=AGENT_VERSION,
            strategy="agentic",
            config_hash=CONFIG_HASH,
            capability_hash=CAPABILITY_HASH,
            input_text="private default Ask prompt",
            idempotency_key="prepare-only",
            authenticated_context=_authenticated_context(),
        )
    )
    assert command.owner_principal_id == OWNER.id
    assert command.input_plaintext == b"private default Ask prompt"


def test_activated_submission_returns_only_one_canonical_run_workflow_target() -> None:
    service, transactions = _service()
    accepted = asyncio.run(_submit(service))

    assert len(transactions.calls) == 1
    command = transactions.calls[0]
    assert command.activation_wire_generation == DEFAULT_ASK_WIRE_GENERATION
    assert command.producer_snapshot_hash == CAPABILITY_HASH
    assert command.start.capability_snapshot_hash == CAPABILITY_HASH
    assert command.start.capability_snapshot_receipt_id.startswith("capability:v1:")
    assert accepted.workflow_request.run_id == accepted.submission.run_id
    assert accepted.workflow_request.target.attempt_id == accepted.submission.attempt_id
    assert accepted.workflow_request.target.conversation_id == "conversation-owned"


def test_default_ask_selects_its_configured_agent_among_multiple_available_agents() -> None:
    configured = _Catalog.row
    deterministic = SimpleNamespace(
        definition=SimpleNamespace(
            agent_id="agt_01k3hardshipreview0000000000",
            version=3,
        ),
        available=True,
    )

    selected = select_default_ask_agent(cast(tuple[CatalogAgent, ...], (deterministic, configured)))

    assert selected is configured
    assert selected.definition.agent_id == DEFAULT_ASK_AGENT_ID


def test_default_ask_never_substitutes_another_available_agent() -> None:
    configured_unavailable = SimpleNamespace(
        definition=_Catalog.definition,
        available=False,
        availability_reasons=("tool-unavailable",),
    )
    other_available = SimpleNamespace(
        definition=SimpleNamespace(
            agent_id="agt_01k3hardshipreview0000000000",
            version=3,
        ),
        available=True,
    )

    with pytest.raises(ValueError, match="configured default Ask Agent is unavailable"):
        select_default_ask_agent(
            cast(tuple[CatalogAgent, ...], (configured_unavailable, other_available))
        )


def test_default_ask_allows_only_cold_knowledge_bootstrap_without_activation() -> None:
    knowledge_cold = SimpleNamespace(
        definition=_Catalog.definition,
        available=False,
        availability_reasons=("knowledge-unavailable",),
    )

    selected = select_default_ask_agent(
        cast(tuple[CatalogAgent, ...], (knowledge_cold,)),
        allow_knowledge_bootstrap=True,
    )

    assert selected is knowledge_cold


def test_default_ask_cold_boot_never_tolerates_an_unavailable_tool() -> None:
    incomplete = SimpleNamespace(
        definition=_Catalog.definition,
        available=False,
        availability_reasons=("knowledge-unavailable", "tool-unavailable"),
    )

    with pytest.raises(ValueError, match="configured default Ask Agent is unavailable"):
        select_default_ask_agent(
            cast(tuple[CatalogAgent, ...], (incomplete,)),
            allow_knowledge_bootstrap=True,
        )


def test_default_ask_agent_id_is_the_production_registry_q_and_a_definition() -> None:
    loaded = load_agent_manifest(REPO_ROOT / "deploy" / "agents" / "registry.toml")
    selected = tuple(
        definition
        for definition in loaded.manifest.agents
        if definition.agent_id == DEFAULT_ASK_AGENT_ID
    )

    assert len(selected) == 1
    assert selected[0].default_strategy == "agentic"
    assert selected[0].workflow is None


def test_activated_ask_creates_the_conversation_under_the_canonical_agent() -> None:
    service, _ = _service()
    store = _CanonicalAskConversationStore(agent_name="console-chat")
    state = SimpleNamespace(
        default_ask_submission=service,
        # NE-GA-G8: the boundary reads composed grounding config. Default-off here
        # keeps every canonical-Ask assertion below on the exact PRE-010G path.
        grounding=None,
        grounded_agent_name=None,
        session_store=store,
        settings=SimpleNamespace(chat_agents=("console-chat", "served-grounding-mode")),
    )

    view = asyncio.run(
        chat.create_session(
            CreateSessionRequest(agent_name="console-chat"),
            OWNER,
            cast(Any, state),
        )
    )

    assert store.created == [(AGENT_ID, "operations", OWNER.id)]
    assert view.session_id == "conversation-owned"
    assert view.agent_name == AGENT_ID


def test_configured_cold_ask_refuses_legacy_session_creation() -> None:
    state = SimpleNamespace(
        default_ask_submission=None,
        session_store=_CanonicalAskConversationStore(agent_name="console-chat"),
        settings=SimpleNamespace(
            chat_agents=("console-chat",),
            default_ask_generation=DEFAULT_ASK_WIRE_GENERATION,
        ),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            chat.create_session(
                CreateSessionRequest(agent_name="console-chat"),
                OWNER,
                cast(Any, state),
            )
        )

    assert raised.value.status_code == 503


def test_configured_cold_ask_refuses_legacy_message_writer() -> None:
    state = SimpleNamespace(
        default_ask_submission=None,
        settings=SimpleNamespace(
            default_ask_generation=DEFAULT_ASK_WIRE_GENERATION,
            sse_keepalive_s=15.0,
            turn_timeout_s=120.0,
        ),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            chat.send_message(
                "legacy-session",
                ChatMessageRequest(prompt="private default Ask prompt"),
                OWNER,
                cast(Any, state),
                "ask-once",
            )
        )

    assert raised.value.status_code == 503


def _activated_route_state(
    service: DefaultAskSubmissionService,
    store: _CanonicalAskConversationStore,
    enqueuer: _RunEnqueuer,
    agent_run_store: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        default_ask_submission=service,
        # Round 7: the canonical branch reads the newest boundary receipt as
        # the conversation's snapshot estimate for an EXPLICIT model switch.
        # ``None`` mirrors ApiState's default and means no fit data.
        agent_run_store=agent_run_store,
        # NE-GA-G8: the boundary reads composed grounding config. Default-off here
        # keeps every canonical-Ask assertion below on the exact PRE-010G path.
        grounding=None,
        grounded_agent_name=None,
        session_store=store,
        settings=SimpleNamespace(
            chat_agents=("console-chat",),
            sse_keepalive_s=15.0,
            turn_timeout_s=120.0,
        ),
        agent_catalog=_Catalog(),
        agent_preflight=_ChatPreflight(),
        config_hash=CONFIG_HASH,
        agent_run_enqueuer=enqueuer,
        # The canonical branch authorizes body.requested_model through the one
        # shared selection authority before admission.
        model_selection=_model_selection(),
        inference_lifecycle=None,
        runtime_settings=SimpleNamespace(task=ASK_TASK),
    )


async def _canonical_send(
    state: SimpleNamespace, body: ChatMessageRequest
) -> tuple[StreamingResponse, bytes]:
    with authenticated_request_scope(_authenticated_context()):
        response = await chat.send_message(
            "conversation-owned",
            body,
            OWNER,
            cast(Any, state),
            "ask-once",
        )
    chunks = [chunk async for chunk in response.body_iterator]
    payload = b"".join(
        chunk.encode() if isinstance(chunk, str) else bytes(chunk) for chunk in chunks
    )
    return response, payload


def test_activated_ask_admits_and_serves_one_exact_canonical_handoff() -> None:
    service, transactions = _service()
    store = _CanonicalAskConversationStore()
    enqueuer = _RunEnqueuer()
    state = _activated_route_state(service, store, enqueuer)

    response, body = asyncio.run(
        _canonical_send(state, ChatMessageRequest(prompt="private default Ask prompt"))
    )
    payload = json.loads(body.split(b"data: ", 1)[1].strip())

    assert len(transactions.calls) == 1
    assert transactions.calls[0].start.conversation_agent_name == AGENT_ID
    assert transactions.calls[0].start.agent_id == AGENT_ID
    assert len(enqueuer.requests) == 1
    assert payload == {
        "type": "accepted",
        "agent_id": AGENT_ID,
        "conversation_id": "conversation-owned",
        "run_id": "run-canonical",
        "attempt_id": "attempt-canonical",
        "status": "submitted",
    }
    assert response.headers["cache-control"] == "no-store"


def test_explicit_ask_model_choice_reaches_canonical_admission() -> None:
    """REGRESSION — the Ask picker must never be decorative again.

    train-53 shipped a canonical default-Ask branch that parsed
    ``body.requested_model`` (extra="forbid" admitted the field) and then never
    read it: the run was admitted with no model choice, executed as
    ``request_mode=auto`` on the gateway default lane, and LOOKED like it
    honored the selection. This pins the wire field all the way into the
    atomic admission command — model, lane, and explicit request hash.
    """
    service, transactions = _service()
    state = _activated_route_state(service, _CanonicalAskConversationStore(), _RunEnqueuer())

    asyncio.run(
        _canonical_send(
            state,
            ChatMessageRequest(
                prompt="run this on the private lane", requested_model="private-default"
            ),
        )
    )

    assert len(transactions.calls) == 1
    start = transactions.calls[0].start
    assert start.requested_model == "private-default"
    assert start.requested_model_lane is Lane.PRIVATE_VLLM
    assert start.authorized_model == "private-default"
    assert start.authorized_model_lane is Lane.PRIVATE_VLLM


def test_omitted_ask_model_choice_stays_request_mode_auto() -> None:
    """No choice means AUTO — never a fabricated explicit request."""
    service, transactions = _service()
    state = _activated_route_state(service, _CanonicalAskConversationStore(), _RunEnqueuer())

    asyncio.run(_canonical_send(state, ChatMessageRequest(prompt="no explicit choice")))

    start = transactions.calls[0].start
    assert start.requested_model is None
    assert start.requested_model_lane is None


def test_a_resend_naming_a_different_model_is_a_different_request() -> None:
    """The explicit choice is part of the turn's replay identity.

    A frozen idempotent resend (same model) must replay the original Run; a
    resend that names a DIFFERENT model must present a different request hash
    so the store conflicts loudly instead of silently joining a run that
    executed on another lane.
    """
    service, transactions = _service()
    asyncio.run(
        _submit(service, requested_model="private-default", requested_model_lane=Lane.PRIVATE_VLLM)
    )
    asyncio.run(
        _submit(service, requested_model="frontier-best", requested_model_lane=Lane.FRONTIER_API)
    )
    asyncio.run(
        _submit(service, requested_model="private-default", requested_model_lane=Lane.PRIVATE_VLLM)
    )

    first, second, third = (call.start.request_hash for call in transactions.calls)
    assert first != second
    assert first == third


def test_activated_ask_rejects_a_legacy_alias_conversation_without_fallback() -> None:
    service, transactions = _service()
    store = _CanonicalAskConversationStore(agent_name="console-chat")
    state = SimpleNamespace(
        default_ask_submission=service,
        # NE-GA-G8: the boundary reads composed grounding config. Default-off here
        # keeps every canonical-Ask assertion below on the exact PRE-010G path.
        grounding=None,
        grounded_agent_name=None,
        session_store=store,
        settings=SimpleNamespace(
            chat_agents=("console-chat",),
            sse_keepalive_s=15.0,
            turn_timeout_s=120.0,
        ),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            chat.send_message(
                "conversation-owned",
                ChatMessageRequest(prompt="private default Ask prompt"),
                OWNER,
                cast(Any, state),
                "ask-once",
            )
        )

    assert raised.value.status_code == 404
    assert transactions.calls == []


@pytest.mark.parametrize(
    "mutation",
    ["without_run", "wrong_attempt", "wrong_conversation", "wrong_dispatch"],
)
def test_activated_execution_without_the_exact_canonical_run_is_refused(mutation: str) -> None:
    service, _ = _service(_AtomicDefaultAskTransactions(mutation=mutation))
    with pytest.raises(DefaultAskUnavailable, match="one canonical Run target"):
        asyncio.run(_submit(service))


def test_stale_or_placeholder_producer_snapshot_never_reaches_admission() -> None:
    service, transactions = _service()
    with pytest.raises(DefaultAskUnavailable, match="producer snapshot is not current"):
        asyncio.run(_submit(service, capability_snapshot_hash="a6" * 32))
    assert transactions.calls == []

    assert (
        compose_default_ask_submission_service(
            run_submissions=_run_submissions(),
            transactions=transactions,
            activation=_pin(producer_snapshot_hash="0" * 64),
        )
        is None
    )

    stale_preflight = _Preflight()
    stale_preflight.current_hash = "a6" * 32
    stale_service = DefaultAskSubmissionService(
        run_submissions=RunSubmissionService(
            transactions=cast(Any, _UnusedRunTransactions()),
            agent_catalog=cast(AgentCatalog, _Catalog()),
            agent_preflight=cast(AgentCapabilityPreflightService, stale_preflight),
            model_selection=_model_selection(),
            config_hash=CONFIG_HASH,
        ),
        transactions=transactions,
        activation=_pin(),
    )
    with pytest.raises(RunSubmissionNotFound):
        asyncio.run(_submit(stale_service))
    assert transactions.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wire_generation", "legacy/attachment/v1"),
        ("config_hash", "0" * 64),
        ("default_agent_id", " "),
        ("default_agent_version", 0),
        ("submission_ready", False),
        ("worker_execution_ready", False),
        ("finalizer_ready", False),
        ("legacy_attachment_writer", "active"),
        ("legacy_terminal_writer", "active"),
        ("legacy_cross_turn_loader", "active"),
    ],
)
def test_partial_readiness_or_one_reopened_legacy_path_keeps_activation_dormant(
    field: str, value: object
) -> None:
    transactions = _AtomicDefaultAskTransactions()
    assert (
        compose_default_ask_submission_service(
            run_submissions=_run_submissions(),
            transactions=transactions,
            activation=replace(_pin(), **{field: value}),
        )
        is None
    )
    assert transactions.calls == []


# --- the canonical Ask route's caller-facing refusal -------------------------


def _short_context(*, remaining_s: int) -> AuthenticatedRequestContext:
    """The same verified context, with exactly ``remaining_s`` of life left."""

    return AuthenticatedRequestContext(
        principal_id=OWNER.id,
        authorization_groups=tuple(sorted(set(OWNER.groups))),
        department="operations",
        issuer="https://issuer.example.test",
        expires_at=int(time.time()) + remaining_s,
    )


async def _send_under(state: SimpleNamespace, context: AuthenticatedRequestContext) -> Any:
    with authenticated_request_scope(context):
        return await chat.send_message(
            "conversation-owned",
            ChatMessageRequest(prompt="private default Ask prompt"),
            OWNER,
            cast(Any, state),
            "ask-once",
        )


class _RaisingAsk:
    """The activated service with one exact refusal substituted for `submit`."""

    def __init__(self, service: DefaultAskSubmissionService, exc: BaseException) -> None:
        self._service = service
        self._exc = exc
        self.activation = service.activation

    async def submit(self, **_kwargs: Any) -> Any:
        raise self._exc


def test_a_credential_too_short_answers_401_with_the_remedy_not_the_409() -> None:
    """The canonical Ask route's refusal is the caller's only instruction.

    ⛔ The 409 this replaces was unactionable: a client that cannot distinguish
    "renew your token" from "this Conversation moved under you" retries the
    same dead credential. `mutant:` delete the `isinstance(exc,
    RunCredentialTooShort)` arm in `chat.send_message` -- the refusal falls
    through to the `RunSubmissionConflict` arm it subclasses and answers
    409 "canonical default Ask conflict". `red:` the first block below.

    The refusal is raised by the REAL `RunSubmissionService` under a REAL short
    context (3 s), not by a stub, so the status code and the derived
    `420s required` are pinned to the same derivation the route will meet in
    production. The 409 leg is a control: it proves the 401 is this SUBCLASS
    being caught first, not a blanket remapping of every conflict.
    """

    service, transactions = _service()
    state = _activated_route_state(service, _CanonicalAskConversationStore(), _RunEnqueuer())

    with pytest.raises(HTTPException) as refused:
        asyncio.run(_send_under(state, _short_context(remaining_s=3)))

    assert refused.value.status_code == 401
    detail = str(refused.value.detail)
    assert "3s remaining" in detail
    assert f"{RUN_CREDENTIAL_FLOOR_BOUND_SECONDS}s required" in detail
    assert "Renew the access token and resubmit." in detail
    # Refused at admission: no Run, no claimed turn, nothing enqueued.
    assert transactions.calls == []

    # control: a credential that clears the floor is admitted exactly as before.
    admitted, _ = _service()
    fine = _activated_route_state(admitted, _CanonicalAskConversationStore(), _RunEnqueuer())
    response = asyncio.run(_send_under(fine, _short_context(remaining_s=3600)))
    assert response.headers["cache-control"] == "no-store"

    # control: the arm it is ordered in front of still answers the opaque 409.
    plain, _ = _service()
    conflicting = _activated_route_state(
        cast(Any, _RaisingAsk(plain, RunSubmissionConflict("expected_version moved"))),
        _CanonicalAskConversationStore(),
        _RunEnqueuer(),
    )
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(_send_under(conflicting, _short_context(remaining_s=3600)))
    assert conflict.value.status_code == 409
    assert conflict.value.detail == "canonical default Ask conflict"
    assert "expected_version moved" not in str(conflict.value.detail)
