<!-- Copyright (c) 2026 Ryan Junee. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# ne-worker — the durable DBOS turn executor (NE-103c)

`ne-worker` is a **composition root** (not a library): it reuses the frozen
governed seams and adds durable execution. It is the SAME first-party `ne-app`
image as `ne-api`, launched with a different command (`python -m ne.worker`) — "one
image, diff command".

## What it does

```
browser ─SSE→ ne-api (stateless)              ne-worker (DBOS executor, 1..N)
                │ validate bearer/session        │ DBOS.launch → recovers PENDING wfs
                │ enqueue chat.turn (=turn_id) ───┼→ dequeues "chat-turns"
                │                                 │ runs the governed turn (C6)
                │           Postgres ◀────────────┤ writes ordered frames per turn
                ◀── tails the frame stream ───────┘ + NE-034 cron + outbox drain
                └─SSE frames (byte-identical §2)→ browser
```

- **Turns:** `ne-api` enqueues `chat.turn` via `DBOSClient` (`workflow_id=turn_id`);
  the worker runs `DepartmentAgentSession.run` as the durable workflow. The turn's
  frames stream back through DBOS workflow streams; `ne-api` is the SOLE SSE encoder,
  so browser bytes are identical to the in-process path.
- **Governance (C6):** the worker composes the SAME `GovernedToolExecutor` /
  audit sink / `DurableApprovalGate` / NE-070H spend-ledger+rate-counter / scope-rail
  via the shared `ne.api.platform.build_platform` — share-by-storage across
  processes. There is NO second ungoverned executor (grep guardian +
  composition-identity test + the cross-process HITL e2e prove it).
- **Cron:** the NE-034 scheduler runs in the same process over the SAME shared state;
  `DBOS.scheduled` drives it in dbos mode, a plain asyncio ticker in off mode. v1
  ships zero scheduled jobs — the governed tick surface is the deliverable.
- **Outbox drain:** an optional background loop ticks `drain_once` behind
  `NE_WORKER_OUTBOX_CONSUMER` (off by default; multi-replica-safe via SKIP LOCKED).

## Run

```
python -m ne.worker          # boot: compose → launch DBOS → run
python -m ne.worker --health # exit 0 iff the heartbeat marker is fresh (< 60s)
```

`--health` is parsed BEFORE any heavy import; the healthcheck never re-imports the
composition root per probe. The heartbeat loop does a `SELECT 1` then touches the
marker every `NE_WORKER_HEARTBEAT_S` — a dead DB stops the touch, the marker goes
stale, and the container is reported unhealthy.

## Runbook honesty (the decisions you must know)

- **Q3 — turn-granularity recovery.** A mid-turn crash re-runs the turn FROM THE TOP
  (never resumes mid-token). Session history is safe (`append_turn` runs once, after
  success). Per-model-call checkpointing is future work.
- **⚠ TRACKED FAIL-SAFE — approval re-prompt.** A re-run that had already obtained a
  tier-2/3 approval submits a FRESH approval request — the human may be asked twice.
  This is the SAFE direction (never auto-approve, never skip) and is accepted v1
  behavior. Do NOT "fix" it by auto-approving.
- **Q8 — mode flip is a coordinated change.** Decoration-time durability means a
  process cannot flip modes without restart, and mixed-mode fleets (api `off`, worker
  `dbos`) fork the `config_hash`. "Upgrade-in-place" = the SAME code both modes (a
  tested property), achieved operationally by a coordinated env change + restart
  across BOTH processes.
- **Q6 — no cancel surface.** An abandoned browser leaves a durable turn running to
  completion, bounded by the shared guardrail caps + `max_requests`. The frozen
  console contract has no cancel.
- **Q7 — timed-out output lands in session history.** The work is not lost; there is
  no history GET endpoint until v1.1.
- **DBOS admin server is OFF** (`run_admin_server=False`) — no unauthenticated
  workflow-admin HTTP server in the hardened container.

## Connector-sync cron surface (NE-INGEST — P1-4)

`ne.worker.ingest` seeds ONE governed NE-034 job per configured connector source
(`NE_CONNECT_SYNC_*`, read at boot via `platform.connect_sync_settings`) — the
flywheel `governed.py` cron-as-isolated-agent pattern:

- **`build_connector_sync_surface(platform)`** → per source: ONE tier-1 tool
  `write.internal.connect.sync_<source>` + an `AgentDefinition` granting exactly that
  tool + a non-credentialed `Principal(id="agent:connect-sync-<source>")` + a
  `ScheduledJob`. The ONE `IngestStack` (governed `KnowledgeWriter` over the composed
  `chunk_store`) is built once and shared. Empty config ⇒ an empty surface ⇒ zero jobs
  (fail-closed no-op). A LISTED-but-misconfigured source fails **loud at worker boot**,
  naming the source (`SyncNotConfiguredError`).
- **`seed_connector_sync_jobs(scheduler, jobs)`** reconciles the `connect-sync-*` jobs
  ONLY: an unchanged job is left as-is (idempotent restart, no audit spam); a changed
  cron is cancel+reschedule; a source deliberately dropped from a **non-empty**
  authoritative set is cancelled — never touching other jobs. An **empty** `jobs`
  (default-off / unconfigured / a transiently-omitted env) is a **non-destructive
  no-op**: because scheduled-job rows live in the shared runtime DB, a worker booting
  without `NE_CONNECT_SYNC_SOURCES` must NOT wipe correctly-configured peers' persisted
  schedules (Codex P1). Turning ingestion off deliberately is an explicit operator
  action, not a side effect of an absent env var.
- Each fire runs as an isolated toolset-scoped governed agent through the ONE cron
  executor (C6): PDP-checked + audited (`job.schedule`/`job.run` dual-append + the
  connector's `connector.sync` event). The scheduled agent calls its single granted
  tool once; the tool result is **counts-only JSON excluding cursors** (C12 — a
  deltaLink/cursor never leaves in tool output).
- **⚠ RISK (jobs owner).** In `durability='dbos'` mode a scheduled sync runs sync
  SQLAlchemy/HTTP I/O on DBOS's background event loop (we are the first scheduled-job
  owner). Accepted today (the turn workflow already does sync work in async steps);
  offloading is a tracked follow-up. Each fire also consumes one model turn (the LLM
  calls the single granted tool) — the SPEC-mandated NE-034 pattern; a deterministic
  fast-path is a possible future ne-runtime enhancement.

## Principal-sync cron surface (NE-FIX-SYNC-INVOKERS — P2-20)

`ne.worker.principal_sync` seeds ONE governed NE-034 job that calls
`PrincipalSyncService.run_once` on a schedule — mirroring the connector-sync surface
exactly, so the SPEC §5 ≤15-min revocation target is actually realizable.

- **`build_principal_sync_surface(platform)`** → when `PermissionsSettings.mode` is a
  PULL source (`entra`/`fixture`): ONE tier-1 tool
  `write.internal.permissions.principal_sync` + an `AgentDefinition` granting exactly
  that tool + `Principal(id="agent:permissions-principal-sync")` + a `ScheduledJob`
  whose cron encodes `sync_interval_seconds` via `interval_to_cron` (900s ⇒
  `*/15 * * * *`; **the interval IS the max revocation lag**, floored to whole
  minutes). Default `oidc-claims` (JIT, in-process) and SCIM (push) ⇒ an **empty
  surface** (fail-closed no-op). A misconfigured pull source fails **loud at worker
  boot**, naming the mode.
- **`seed_principal_sync_jobs(scheduler, jobs)`** reconciles the
  `permissions-principal-sync` job via the shared `reconcile_prefixed_jobs` — same
  non-destructive empty-set no-op as the connector-sync seed (a transiently
  unconfigured reboot never wipes a peer's persisted job).
- Each fire runs run_once through the ONE cron executor (C6): PDP-checked + audited
  (`job.schedule`/`job.run` + the service's own `permissions.principal_sync` event).
  The tool result is **counts-only JSON excluding `changed_members`** (C12 — principal
  ids never leave in tool output; the per-member audit events carry the detail).
- First-seed / manual re-sync: **`ne permissions sync`** (tools/ne-cli).

## Credential-rotation cron surface (NE-IDENTITY-ROTATION-CRON)

`ne.worker.credential_rotation` wires the `ne.identity.agents.service` line-409
seam ("NE-034 cron wires this; wiring out of scope [here]") to a real caller —
`AgentCredentialService.rotation_due_agents()` / `.rotate()` now run as a
scheduled, governed sweep, mirroring the connector-sync / principal-sync
surfaces exactly:

- **`build_credential_rotation_surface(platform)`** → ONE tier-1 tool
  `write.internal.identity.credential_rotation` + an `AgentDefinition` granting
  exactly that tool + `Principal(id="agent:identity-credential-rotation")` + ONE
  `ScheduledJob` whose cron is `rotation_check_cron(rotate_after_seconds)` —
  `min(3600, max(60, int(rotate_after_seconds) // 24))` seconds, i.e. at most
  ~1/24 of the rotation window, floored at 60s and capped at hourly (the
  default `rotate_after_seconds=86400` ⇒ `'0 */1 * * *'`); **the check cadence
  IS the max rotation lag**, the same "encoded interval IS the max lag"
  discipline `interval_to_cron` documents for principal-sync's revocation lag.
- **ALWAYS-ON — no config gate.** Unlike the pull-source-gated surfaces above,
  `rotate_after_seconds` always exists and validates
  (`AgentCredentialConfig`'s positive-value validators), so this surface is
  unconditionally non-empty; an empty credential set just makes the sweep a
  cheap, audited no-op (`due=0`). **Known limit:** `AgentCredentialService`
  state is in-memory, per-process (see service.py's `__init__`) — this job only
  ever rotates credentials `issue()`-d IN THE SAME WORKER PROCESS running the
  sweep. Today production issues no agent credentials anywhere (repo-wide grep:
  only tests call `issue()`), so the sweep is an empty no-op in every current
  deployment; this unit makes the rotation property hold **by construction**
  for every future worker-side issuer, mirroring how principal-sync's
  `oidc-claims` mode documents its own fresh-instance-is-empty caveat.
- **`seed_credential_rotation_jobs(scheduler, jobs)`** delegates to the shared
  `reconcile_prefixed_jobs` (prefix `identity-credential-rotation`, disjoint
  from `connect-sync-*` / `permissions-principal-sync` / `skill-forge-*` /
  `skill-gardener-*`); idempotent across restarts. Since the surface is always
  non-empty, the reconciler's non-destructive empty-desired-set arm never
  actually fires here — it stays only because it's the one shared reconciler
  every cron seed in this package delegates to.
- **Partial-failure discipline** mirrors `AgentCredentialService.revoke_agent`:
  the sweep attempts EVERY due agent; the first per-agent failure is captured
  and re-raised only after the full sweep completes, so one bad credential can
  never starve the rest of the fleet's rotation. **Observed (not chosen)
  effect:** because this tool has exactly one call site, a re-raised error is
  caught by `ne.runtime.runner`'s `_BLOCKING_ERRORS` handling and surfaces as a
  safe `BLOCKED: ToolExecutionError: ...` tool result — the scheduled JOB
  itself still records `succeeded` (a `tool.execute` ERROR audit event plus the
  per-credential state is where the failure signal lives, not
  `job.run.succeeded`).
- The tool result is **counts-only JSON** (`{"due", "rotated", "failed"}`) —
  never agent keys/departments/token material (C12).

## Real outbox → SIEM exporter + retention (NE-FIX-SYNC-INVOKERS — P2-21)

The outbox drain loop now ships a REAL exporter and bounds the table:

- When `NE_WORKER_OUTBOX_OTLP_ENDPOINT` is set, the drain handler is
  `ne.obs.otlp.OutboxOtelLogHandler` over the in-cluster `otel-collector` (C2:
  `validate_in_vpc_endpoint` rejects an external host at worker boot — fail-closed);
  otherwise the legacy count-log handler. The drain fail-opens on an export/DB fault
  (cursor unmoved, throttled 60s warning, at-least-once redelivery).
- Every `NE_WORKER_OUTBOX_PRUNE_INTERVAL_S` the loop runs
  `ne.events.outbox.prune_acked`, deleting rows acked by EVERY consumer older than
  `NE_WORKER_OUTBOX_RETENTION_S` (default 7d) so `ne_outbox_events` stays bounded.

## Periodic signed-WORM audit checkpoints (NE-AUDIT-CRON)

A plain maintenance loop — the same shape as the heartbeat/outbox-drain loops, NOT a
`ScheduledJob` (an integrity primitive must not depend on a nondeterministic model
call; see `ne.worker.audit_checkpoint`'s module docstring for the full decision) —
ticks `ne.audit.checkpoint.maybe_checkpoint` so signed WORM checkpoints are written
automatically, closing the gap between the "periodic checkpoint" claim and the
previously on-demand-only `ne audit checkpoint` CLI.

- Default-off: `NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE` empty ⇒ no task is created.
  A configured-but-unknown `ne.plugins.object_store` adapter name fails worker BOOT
  loud. A missing signing key at runtime is a throttled (60s), visible WARNING —
  never a crash, never a silent no-op — and recovers once the key is written.
- `NE_WORKER_AUDIT_CHECKPOINT_INTERVAL_S` is the tick cadence only; the POLICY knobs
  (signing key path, prefix, retention, the `every_n_records` threshold) stay in the
  frozen `AuditSettings.checkpoint` (`NE_AUDIT_CHECKPOINT__*`) so the loop and the CLI
  always agree.
- Multi-replica safe: same-`seq` races converge on one WORM object key; the losing
  replica's `WormViolationError` is logged INFO and skipped (a benign convergent
  race) — deliberately not the `read_head` row lock, which would stall the hot
  audit-append path across an object-store round trip.
- Only the in-memory `ne.plugins.object_store` adapter (`memory`) exists today — a
  production deployment needs a future S3/Object-Lock adapter to point this loop at
  real cloud WORM.

## Environment reference (`NE_WORKER_*`, `extra='forbid'`)

| Var | Default | Meaning |
|---|---|---|
| `NE_WORKER_DBOS_DATABASE_URL` | `""` | Required in dbos mode — the dedicated `ne_dbos` system database. |
| `NE_WORKER_DBOS_APP_NAME` | `ne-worker` | DBOS application name. |
| `NE_WORKER_TURN_QUEUE` | `chat-turns` | The queue ne-api enqueues onto. |
| `NE_WORKER_TURN_CONCURRENCY` | `4` | Queue `worker_concurrency` (a flood-brake under the guardrail caps; sized ≥ expected concurrent HITL waits). |
| `NE_WORKER_CRON_TICK_S` | `60.0` | Plain-asyncio cron cadence (off mode). |
| `NE_WORKER_HEARTBEAT_S` | `15.0` | Heartbeat cadence. |
| `NE_WORKER_HEARTBEAT_PATH` | `/tmp/ne-worker.health` | Health marker path. |
| `NE_WORKER_OUTBOX_CONSUMER` | `""` | Outbox drain consumer (empty ⇒ loop OFF in code; the pilot compose/Helm set `siem-otel`). |
| `NE_WORKER_OUTBOX_OTLP_ENDPOINT` | `""` | In-cluster OTLP/HTTP base for the SIEM export (e.g. `http://otel-collector:4318`); empty ⇒ the legacy count-log handler. A C2-invalid host fails boot. |
| `NE_WORKER_OUTBOX_RETENTION_S` | `604800.0` | Acked-row retention window (7d) for `prune_acked`. |
| `NE_WORKER_OUTBOX_PRUNE_INTERVAL_S` | `3600.0` | How often the drain loop runs the retention prune. |
| `NE_WORKER_OUTBOX_DRAIN_S` | `5.0` | Drain tick cadence. |
| `NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE` | `""` | `ne.plugins.object_store` adapter name for the periodic checkpoint loop (empty ⇒ loop OFF; unknown name fails boot). |
| `NE_WORKER_AUDIT_CHECKPOINT_INTERVAL_S` | `900.0` | Audit-checkpoint loop tick cadence. |
| `NE_WORKER_SEAMS` | `""` | CI-ONLY `module:callable` returning `AppSeams` — NOT for production. |

`WorkerSettings` is deliberately NOT in the composed `config_hash` (it holds
placement knobs only; the decision-affecting `durability` lives in the frozen
`RuntimeSettings`). See ADR-0006 §4.

The worker reads all governance/module settings (`NE_RUNTIME_*`, `NE_AUDIT_*`,
`NE_API_*`, …) IDENTICALLY to `ne-api` — the pilot compose shares one `x-ne-app-env`
anchor so the two processes freeze the same `config_hash`.

PRE-008 follows that same recipe for `NE_API_AGENT_MANIFEST_PATH`: the worker
parses, fingerprints, and reconciles the identical read-only registry bytes as
the API, but does not activate an Agent, Skill, or workflow. Missing dependencies
remain explicit unavailable catalog state for their owning later units.

PRE-011F supplies the exact unchanged `deploy/skills` bundle to both roots at
`/etc/neutron/skills`. The worker is the sole registry writer: it validates the
mounted canonical Skill pin and authoritative raw `SKILL.md` SHA against the
Agent manifest, reconciles with a temporary shared-registry engine, re-reads the
hash-verified row, and disposes that engine before platform/catalog composition,
worker globals, or DBOS. The API uses the API-owned shared verifier in read-only
mode before production `create_app`/`build_platform` and again in deployment
readiness. Missing bytes, quarantine, mounted/manifest/hash drift, registry
tamper, or an unreconciled worker therefore keeps API composition closed without
returning a Skill body or hidden reason. Worker heartbeat markers also bind the
live process epoch, so a same-Pod restart cannot inherit a fresh stale marker.

## Grounded-agent composition wiring (NE-GA-G5)

The worker inherits `Platform.grounding`/`grounded_agent_name` via the SAME
`build_platform` recipe `ne-api` runs — there is no worker-specific grounding
wiring. `execute_turn_step` threads `grounding=platform.grounding` into
`create_department_session` only when `req.agent_name ==
platform.grounded_agent_name` (identical to `routers/chat.py`'s `_open` name
gate); the terminal `done` frame's additive `grounded` key mirrors `verified`
byte-for-byte, so an enqueued grounded turn's structural
`GroundedProvenance` is identical to the same turn run inline.
