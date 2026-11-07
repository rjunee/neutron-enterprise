# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Server-side dispatch caps — depth, global concurrency, local fan-out (NE-034).

A lying ``SubAgentSpec`` can only NARROW its budget, never widen it; global
concurrency is registry-counted, never trusted from the spec; every cap rejection
emits its ``subagent.cap_rejected`` AuditEvent before raising.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from ne.contracts.models import CompletionRequest
from ne.contracts.runtime import SubAgentSpec
from ne.runtime.config import DispatchSettings, RuntimeSettings
from ne.runtime.dispatch import AgentDefinition, GovernedSubAgentDispatcher
from ne.runtime.errors import DispatchCapExceededError
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.processes import ProcessRegistry
from ne.schema import AuditOutcome

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
    text_completion,
)

_DEFINITIONS = {
    "researcher": AgentDefinition(name="researcher", instructions="Research.", toolset=()),
}


def _dispatcher(
    sink: Any,
    *,
    gateway: Any,
    registry: ProcessRegistry | None = None,
    settings: RuntimeSettings | None = None,
) -> GovernedSubAgentDispatcher:
    pdp = make_pdp(sink)
    executor = GovernedToolExecutor(
        standard_bindings(ToolLog()),
        pdp=pdp,
        guardrails=make_guardrails(sink),
        approval_gate=NamedApproverGate("alice"),
        sink=sink,
    )
    return GovernedSubAgentDispatcher(
        definitions=_DEFINITIONS,
        executor=executor,
        gateway=gateway,
        registry=registry or ProcessRegistry(),
        sink=sink,
        settings=settings or RuntimeSettings(),
        principal=make_principal(),
        pdp=pdp,
    )


def test_lying_spec_max_depth_cannot_exceed_server_cap() -> None:
    sink = RecordingAuditSink()
    settings = RuntimeSettings(dispatch=DispatchSettings(max_depth=2))
    root = _dispatcher(sink, gateway=ScriptedGateway([]), settings=settings)
    lying = SubAgentSpec(agent="researcher", task="t", max_depth=999)

    # Chain child_for until the SERVER budget (max_depth=2) is exhausted — the lying
    # spec.max_depth=999 cannot widen it.
    child = root.child_for(lying)
    grandchild = child.child_for(lying)
    with pytest.raises(DispatchCapExceededError) as exc:
        asyncio.run(grandchild.dispatch(lying))
    assert exc.value.reason == "depth"
    assert exc.value.scope == "server"
    rejected = sink.with_action("subagent.cap_rejected")
    assert rejected and rejected[-1].outcome == AuditOutcome.denied.value


def test_spec_max_depth_narrows_child_budget() -> None:
    sink = RecordingAuditSink()
    settings = RuntimeSettings(dispatch=DispatchSettings(max_depth=2))
    root = _dispatcher(sink, gateway=ScriptedGateway([]), settings=settings)
    # A spec that NARROWS depth to 1: the child's budget collapses to 0.
    child = root.child_for(SubAgentSpec(agent="researcher", task="t", max_depth=1))
    with pytest.raises(DispatchCapExceededError) as exc:
        asyncio.run(child.dispatch(SubAgentSpec(agent="researcher", task="t")))
    assert exc.value.reason == "depth"


def test_global_concurrency_cap_enforced_via_registry() -> None:
    sink = RecordingAuditSink()
    settings = RuntimeSettings(dispatch=DispatchSettings(max_concurrent_subagents=1))
    registry = ProcessRegistry()
    # Prime the registry to the global cap with a live sub-agent.
    registry.register(kind="subagent", name="other", principal_id="p")
    dispatcher = _dispatcher(
        sink, gateway=ScriptedGateway([text_completion("x")]), registry=registry, settings=settings
    )
    with pytest.raises(DispatchCapExceededError) as exc:
        asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="t")))
    assert exc.value.reason == "concurrency"
    assert exc.value.scope == "server"
    rejected = sink.with_action("subagent.cap_rejected")
    assert rejected and rejected[-1].outcome == AuditOutcome.denied.value


class _Controller:
    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()


class _CoordGateway:
    """Signals when a dispatch has reached the model (past the local-fanout increment),
    then blocks until released — making the concurrency window deterministic."""

    def __init__(self, controller: _Controller) -> None:
        self._c = controller
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Any:
        self.requests.append(request)
        self._c.reached.set()
        await self._c.release.wait()
        return text_completion("A done")

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


def test_spec_max_concurrency_narrows_local_fanout() -> None:
    controller = _Controller()
    sink = RecordingAuditSink()
    dispatcher = _dispatcher(sink, gateway=_CoordGateway(controller))

    async def scenario() -> None:
        task_a = asyncio.create_task(
            dispatcher.dispatch(SubAgentSpec(agent="researcher", task="a", max_concurrency=1))
        )
        await controller.reached.wait()  # A is past the increment (_active_local == 1)
        with pytest.raises(DispatchCapExceededError) as exc:
            await dispatcher.dispatch(SubAgentSpec(agent="researcher", task="b", max_concurrency=1))
        assert exc.value.reason == "concurrency"
        assert exc.value.scope == "spec"
        controller.release.set()
        result_a = await task_a
        assert result_a.succeeded is True

    asyncio.run(scenario())
    rejected = sink.with_action("subagent.cap_rejected")
    assert any("spec" in (r.detail or "") for r in rejected)


def test_every_cap_rejection_is_audited_before_raise() -> None:
    sink = RecordingAuditSink()
    settings = RuntimeSettings(dispatch=DispatchSettings(max_depth=1))
    root = _dispatcher(sink, gateway=ScriptedGateway([]), settings=settings)
    # child budget collapses to 0 → the next dispatch is a depth rejection.
    child = root.child_for(SubAgentSpec(agent="researcher", task="t"))
    with contextlib.suppress(DispatchCapExceededError):
        asyncio.run(child.dispatch(SubAgentSpec(agent="researcher", task="t")))
    # By the time the error surfaced, the audit event was already appended.
    assert sink.with_action("subagent.cap_rejected")


def test_local_fanout_slot_reserved_before_await() -> None:
    # RED CONTROL: pre-fix, the local-cap CHECK and the _active_local increment were
    # separated by an `await self._emit(dispatch audit)`, so two GATHERED dispatches
    # both passed the check (each saw _active_local == 0) before either reserved,
    # over-fanning-out past max_concurrency. The fix reserves the slot in the SAME
    # synchronous step as the check (no intervening await).
    controller = _Controller()
    sink = RecordingAuditSink()
    dispatcher = _dispatcher(sink, gateway=_CoordGateway(controller))

    async def scenario() -> list[Any]:
        results = asyncio.gather(
            dispatcher.dispatch(SubAgentSpec(agent="researcher", task="a", max_concurrency=1)),
            dispatcher.dispatch(SubAgentSpec(agent="researcher", task="b", max_concurrency=1)),
            return_exceptions=True,
        )
        await controller.reached.wait()  # the winner reached the model (slot held)
        controller.release.set()
        return list(await results)

    outcomes = asyncio.run(scenario())
    errors = [o for o in outcomes if isinstance(o, DispatchCapExceededError)]
    assert len(errors) == 1
    assert errors[0].reason == "concurrency"
    assert errors[0].scope == "spec"
    succeeded = [o for o in outcomes if not isinstance(o, Exception)]
    assert len(succeeded) == 1 and succeeded[0].succeeded is True
    assert dispatcher._active_local == 0  # slot released after both settled


def test_slot_released_after_registry_rejection() -> None:
    sink = RecordingAuditSink()
    settings = RuntimeSettings(dispatch=DispatchSettings(max_concurrent_subagents=1))
    registry = ProcessRegistry()
    registry.register(kind="subagent", name="other", principal_id="p")  # prime to global cap
    dispatcher = _dispatcher(
        sink,
        gateway=ScriptedGateway([text_completion("x")]),
        registry=registry,
        settings=settings,
    )
    with pytest.raises(DispatchCapExceededError) as exc:
        asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="t")))
    assert exc.value.scope == "server"
    # The local fan-out slot reserved before the GLOBAL registry check was released
    # on the rejection path — the outer finally dominates every exit.
    assert dispatcher._active_local == 0
