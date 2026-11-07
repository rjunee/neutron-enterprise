# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-FIX-SYNC-INVOKERS (P2-20) — the governed principal-sync cron surface + seeding.

Over the REAL governed spine (a self-built AppSeams passing a fixture-mode
PermissionsSettings — the shared conftest is NOT modified): default oidc-claims ⇒ zero
jobs; a configured fixture pull source ⇒ exactly one governed job that RUNS as an
isolated toolset-scoped agent via CronScheduler (scripted tool-calling gateway) —
succeeded, principal-store rows populated, counts-only tool output (NO changed_members,
C12), job.schedule/job.run + permissions.principal_sync audit events; the seed is
non-destructive on an empty restart; a listed-but-misconfigured source fails loud at
surface build.
"""

from __future__ import annotations

import json
import uuid
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
from ne.contracts.types import Lane
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings
from ne.runtime.factory import create_cron_scheduler
from ne.runtime.principals import StaticAgentPrincipalProvider
from ne.runtime.scheduler import CronScheduler
from ne.worker.principal_sync import (
    PrincipalSyncSurface,
    build_principal_sync_surface,
    interval_to_cron,
    seed_principal_sync_jobs,
)
from ne.worker.testing import (
    ISSUER,
    RegoFaithfulPdpFactory,
    StubChunkStore,
    StubEmbedder,
    StubSparseEncoder,
)

from .conftest import ScriptedGateway, run

_TOOL = "write.internal.permissions.principal_sync"
_JOB = "permissions-principal-sync"

_FIXTURE = {
    "name": "fixture",
    "snapshots": [
        {
            "is_full": True,
            "principals": [
                {"principal_id": "u-ada", "kind": "user", "display_name": "Ada"},
                {"principal_id": "g-eng", "kind": "group"},
            ],
            "edges": [{"member_id": "u-ada", "group_id": "g-eng"}],
            "replaced_groups": ["g-eng"],
            "replaced_members": [],
            "removed_principal_ids": [],
            "state": {},
        }
    ],
}


def _write_fixture(tmp_path: Path) -> str:
    path = tmp_path / f"principals-{uuid.uuid4().hex}.json"
    path.write_text(json.dumps(_FIXTURE), encoding="utf-8")
    return str(path)


def _platform(
    tmp_path: Path,
    *,
    perms: PermissionsSettings,
    gateway: Any | None = None,
) -> Platform:
    """Compose a REAL governed platform over sqlite + stub edges (self-contained).

    Mirrors the shared conftest's ``sqlite_seams`` but takes an explicit
    ``PermissionsSettings`` so a test can drive fixture/entra/oidc-claims mode — the
    shared conftest is deliberately NOT edited.
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
        permissions_settings=perms,
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


def _fixture_perms(tmp_path: Path) -> PermissionsSettings:
    return PermissionsSettings(
        database_url=f"sqlite:///{tmp_path}/pm-{uuid.uuid4().hex}.db",
        mode="fixture",
        fixture_path=_write_fixture(tmp_path),
    )


def _tool_call_completion(name: str, arguments: dict[str, Any]) -> Completion:
    line = json.dumps({"tool_call": {"name": name, "arguments": arguments}})
    return Completion(text=line, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


def _make_scheduler(platform: Platform, surface: PrincipalSyncSurface) -> CronScheduler:
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


# --- (1) default oidc-claims mode seeds NO job ------------------------------


def test_default_mode_seeds_no_job(tmp_path: Path) -> None:
    perms = PermissionsSettings(database_url=f"sqlite:///{tmp_path}/pm.db")
    assert perms.mode == "oidc-claims"
    platform = _platform(tmp_path, perms=perms)
    surface = build_principal_sync_surface(platform)
    assert surface == PrincipalSyncSurface(bindings=(), definitions={}, principals={}, jobs=())
    scheduler = _make_scheduler(platform, surface)
    seed_principal_sync_jobs(scheduler, surface.jobs)
    assert [j.name for j in scheduler.list_jobs() if j.name.startswith(_JOB)] == []


# --- (2) fixture mode seeds exactly one governed job at the default cron -----


def test_fixture_mode_seeds_governed_job(tmp_path: Path) -> None:
    platform = _platform(tmp_path, perms=_fixture_perms(tmp_path))
    surface = build_principal_sync_surface(platform)
    assert len(surface.bindings) == 1
    assert len(surface.definitions) == 1
    assert len(surface.principals) == 1
    assert len(surface.jobs) == 1

    scheduler = _make_scheduler(platform, surface)
    seed_principal_sync_jobs(scheduler, surface.jobs)

    jobs = [j for j in scheduler.list_jobs() if j.name == _JOB]
    assert len(jobs) == 1
    assert jobs[0].cron == "*/15 * * * *"  # default 900s
    assert jobs[0].agent == _JOB
    assert jobs[0].toolset == (_TOOL,)
    assert "job.schedule" in _actions(platform.runtime_engine)


# --- (3) firing the cron runs run_once through the governed executor ---------


def test_cron_runs_governed_principal_sync(tmp_path: Path) -> None:
    gateway = ScriptedGateway([_tool_call_completion(_TOOL, {})])
    platform = _platform(tmp_path, perms=_fixture_perms(tmp_path), gateway=gateway)

    surface = build_principal_sync_surface(platform)
    scheduler = _make_scheduler(platform, surface)
    seed_principal_sync_jobs(scheduler, surface.jobs)

    now = datetime.now(UTC) + timedelta(hours=1)
    outcomes = run(scheduler.run_due_jobs(now=now))
    assert len(outcomes) == 1
    assert outcomes[0].succeeded is True

    # The fixture principals + membership edge landed in the store (non-vacuous).
    assert platform.perm_store.is_known("u-ada")
    assert platform.perm_store.is_known("g-eng")
    assert platform.perm_store.direct_groups_of(["u-ada"])["u-ada"] == frozenset({"g-eng"})

    # Governed audit trail: job.run on the runtime engine, permissions.principal_sync
    # (the service's own event) on the audit engine.
    assert "job.run" in _actions(platform.runtime_engine)
    assert "permissions.principal_sync" in _actions(platform.audit_engine)

    # Sole-tool execution (2026-08-28 loop-affinity incident): the scheduler
    # invokes the governed tool DIRECTLY — the shared model gateway is never
    # consulted on the cron path — and the run output is the tool's counts-only
    # JSON, with NO changed_members.
    assert gateway.requests == []
    blob = outcomes[0].output
    assert '"principals_upserted": 2' in blob
    assert "changed_members" not in blob


# --- (4) empty restart is non-destructive (the NE-INGEST Codex-P1 lesson) ----


def test_empty_config_restart_preserves_peer_job(tmp_path: Path) -> None:
    platform = _platform(tmp_path, perms=_fixture_perms(tmp_path))
    surface = build_principal_sync_surface(platform)
    scheduler = _make_scheduler(platform, surface)
    seed_principal_sync_jobs(scheduler, surface.jobs)
    assert [j.name for j in scheduler.list_jobs() if j.name == _JOB] == [_JOB]

    # Simulate a transiently-unconfigured reboot: empty desired set.
    seed_principal_sync_jobs(scheduler, ())

    assert [j.name for j in scheduler.list_jobs() if j.name == _JOB] == [_JOB]
    assert "job.cancel" not in _actions(platform.runtime_engine)


# --- (5) interval_to_cron encoding + documented clamps ----------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (900, "*/15 * * * *"),
        (60, "*/1 * * * *"),
        (30, "*/1 * * * *"),
        (3540, "*/59 * * * *"),
        (3600, "0 */1 * * *"),
        (7200, "0 */2 * * *"),
        (172800, "0 */23 * * *"),
    ],
)
def test_interval_to_cron(seconds: int, expected: str) -> None:
    assert interval_to_cron(seconds) == expected


# --- (6) a misconfigured fixture source fails loud at surface build ----------


def test_misconfigured_fixture_fails_loud_at_surface_build(tmp_path: Path) -> None:
    missing = str(tmp_path / "does-not-exist.json")
    perms = PermissionsSettings(
        database_url=f"sqlite:///{tmp_path}/pm-{uuid.uuid4().hex}.db",
        mode="fixture",
        fixture_path=missing,
    )
    platform = _platform(tmp_path, perms=perms)
    with pytest.raises(FileNotFoundError):
        build_principal_sync_surface(platform)
