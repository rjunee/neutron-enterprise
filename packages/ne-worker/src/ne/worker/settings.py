# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""WorkerSettings — the ne-worker execution-placement configuration (NE-103c §6.2).

``env_prefix='NE_WORKER_'``, ``extra='forbid'`` (a typo fails loudly). These are
EXECUTION-PLACEMENT knobs only (queue name, concurrency, tick cadences, health
marker) — NOT governance config.

DELIBERATE DECISION — WorkerSettings is NOT in the composed ``config_hash`` freeze
(design §6.2/Q5). ``freeze_platform_config`` covers ``RuntimeSettings`` +
``GovernSettings`` + the ne-api extra-modules mapping (identity/gateway/audit/eval/
flywheel/api/permissions/secrets/obs) — the SAME mapping both processes freeze. The
one decision-affecting durability knob lives INSIDE the frozen ``RuntimeSettings``
(``durability``). Freezing WorkerSettings in the worker process ONLY would fork the
ADR-0003 one-platform-hash between ne-api and ne-worker (ne-api has no WorkerSettings
to freeze), silently splitting audit correlation. So it stays unfrozen by design.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings


class WorkerSettings(NeSettings):
    """Execution-placement settings for the ne-worker process (NE-103c)."""

    model_config = SettingsConfigDict(
        env_prefix="NE_WORKER_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: The DBOS system database URL (the dedicated ``ne_dbos`` database). REQUIRED
    #: when ``RuntimeSettings.durability == 'dbos'`` (else a boot error).
    dbos_database_url: str = ""
    #: DBOS application name (the launch-time ``DBOSConfig.name``).
    dbos_app_name: str = "ne-worker"
    #: The named queue ne-api enqueues turns onto and the worker dequeues.
    turn_queue: str = "chat-turns"
    #: The queue's ``worker_concurrency`` — a flood-brake UNDER the per-principal
    #: guardrail caps. Sized >= expected concurrent HITL waits so a turn blocked for
    #: hours in ``await_outcome`` cannot starve the queue (design Q4).
    turn_concurrency: int = 4
    #: The plain-asyncio cron tick cadence used in ``durability='off'`` mode (in
    #: dbos mode ``DBOS.scheduled`` drives the tick instead).
    cron_tick_s: float = 60.0
    #: Heartbeat cadence: every tick, ``SELECT 1`` on the runtime engine then touch
    #: the marker. A dead DB stops the touch → the marker goes stale → unhealthy.
    heartbeat_s: float = 15.0
    #: The health marker path (mtime freshness is the ``--health`` liveness signal).
    heartbeat_path: str = "/tmp/ne-worker.health"
    #: The outbox drain-loop consumer name. EMPTY (default) ⇒ the drain loop is OFF in
    #: code. The pilot compose + Helm ne-worker env set this ON (``siem-otel``) — the
    #: P2-21 day-one SIEM export posture (the drain ships audit/cost/approval events as
    #: OTel logs to the in-cluster otel-collector).
    outbox_consumer: str = ""
    #: The in-cluster OTLP/HTTP base for the SIEM export (e.g.
    #: ``http://otel-collector:4318``). EMPTY with a configured ``outbox_consumer``
    #: FAILS worker BOOT loud (fail-closed — never ack/prune un-exported events);
    #: a C2-invalid (external) endpoint also fails boot loud (fail-closed).
    outbox_otlp_endpoint: str = ""
    #: Acked-row retention window (seconds) for :func:`ne.events.outbox.prune_acked`.
    #: Default 7 days — rows acked by EVERY consumer older than this are pruned so
    #: ``ne_outbox_events`` stays bounded (P2-21).
    outbox_retention_s: float = 604800.0
    #: How often the drain loop runs the retention prune, seconds (default hourly).
    outbox_prune_interval_s: float = 3600.0
    #: The outbox drain-loop tick cadence, seconds.
    outbox_drain_s: float = 5.0
    #: BD-10 — the bounded Answer Ledger scheduled re-ask sweep is explicitly
    #: default-off. Enabling the worker job does not schedule any owner by itself;
    #: each pinned question must separately opt in through the API.
    answer_ledger_reask_enabled: bool = False
    answer_ledger_reask_cron: str = "*/5 * * * *"
    answer_ledger_reask_limit: int = Field(default=10, ge=1, le=100)
    #: A dotted ``module:callable`` returning an ``AppSeams`` — CI-ONLY (the
    #: subprocess-boot seam), documented NOT-for-production. Empty ⇒ real edges.
    seams: str = ""
    #: NE-AUDIT-CRON — the ``ne.plugins.object_store`` adapter name for the periodic
    #: signed-checkpoint loop. EMPTY (default) ⇒ the loop is OFF in code — mirrors
    #: ``outbox_consumer``'s default-safe posture. A non-empty UNKNOWN name fails
    #: worker BOOT loud (the ``_build_outbox_handler`` precedent). POLICY (signing
    #: key path, retention, prefix, threshold) stays in the FROZEN
    #: ``AuditSettings.checkpoint`` (``ne.audit.config.CheckpointConfig``) —
    #: deliberately NOT duplicated here, since WorkerSettings is excluded from
    #: config_hash by design (see the module docstring).
    audit_checkpoint_object_store: str = ""
    #: NE-AUDIT-CRON — the audit-checkpoint loop tick cadence, seconds. An
    #: execution-placement knob only; ``maybe_checkpoint``'s own threshold
    #: (``every_n_records``) is what actually gates whether a tick writes anything.
    audit_checkpoint_interval_s: float = 900.0
