# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-INGEST (P1-4) — the governed connector-sync cron surface + seeding proof.

Over the REAL governed spine (build_sqlite_platform: real executor/PDP/gate/scheduler,
stub network edges): unconfigured ⇒ zero jobs; a configured fixture source ⇒ exactly
one governed job that deterministically invokes its sole tool via CronScheduler with
zero model calls — succeeded, counts-only output (written==3, no cursors),
job.schedule/job.run + connector.sync audit events; the seed is idempotent and
reconciles cron changes / source removal; a listed-but-misconfigured source fails loud
at surface build.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa

import ne.worker.ingest as ingest_module
from ne.audit.store import ne_audit_log
from ne.connect.bridge import SyncReport
from ne.connect.runner import ConnectSyncSettings, SyncNotConfiguredError
from ne.contracts.models import Completion, Usage
from ne.contracts.plugins import PluginRegistry
from ne.contracts.runtime import ScheduledJob, ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.factory import create_cron_scheduler
from ne.runtime.middleware import current_tool_principal
from ne.runtime.principals import StaticAgentPrincipalProvider
from ne.runtime.scheduler import CronScheduler, ManualJobRequestStore
from ne.runtime.tools import ToolBinding
from ne.worker.ingest import (
    AuthoritySweepCronError,
    ConnectorSyncSurface,
    _sync_handler,
    build_connector_sync_surface,
    max_firing_gap_seconds,
    seed_connector_sync_jobs,
)
from ne.worker.testing import RegoFaithfulPdpFactory

from .conftest import ScriptedGateway, build_sqlite_platform, run

_TOOL = "write.internal.connect.sync_fixture"
_JOB = "connect-sync-fixture"
_FIXTURE = {
    "source": "acme-crm",
    "objects": [
        {"kind": "resource", "name": "contacts", "source_id": "acme-crm/contacts"},
        {"kind": "resource", "name": "accounts", "source_id": "acme-crm/accounts"},
    ],
    "records": [
        {
            "cursor": "2026-01-01T00:00:00Z",
            "payload": {"name": "Ada Byron", "email": "ada@example.test"},
            "record_id": "c1",
            "source_id": "acme-crm/contacts",
        },
        {
            "cursor": "2026-01-02T00:00:00Z",
            "payload": {"name": "Grace Hopper", "email": "grace@example.test"},
            "record_id": "c2",
            "source_id": "acme-crm/contacts",
        },
        {
            "cursor": "2026-01-01T00:00:00Z",
            "payload": {"name": "Globex Industries"},
            "record_id": "a1",
            "source_id": "acme-crm/accounts",
        },
    ],
    "acl": [
        {"principals": ["oidc:sales"], "resource_id": "acme-crm/contacts"},
        {"principals": ["oidc:finance"], "resource_id": "acme-crm/accounts"},
    ],
}


def _tool_call_completion(name: str, arguments: dict[str, Any]) -> Completion:
    line = json.dumps({"tool_call": {"name": name, "arguments": arguments}})
    return Completion(text=line, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


def _fixture_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixture_path = tmp_path / "fixture_source.json"
    fixture_path.write_text(json.dumps(_FIXTURE), encoding="utf-8")
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["fixture"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "quarantine.db"))
    monkeypatch.setenv("NE_CONNECT_FIXTURE_PATH", str(fixture_path))
    monkeypatch.setenv("NE_CONNECT_FIXTURE_OWNER", "data-eng@acme.test")


def _make_scheduler(platform: Any, surface: ConnectorSyncSurface) -> CronScheduler:
    """Mirror ne.worker.main._run's create_cron_scheduler call over the surface.

    ``plugin_registry`` carries the Rego-faithful fake PDP factory (the OPA wire is a
    network edge — the conftest discipline); production ``main._run`` omits it so the
    cron PDP resolves the real ``opa`` adapter against the sidecar.
    """
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


def _events(engine: sa.Engine, action: str) -> list[dict[str, Any]]:
    with engine.begin() as conn:
        rows = [
            dict(r)
            for r in conn.execute(sa.select(ne_audit_log).order_by(ne_audit_log.c.id)).mappings()
        ]
    return [r for r in rows if r["action"] == action]


# --- (t1) unconfigured ⇒ empty surface + zero jobs --------------------------


def test_unconfigured_seeds_no_jobs(tmp_path: Path) -> None:
    platform = build_sqlite_platform(tmp_path)
    surface = build_connector_sync_surface(platform)
    assert surface == ConnectorSyncSurface(bindings=(), definitions={}, principals={}, jobs=())
    scheduler = _make_scheduler(platform, surface)
    seed_connector_sync_jobs(scheduler, surface.jobs)
    assert scheduler.list_jobs() == ()


# --- (t2) configured ⇒ one governed job that RUNS + is audited --------------


def test_configured_source_seeds_governed_job_and_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture_env(monkeypatch, tmp_path)
    gateway = ScriptedGateway([])
    platform = build_sqlite_platform(tmp_path, gateway=gateway)

    surface = build_connector_sync_surface(platform)
    assert len(surface.bindings) == 1
    assert len(surface.definitions) == 1
    assert len(surface.principals) == 1
    assert len(surface.jobs) == 1
    assert surface.definitions[_JOB].scheduled_execution == "sole-tool"

    scheduler = _make_scheduler(platform, surface)
    seed_connector_sync_jobs(scheduler, surface.jobs)

    jobs = scheduler.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].name == _JOB
    assert jobs[0].toolset == (_TOOL,)
    assert jobs[0].cron == "0 * * * *"

    now = datetime.now(UTC) + timedelta(hours=2)
    outcomes = run(scheduler.run_due_jobs(now=now))
    assert len(outcomes) == 1
    assert outcomes[0].succeeded is True

    # The connector.sync event (via platform.sink → the audit engine) proves the
    # governed write of all 3 records.
    sync_events = _events(platform.audit_engine, "connector.sync")
    assert len(sync_events) == 1
    assert "written=3" in str(sync_events[0]["payload"]["detail"])
    assert sync_events[0]["actor"] == "agent:connect-sync-fixture"

    # job.schedule + job.run land on the scheduler (runtime) engine — the NE-034
    # dual-append (outbox + hash chain) is visible.
    runtime_actions = _actions(platform.runtime_engine)
    assert "job.schedule" in runtime_actions
    assert "job.run" in runtime_actions

    # The scheduled outcome is counts-only and the gateway was never consulted.
    assert '"written": 3' in outcomes[0].output
    assert "cursors" not in outcomes[0].output
    assert gateway.requests == []


def test_configured_m365_can_remain_visible_while_only_csv_is_scheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = ConnectSyncSettings(
        sources=("m365", "csv"),
        runnable_sources=("csv",),
        state_path="state.db",
        quarantine_path="quarantine.db",
    )
    stack = SimpleNamespace(
        writer=object(),
        state_store=object(),
        quarantine=object(),
        authority_for=lambda _source: None,
        chunk_reader=None,
    )
    platform = SimpleNamespace(
        connect_sync_settings=settings,
        chunk_store=object(),
        runtime_engine=object(),
        sink=object(),
        embedder=object(),
        document_lineage=object(),
        m365_graph_sync_host=None,
    )
    monkeypatch.setattr(ingest_module, "build_ingest_stack", lambda **_kwargs: stack)
    monkeypatch.setattr(
        ingest_module,
        "build_sync_settings",
        lambda source: SimpleNamespace(source_name=source, authority_enabled=False),
    )

    surface = build_connector_sync_surface(cast(Any, platform))

    assert settings.sources == ("m365", "csv")
    assert settings.sources_to_run == ("csv",)
    assert [job.name for job in surface.jobs] == ["connect-sync-csv"]
    assert set(surface.definitions) == {"connect-sync-csv"}


def test_manual_sync_preserves_the_admitted_requester_as_connector_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture_env(monkeypatch, tmp_path)
    platform = build_sqlite_platform(tmp_path)
    surface = build_connector_sync_surface(platform)
    scheduler = _make_scheduler(platform, surface)
    seed_connector_sync_jobs(scheduler, surface.jobs)
    requester = Principal(id="user:meridian-operator", groups=("ops",))

    request = ManualJobRequestStore(platform.runtime_engine).request(
        _JOB,
        source="fixture",
        requested_action="write.internal.connect.request_sync_fixture",
        tool_name=_TOOL,
        principal=requester,
        config_hash=platform.config_hash,
    )
    outcomes = run(scheduler.run_requested_jobs())

    assert len(outcomes) == 1
    assert outcomes[0].succeeded is True
    persisted = ManualJobRequestStore(platform.runtime_engine).get(request.request_id)
    assert persisted is not None and persisted.status == "succeeded"
    sync_events = _events(platform.audit_engine, "connector.sync")
    assert len(sync_events) == 1
    assert sync_events[0]["actor"] == requester.id


def test_sync_binding_refuses_execution_without_executor_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture_env(monkeypatch, tmp_path)
    platform = build_sqlite_platform(tmp_path)
    surface = build_connector_sync_surface(platform)

    with pytest.raises(RuntimeError, match="executor-bound principal"):
        run(surface.bindings[0].handler({}))

    assert _events(platform.audit_engine, "connector.sync") == []


def test_m365_graph_host_runs_only_after_t1_success_and_keeps_counts_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graph projection is handed to run_configured_sync as its ``post_sync``
    hook — it runs INSIDE the connector_sync_gate hold, after the connector body
    succeeds (2026-08-29 U13 review F3: run OUTSIDE the gate, the graph's long
    BEGIN IMMEDIATE refresh overlapped sibling syncs admitted by later ticks).
    The stub honors the real contract: post_sync is awaited before the gate
    releases, and never on the failure path.
    """
    order: list[str] = []

    async def t1(_source: str, *, post_sync: Any = None, **_kwargs: Any) -> SyncReport:
        order.append("t1")
        # m365 with a wired graph host MUST project inside the gate hold.
        assert post_sync is not None, "graph projection must ride the in-gate post_sync seam"
        await post_sync()
        return SyncReport(
            mode="backfill",
            records_seen=1,
            written=1,
            skipped_unchanged=0,
            invalid=0,
            quarantined=0,
            cursors={"secret-delta": "never-return"},
        )

    class _GraphHost:
        async def run(self, _settings: Any, **_kwargs: Any) -> Any:
            from ne.brain_graph.construct.runner import GraphSyncReport

            order.append("graph")
            return GraphSyncReport(
                mode="backfill",
                seen=1,
                upserted=1,
                unchanged=0,
                acl_healed=0,
                quarantined=0,
                unmapped=0,
                pruned=0,
                pending_edges=0,
            )

    monkeypatch.setattr("ne.worker.ingest.run_configured_sync", t1)
    stack = SimpleNamespace(
        writer=object(),
        state_store=object(),
        quarantine=None,
        authority_for=lambda source: None,
        chunk_reader=None,
    )
    platform = SimpleNamespace(sink=object(), m365_graph_sync_host=_GraphHost())
    handler = _sync_handler(
        "m365",
        platform=cast(Any, platform),
        stack=stack,
        settings=object(),
        kpi_tag=None,
        principal_provider=lambda: Principal(id="user:operator"),
    )

    result = json.loads(run(handler({})))
    assert order == ["t1", "graph"]
    assert result["written"] == 1
    assert result["graph"]["upserted"] == 1
    assert "cursors" not in result

    async def failed_t1(_source: str, *, post_sync: Any = None, **_kwargs: Any) -> SyncReport:
        del post_sync  # a failing connector body never reaches the hook
        order.append("t1")
        raise RuntimeError("T1 failed")

    monkeypatch.setattr("ne.worker.ingest.run_configured_sync", failed_t1)
    order.clear()
    failed_handler = _sync_handler(
        "m365",
        platform=cast(Any, platform),
        stack=stack,
        settings=object(),
        kpi_tag=None,
        principal_provider=lambda: Principal(id="user:operator"),
    )
    with pytest.raises(RuntimeError, match="T1 failed"):
        run(failed_handler({}))
    assert order == ["t1"]


def test_sync_actor_context_is_isolated_across_concurrent_and_nested_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture_env(monkeypatch, tmp_path)
    observed: list[tuple[str, str, str]] = []

    async def capture_sync(_source: str, *, actor: str, **_kwargs: Any) -> SyncReport:
        before = current_tool_principal()
        await asyncio.sleep(0)
        after = current_tool_principal()
        assert before is not None and after is not None
        observed.append((actor, before.id, after.id))
        return SyncReport(
            mode="backfill",
            records_seen=0,
            written=0,
            skipped_unchanged=0,
            invalid=0,
            quarantined=0,
            cursors={},
        )

    monkeypatch.setattr("ne.worker.ingest.run_configured_sync", capture_sync)
    platform = build_sqlite_platform(tmp_path)
    surface = build_connector_sync_surface(platform)

    scheduler = _make_scheduler(platform, surface)
    executor = scheduler._executor

    async def _concurrent() -> None:
        await asyncio.gather(
            executor.execute(_TOOL, {}, principal=Principal(id="user:a")),
            executor.execute(_TOOL, {}, principal=Principal(id="user:b")),
        )

    run(_concurrent())
    assert sorted(observed) == [
        ("user:a", "user:a", "user:a"),
        ("user:b", "user:b", "user:b"),
    ]

    observed.clear()
    holder: dict[str, Any] = {}
    outer_seen: list[str] = []

    async def _outer(_args: Any) -> str:
        before = current_tool_principal()
        assert before is not None
        outer_seen.append(before.id)
        await holder["executor"].execute(_TOOL, {}, principal=Principal(id="user:nested"))
        after = current_tool_principal()
        assert after is not None
        outer_seen.append(after.id)
        return "ok"

    outer_name = "write.internal.connect.request_sync_fixture"
    outer_binding = ToolBinding(
        spec=ToolSpec(
            name=outer_name,
            description="test-only nested governed execution",
            tier=ApprovalTier(1),
        ),
        handler=_outer,
    )
    nested_surface = ConnectorSyncSurface(
        bindings=(*surface.bindings, outer_binding),
        definitions=surface.definitions,
        principals=surface.principals,
        jobs=surface.jobs,
    )
    nested_scheduler = _make_scheduler(platform, nested_surface)
    nested_executor = nested_scheduler._executor
    holder["executor"] = nested_executor

    run(nested_executor.execute(outer_name, {}, principal=Principal(id="user:outer")))
    assert outer_seen == ["user:outer", "user:outer"]
    assert observed == [("user:nested", "user:nested", "user:nested")]
    assert current_tool_principal() is None


def test_sync_tool_error_fails_job_even_when_agent_returns_final_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production binding: an errored sync tool cannot yield job.run success."""
    _fixture_env(monkeypatch, tmp_path)

    async def fail_sync(_source: str, **_kwargs: Any) -> Any:
        raise ConnectionError("https://user:secret@source.invalid/sync")

    monkeypatch.setattr("ne.worker.ingest.run_configured_sync", fail_sync)
    gateway = ScriptedGateway([_tool_call_completion(_TOOL, {})])
    platform = build_sqlite_platform(tmp_path, gateway=gateway)
    surface = build_connector_sync_surface(platform)
    scheduler = _make_scheduler(platform, surface)
    seed_connector_sync_jobs(scheduler, surface.jobs)

    outcomes = run(scheduler.run_due_jobs(now=datetime.now(UTC) + timedelta(hours=2)))

    assert len(outcomes) == 1
    assert outcomes[0].succeeded is False
    assert outcomes[0].output == "ERROR: ToolExecutionError"
    assert "secret" not in outcomes[0].output
    run_events = _events(platform.runtime_engine, "job.run")
    assert len(run_events) == 1
    assert run_events[0]["outcome"] == "error"
    execute_events = _events(platform.audit_engine, "tool.execute")
    assert len(execute_events) == 1
    assert execute_events[0]["outcome"] == "error"


# --- (t3) seed is idempotent + reconciles cron change / source removal ------


def test_seed_is_idempotent_and_reconciles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(monkeypatch, tmp_path)
    platform = build_sqlite_platform(tmp_path)
    surface = build_connector_sync_surface(platform)
    scheduler = _make_scheduler(platform, surface)

    seed_connector_sync_jobs(scheduler, surface.jobs)
    seed_connector_sync_jobs(scheduler, surface.jobs)  # idempotent
    jobs = scheduler.list_jobs()
    assert len(jobs) == 1
    first_job_id = jobs[0].job_id

    # A cron override reconciles via cancel+reschedule (a NEW job_id, new cron).
    overridden = (ScheduledJob(name=_JOB, cron="*/5 * * * *", agent=_JOB, toolset=(_TOOL,)),)
    seed_connector_sync_jobs(scheduler, overridden)
    jobs = scheduler.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].cron == "*/5 * * * *"
    assert jobs[0].job_id != first_job_id

    # Authoritative removal: a NON-EMPTY desired set that omits a job cancels ONLY the
    # omitted connect-sync-* job (a deliberately dropped source). A second job reusing
    # the same defined agent/toolset stands in for a second configured source.
    fixture_job = ScheduledJob(name=_JOB, cron="*/5 * * * *", agent=_JOB, toolset=(_TOOL,))
    extra_job = ScheduledJob(
        name="connect-sync-fixture-2", cron="0 * * * *", agent=_JOB, toolset=(_TOOL,)
    )
    seed_connector_sync_jobs(scheduler, (fixture_job, extra_job))
    names = {j.name for j in scheduler.list_jobs() if j.name.startswith("connect-sync-")}
    assert names == {_JOB, "connect-sync-fixture-2"}
    seed_connector_sync_jobs(scheduler, (fixture_job,))  # drop the extra source
    names = {j.name for j in scheduler.list_jobs() if j.name.startswith("connect-sync-")}
    assert names == {_JOB}


# --- (t5) empty config is NON-DESTRUCTIVE — never wipes persisted schedules --


def test_empty_config_does_not_destroy_persisted_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P1: a worker booting with NO configured sources (default-off, or a
    transiently-omitted env / api-worker mismatch) MUST NOT cancel the persisted
    connect-sync-* schedules on the SHARED runtime DB — otherwise a single mis-
    configured restart silently wipes correctly-configured peers' ingestion jobs.
    """
    _fixture_env(monkeypatch, tmp_path)
    platform = build_sqlite_platform(tmp_path)
    surface = build_connector_sync_surface(platform)
    scheduler = _make_scheduler(platform, surface)
    seed_connector_sync_jobs(scheduler, surface.jobs)
    assert [j.name for j in scheduler.list_jobs() if j.name.startswith("connect-sync-")] == [_JOB]

    # Simulate the transiently-unconfigured reboot: empty desired set.
    seed_connector_sync_jobs(scheduler, ())

    # The persisted schedule SURVIVES (non-destructive no-op), and no job.cancel was
    # emitted for it.
    assert [j.name for j in scheduler.list_jobs() if j.name.startswith("connect-sync-")] == [_JOB]
    assert "job.cancel" not in _actions(platform.runtime_engine)


def test_explicit_empty_schedule_subset_cancels_only_connector_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture_env(monkeypatch, tmp_path)
    platform = build_sqlite_platform(tmp_path)
    surface = build_connector_sync_surface(platform)
    scheduler = _make_scheduler(platform, surface)
    seed_connector_sync_jobs(scheduler, surface.jobs)
    scheduler.schedule(
        ScheduledJob(
            name="unrelated-fixture-job",
            cron="0 * * * *",
            agent=_JOB,
            toolset=(_TOOL,),
        )
    )

    seed_connector_sync_jobs(scheduler, (), authoritative_empty=True)

    assert [job.name for job in scheduler.list_jobs() if job.name.startswith("connect-sync-")] == []
    assert [job.name for job in scheduler.list_jobs()] == ["unrelated-fixture-job"]
    assert "job.cancel" in _actions(platform.runtime_engine)


# --- (t4) a listed-but-misconfigured source fails loud at surface build -----


def test_misconfigured_listed_source_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["m365"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "quarantine.db"))
    with pytest.raises(SyncNotConfiguredError, match="m365"):
        platform = build_sqlite_platform(tmp_path)
        build_connector_sync_surface(platform)


# --- (t7) C8 — the sync agent's own model spend carries the connector's outcome ---


def test_sync_agent_definition_declares_the_connectors_kpi_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``NE_CONNECT_SYNC_KPI_TAG`` already labelled the sync's own audit events; it
    must also label the model spend of the AGENT that runs the sync.

    Without this the connector's outcome is declared, the sync events carry it,
    and the agent's completion — the only part of the job that actually costs
    money — lands in the Insights page's unattributed bucket. The tag comes from
    the SAME connector setting, so nothing new is declared here.
    """
    _fixture_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NE_CONNECT_SYNC_KPI_TAG", "corpus-freshness")
    platform = build_sqlite_platform(tmp_path)

    surface = build_connector_sync_surface(platform)

    (definition,) = surface.definitions.values()
    assert definition.kpi_tag == "corpus-freshness"


def test_sync_agent_definition_declares_no_tag_when_the_connector_declares_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control. An undeclared connector must leave the agent UNTAGGED, so its
    spend reports as unattributed rather than under a fabricated outcome."""
    _fixture_env(monkeypatch, tmp_path)
    monkeypatch.delenv("NE_CONNECT_SYNC_KPI_TAG", raising=False)
    platform = build_sqlite_platform(tmp_path)

    surface = build_connector_sync_surface(platform)

    (definition,) = surface.definitions.values()
    assert definition.kpi_tag is None


# -- 015A0-AUTHORITY-M365: the sub-SLA sweep cadence and unadvertised scope ----


def test_the_sweep_cron_floors_the_cadence_so_the_job_never_fires_more_slowly() -> None:
    from ne.worker.ingest import authority_sweep_cron

    assert authority_sweep_cron(120.0) == "*/2 * * * *"
    # 179s must not become a 3-minute job: flooring keeps the firing gap at or
    # under the cadence the sweep plan was proven against.
    assert authority_sweep_cron(179.0) == "*/2 * * * *"
    assert authority_sweep_cron(30.0) == "*/1 * * * *"


def test_the_sweep_cron_is_capped_so_it_can_never_become_the_generic_hourly_cron() -> None:
    from ne.worker.ingest import authority_sweep_cron

    assert authority_sweep_cron(3600.0) == "*/30 * * * *"
    assert authority_sweep_cron(86_400.0) == "*/30 * * * *"


def test_a_non_positive_sweep_cadence_has_no_cron() -> None:
    from ne.worker.ingest import authority_sweep_cron

    for cadence in (0.0, -1.0):
        with pytest.raises(ValueError):
            authority_sweep_cron(cadence)


class _AuthoritySettings:
    """The two members ``build_connector_sync_surface`` reads off m365 settings."""

    source_name = "m365-sharepoint"
    authority_enabled = True
    authority_sweep_cadence_seconds = 120


def _m365_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    authority: object | None,
    reason: str | None,
) -> Any:
    from ne.connect.runner import ConnectSyncSettings, IngestStack

    monkeypatch.setattr(
        "ne.worker.ingest.build_sync_settings", lambda source, **_: _AuthoritySettings()
    )
    monkeypatch.setattr(
        "ne.worker.ingest.build_ingest_stack",
        lambda **_: IngestStack(
            writer=cast(Any, object()),
            state_store=cast(Any, object()),
            quarantine=None,
            authority=cast(Any, authority),
            authority_unavailable_reason=reason,
        ),
    )
    platform = SimpleNamespace(
        connect_sync_settings=ConnectSyncSettings(
            sources=("m365",),
            state_path=str(tmp_path / "s.db"),
            quarantine_path=str(tmp_path / "q.db"),
        ),
        chunk_store=object(),
        runtime_engine=object(),
        sink=object(),
        embedder=object(),
        document_lineage=object(),
        m365_graph_sync_host=None,
    )
    return build_connector_sync_surface(cast(Any, platform))


def test_an_authority_enabled_m365_job_is_seeded_at_its_sweep_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generic connector cron is not consulted for an authority-bearing source.

    The cadence is DERIVED, so the inherited hourly DEFAULT is never what gets
    seeded. An explicit per-source override is the documented exception and is
    covered by ``test_an_explicit_cron_override_wins_over_the_derived_sweep_cadence``.
    """
    surface = _m365_surface(monkeypatch, tmp_path, authority=object(), reason=None)
    assert len(surface.jobs) == 1
    assert surface.jobs[0].cron == "*/2 * * * *"


def test_an_m365_source_whose_authority_is_unavailable_is_not_advertised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface = _m365_surface(
        monkeypatch, tmp_path, authority=None, reason="the sweep cannot beat the SLA"
    )
    assert surface.jobs == ()
    assert surface.bindings == ()
    assert surface.definitions == {}
    assert surface.principals == {}


def test_salesforce_authority_job_uses_its_factory_and_sweep_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ne.connect.runner import ConnectSyncSettings, IngestStack

    factory = object()
    monkeypatch.setattr(
        "ne.worker.ingest.build_sync_settings", lambda source, **_: _AuthoritySettings()
    )
    monkeypatch.setattr(
        "ne.worker.ingest.build_ingest_stack",
        lambda **_: IngestStack(
            writer=cast(Any, object()),
            state_store=cast(Any, object()),
            salesforce_authority_factory=factory,
        ),
    )
    platform = SimpleNamespace(
        connect_sync_settings=ConnectSyncSettings(
            sources=("salesforce",),
            state_path=str(tmp_path / "sf-s.db"),
            quarantine_path=str(tmp_path / "sf-q.db"),
        ),
        chunk_store=object(),
        runtime_engine=object(),
        sink=object(),
        embedder=object(),
        document_lineage=object(),
        m365_graph_sync_host=None,
    )
    surface = build_connector_sync_surface(cast(Any, platform))
    assert len(surface.jobs) == 1
    assert surface.jobs[0].cron == "*/2 * * * *"

    seen: list[object | None] = []

    async def capture(_source: str, *, authority: object | None, **_kwargs: Any) -> SyncReport:
        seen.append(authority)
        return SyncReport(
            mode="backfill",
            records_seen=0,
            written=0,
            skipped_unchanged=0,
            invalid=0,
            quarantined=0,
            cursors={},
        )

    monkeypatch.setattr("ne.worker.ingest.run_configured_sync", capture)
    handler = _sync_handler(
        "salesforce",
        platform=cast(Any, platform),
        stack=IngestStack(
            writer=cast(Any, object()),
            state_store=cast(Any, object()),
            salesforce_authority_factory=factory,
        ),
        settings=_AuthoritySettings(),
        kpi_tag=None,
        principal_provider=lambda: Principal(id="user:operator"),
    )
    run(handler({}))
    assert seen == [factory]


def test_salesforce_missing_authority_factory_is_not_advertised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ne.connect.runner import ConnectSyncSettings, IngestStack

    monkeypatch.setattr(
        "ne.worker.ingest.build_sync_settings", lambda source, **_: _AuthoritySettings()
    )
    monkeypatch.setattr(
        "ne.worker.ingest.build_ingest_stack",
        lambda **_: IngestStack(
            writer=cast(Any, object()),
            state_store=cast(Any, object()),
            salesforce_authority_unavailable_reason="Salesforce embedder unavailable",
        ),
    )
    platform = SimpleNamespace(
        connect_sync_settings=ConnectSyncSettings(
            sources=("salesforce",),
            state_path=str(tmp_path / "sf-missing-s.db"),
            quarantine_path=str(tmp_path / "sf-missing-q.db"),
        ),
        chunk_store=object(),
        runtime_engine=object(),
        sink=object(),
        embedder=None,
        document_lineage=object(),
        m365_graph_sync_host=None,
    )
    surface = build_connector_sync_surface(cast(Any, platform))
    assert surface.jobs == ()
    assert surface.bindings == ()


class _SalesforceAuthoritySettings:
    """The three members ``build_connector_sync_surface`` reads off salesforce settings."""

    source_name = "salesforce"
    authority_enabled = True
    authority_sweep_cadence_seconds = 120


def _authority_surface_with_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    sources: tuple[str, ...],
    cron_overrides: dict[str, str],
) -> Any:
    """One authority-enabled surface over ``sources`` with ``cron_overrides`` applied."""
    from ne.connect.runner import ConnectSyncSettings as _Settings
    from ne.connect.runner import IngestStack

    def _settings_for(source: str, **_: Any) -> Any:
        return _SalesforceAuthoritySettings() if source == "salesforce" else _AuthoritySettings()

    monkeypatch.setattr("ne.worker.ingest.build_sync_settings", _settings_for)
    monkeypatch.setattr(
        "ne.worker.ingest.build_ingest_stack",
        lambda **_: IngestStack(
            writer=cast(Any, object()),
            state_store=cast(Any, object()),
            quarantine=None,
            authority=cast(Any, object()),
            salesforce_authority_factory=cast(Any, object()),
        ),
    )
    platform = SimpleNamespace(
        connect_sync_settings=_Settings(
            sources=sources,
            cron_overrides=cron_overrides,
            state_path=str(tmp_path / "ov-s.db"),
            quarantine_path=str(tmp_path / "ov-q.db"),
        ),
        chunk_store=object(),
        runtime_engine=object(),
        sink=object(),
        embedder=object(),
        document_lineage=object(),
        m365_graph_sync_host=None,
    )
    return build_connector_sync_surface(cast(Any, platform))


def test_an_explicit_cron_override_faster_than_the_cadence_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator's explicit per-source cron wins while it keeps the sweep's pace.

    A provider daily-API cap is a real bound on how often the connector may be
    pulled, and an operator statement about it is honoured — but only in the
    direction that cannot invalidate the rotation proof. ``*/1`` fires MORE
    often than the 120s cadence, so every point is still re-read at least as
    promptly as ``require_sub_sla`` proved.

    The GENERIC ``cs.cron`` is still never inherited by an authority-bearing
    source — only an EXPLICIT entry naming this source is consulted at all.
    """
    surface = _authority_surface_with_overrides(
        monkeypatch,
        tmp_path,
        sources=("salesforce", "m365"),
        cron_overrides={"salesforce": "*/1 * * * *"},
    )
    crons = {job.name: job.cron for job in surface.jobs}
    assert crons["connect-sync-salesforce"] == "*/1 * * * *"
    # The no-override source in the SAME surface is untouched: the derived
    # sweep cadence still wins over the generic hourly default for m365.
    assert crons["connect-sync-m365"] == "*/2 * * * *"


def test_an_override_slower_than_the_proven_sweep_cadence_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seeded cron may never be slower than the cadence proved sub-SLA.

    This is the regression that matters. ``require_sub_sla`` proves a full
    permission rotation fits inside the contract SLA using
    ``authority_sweep_cadence_seconds`` — NOT the cron the job is actually
    seeded at. A 6-hourly override against a 120s cadence therefore does not
    relax that proof, it makes the proof describe a job that does not exist:
    ``ceil(500/50) * (120 + 60) = 1800s`` is certified against the PT1H SLA
    while the job really fires every 21600s, a rotation of ~60 HOURS.

    Nothing downstream catches it in time. There is no read-time authority
    freshness gate, so the only backstop is the sweep's own
    ``require_points_swept_within_sla`` failing every run and degrading the
    whole source to ``unavailable`` after TRANSIENT_SYNC_FAILURE_LIMIT (3)
    consecutive failures — three SIX-HOUR firings, an 18-hour window of served
    stale ACLs, ending in a permanent blackout of the source rather than a
    recovery. Refusing at seed time states the contradiction where an operator
    can still fix it.
    """
    with pytest.raises(AuthoritySweepCronError) as excinfo:
        _authority_surface_with_overrides(
            monkeypatch,
            tmp_path,
            sources=("salesforce", "m365"),
            cron_overrides={"salesforce": "0 */6 * * *"},
        )
    message = str(excinfo.value)
    assert "21600s" in message
    assert "120s sweep cadence" in message
    # It names the honest remedy: the one knob that moves the schedule and the
    # rotation proof together.
    assert "NE_SALESFORCE_AUTHORITY_SWEEP_CADENCE_SECONDS" in message


def test_a_daily_override_on_an_authority_source_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A once-a-day override is refused on the same rule, not just ``*/N`` shapes."""
    with pytest.raises(AuthoritySweepCronError):
        _authority_surface_with_overrides(
            monkeypatch,
            tmp_path,
            sources=("m365", "salesforce"),
            cron_overrides={"salesforce": "17 3 * * *"},
        )


def test_an_override_naming_another_source_does_not_reach_an_authority_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precedence is per-source: m365 keeps its derived cadence beside an override."""
    surface = _authority_surface_with_overrides(
        monkeypatch,
        tmp_path,
        sources=("m365", "salesforce"),
        cron_overrides={"salesforce": "*/2 * * * *"},
    )
    crons = {job.name: job.cron for job in surface.jobs}
    assert crons["connect-sync-m365"] == "*/2 * * * *"
    assert crons["connect-sync-salesforce"] == "*/2 * * * *"


def test_a_non_authority_source_keeps_an_arbitrarily_slow_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule binds authority-bearing sources ONLY.

    A source that issues no authority has no permission rotation to invalidate,
    so an operator's API-cap statement stands unqualified.
    """
    from ne.connect.runner import ConnectSyncSettings as _Settings
    from ne.connect.runner import IngestStack

    class _Plain:
        source_name = "salesforce"
        authority_enabled = False

    monkeypatch.setattr("ne.worker.ingest.build_sync_settings", lambda source, **_: _Plain())
    monkeypatch.setattr(
        "ne.worker.ingest.build_ingest_stack",
        lambda **_: IngestStack(writer=cast(Any, object()), state_store=cast(Any, object())),
    )
    platform = SimpleNamespace(
        connect_sync_settings=_Settings(
            sources=("salesforce",),
            cron_overrides={"salesforce": "0 */6 * * *"},
            state_path=str(tmp_path / "plain-s.db"),
            quarantine_path=str(tmp_path / "plain-q.db"),
        ),
        chunk_store=object(),
        runtime_engine=object(),
        sink=object(),
        embedder=object(),
        document_lineage=object(),
        m365_graph_sync_host=None,
    )
    surface = build_connector_sync_surface(cast(Any, platform))
    assert surface.jobs[0].cron == "0 */6 * * *"


def test_max_firing_gap_seconds_measures_the_widest_gap_not_the_average() -> None:
    """The bound is the WIDEST gap, so a bursty expression cannot average its way in."""
    assert max_firing_gap_seconds("*/2 * * * *") == 120.0
    assert max_firing_gap_seconds("0 */6 * * *") == 21600.0
    # Two firings a minute apart, then the rest of the day: the widest gap is
    # what a revocation actually waits for, not the 60s burst.
    assert max_firing_gap_seconds("0,1 3 * * *") == 86340.0


def test_max_firing_gap_seconds_refuses_to_call_a_sparse_expression_fast() -> None:
    """An expression whose worst-case gap cannot be BOUNDED returns ``None``.

    A finite probe can only ever be too SHORT, so a window that proves nothing
    must never be read as proof of speed — the caller fails closed on ``None``.
    """
    assert max_firing_gap_seconds("0 0 29 2 *") is None


def test_a_seasonal_expression_is_refused_though_a_short_probe_would_call_it_fast() -> None:
    """⭐ The soundness case: a fast-inside-January cron is NOT a fast cron.

    ``*/2 * * 1 *`` fires every two minutes all through January and then does
    not fire again for ELEVEN MONTHS. Any probe anchored in January and shorter
    than a year measures a 120-second worst case and reports the expression as
    keeping pace with a 120s sweep cadence — the exact permanent corpus blackout
    the guard exists to prevent, waved through by the guard itself.

    The bound is therefore established from the PARSED FIELDS, not from what a
    sample happened to show: a month field that names fewer than twelve months
    has a dormant stretch, and the answer is ``None`` (refuse), never a number.
    """
    assert max_firing_gap_seconds("*/2 * * 1 *") is None
    # The same shape at a coarser step, and the "fires every minute, in January"
    # extreme — both are dormant for eleven months, so neither gets a number.
    assert max_firing_gap_seconds("*/5 * * 1 *") is None
    assert max_firing_gap_seconds("* * * 1 *") is None
    # A day-of-month restriction hides the same kind of dormancy inside a month.
    assert max_firing_gap_seconds("*/2 * 1 * *") is None
    # ``#`` and ``L`` weekday forms are positions inside a MONTH, not a week, so
    # they are not week-periodic either.
    assert max_firing_gap_seconds("0 3 * * 5#3") is None
    assert max_firing_gap_seconds("0 3 * * 1L") is None
    assert max_firing_gap_seconds("0 3 L * *") is None


def test_a_genuinely_fast_expression_is_still_bounded_exactly() -> None:
    """Refusing the unboundable must not cost the shapes a sweep actually uses."""
    assert max_firing_gap_seconds("* * * * *") == 60.0
    assert max_firing_gap_seconds("*/1 * * * *") == 60.0
    assert max_firing_gap_seconds("*/30 * * * *") == 1800.0
    # Sunday spelled 0 and 7 both mean "every weekday is allowed" — an
    # exhaustive weekday list must not be mistaken for a restriction.
    assert max_firing_gap_seconds("*/5 * * * 0,1,2,3,4,5,6") == 300.0
    assert max_firing_gap_seconds("*/5 * * * 1,2,3,4,5,6,7") == 300.0


def test_a_weekly_expression_is_bounded_rather_than_refused() -> None:
    """The sparse-but-safe direction: a plain weekday cron gets an exact number.

    ``0 3 * * 0`` is week-periodic, so its worst case is exactly seven days —
    knowable, and knowable without guessing. An eight-day window anchored on a
    Monday sees this expression fire ONCE (the Sunday), which is not two
    firings, so a window-length probe refuses it and bricks a worker boot on a
    valid config. Establishing the period first removes that failure mode.

    Being bounded is not the same as being accepted: 604800s is still compared
    against the sweep cadence, and an authority source with a cadence in minutes
    still refuses it.
    """
    assert max_firing_gap_seconds("0 3 * * 0") == 604800.0
    assert max_firing_gap_seconds("0 3 * * 1") == 604800.0
    # Mon-Fri: the widest gap is the Friday-to-Monday weekend, not a day.
    assert max_firing_gap_seconds("0 3 * * 1-5") == 259200.0


def test_a_seasonal_override_on_an_authority_source_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⭐ End to end: the seasonal override never reaches a seeded job.

    Against the 120s salesforce cadence, ``*/2 * * 1 *`` measures 120s inside
    January — no slower than the cadence — so a window-length probe HONOURS it
    and seeds a job that goes dormant on 1 February. The guard refuses it, and
    says why: the gap is unbounded, not merely wide.
    """
    with pytest.raises(AuthoritySweepCronError) as excinfo:
        _authority_surface_with_overrides(
            monkeypatch,
            tmp_path,
            sources=("salesforce", "m365"),
            cron_overrides={"salesforce": "*/2 * * 1 *"},
        )
    message = str(excinfo.value)
    assert "unbounded" in message
    assert "120s sweep cadence" in message
    assert "NE_SALESFORCE_AUTHORITY_SWEEP_CADENCE_SECONDS" in message


def test_a_fast_override_is_still_honoured_beside_the_seasonal_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed on the unboundable must not turn into fail-closed on everything.

    ``* * * * *`` is bounded at 60s, inside the 120s cadence, and is seeded. The
    no-override m365 source in the same surface keeps taking the DERIVED branch,
    untouched by any of this.
    """
    surface = _authority_surface_with_overrides(
        monkeypatch,
        tmp_path,
        sources=("salesforce", "m365"),
        cron_overrides={"salesforce": "* * * * *"},
    )
    crons = {job.name: job.cron for job in surface.jobs}
    assert crons["connect-sync-salesforce"] == "* * * * *"
    assert crons["connect-sync-m365"] == "*/2 * * * *"
