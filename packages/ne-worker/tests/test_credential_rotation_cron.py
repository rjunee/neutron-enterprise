# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-IDENTITY-ROTATION-CRON — the governed agent-credential rotation cron surface.

Over the REAL governed spine (a self-built AppSeams — the shared conftest is NOT
modified): the surface is ALWAYS non-empty (no config gate, unlike the pull-source
surfaces elsewhere in this package); firing the seeded job through the REAL
CronScheduler -> ToolsetScopedExecutor -> guardrails -> PDP -> audit spine rotates
every agent past its ``rotate_after_seconds`` window and leaves a fresh agent
untouched; a rotation failure for one due agent does not block the others in the
same sweep (mirrors ``AgentCredentialService.revoke_agent``'s
sweep-then-re-raise-first-error discipline); an empty credential set is a safe,
succeeded no-op; and :func:`rotation_check_cron`'s lag-bounding formula is pinned.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
from ne.audit.store import ne_audit_log
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.models import Completion, Usage
from ne.contracts.plugins import PluginRegistry
from ne.contracts.secrets import SecretVersion
from ne.contracts.testing.memory import MemorySecretStore
from ne.contracts.types import Lane
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.agents import (
    AgentCredentialConfig,
    AgentCredentialService,
    AgentRef,
    CredentialScope,
)
from ne.identity.agents.model import CredentialState
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings
from ne.runtime.factory import create_cron_scheduler
from ne.runtime.principals import StaticAgentPrincipalProvider
from ne.runtime.scheduler import CronScheduler
from ne.worker.credential_rotation import (
    CredentialRotationSurface,
    build_credential_rotation_surface,
    rotation_check_cron,
    seed_credential_rotation_jobs,
)
from ne.worker.testing import (
    ISSUER,
    RegoFaithfulPdpFactory,
    StubChunkStore,
    StubEmbedder,
    StubSparseEncoder,
)

from .conftest import ScriptedGateway, run

_TOOL = "write.internal.identity.credential_rotation"
_JOB = "identity-credential-rotation"


# --- local test doubles (worker tests cannot import ne-identity's tests package) --


class MutableClock:
    """An injectable, advanceable wall clock (UTC)."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class ArmableFailingStore:
    """Delegates to an inner :class:`MemorySecretStore`; ``write`` can be armed to
    raise for any path containing a marker substring (proves per-agent isolation)."""

    def __init__(self, inner: MemorySecretStore, *, fail_marker: str) -> None:
        self._inner = inner
        self._fail_marker = fail_marker
        self.armed = False

    async def read(self, path: str, *, version: int | None = None) -> SecretVersion:
        return await self._inner.read(path, version=version)

    async def write(self, path: str, data: Mapping[str, str]) -> SecretVersion:
        if self.armed and self._fail_marker in path:
            raise RuntimeError("injected write failure")
        return await self._inner.write(path, data)

    async def delete(self, path: str, *, versions: Sequence[int] | None = None) -> None:
        return await self._inner.delete(path, versions=versions)

    async def undelete(self, path: str, versions: Sequence[int]) -> None:
        return await self._inner.undelete(path, versions)

    async def destroy(self, path: str, versions: Sequence[int]) -> None:
        return await self._inner.destroy(path, versions)

    async def list_secrets(self, prefix: str) -> Sequence[str]:
        return await self._inner.list_secrets(prefix)


def _platform(tmp_path: Path, *, gateway: Any | None = None) -> Platform:
    """Compose a REAL governed platform over sqlite + stub edges (self-contained).

    Default ``PermissionsSettings`` (``mode='oidc-claims'``) seeds no principal-sync
    job, keeping this suite single-job.
    """
    reset_config_hash_for_testing()
    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    key = uuid.uuid4().hex
    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-worker"),
            principal_claims=("email", "department"),
        ),
        runtime_settings=RuntimeSettings(session_database_url=f"sqlite:///{tmp_path}/rt-{key}.db"),
        audit_settings=AuditSettings(database_url=f"sqlite:///{tmp_path}/au-{key}.db"),
        permissions_settings=PermissionsSettings(database_url=f"sqlite:///{tmp_path}/pm-{key}.db"),
        gateway_settings=GatewaySettings(
            models=[
                ModelDef(
                    name="stub",
                    litellm_model="openai/stub",
                    provider="stub",
                    lane=Lane.FRONTIER_API,
                    context_window=32768,
                )
            ]
        ),
        gateway=gateway if gateway is not None else ScriptedGateway(),
        embedder=StubEmbedder(),
        sparse_encoder=StubSparseEncoder(),
        chunk_store=StubChunkStore(),
        plugin_registry=registry,
    )
    settings = ApiSettings(
        qdrant_url="http://qdrant.test", qdrant_collection="t", tei_url="http://tei.test"
    )
    return build_platform(settings, seams)


def _tool_call_completion(name: str, arguments: dict[str, Any]) -> Completion:
    line = json.dumps({"tool_call": {"name": name, "arguments": arguments}})
    return Completion(text=line, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


def _make_scheduler(platform: Platform, surface: CredentialRotationSurface) -> CronScheduler:
    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    return create_cron_scheduler(
        engine=platform.runtime_engine,
        bindings=surface.bindings,
        definitions=surface.definitions,
        sink=platform.sink,
        approval_gate=platform.gate,
        gateway=platform.gateway,
        principals=StaticAgentPrincipalProvider(surface.principals),
        settings=platform.runtime_settings,
        govern_settings=platform.govern_settings,
        ledger=platform.ledger,
        counter=platform.counter,
        extra_guardrails=(platform.scope_rail,),
        plugin_registry=registry,
    )


def _actions(engine: sa.Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(ne_audit_log.c.action)).fetchall()
    return [r[0] for r in rows]


# --- (1) the surface is ALWAYS non-empty and seeds one idempotent job --------


def test_surface_always_seeds_governed_job(tmp_path: Path) -> None:
    platform = _platform(tmp_path)
    surface = build_credential_rotation_surface(platform)
    assert len(surface.bindings) == 1
    assert len(surface.definitions) == 1
    assert len(surface.principals) == 1
    assert len(surface.jobs) == 1

    job = surface.jobs[0]
    assert job.name == _JOB
    assert job.agent == _JOB
    assert job.toolset == (_TOOL,)
    assert job.cron == "0 */1 * * *"  # default rotate_after_seconds=86400

    scheduler = _make_scheduler(platform, surface)
    seed_credential_rotation_jobs(scheduler, surface.jobs)

    jobs = [j for j in scheduler.list_jobs() if j.name == _JOB]
    assert len(jobs) == 1
    assert "job.schedule" in _actions(platform.runtime_engine)

    # A second seed with the SAME surface is idempotent — no extra
    # job.schedule/job.cancel (the reconcile_prefixed_jobs unchanged-tuple arm).
    before = _actions(platform.runtime_engine)
    seed_credential_rotation_jobs(scheduler, surface.jobs)
    after = _actions(platform.runtime_engine)
    assert after == before
    assert [j.name for j in scheduler.list_jobs() if j.name == _JOB] == [_JOB]


# --- (2) firing the job rotates a due agent, leaves a fresh one untouched ----


def test_cron_rotates_due_agent_and_leaves_fresh_agent(tmp_path: Path) -> None:
    gateway = ScriptedGateway([_tool_call_completion(_TOOL, {})])
    platform = _platform(tmp_path, gateway=gateway)

    clock = MutableClock()
    store = MemorySecretStore()
    svc = AgentCredentialService(store, platform.sink, AgentCredentialConfig(), clock=clock)
    platform = dataclasses.replace(platform, credential_service=svc)

    due_agent = AgentRef(department="sales", agent_name="due")
    run(svc.issue(due_agent, CredentialScope(tools=("read.notes",)), actor="test"))

    clock.advance(86401.0)  # past the default rotate_after_seconds=86400

    fresh_agent = AgentRef(department="sales", agent_name="fresh")
    run(svc.issue(fresh_agent, CredentialScope(tools=("read.notes",)), actor="test"))

    surface = build_credential_rotation_surface(platform)
    scheduler = _make_scheduler(platform, surface)
    seed_credential_rotation_jobs(scheduler, surface.jobs)

    now = datetime.now(UTC) + timedelta(hours=2)
    outcomes = run(scheduler.run_due_jobs(now=now))
    assert len(outcomes) == 1
    assert outcomes[0].succeeded is True

    # The due agent rotated: new v2 ACTIVE, old v1 GRACE.
    assert svc.credential_state("sales/due/v2") is CredentialState.ACTIVE
    assert svc.credential_state("sales/due/v1") is CredentialState.GRACE
    # The fresh agent is untouched: v1 ACTIVE, no v2.
    assert svc.credential_state("sales/fresh/v1") is CredentialState.ACTIVE
    assert svc.credential_state("sales/fresh/v2") is None

    # Governance parity with the other jobs: job.run on the runtime engine,
    # agent.credential.rotate (the service's own event) on the audit engine.
    assert "job.run" in _actions(platform.runtime_engine)
    assert "agent.credential.rotate" in _actions(platform.audit_engine)

    # Sole-tool execution (2026-08-28 loop-affinity incident): the scheduler
    # invokes the governed tool DIRECTLY — the shared model gateway (litellm
    # Router, loop-affine client caches) is never consulted on the cron path.
    # Counts-only run output — never agent keys/departments (C12).
    assert gateway.requests == []
    blob = outcomes[0].output
    assert '"due": 1' in blob
    assert '"rotated": 1' in blob
    assert "sales" not in blob
    assert "due/v" not in blob


# --- (3) one rotation failure does not block the rest of the sweep ----------


def test_one_rotation_failure_does_not_block_others(tmp_path: Path) -> None:
    gateway = ScriptedGateway([_tool_call_completion(_TOOL, {})])
    platform = _platform(tmp_path, gateway=gateway)

    clock = MutableClock()
    inner = MemorySecretStore()
    store = ArmableFailingStore(inner, fail_marker="flaky")
    svc = AgentCredentialService(store, platform.sink, AgentCredentialConfig(), clock=clock)
    platform = dataclasses.replace(platform, credential_service=svc)

    # rotation_due_agents() preserves insertion order — the flaky agent is issued
    # (and thus swept) FIRST.
    flaky_agent = AgentRef(department="ops", agent_name="flaky")
    good_agent = AgentRef(department="sales", agent_name="good")
    run(svc.issue(flaky_agent, CredentialScope(tools=("read.notes",)), actor="test"))
    run(svc.issue(good_agent, CredentialScope(tools=("read.notes",)), actor="test"))

    clock.advance(86401.0)  # both past rotate_after_seconds

    surface = build_credential_rotation_surface(platform)
    scheduler = _make_scheduler(platform, surface)
    seed_credential_rotation_jobs(scheduler, surface.jobs)

    store.armed = True  # arm AFTER issuance so both agents were issued cleanly

    now = datetime.now(UTC) + timedelta(hours=2)
    outcomes = run(scheduler.run_due_jobs(now=now))
    assert len(outcomes) == 1

    # The later (non-flaky) agent still rotated despite the earlier failure —
    # the sweep-then-re-raise-first-error discipline never starves a sibling.
    assert svc.credential_state("sales/good/v2") is CredentialState.ACTIVE
    assert svc.credential_state("sales/good/v1") is CredentialState.GRACE
    # The failed rotate left no partial state: v1 still ACTIVE, no v2 (the store
    # write raised BEFORE any credential mutation was recorded).
    assert svc.credential_state("ops/flaky/v1") is CredentialState.ACTIVE
    assert svc.credential_state("ops/flaky/v2") is None

    # The good agent's rotation was audited regardless of the sibling failure,
    # AND the sweep's re-raised error was itself audited as a tool.execute
    # error on the audit engine (GovernedToolExecutor._run_handler).
    audit_actions = _actions(platform.audit_engine)
    assert "agent.credential.rotate" in audit_actions
    assert "tool.execute" in audit_actions
    assert "job.run" in _actions(platform.runtime_engine)

    # The scheduler must not launder a partial handler failure into a successful
    # durable run: the good sibling was processed, while job.run truthfully
    # records the failed sweep with a class-name-only (non-leaky) output. The
    # model gateway is never consulted (sole-tool; 2026-08-28 loop-affinity
    # incident), so no agent name can leak through a model transcript either.
    assert outcomes[0].succeeded is False
    assert outcomes[0].output == "ERROR: ToolExecutionError"
    assert gateway.requests == []
    assert "flaky" not in outcomes[0].output
    assert "good" not in outcomes[0].output


# --- (4) rotation_check_cron: the documented lag<=window/24-capped-hourly pins --


@pytest.mark.parametrize(
    ("rotate_after_seconds", "expected"),
    [
        (86400, "0 */1 * * *"),
        (3600, "*/2 * * * *"),  # 3600 // 24 = 150s -> 2min
        (600, "*/1 * * * *"),  # 60s floor
        (1_000_000, "0 */1 * * *"),  # 3600s cap
    ],
)
def test_rotation_check_cron_formula(rotate_after_seconds: float, expected: str) -> None:
    assert rotation_check_cron(rotate_after_seconds) == expected


# --- (5) an empty credential set is a safe, succeeded no-op -----------------


def test_empty_service_sweep_is_noop_success(tmp_path: Path) -> None:
    gateway = ScriptedGateway([_tool_call_completion(_TOOL, {})])
    platform = _platform(tmp_path, gateway=gateway)

    surface = build_credential_rotation_surface(platform)
    scheduler = _make_scheduler(platform, surface)
    seed_credential_rotation_jobs(scheduler, surface.jobs)

    now = datetime.now(UTC) + timedelta(hours=2)
    outcomes = run(scheduler.run_due_jobs(now=now))
    assert len(outcomes) == 1
    assert outcomes[0].succeeded is True

    # Sole-tool: the tool ran directly; the gateway saw nothing (loop-affinity).
    assert gateway.requests == []
    assert '"due": 0' in outcomes[0].output
