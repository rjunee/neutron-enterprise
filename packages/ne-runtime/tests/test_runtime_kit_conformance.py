# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-RTSWAP phase (a): the reference runtime implementations are the first
passing bindings of the five runtime-slot conformance kits. Every binding
declares FULL capabilities — zero skips expected.
"""

from __future__ import annotations

import asyncio

import sqlalchemy as sa

from ne.contracts.runtime import (
    AgentSession,
    MemoryProvider,
    ScheduledJob,
    Scheduler,
    SubAgentDispatcher,
    SubAgentSpec,
    ToolRegistry,
)
from ne.contracts.testing.kits.agent_session import AgentSessionContractKit
from ne.contracts.testing.kits.memory_provider import MemoryProviderContractKit
from ne.contracts.testing.kits.scheduler import SchedulerContractKit
from ne.contracts.testing.kits.sub_agent_dispatcher import SubAgentDispatcherContractKit
from ne.contracts.testing.kits.tool_registry import ToolRegistryContractKit
from ne.contracts.types import Principal
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.dispatch import AgentDefinition, GovernedSubAgentDispatcher
from ne.runtime.memory_store import AgentMemory, SqlAlchemyEpisodicStore, ensure_memory_schema
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.principals import StaticAgentPrincipalProvider
from ne.runtime.processes import ProcessRegistry
from ne.runtime.scheduler import CronScheduler, ensure_scheduler_schema
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.runtime.tools import RuntimeToolRegistry

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_executor,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
)


class TestDepartmentAgentSessionConformance(AgentSessionContractKit):
    def create_session(self) -> AgentSession:
        engine = sa.create_engine("sqlite://")
        ensure_runtime_schema(engine)
        store = SqlAlchemySessionStore(engine)
        executor = make_executor(
            standard_bindings(ToolLog()),
            RecordingAuditSink(),
            approval_gate=NamedApproverGate("alice"),
        )
        session_id = asyncio.run(
            store.create_conversation("assistant", "eng", "user:ada")
        ).conversation_id
        return DepartmentAgentSession(
            session_id=session_id,
            agent_name="assistant",
            department="eng",
            owner_principal_id="user:ada",
            store=store,
            executor=executor,
            gateway=ScriptedGateway([]),
            settings=RuntimeSettings(),
        )

    def session_principal(self) -> Principal:
        return make_principal(department="eng")


class TestRuntimeToolRegistryConformance(ToolRegistryContractKit):
    def create_registry(self) -> ToolRegistry:
        return RuntimeToolRegistry()


class TestGovernedSubAgentDispatcherConformance(SubAgentDispatcherContractKit):
    def create_dispatcher(self) -> SubAgentDispatcher:
        sink = RecordingAuditSink()
        pdp = make_pdp(sink)
        executor = GovernedToolExecutor(
            standard_bindings(ToolLog()),
            pdp=pdp,
            guardrails=make_guardrails(sink),
            approval_gate=NamedApproverGate("alice"),
            sink=sink,
        )
        return GovernedSubAgentDispatcher(
            definitions={
                "researcher": AgentDefinition(
                    name="researcher",
                    instructions="You research things.",
                    toolset=("read.docs.search",),
                ),
            },
            executor=executor,
            gateway=ScriptedGateway([]),
            registry=ProcessRegistry(),
            sink=sink,
            settings=RuntimeSettings(),
            principal=make_principal(),
            pdp=pdp,
        )

    def make_spec(self) -> SubAgentSpec:
        return SubAgentSpec(agent="researcher", task="kit conformance task")


class TestCronSchedulerConformance(SchedulerContractKit):
    def create_scheduler(self) -> Scheduler:
        engine = sa.create_engine("sqlite://")
        sink = RecordingAuditSink()
        pdp = make_pdp(sink)
        executor = GovernedToolExecutor(
            standard_bindings(ToolLog()),
            pdp=pdp,
            guardrails=make_guardrails(sink),
            approval_gate=NamedApproverGate("alice"),
            sink=sink,
        )
        ensure_scheduler_schema(engine)
        return CronScheduler(
            engine=engine,
            definitions={
                "researcher": AgentDefinition(
                    name="researcher", instructions="Research.", toolset=("read.docs.search",)
                ),
            },
            executor=executor,
            pdp=pdp,
            gateway=ScriptedGateway([]),
            registry=ProcessRegistry(),
            sink=sink,
            settings=RuntimeSettings(),
            principals=StaticAgentPrincipalProvider({"researcher": make_principal()}),
        )

    def make_job(self, name: str) -> ScheduledJob:
        return ScheduledJob(
            name=name, cron="*/5 * * * *", agent="researcher", toolset=("read.docs.search",)
        )


class TestAgentMemoryConformance(MemoryProviderContractKit):
    def create_provider(self) -> MemoryProvider:
        engine = sa.create_engine("sqlite://")
        ensure_memory_schema(engine)
        return AgentMemory(SqlAlchemyEpisodicStore(engine), "agent-kit", session_id="kit-session")
