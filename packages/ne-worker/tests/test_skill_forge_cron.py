# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-1b — the governed Skill Forge cron surface + seeding.

Mirrors ``test_principal_sync_cron`` over the REAL governed spine: default-off
(forge_enabled=False) ⇒ EMPTY surface / zero jobs; enabled ⇒ exactly one governed
job that RUNS as an isolated toolset-scoped agent via CronScheduler (scripted
tool-calling gateway) — succeeded, job.run + skill.forge_run audited, the handler
IGNORES injected hostile args; the seed is non-destructive on an empty restart.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
from ne.audit.store import ne_audit_log
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.models import Completion, Usage
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane
from ne.flywheel.config import FlywheelSettings
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings
from ne.runtime.factory import create_cron_scheduler
from ne.runtime.principals import StaticAgentPrincipalProvider
from ne.runtime.scheduler import CronScheduler
from ne.skills.governed_forge import FORGE_TOOL_NAME
from ne.worker.skill_forge import (
    SkillForgeSurface,
    build_skill_forge_surface,
    seed_skill_forge_jobs,
)
from ne.worker.testing import (
    ISSUER,
    RegoFaithfulPdpFactory,
    StubChunkStore,
    StubEmbedder,
    StubSparseEncoder,
)

from .conftest import ScriptedGateway, run

_JOB = "skill-forge"


def _platform(
    tmp_path: Path,
    *,
    forge_enabled: bool,
    monkeypatch: Any,
    gateway: Any | None = None,
) -> Platform:
    reset_config_hash_for_testing()
    monkeypatch.setenv("NE_SKILLS_FORGE_ENABLED", "true" if forge_enabled else "false")
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
        flywheel_settings=FlywheelSettings(
            signal_database_url=f"sqlite:///{tmp_path}/sig-{key}.db"
        ),
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


def _make_scheduler(platform: Platform, surface: SkillForgeSurface) -> CronScheduler:
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


def test_default_off_seeds_no_job(tmp_path: Path, monkeypatch: Any) -> None:
    platform = _platform(tmp_path, forge_enabled=False, monkeypatch=monkeypatch)
    surface = build_skill_forge_surface(platform)
    assert surface == SkillForgeSurface(bindings=(), definitions={}, principals={}, jobs=())
    scheduler = _make_scheduler(platform, surface)
    seed_skill_forge_jobs(scheduler, surface.jobs)
    assert [j.name for j in scheduler.list_jobs() if j.name.startswith(_JOB)] == []


def test_enabled_seeds_governed_job(tmp_path: Path, monkeypatch: Any) -> None:
    platform = _platform(tmp_path, forge_enabled=True, monkeypatch=monkeypatch)
    surface = build_skill_forge_surface(platform)
    assert len(surface.bindings) == 1
    assert len(surface.definitions) == 1
    assert len(surface.principals) == 1
    assert len(surface.jobs) == 1

    scheduler = _make_scheduler(platform, surface)
    seed_skill_forge_jobs(scheduler, surface.jobs)
    jobs = [j for j in scheduler.list_jobs() if j.name == _JOB]
    assert len(jobs) == 1
    assert jobs[0].cron == "15 3 * * *"  # default forge_cron
    assert jobs[0].toolset == (FORGE_TOOL_NAME,)


def test_cron_fire_runs_governed_forge(tmp_path: Path, monkeypatch: Any) -> None:
    # Sole-tool (2026-08-29 U13 review F6): the scheduler invokes the forge's
    # exact singleton governed tool directly — the SCHEDULER's model gateway is
    # never consulted from the cron tick path (the scripted completion is bait
    # and must stay unserved). The forge tool's own skill-author sub-agent
    # dispatch remains its audited work; with no snapshots in this fixture it
    # authors nothing, so the gateway sees zero requests end to end.
    gateway = ScriptedGateway([_tool_call_completion(FORGE_TOOL_NAME, {})])
    platform = _platform(tmp_path, forge_enabled=True, monkeypatch=monkeypatch, gateway=gateway)
    surface = build_skill_forge_surface(platform)
    assert surface.definitions[_JOB].scheduled_execution == "sole-tool"
    scheduler = _make_scheduler(platform, surface)
    seed_skill_forge_jobs(scheduler, surface.jobs)

    now = datetime.now(UTC) + timedelta(days=1)
    outcomes = run(scheduler.run_due_jobs(now=now))
    assert len(outcomes) == 1
    assert outcomes[0].succeeded is True

    assert "job.run" in _actions(platform.runtime_engine)
    # The forge's own governed run event landed on the skills/flywheel engine.
    assert platform.skills_engine is not None
    assert "skill.forge_run" in _actions(platform.skills_engine)

    # Counts-only JSON (no snapshots ⇒ zero authored, honest counts) is the job
    # output itself; no model round-trip carried it.
    assert "authored" in outcomes[0].output
    assert gateway.requests == []


def test_cron_fire_has_no_injectable_arg_surface(tmp_path: Path, monkeypatch: Any) -> None:
    # Under sole-tool there is NO model in the loop to inject arguments: the
    # scheduler calls the one granted tool with an EMPTY argument map by
    # construction, so the scripted hostile arg map has no path to the handler
    # (the empty-input-schema toolargs guardrail remains beneath as depth).
    gateway = ScriptedGateway(
        [_tool_call_completion(FORGE_TOOL_NAME, {"evil": "steer", "batch_limit": 999999})]
    )
    platform = _platform(tmp_path, forge_enabled=True, monkeypatch=monkeypatch, gateway=gateway)
    surface = build_skill_forge_surface(platform)
    scheduler = _make_scheduler(platform, surface)
    seed_skill_forge_jobs(scheduler, surface.jobs)

    now = datetime.now(UTC) + timedelta(days=1)
    outcomes = run(scheduler.run_due_jobs(now=now))
    assert [o.succeeded for o in outcomes] == [True]
    assert gateway.requests == []  # the hostile completion had no path to run
    # The un-steered pass ran normally (skill.forge_run audited).
    assert platform.skills_engine is not None
    assert "skill.forge_run" in _actions(platform.skills_engine)


def test_seed_is_non_destructive_on_empty_restart(tmp_path: Path, monkeypatch: Any) -> None:
    platform = _platform(tmp_path, forge_enabled=True, monkeypatch=monkeypatch)
    surface = build_skill_forge_surface(platform)
    scheduler = _make_scheduler(platform, surface)
    seed_skill_forge_jobs(scheduler, surface.jobs)
    assert [j.name for j in scheduler.list_jobs() if j.name == _JOB] == [_JOB]

    # A transiently-unconfigured reboot: empty desired set must NOT wipe the peer.
    seed_skill_forge_jobs(scheduler, ())
    assert [j.name for j in scheduler.list_jobs() if j.name == _JOB] == [_JOB]
