# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa

from ne.api.agent_manifest import load_agent_manifest
from ne.api.agent_skills import SkillActivation, current_run_skill_pins
from ne.api.knowledge_scope import current_run_knowledge_scope
from ne.api.model_routing import ModelRouteStore, ensure_model_route_schema
from ne.contracts.types import Lane, Principal
from ne.gateway.config import GatewaySettings, ModelDef
from ne.govern.config import GovernSettings
from ne.runtime.agent import AgentExecutionBinding
from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import RunActivationError
from ne.runtime.run_finalizer import RunTurnTarget
from ne.runtime.run_runner import RunExecutionContext
from ne.runtime.run_store import AgentRun, RunState
from ne.runtime.source_results import (
    complete_run_delivery_observation,
    model_call_scope,
    run_delivery_observation_scope,
)
from ne.runtime.workflow_execution_profile import ExecutionStrategy
from ne.skills.registry import SkillPin
from ne.worker.run_execution import (
    ProfileBoundRunExecutionAdapter,
    build_profile_bound_run_execution_adapter,
)

_ROOT = Path(__file__).parents[3]
_REGISTRY = _ROOT / "deploy/agents/registry.toml"


class _Resolver:
    async def resolve(self, subject: str) -> Principal:
        return Principal(id=subject)


@dataclass(frozen=True)
class _CapturingAgentic:
    calls: list[dict[str, Any]]
    events: list[str]
    instructions: str = "activation-required"
    max_tool_calls: int = 10
    total_tokens_limit: int = 28_000
    wall_clock_seconds: float = 120.0
    max_parked_writes: int = 1

    async def run(self, **kwargs: Any) -> Any:
        self.events.append("model")
        pins = current_run_skill_pins()
        assert pins is not None
        # The Agent's DECLARED knowledge boundary is bound for the whole run,
        # exactly like its frozen Skill pins. Captured here rather than asserted
        # in place so the tests below can pin the value the REGISTRY declares.
        scope = current_run_knowledge_scope()
        assert scope is not None, "an agentic run must never reach the store unscoped"
        self.calls.append(
            {**kwargs, "pins": pins, "scope": scope, "instructions": self.instructions}
        )
        complete_run_delivery_observation()
        return SimpleNamespace(outcome="complete", output="bounded output")


class _Skills:
    def __init__(self, definition: Any, events: list[str]) -> None:
        grant = definition.skills[0]
        self.pin = SkillPin(
            name=grant.skill_id,
            version=grant.version,
            content_hash=grant.content_hash,
        )
        self.events = events
        self.surface_hash = "d" * 64

    async def activate(
        self, definition: Any, principal: Principal, *, surface_key: str
    ) -> SkillActivation:
        self.events.append("activate")
        return SkillActivation(
            agent_id=definition.agent_id,
            agent_version=definition.version,
            surface_key=surface_key,
            pins=(self.pin,),
            index_text="Activated skills:\n- meridian-hardship-draft: bounded description",
            index_hash="c" * 64,
            surface_hash=self.surface_hash,
        )


class _Receipts:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.intent: dict[str, Any] | None = None

    async def record_skill_activation(self, **values: Any) -> Any:
        self.events.append("receipt")
        if self.intent is None:
            self.intent = values
        elif self.intent != values:
            raise RuntimeError("Run attempt was replayed under a different Skill activation")
        return SimpleNamespace(receipt_hash="e" * 64)


def _definition() -> Any:
    # NE-CS-SPLIT: the Skill-pinned agentic agent is Meridian Customer Support
    # now (the read-only Analyst carries no Skill to activate).
    return next(
        definition
        for definition in load_agent_manifest(_REGISTRY).manifest.agents
        if definition.label == "Meridian Customer Support"
    )


def _context(definition: Any) -> RunExecutionContext:
    now = datetime.now(UTC)
    run = AgentRun(
        "run-1",
        "conversation-1",
        "owner-1",
        "claims",
        definition.label,
        definition.agent_id,
        definition.version,
        definition.default_strategy,
        "a" * 64,
        "b" * 64,
        "snapshot-receipt",
        1,
        "Run 1",
        RunState.RUNNING,
        now,
        now,
        None,
    )
    return RunExecutionContext(
        run=run,
        attempt_id="attempt-1",
        target=RunTurnTarget(
            conversation_id="conversation-1",
            run_id="run-1",
            turn_id="turn-1",
            turn_sequence=1,
            attachment_id="attachment-1",
            attempt_id="attempt-1",
        ),
        input_bytes=b"PRIVATE accepted Run input",
        gateway=cast(Any, object()),
        executor=cast(Any, object()),
        audit_sink=cast(Any, object()),
        recovered_uncertain_model_attempt=False,
    )


async def _execute(adapter: ProfileBoundRunExecutionAdapter, context: RunExecutionContext) -> Any:
    with (
        run_delivery_observation_scope(run_id="run-1", attempt_id="attempt-1"),
        model_call_scope(run_id="run-1", model_call_attempt_id="attempt-1"),
    ):
        return await adapter.execute(context)


def test_agentic_run_activates_exact_pin_and_receipt_before_scoped_model_effect() -> None:
    definition = _definition()
    events: list[str] = []
    calls: list[dict[str, Any]] = []
    agentic = _CapturingAgentic(calls, events)
    binding = AgentExecutionBinding(
        agent_id=definition.agent_id,
        agent_version=definition.version,
        strategy=ExecutionStrategy.AGENTIC,
        agentic_adapter=cast(Any, agentic),
    )
    skills = _Skills(definition, events)
    receipts = _Receipts(events)
    scoped = object()
    effective_grants = tuple(
        grant for grant in definition.tool_grants if grant != "read.knowledge.graph_path"
    )

    def scope_executor(executor: Any, *, granted: Any, scope: str) -> object:
        events.append("scope")
        assert executor is context.executor
        assert tuple(granted) == effective_grants
        assert scope == "agent-run:run-1:attempt-1"
        return scoped

    catalog = SimpleNamespace(
        get_visible=lambda agent_id, principal: SimpleNamespace(
            definition=definition,
            present=True,
            available=True,
        )
    )
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={definition.agent_id: binding},
        principal_resolver=_Resolver(),
        deterministic_executor=cast(Any, None),
        runtime_settings=RuntimeSettings(task="generic-task"),
        sink=object(),
        catalog=catalog,
        agent_skills=skills,
        run_store=receipts,
        effective_tool_grants={definition.agent_id: effective_grants},
        scope_executor=scope_executor,
    )
    context = _context(definition)

    async def scenario() -> None:
        assert (await _execute(adapter, context)).outcome == "execution_complete"
        # A worker restart/replay of the same attempt reproduces the same receipt.
        assert (await _execute(adapter, context)).outcome == "execution_complete"
        skills.surface_hash = "f" * 64
        with pytest.raises(RuntimeError, match="different Skill activation"):
            await _execute(adapter, context)

    asyncio.run(scenario())

    assert events[:4] == ["activate", "receipt", "scope", "model"]
    assert events[4:8] == ["activate", "receipt", "scope", "model"]
    assert events[8:] == ["activate", "receipt"]
    assert len(calls) == 2
    call = calls[0]
    assert call["task"] == "PRIVATE accepted Run input"
    assert call["settings"].task == definition.model_policy_id == "agent.meridian-analyst.v2"
    assert call["executor"] is scoped
    assert call["instructions"] == (
        "Activated skills:\n- meridian-hardship-draft: bounded description"
    )
    assert tuple(call["pins"]) == ("meridian-hardship-draft",)
    # The scope the REGISTRY declares, enforced as a retrieval boundary rather
    # than merely recorded as an availability fact (see ne.api.knowledge_scope).
    # NE-CS-WHOLE-LADDER: Customer Support now holds the five hardship read.kg
    # templates, so the Tier-3 release joins its declared scope exactly as on
    # the Analyst (it serves no chunk-store corpus, so it cannot widen a
    # `read.knowledge.search` filter — ne.api.knowledge_scope refuses a scope
    # naming only it rather than silently widening).
    assert call["scope"] == frozenset({"meridian-servicing", "meridian-governed-local-t3"})
    assert current_run_skill_pins() is None
    assert current_run_knowledge_scope() is None


def test_agentic_run_binds_a_scope_even_when_the_agent_declares_no_source() -> None:
    """An Agent declaring nothing gets an EMPTY scope, never an ABSENT one.

    ``None`` means "no Agent Run is in scope" and leaves the search unfiltered —
    correct for an interactive chat turn, catastrophic for an agentic run. The
    binding is therefore unconditional: a definition with no declared knowledge
    source binds ``frozenset()``, which ``read.knowledge.search`` refuses rather
    than widening to every corpus in the store.
    """
    definition = _definition().model_copy(
        update={"knowledge_sources": (), "optional_knowledge_sources": ()}
    )
    events: list[str] = []
    calls: list[dict[str, Any]] = []
    agentic = _CapturingAgentic(calls, events)
    binding = AgentExecutionBinding(
        agent_id=definition.agent_id,
        agent_version=definition.version,
        strategy=ExecutionStrategy.AGENTIC,
        agentic_adapter=cast(Any, agentic),
    )
    effective_grants = tuple(definition.tool_grants)
    catalog = SimpleNamespace(
        get_visible=lambda agent_id, principal: SimpleNamespace(
            definition=definition, present=True, available=True
        )
    )
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={definition.agent_id: binding},
        principal_resolver=_Resolver(),
        deterministic_executor=cast(Any, None),
        runtime_settings=RuntimeSettings(task="generic-task"),
        sink=object(),
        catalog=catalog,
        agent_skills=_Skills(definition, events),
        run_store=_Receipts(events),
        effective_tool_grants={definition.agent_id: effective_grants},
        scope_executor=lambda executor, *, granted, scope: object(),
    )
    context = _context(definition)

    assert asyncio.run(_execute(adapter, context)).outcome == "execution_complete"
    assert calls[0]["scope"] == frozenset()
    assert calls[0]["scope"] is not None


def test_agentic_run_rejects_current_catalog_version_fallback_before_activation() -> None:
    definition = _definition()
    calls: list[dict[str, Any]] = []
    events: list[str] = []
    binding = AgentExecutionBinding(
        agent_id=definition.agent_id,
        agent_version=definition.version,
        strategy=ExecutionStrategy.AGENTIC,
        agentic_adapter=cast(Any, _CapturingAgentic(calls, events)),
    )
    newer = definition.model_copy(update={"version": definition.version + 1})
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={definition.agent_id: binding},
        principal_resolver=_Resolver(),
        deterministic_executor=cast(Any, None),
        runtime_settings=RuntimeSettings(),
        sink=object(),
        catalog=SimpleNamespace(
            get_visible=lambda agent_id, principal: SimpleNamespace(
                definition=newer, present=True, available=True
            )
        ),
        agent_skills=_Skills(definition, events),
        run_store=_Receipts(events),
    )
    # The refusal is unchanged; it is now TYPED so the run publishes WHY instead
    # of "Recorded failure class: RuntimeError".
    with pytest.raises(RunActivationError, match="not the version the catalog now holds"):
        asyncio.run(_execute(adapter, _context(definition)))
    assert events == []
    assert calls == []


def test_production_builder_supplies_the_complete_protected_run_composition() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_model_route_schema(engine)
    gateway_settings = GatewaySettings(
        models=[
            ModelDef(
                name="private-test",
                litellm_model="hosted_vllm/test",
                provider="hosted_vllm",
                lane=Lane.PRIVATE_VLLM,
                api_base="http://vllm.test/v1",
                context_window=4096,
            )
        ]
    )
    platform = SimpleNamespace(
        agent_execution_bindings={},
        effective_agent_tool_grants={},
        expander=object(),
        runtime_settings=RuntimeSettings(),
        sink=object(),
        source_guard=object(),
        agent_catalog=object(),
        agent_skills=object(),
        agent_run_store=object(),
        gateway_settings=gateway_settings,
        audit_engine=engine,
        govern_settings=GovernSettings(),
        config_hash="a" * 64,
        egress_settings_provider=None,
        inference_lifecycle=None,
        route_store=ModelRouteStore(engine, gateway_settings),
        # NE-FRONTIER-LIVE: the worker's ModelSelectionService reads the SAME
        # default-OFF frontier-warm flag the API root does.
        api_settings=SimpleNamespace(frontier_warm_enabled=False),
        gateway=object(),
        workflow_dispatcher=object(),
        gate=object(),
        protected_source_output_writer=object(),
        protected_source_execution_writer=object(),
        # NE-GA-RUN1: the Platform dataclass carries both with these defaults.
        grounding=None,
        grounded_run_agent_ids=(),
        # NE-GA-RUN4: and the Run-path strictness knob with its strict default.
        grounded_run_strictness="strict",
    )
    built = build_profile_bound_run_execution_adapter(platform)
    assert built._catalog is platform.agent_catalog
    assert built._agent_skills is platform.agent_skills
    assert built._run_store is platform.agent_run_store
    assert built._model_selection is not None
    # Default OFF ⇒ no warmer ⇒ the selection authority makes no provider call
    # of its own, exactly as before this seam existed.
    assert built._model_selection._frontier_warm is None

    engine.dispose()
