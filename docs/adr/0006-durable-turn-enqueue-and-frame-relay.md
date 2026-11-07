# 0006 — Durable turn-enqueue + cross-process frame relay (ne-worker)

## Status

Accepted (2026-07-09; decided and committed by NE-103c). Non-contract decision —
no `ne-contracts` change, so no contract version bump. Records the ADR-0001
sanctioned deviation for the new database-mediated cross-process channels.

## Context

NE-103c stands up the durable `ne-worker` of the SPEC §4.1 topology: `ne-api`
becomes the stateless HTTP/SSE surface, the worker executes each chat turn in a
DIFFERENT process as a DBOS durable workflow, and `ne-api` must relay the worker's
token stream back to the browser with the SSE frames **byte-identical** to the
ne-api spec §2. Everything crosses through Postgres (no broker, no worker HTTP
port). ADR 0001 sanctions "…and the database" as a cross-process channel but
adds "Any deviation from this topology requires a new ADR" — the turn-enqueue and frame-relay channels are a
new *use* of the database, so this ADR records the decisions.

## Decision

**1. Turn-enqueue rides DBOS's own Postgres schema in a dedicated `ne_dbos`
database.** `ne-api` enqueues `chat.turn` via `DBOSClient` with
`workflow_id=turn_id` (also the idempotency key); the worker dequeues the
`chat-turns` queue and runs `DepartmentAgentSession.run` as the durable workflow.
The workflow/queue state lives in DBOS's own schema ("DBOS owns its Postgres
schema", SPEC §4.1) in a dedicated `ne_dbos` database on the same instance — keeping
durability state out of the platform schema's backup/migration story at pilot scale.

**2. Token frames are ephemeral transport, NOT typed platform events.** Frames
carry no `kpi_tag`/`config_hash` governance semantics and do NOT ride
`ne_outbox_events`. The outbox is a governance-event log with per-consumer
registered-immutable filters and at-least-once multi-worker drain semantics — all
wrong-shaped for thousands of ephemeral per-turn frames (each turn would need a
throwaway consumer row; token text does not belong in the audit-adjacent typed
event log; deleting relayed frames would violate the outbox's append-only
discipline). We reuse the *discipline* (idempotent ordered append), not the table.

**3. Transport = DBOS workflow streams, with a poll-tail table as the specified
fallback, behind transport-agnostic seams.** The worker emits typed `TurnFrame`
records via `TurnFrameWriter` (`DBOS.write_stream`); `ne-api` tails them via
`TurnFrameReader` (`DBOSClient.read_stream_async`) and its SOLE `sse_frame` encoder
renders them — byte-identity by construction (one serializer in the platform).
LISTEN/NOTIFY was **rejected** as the mechanism: it loses the first frames on the
enqueue→dequeue attach race and caps payloads at ~8 KB (the `done` frame carries the
full output). The Writer/Reader Protocol seams ARE the fallback insurance: a
build-time stream-API gap swaps to a durable `(turn_id, seq, frame_json)` poll-tail
table without touching the turn workflow, the relay loop, or a single SSE-byte
assertion. The DBOS stream API is verified present in the locked `dbos>=2.26.0`, so
the fallback table is not built (decide in round one, do not carry both).

**4. `WorkerSettings` is deliberately NOT in the composed `config_hash` freeze.**
It holds execution-placement knobs only (queue name, concurrency, tick cadences,
health marker). The one decision-affecting knob — `durability` — lives inside the
frozen `RuntimeSettings`. Both processes run the SAME `build_platform` over the SAME
composed mapping (identity/gateway/audit/eval/flywheel/api/permissions/secrets/obs +
runtime + govern), so their `config_hash` matches (ADR-0003 one-platform-hash).
Freezing `WorkerSettings` in the worker process ONLY would fork that hash (ne-api
has no `WorkerSettings` to freeze) and silently split audit correlation.

## Consequences

- **Recovery is at turn granularity.** A mid-turn worker restart re-runs the turn
  FROM THE TOP (not from the last token). Session history is safe (`append_turn`
  runs once, after success). A re-run that had already obtained a tier-2/3 approval
  submits a FRESH approval request — the human may be asked twice; the direction is
  fail-SAFE (never auto-approved, never skipped), and the frequency is worker-crash-
  rate, not steady-state. Accepted for v1; the mitigation path (approval-request
  idempotency key `(turn_id, action, args_hash)`) is an ne-runtime unit, out of lane.
- **`done.output` self-heals re-exec duplicates.** Because the authoritative full
  text replaces accumulated deltas client-side, any duplicate/dropped token frame
  during recovery is cosmetic at worst.
- **A timed-out browser leg does not abandon the turn.** The turn keeps running
  durably; the pending approval row stays actionable and the output lands in session
  history (a material UX upgrade over the in-process timeout).
- **Both processes run `NE_RUNTIME_DURABILITY=dbos` in the pilot** (config_hash
  parity). Safe for ne-api because its import graph is decoration-free — an
  import-linter contract forbids `ne.api` importing `ne.worker` or
  `ne.runtime.approvals_durable`.
- **DBOS is constructed with `run_admin_server=False`** (a documented deviation from
  `launch_durable_runtime`, which cannot pass the kwarg) so no unauthenticated
  workflow-admin HTTP server runs in the hardened container. Follow-up
  NE-RUNTIME-LAUNCH-KWARGS tracks giving the launch helper the kwarg.
