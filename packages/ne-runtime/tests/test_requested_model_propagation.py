# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Run-scoped requested-model propagation without history or context bleed."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from ne.contracts.models import Completion, CompletionRequest
from ne.contracts.types import Lane
from ne.runtime import (
    current_run_requested_model,
    current_run_requested_model_lane,
    run_requested_model_scope,
)
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.model import GatewayModel
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_executor,
    make_principal,
    standard_bindings,
    text_completion,
)

_RESIDUE = '{"tool_call": {"arguments": {"query": "SLA"}}}'


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    value = sa.create_engine("sqlite://")
    ensure_runtime_schema(value)
    try:
        yield value
    finally:
        value.dispose()


async def _unused_hook(name: str, args: Any) -> None:
    raise AssertionError("unknown-tool hook should not run")


def _session(
    engine: sa.Engine,
    gateway: ScriptedGateway,
    *,
    settings: RuntimeSettings | None = None,
) -> tuple[DepartmentAgentSession, SqlAlchemySessionStore]:
    store = SqlAlchemySessionStore(engine)
    sink = RecordingAuditSink()
    executor = make_executor(
        standard_bindings(ToolLog()),
        sink,
        approval_gate=NamedApproverGate("alice"),
    )
    session = DepartmentAgentSession(
        session_id=asyncio.run(
            store.create_conversation("assistant", "eng", "user:ada")
        ).conversation_id,
        agent_name="assistant",
        department="eng",
        owner_principal_id="user:ada",
        store=store,
        executor=executor,
        gateway=gateway,
        settings=settings or RuntimeSettings(),
    )
    return session, store


def _direct_request(gateway: ScriptedGateway, settings: RuntimeSettings | None = None) -> None:
    model = GatewayModel(gateway, settings or RuntimeSettings(), on_unknown_tool=_unused_hook)
    asyncio.run(
        model.request(
            [ModelRequest(parts=[UserPromptPart(content="hello")])],
            None,
            ModelRequestParameters(),
        )
    )


def test_gateway_model_stamps_explicit_requested_model_and_legacy_none() -> None:
    gateway = ScriptedGateway(
        [text_completion("first"), text_completion("second"), text_completion("third")]
    )
    workflow_settings = RuntimeSettings(lane=Lane.PRIVATE_VLLM.value)

    with run_requested_model_scope("anthropic-frontier", lane=Lane.FRONTIER_API):
        _direct_request(gateway, workflow_settings)
    with run_requested_model_scope("private-gpu"):
        _direct_request(gateway, workflow_settings)
    _direct_request(gateway, workflow_settings)

    assert [request.requested_model for request in gateway.requests] == [
        "anthropic-frontier",
        "private-gpu",
        None,
    ]
    # Only server-resolved selection data can override the workflow's legacy
    # private lane. No selection, or an alias without a resolved lane, preserves
    # RuntimeSettings byte-for-byte.
    assert [request.lane for request in gateway.requests] == [
        Lane.FRONTIER_API,
        Lane.PRIVATE_VLLM,
        Lane.PRIVATE_VLLM,
    ]


def test_requested_model_scope_nested_none_clears_and_resets_outer() -> None:
    assert current_run_requested_model() is None
    with run_requested_model_scope("outer", lane=Lane.FRONTIER_API):
        assert current_run_requested_model() == "outer"
        assert current_run_requested_model_lane() == Lane.FRONTIER_API
        with run_requested_model_scope(None):
            assert current_run_requested_model() is None
            assert current_run_requested_model_lane() is None
        assert current_run_requested_model() == "outer"
        assert current_run_requested_model_lane() == Lane.FRONTIER_API
        with run_requested_model_scope("inner", lane=Lane.PRIVATE_VLLM):
            assert current_run_requested_model() == "inner"
            assert current_run_requested_model_lane() == Lane.PRIVATE_VLLM
        assert current_run_requested_model() == "outer"
        assert current_run_requested_model_lane() == Lane.FRONTIER_API
    assert current_run_requested_model() is None
    assert current_run_requested_model_lane() is None


def test_requested_lane_without_model_is_refused() -> None:
    with (
        pytest.raises(ValueError, match="requires an explicit requested model"),
        run_requested_model_scope(None, lane=Lane.FRONTIER_API),
    ):
        raise AssertionError("invalid selection scope must not open")


def test_requested_model_scope_is_task_local_under_concurrency() -> None:
    entered = asyncio.Barrier(2)
    read = asyncio.Barrier(2)
    observed: dict[str, tuple[str | None, Lane | None]] = {}

    async def run_with(model: str, lane: Lane) -> None:
        with run_requested_model_scope(model, lane=lane):
            await entered.wait()
            observed[model] = (
                current_run_requested_model(),
                current_run_requested_model_lane(),
            )
            await read.wait()

    async def both() -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(run_with("anthropic-frontier", Lane.FRONTIER_API))
            tasks.create_task(run_with("private-gpu", Lane.PRIVATE_VLLM))

    asyncio.run(both())

    assert observed == {
        "anthropic-frontier": ("anthropic-frontier", Lane.FRONTIER_API),
        "private-gpu": ("private-gpu", Lane.PRIVATE_VLLM),
    }
    assert current_run_requested_model() is None
    assert current_run_requested_model_lane() is None


def test_session_explicit_none_clears_outer_selection_for_whole_run(engine: sa.Engine) -> None:
    gateway = ScriptedGateway([text_completion("routed")])
    session, _ = _session(
        engine,
        gateway,
        settings=RuntimeSettings(lane=Lane.PRIVATE_VLLM.value),
    )

    with run_requested_model_scope("outer", lane=Lane.FRONTIER_API):
        result = asyncio.run(
            session.run(
                "use the route",
                principal=make_principal(department="eng"),
                requested_model=None,
            )
        )
        assert current_run_requested_model() == "outer"
        assert current_run_requested_model_lane() == Lane.FRONTIER_API

    assert result.output == "routed"
    assert gateway.requests[0].requested_model is None
    assert gateway.requests[0].lane == Lane.PRIVATE_VLLM
    assert current_run_requested_model() is None
    assert current_run_requested_model_lane() is None


def test_same_session_switches_model_without_losing_prior_history(engine: sa.Engine) -> None:
    gateway = ScriptedGateway([text_completion("first-answer"), text_completion("second-answer")])
    session, store = _session(engine, gateway)
    principal = make_principal(department="eng")

    first = asyncio.run(
        session.run(
            "first-prompt",
            principal=principal,
            requested_model="anthropic-frontier",
            requested_model_lane=Lane.FRONTIER_API,
        )
    )
    second = asyncio.run(
        session.run(
            "second-prompt",
            principal=principal,
            requested_model="private-gpu",
            requested_model_lane=Lane.PRIVATE_VLLM,
        )
    )

    assert first.session_id == second.session_id == session.session_id
    assert [request.requested_model for request in gateway.requests] == [
        "anthropic-frontier",
        "private-gpu",
    ]
    assert [request.lane for request in gateway.requests] == [
        Lane.FRONTIER_API,
        Lane.PRIVATE_VLLM,
    ]
    second_wire = json.dumps(
        [message.model_dump() for message in gateway.requests[1].messages], default=str
    )
    assert "first-prompt" in second_wire
    assert "first-answer" in second_wire
    history = asyncio.run(
        store.load_conversation_history(session.session_id, "user:ada", "eng", "assistant")
    )
    assert len(history) == 4


def test_retry_requests_keep_the_same_run_selection(engine: sa.Engine) -> None:
    gateway = ScriptedGateway([text_completion(_RESIDUE), text_completion("clean")])
    session, _ = _session(engine, gateway)

    result = asyncio.run(
        session.run(
            "answer",
            principal=make_principal(department="eng"),
            requested_model="private-gpu",
            requested_model_lane=Lane.PRIVATE_VLLM,
        )
    )

    assert result.output == "clean"
    assert len(gateway.requests) == 2
    assert all(request.requested_model == "private-gpu" for request in gateway.requests)
    assert all(request.lane == Lane.PRIVATE_VLLM for request in gateway.requests)


def test_session_exception_resets_requested_model_to_prior_scope(engine: sa.Engine) -> None:
    class RaisingGateway(ScriptedGateway):
        async def complete(self, request: CompletionRequest) -> Completion:
            self.requests.append(request)
            raise RuntimeError("provider failed")

    gateway = RaisingGateway()
    session, _ = _session(engine, gateway)

    with run_requested_model_scope("outer", lane=Lane.PRIVATE_VLLM):
        with pytest.raises(RuntimeError, match="provider failed"):
            asyncio.run(
                session.run(
                    "fail",
                    principal=make_principal(department="eng"),
                    requested_model="anthropic-frontier",
                    requested_model_lane=Lane.FRONTIER_API,
                )
            )
        assert current_run_requested_model() == "outer"
        assert current_run_requested_model_lane() == Lane.PRIVATE_VLLM

    assert gateway.requests[0].requested_model == "anthropic-frontier"
    assert gateway.requests[0].lane == Lane.FRONTIER_API
    assert current_run_requested_model() is None
    assert current_run_requested_model_lane() is None
