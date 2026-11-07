# NE-103c — the durable `ne-worker` + cross-process turn streaming (build-ready design)

**Status:** design memo settling the NE-103c architecture BEFORE the build (owner
decision (iv)=(b), 2026-07-08). NE-103c is upgraded from a thin cron container to the real
`ne-worker` durable-execution service of the SPEC §4.1 topology ("`ne-worker` |
agent runtime (DBOS), flywheel, schedulers, signal/outbox exporter | DBOS owns its Postgres
schema"). Companion to `docs/architecture/ne-api-service-spec.md` (the ne-api producer spec —
"the ne-api spec" below); this memo changes WHERE a chat turn runs, never WHAT the browser sees.

**Sequencing:** after NE-104a/b/c (`ne-api`) + NE-103a (the one first-party `ne-app` image). Touches `ne-runtime` (test
promotion only), a new `packages/ne-worker`, `ne-api` (enqueue + relay), `deploy/compose/pilot`,
and CI.

**The one genuinely-new design problem** (owner framing): ne-api is the HTTP/SSE
surface, the worker executes the turn in another process, and ne-api must relay the worker's
token stream back to the browser with the SSE frames **byte-identical** to the ne-api spec §2.
§4 below settles that mechanism; everything else is composition of already-proven parts.

---

## 0. What exists today (the parts this design composes — all cited)

| Part | Where | State |
|---|---|---|
| `@ne_workflow` / `@ne_step` decorators | `packages/ne-runtime/src/ne/runtime/durable.py:44-87` | durability read from `RuntimeSettings().durability` at **DECORATION time** (durable.py:15-21): `'off'` returns the function unchanged; `'dbos'` applies the real `DBOS.workflow/step` via the single import seam `_load_dbos` (durable.py:33-37, 56-59, 79-83) |
| DBOS launch path | `durable.py:90-105` — `launch_durable_runtime(name=, database_url=)` raises `DurabilityUnavailableError` unless `NE_RUNTIME_DURABILITY=dbos` (durable.py:97-101), else constructs `dbos.DBOS(config=DBOSConfig(name, database_url))` + `DBOS.launch()` (durable.py:102-104) | coded, **LIVE-PARKED** — never stood up |
| The live-parked CI proof | `packages/ne-runtime/tests/test_dbos_live.py` — module-level skip unless `NE_LIVE_DBOS=1` + reachable `NE_RUNTIME_DBOS_DATABASE_URL` (test_dbos_live.py:46-56); proves one `@ne_workflow`→`@ne_step` exactly-once run (test_dbos_live.py:59-76). Sibling `tests/test_approvals_dbos_live.py` for the approval steps | parked; §6 promotes it |
| Serializable-args durable-function precedent | `packages/ne-runtime/src/ne/runtime/approvals_durable.py:47-79` — module-level sync `@ne_step` functions taking `(database_url: str, request_json: str)`, rebuilding engines internally; workflow at :169-191; "steps never call other steps" discipline at :22-23 | merged, the pattern §5 reuses |
| The turn function | `DepartmentAgentSession.run` (`packages/ne-runtime/src/ne/runtime/agent.py:96-138`) — args `(prompt: str, *, principal: Principal, context)`; NOT itself serializable-args (bound method over live store/executor/gateway); the honesty note in durable.py:5-13 says exactly this: "a composition root wraps ITS turn functions (serializable in/out)" | §5 defines that turn function |
| Fail-closed department re-check inside the turn | agent.py:103-108 — `principal.claims['department'] != session.department` raises before any model/tool activity | defense-in-depth the worker inherits for free |
| Audit-failure kills the run | agent.py:28-31 (docstring; `RuntimeAuditError`/`AuditEmissionError` propagate) | inherited in the worker |
| The C6 executor recipe (NE-CKD1) | `packages/ne-runtime/src/ne/runtime/factory.py:19-47` — ONE `create_shared_guardrail_state()` pair + `AgentScopeGuardrail` on `extra_guardrails` across session/dispatch/cron executors; `create_tool_executor` (factory.py:182-222) with `sink=`/`approval_gate=` REQUIRED | merged; §6 wires it in the worker root |
| Durable cross-**process** guardrail state (NE-070H) | `create_shared_guardrail_state(database_url=…)` (factory.py:149-179) → `SqlAlchemySpendLedger` + `SqlAlchemySlidingWindowCounter` — "ATOMIC + SHARED across ALL workers and SURVIVE restarts" (factory.py:166-172); proven with 16 concurrent workers on live PG16 | merged — this is what makes "share the same guardrail ledger" possible across processes |
| Durable HITL gate | `create_approval_gate` (factory.py:278-314) → `DurableApprovalGate` over `SqlAlchemyApprovalStore`; `await_outcome` is a DB poll loop (`approvals.py:264-290`, cadence `approvals.poll_interval_s`, config.py:49) — gate state lives in Postgres, so gate instances in DIFFERENT processes see the same rows | merged |
| Cron scheduler (NE-034) | `create_cron_scheduler` (factory.py:382-444); CAS due-claim is single-fire-safe under multi-worker (scheduler.py:35-37); the DBOS tick composition is documented and deliberately DBOS-import-free (scheduler.py:38-40; `packages/ne-runtime/README.md:438-444`) | merged; §7 stands it up |
| Transactional outbox (NE-070-hardened) | `packages/ne-events/src/ne/events/outbox.py` — settled-horizon multi-producer safety (outbox.py:18-62), idempotent `append` (outbox.py:225-275), `FOR UPDATE SKIP LOCKED` cursor claim (outbox.py:423-428), `drain_once` exporter tick "(ne-worker owns the loop, a later unit)" (outbox.py:599) | merged; §4.2 assesses it for token relay (verdict: wrong shape for tokens, right home for the exporter loop) |
| ne-api SSE contract | ne-api spec §2 — frame grammar (§2.2), event union token/citations/done/error (§2.3), v1 **turn-then-stream** server behavior + 15 s comment keep-alives (§2.4) | frozen consumer contract; MUST NOT change |
| dbos pin | `packages/ne-runtime/pyproject.toml:17` — `dbos>=2.26.0,<3` | in the lock |
| Pilot compose | `deploy/compose/pilot/compose.yaml` (digest-pinned services, profiles core/brain/obs/serve-gpu); the `agents` profile is **reserved and empty** (`deploy/compose/pilot/README.md:27`) | NE-103c fills it |

---

## 1. Process topology and trust model

```
browser ──SSE──▶ ne-api (stateless, N replicas)          ne-worker (DBOS executor, 1..N)
                   │ validate bearer/session (spec §1.2)     │ DBOS.launch → recovers PENDING wfs
                   │ enqueue chat.turn (workflow_id=turn_id) │ dequeues "chat-turns" queue
                   │──────────────┐                          │ runs the governed turn (C6)
                   │              ▼                          │ writes ordered frames per turn
                   │        Postgres (platform)              │──────────────┐
                   │   • DBOS system schema (queue/wf state) │              │
                   │   • sessions/approvals/guardrail state  ◀──────────────┘
                   │   • audit chain • outbox • turn frames
                   ◀── tails the turn's frame stream ────────┘
                   └──SSE frames (byte-identical §2)──▶ browser
```

- **Everything crosses through Postgres** — no message broker, no worker HTTP port. This is
  inside the SPEC §4.1 sanctioned cross-process set ("…and the database"), but the
  turn-enqueue + frame-relay channels are a new *use* of the database, and the same section says "Any
  deviation needs an ADR" — NE-103c therefore lands a short ADR (§9, deliverable D6) recording:
  (1) durable workflow state rides DBOS's own Postgres schema ("DBOS owns its Postgres schema" —
  this was always the topology); (2) token frames are **ephemeral transport, not
  typed platform events** — they carry no `kpi_tag`/`config_hash` governance semantics and do
  NOT ride `ne_outbox_events` (rationale §4.2).
- **Trust boundary:** the enqueue payload is trusted because ONLY platform processes hold the
  Postgres credentials (SecretStore-injected, never in compose/env — NE-070 bundle).
  The browser can never write a queue row; ne-api constructs the payload exclusively from the
  middleware-verified bearer (ne-api spec §1 global conventions + §4.4). The worker additionally
  re-checks the department claim inside `run()` (agent.py:103-108) — even a forged queue row
  cannot cross departments without also forging a matching principal, and every tool call it
  could make still hits the full guardrail→PDP→HITL→audit chain (C6).

---

## 2. Enqueue design (ne-api → durable workflow)

### 2.1 The wire: DBOS queue + `DBOSClient`, workflow_id = turn_id

`ne-api` does **not** launch DBOS (it is stateless/horizontal, and must own no
workflow-recovery duties). It enqueues via the DBOS **client API** (`dbos.DBOSClient` — the
documented enqueue-from-a-non-DBOS-process seam; present in the pinned `dbos>=2.26.0`,
**re-verify exact signature at build time**):

```python
# ne/api/routers/chat.py (enqueue mode) — after ALL pre-stream validation of spec §1.2 #3
turn_id = f"turn-{uuid4()}"
payload = TurnRequest(                          # §5.1 — pydantic, extra="forbid"
    turn_id=turn_id, session_id=session_id,
    agent_name=agent_name, department=department,          # server-derived, spec §1.2 #2
    prompt=body.prompt,
    principal_id=principal.id, principal_groups=list(principal.groups),
    principal_claims=dict(principal.claims),               # bearer-derived ONLY
).model_dump_json()
handle = client.enqueue(
    {"queue_name": "chat-turns", "workflow_name": "chat.turn", "workflow_id": turn_id},
    payload,
)
```

- **Serializable args:** one JSON string, exactly the `approvals_durable.py:47-54` house pattern
  (`request_json: str`). `Principal` (`packages/ne-contracts/src/ne/contracts/types.py:32-37`:
  `id`, `groups`, `claims`) round-trips losslessly through `model_dump(mode="json")`; a unit
  test pins the round-trip (claims must be JSON-clean — they already are, they came out of a JWT).
- **Principal discipline:** identical to the ne-api spec §4.4 — the ONLY identity source is
  `current_principal(request)` from the verified bearer; no request field ever reaches
  `TurnRequest`'s principal fields. The console body carries only `{prompt}` (spec §1.2 #3), so
  there is nothing to smuggle; the request model is `extra="forbid"` anyway.
- **Correlation handle:** `workflow_id = turn_id`, minted by ne-api per message POST. This is
  also the **idempotency key** — DBOS deduplicates by workflow id, so an ne-api retry of the
  same enqueue can never double-run a turn. ne-api holds `turn_id` for (a) tailing the frame
  stream (§4), (b) `client.retrieve_workflow(turn_id)` for terminal status/result, (c) audit
  correlation (ne-api emits its surface record `api.chat.turn_enqueued` with ids only — the
  redaction discipline of spec §4.8: never the prompt).
- **Queue, not bare `start_workflow`:** a named queue (`Queue("chat-turns")`, registered in the
  worker) gives a global worker-side concurrency cap — a deterministic flood-brake in the C12
  spirit layered UNDER the per-principal rate/spend guardrails, which remain the
  authoritative caps (shared durable state, factory.py:149-179).
- **Toggle:** `ApiSettings` gains `turn_execution: Literal["inline", "enqueue"] = "inline"`
  (env `NE_API_TURN_EXECUTION`). `inline` is NE-104a's in-process path, unchanged; `enqueue` is
  the pilot-profile setting. This is the upgrade-in-place seam the owner decision names:
  `DepartmentAgentSession.run` is the same callable either way; only the calling
  process moves. The pilot ships `enqueue` — the durable path is what the delivered artifact
  PROVES; `inline`/`off` remains a valid lightweight config, not the proof target.

### 2.2 Fallback if `DBOSClient` verification fails at build time

If the pinned dbos's client API cannot enqueue-by-name from a non-launched process (not
expected), the fallback is an `ne_turn_requests` claim table drained by the worker (INSERT by
ne-api; worker claims via `FOR UPDATE SKIP LOCKED` — the outbox.py:423-428 pattern — then calls
`DBOS.start_workflow` locally). This keeps the same TurnRequest payload and turn_id semantics;
only the hand-off mechanism changes. Decide in the first build round; do not carry both.

---

## 3. The SSE contract stays byte-identical (what "relay" must preserve)

Non-negotiables inherited from the ne-api spec §2 (consumer parser `ne-console/src/api/sse.ts`):

1. 200 + `text/event-stream`, pre-stream errors as plain JSON `{"detail"}` (spec §2.1).
2. Frame grammar `event:`/single-line `data:` JSON/blank line; `event:` and payload `type`
   both set (spec §2.2).
3. Event union exactly `token`/`citations`/`done`/`error` (spec §2.3); `done.output` is the
   authoritative full text that REPLACES accumulated deltas client-side (spec §2.3, citing
   useAgentStream.ts:75-81).
4. Comment keep-alives `: keep-alive` every `sse_keepalive_s` during silence (spec §2.4 step 2).

**ne-api remains the sole SSE encoder** (its `ne/api/sse.py`, spec §5.1). The worker never
emits SSE bytes; it emits typed **frame records** (§4.3) that ne-api's existing encoder renders.
That is the structural guarantee of byte-identity — there is exactly one SSE serializer in the
platform, and NE-103c does not add a second.

Property (3) is a designed safety net: because `done.output` is authoritative, any mid-stream
relay glitch (duplicate or dropped token frame during worker crash-recovery) is **self-healing
at `done`** — cosmetic at worst, never a corrupted final message.

---

## 4. Cross-process streaming — the core decision

### 4.1 Requirements the mechanism must meet

R-a **Ordering** — frames delivered to the SSE encoder in emission order, per turn.
R-b **Reconnect/late-attach** — ne-api may attach after the worker has already emitted frames
   (enqueue→dequeue race) and must not lose them; an ne-api replica restart mid-turn must be
   able to re-tail from the start (the browser's request died with the replica, but a retry of
   the POST must not observe a half-consumed channel).
R-c **Worker-crash semantics** — DBOS re-executes an unfinished workflow on recovery; re-emitted
   frames must not corrupt the stream (dedupe or self-heal).
R-d **HITL mid-turn** — a turn blocked in `DurableApprovalGate.await_outcome`
   (approvals.py:264-290) emits nothing for minutes-to-hours; the browser must stay alive
   (keep-alives are ne-api-local, spec §2.4) and the mechanism must not time out or leak.
R-e **Backpressure/volume** — must tolerate future token-rate emission (spec §7-R7 upgrade)
   without unbounded growth; a slow browser must not stall the worker.
R-f **Payload size** — the `done` frame carries the FULL final output (spec §2.3) — arbitrarily
   large.
R-g **Cleanup** — per-turn channel state must not accumulate forever.
R-h **No new infra** — Postgres only (no broker at pilot scale).

### 4.2 Candidates

**(a) Postgres LISTEN/NOTIFY (push).** Worker `NOTIFY`s frames on a channel; ne-api holds a
LISTEN connection and relays.
- Latency: best-in-class (~ms push). Ordering: preserved per producer connection (R-a ok).
- **Fatal 1 — fire-and-forget:** NOTIFY is delivered only to connections listening at commit
  moment. The enqueue→dequeue race (R-b) loses the first frames whenever the worker starts
  before ne-api's LISTEN is up; an ne-api reconnect loses everything prior. Fixing this
  requires a backing table — at which point the table is the mechanism and NOTIFY is an
  accelerator.
- **Fatal 2 — the 8000-byte payload cap:** `NOTIFY` payloads are hard-capped at ~8000 bytes;
  the `done` frame's full output (R-f) routinely exceeds it. Chunking full outputs through
  NOTIFY payloads is exactly the kind of bespoke wire protocol this platform refuses to build.
- Also: R-c dedupe impossible without persisted sequence numbers; ne-api needs a raw-driver
  (asyncpg) LISTEN connection outside SQLAlchemy's pool — an odd new seam.
- **Verdict: rejected as the mechanism; retained only as an optional wake-up accelerator.**

**(b) Poll-relay over a durable frame table (pull).** Worker appends
`(turn_id, seq, frame_json)` rows; ne-api tails `WHERE turn_id=? AND seq>? ORDER BY seq` on a
short interval.
- R-a: explicit `seq`, and — decisive simplification vs the outbox — **each turn has exactly ONE
  producer** (the one workflow execution), so `seq` is assigned monotonically in-process. The
  entire NE-070 settled-horizon apparatus (multi-producer id gaps, dead-hole fences,
  outbox.py:18-62) is *unnecessary here by construction*.
- R-b: rows are durable; any reader tails from seq 0 at any time.
- R-c: `PRIMARY KEY (turn_id, seq)` + idempotent insert (the SAVEPOINT/`ON CONFLICT` discipline
  of outbox.py:264-275): a DBOS re-execution re-emitting frame N is a no-op. Perfect fit with
  at-least-once step re-execution.
- R-d: no rows during a HITL wait is simply an empty poll; nothing times out.
- R-e: the table is the buffer — worker never blocks on the browser; delta coalescing (flush
  every ≥50 ms or ≥512 chars once true token streaming exists; v1 emits 2 frames per turn, §4.4)
  bounds row counts. R-f: `frame_json` is TEXT/JSONB — unbounded.
- R-g: needs an explicit sweeper (delete on relay-complete + TTL sweep for orphans).
- Cost: ~150 LOC of ours (table + append + tail + sweeper) plus tests; poll adds ≤ one interval
  (150–250 ms) of latency per frame batch — imperceptible for chat, and a LISTEN wake-up can be
  layered later if it ever matters.
- **Why NOT `ne_outbox_events` + cursors:** the outbox is a governance-event log with
  per-consumer registered-immutable filters (outbox.py:72-76, 315-321), at-least-once
  multi-worker drain semantics, and the fence machinery — all wrong-shaped for thousands of
  ephemeral per-turn frames (each turn would need a throwaway consumer row; the filter-superset
  invariant forbids per-turn filters; token text does not belong in the audit-adjacent typed
  event log, and deleting relayed frames would violate the outbox's append-only discipline).
  Reuse the *discipline* (idempotent transactional append), not the table.

**(c) DBOS workflow streams (adopt).** DBOS Transact ships exactly this primitive: a workflow
writes ordered values to a named per-workflow stream (`DBOS.write_stream(key, value)` /
`DBOS.close_stream(key)`), and an external process reads them in order via the client
(`DBOSClient.read_stream(workflow_id, key)` — a generator that yields as values arrive and ends
at close). Internally it is (b) — an ordered, durable table in the DBOS system database with a
poll/notify reader — built and maintained by the library we already pin.
- R-a/b/c: ordered, durable, cursor-per-reader; writes from **workflow** context are recorded
  exactly-once (checkpointed like any workflow action); writes from **step** context are
  immediate but may duplicate on step retry — which our frame schema tolerates by carrying
  `seq` for dedupe, and which `done.output` self-heals regardless (§3).
- R-d: an idle stream just blocks/polls the reader; nothing times out. R-e/f: values are
  serialized payloads in Postgres; the reader never backpressures the writer. R-g: stream rows
  are garbage-collected with their workflow by DBOS's retention machinery (verify the retention
  knob at build time). R-h: Postgres only.
- Cost: ~0 LOC of transport code of ours; the client handle is the SAME `DBOSClient` §2.1
  already requires for enqueue — one seam, two uses.
- Risk: API-surface verification against the pinned `dbos>=2.26.0,<3` (streams shipped in 2025,
  well before 2.x, but signatures must be pinned at build time); and an async
  read path for ne-api's event loop (wrap the blocking generator in `anyio.to_thread` /
  iterator-in-thread if the client exposes only sync — the same sync-library discipline as
  ne-api spec §3.3).

### 4.3 DECISION

**Adopt (c) DBOS workflow streams as the transport, with (b) as the specified fallback**, and
(a) rejected standalone. Rationale: (c) and (b) are the same design — durable ordered frames in
Postgres, pull-relayed — and the platform's standing build-vs-adopt directive (SPEC §11
owner directive 2026-07-04: default adopt) says don't hand-roll a mechanism the already-pinned
durability engine ships. The fallback matters because it is *drop-in*: the worker-side
`TurnFrameWriter` and api-side `TurnFrameReader` seams below are transport-agnostic, so a
build-time verification failure on the stream API swaps the implementation without touching the
turn workflow, the relay loop, or a single test assertion about SSE bytes.

**Frame record schema** (transport-independent; this is what the worker emits and ne-api's
existing `sse.py` encoder renders — spec §2.3 payloads verbatim plus a transport envelope):

```json
{"seq": 3, "type": "token",     "delta": "…"}
{"seq": 4, "type": "citations", "citations": []}
{"seq": 5, "type": "done",      "session_id": "…", "output": "<full text>"}
{"seq": 5, "type": "error",     "detail": "<error class / safe message>"}
```

`seq` is stripped before SSE encoding (the browser payloads stay exactly spec §2.3 —
byte-identical). `done`/`error` are terminal and mutually exclusive; the reader stops at either
or at stream-close. Redaction discipline for `error.detail` is inherited verbatim from spec
§2.4 step 4 (error class / safe message; never stack traces or backend URLs).

**Seams:**

```python
# packages/ne-worker/src/ne/worker/relay.py
class TurnFrameWriter(Protocol):
    def emit(self, frame: TurnFrame) -> None: ...   # called from workflow context (v1)
    def close(self) -> None: ...
# impl A (primary):  DBOS.write_stream("frames", frame.model_dump()) / DBOS.close_stream
# impl B (fallback): idempotent INSERT into ne_turn_frames (PK turn_id,seq; ON CONFLICT DO NOTHING)

# packages/ne-api/src/ne/api/turn_relay.py
class TurnFrameReader(Protocol):
    def frames(self, turn_id: str) -> AsyncIterator[TurnFrame]: ...  # ordered, from seq 0, dedup by seq
# impl A: DBOSClient.read_stream(turn_id, "frames") via to_thread;  impl B: seq-tail poll loop
```

### 4.4 The relay loop in ne-api (replaces spec §2.4 step 3's inline turn, keeps everything else)

```
1. validate  (spec §1.2 #3 — unchanged, pre-stream JSON errors)
2. enqueue   (§2.1)  → turn_id
3. open SSE; start the keep-alive ticker (spec §2.4 step 2 — unchanged, ne-api-local)
4. async for frame in reader.frames(turn_id):      # dedupe on seq; multiplexed with ticker
       emit SSE frame (event: <type> + payload sans seq)
       if frame.type in ("done", "error"): break
5. deadline: NE_API_TURN_TIMEOUT_S (default 300 s) of TOTAL wall clock without a terminal frame
   → emit `error {"detail": "turn still running; the approval queue and session history hold
   the outcome"}` and close the SSE. THE TURN KEEPS RUNNING DURABLY — this is the material UX
   upgrade over spec §7-R9: under NE-104a a timeout abandoned the turn; under NE-103c the
   pending approval row stays actionable in /v1/approvals and the completed output lands in
   session history (`append_turn`, agent.py:133).
6. client abort (AbortSignal, spec §2.4 step 6): stop reading and close. The workflow is NOT
   cancelled (deliberate: durable turns outlive browsers; spend remains bounded by the shared
   guardrail caps — C12). A cancel surface is out of scope (the frozen console contract has
   none) — logged as an open question (§10-Q6).
```

**Failure-mode walk-through of the chosen mechanism:**

| Failure | Behavior |
|---|---|
| Worker crashes mid-turn | DBOS recovery re-runs the workflow (§6.2). NE-TURN-PROGRESS: the step streams per-hop `tool` frames from STEP context WHILE the turn runs (at-least-once — `write_stream_from_step`); a re-run step re-emits from seq 0 and the reader's `seq` dedupe drops the overlap. The terminal `token`/`done` pair stays workflow-context exactly-once, numbered after the hops (`TurnStepResult.frames_emitted`); terminals pass BOTH dedup sites regardless of seq, so a raised re-run's seq-0 `error` still renders. `done.output` self-heals regardless (§3). A still-attached browser may see the re-run's hops continue after the first run's — real events of the re-execution, deduped only by seq overlap. Keep-alives hold the browser between hops; the timeout (step 5) bounds the wait. |
| ne-api replica crashes mid-relay | Browser's fetch dies (client re-POSTs a NEW turn if the user retries — new turn_id, old turn completes durably in history). No worker impact; frame state GC'd with the workflow. |
| Enqueue succeeds, SSE reader attaches before worker dequeues | Reader simply waits at seq 0 — durable stream has no "attached too early" failure (the LISTEN/NOTIFY fatal, solved by construction). |
| Turn blocked on HITL for hours | The park is VISIBLE (NE-TURN-PROGRESS): a `tool` frame with `decision=pending` + the REAL gate `request_id` streams the moment `approval.requested` lands on the chain; keep-alives tick after it; timeout (step 5) closes the browser leg with the documented message; approval stays in /v1/approvals/pending (same DB rows — §6.1); on approve, the WORKER's `await_outcome` poll (approvals.py:264-290) releases and the turn completes durably (the post-approval execute streams its allow frame). |
| Postgres down | Everything stops together — the platform's single-spine failure mode; no partial states (enqueue is transactional; frames unread remain; DBOS resumes on recovery). |
| Frame/stream growth | v1: ≤ 3 frames/turn. Post-R7 with coalescing: ~10²/turn. GC: DBOS workflow retention (impl A) or the sweeper cron job (impl B) — §7 gives cron a home either way. |

---

## 5. The turn workflow (serialization + recovery semantics)

### 5.1 Shape — house pattern of `approvals_durable.py`

Module-level functions in `packages/ne-worker/src/ne/worker/workflows.py`, serializable
args/returns, resources from the worker's process-global composition root (never from args):

```python
class TurnRequest(BaseModel):        # frozen, extra="forbid"
    turn_id: str; session_id: str; agent_name: str; department: str; prompt: str
    principal_id: str; principal_groups: tuple[str, ...]; principal_claims: dict[str, Any]

@ne_step(name="chat.turn.execute")
async def execute_turn_step(request_json: str) -> str:
    req = TurnRequest.model_validate_json(request_json)
    platform = worker_platform()                     # §6.1 process singletons
    principal = Principal(id=req.principal_id, groups=tuple(req.principal_groups),
                          claims=req.principal_claims)
    session = await create_department_session(       # factory.py:447-467; open_session is
        req.agent_name, req.department,              # get-or-create (store.py:145) → the
        store=platform.session_store,                # api-created session_id is re-derived;
        gateway=platform.gateway,                    # assert == req.session_id (defense)
        executor=platform.executor, settings=platform.runtime_settings)
    result = await session.run(req.prompt, principal=principal)   # agent.py:96-138 — C6 intact
    return result.output

@ne_workflow(name="chat.turn")
async def run_turn_workflow(request_json: str) -> str:
    req = TurnRequest.model_validate_json(request_json)
    writer = frame_writer(req.turn_id)
    try:
        output = await execute_turn_step(request_json)
    except Exception as exc:                # safe-detail mapping mirrors spec §2.4 step 4
        writer.emit(error_frame(exc)); writer.close(); raise
    writer.emit(token_frame(output))        # v1 turn-then-stream: ONE token frame (spec §2.4-3)
    writer.emit(done_frame(req.session_id, output)); writer.close()
    return output
```

- Async `@ne_workflow`/`@ne_step` are supported by DBOS Python (verify on the pinned version);
  if the build finds otherwise, the step body runs the coroutine on the worker's loop via a
  sync shim — the decorator seam (durable.py:44-87) is unchanged either way.
- Frames are written from **workflow** context (exactly-once under DBOS), not from inside the
  step — deliberate for v1. When spec §7-R7's `run_stream` seam lands in ne-runtime, token
  frames move inside the step (at-least-once) and the `seq`-dedupe + `done`-self-heal of §4.3
  absorbs the semantic change with zero transport rework.
- "Steps never call other steps" (approvals_durable.py:22-23) is honored: one step, called
  from the workflow only.

### 5.2 Recovery semantics — stated honestly

The turn is durable at **turn granularity**: `execute_turn_step` is one step wrapping the whole
pydantic-ai loop, because the loop's internals (live model/tool calls) are non-deterministic
and not yet step-decomposed — exactly the honesty note of durable.py:5-13. Consequences:

1. **Worker restart mid-turn ⇒ the turn re-executes from the top**, not from the last token.
   Session history is safe (`append_turn` happens once, after success — agent.py:133; a
   crashed attempt appended nothing). Completed turns never re-execute (workflow-id
   idempotency; the recorded result is returned).
2. **Re-executed tool calls:** tier-0/1 reads are idempotent; NE-050 write actions are
   dedup-protected across a step re-run via `action_scope(turn_id)` — a derived key of
   `sha256(action:derived:scope:payload_hash)` (NE-ACTION-DETERMINISTIC-REQID) —
   **not** via a model-chosen `request_id`, which a re-prompted model need not
   reproduce; an explicit caller-supplied `request_id` (a programmatic caller, not the
   agentic path) keeps Stripe-style semantics and is scope-independent. **Honest
   residual:** payload DRIFT on re-prompt (a re-run model rephrasing the write) still
   changes the derived key, so a drifted retry is NOT deduplicated — it can reach a
   second human approval and a second write; full closure needs the per-call step
   decomposition of §10-Q3. **But** a re-run
   that had already obtained a tier-2/3 approval will submit a FRESH approval request
   (`DurableApprovalGate.submit` per call) — the human may be asked twice. Direction of
   failure is safe (never auto-approved, never skipped — SPEC §4.2 "never auto-approve"), and
   the frequency is worker-crash-rate, not steady-state. Accepted for v1; recorded as §10-Q2
   with the mitigation path (approval request keyed by `(turn_id, action, args_hash)` dedupe —
   an ne-runtime change, deliberately out of NE-103c's lane).
3. **Audit is append-only truth:** the audit chain records both attempts' tool decisions —
   correct, that is what happened. No dedupe belongs there.
4. Finer-grained checkpointing (per-model-call steps inside the loop) is the known future work
   unlocked by the same seam as R7 — out of scope, tracked in §10-Q3.

The **CI durability proof asserts exactly semantics (1)** — see §6.3.

---

## 6. Governed execution in the worker — no second ungoverned executor

### 6.1 The worker composition root mirrors ne-api's, sharing state through Postgres

`packages/ne-worker/src/ne/worker/main.py` performs the ne-api spec §3.2 lifespan steps 1-11
with the SAME normative order — settings/secrets → **`freeze_platform_config` FIRST**
(factory.py:102-146; `ConfigHashNotFrozenError` is a boot failure) → engines (+
`ensure_transactional_sqlite`) → ONE audit sink → `create_approval_gate` (factory.py:278-314)
→ `create_shared_guardrail_state(platform_postgres_url)` (factory.py:149-179) → permissions/
brain/gateway as tool bindings require → the NE-CKD1 recipe (factory.py:30-47):

```python
ledger, counter = create_shared_guardrail_state(settings.session_database_url)   # NE-070H durable pair
scope_rail = AgentScopeGuardrail(credential_service)      # root MAY import ne-identity (factory.py:32-33)
executor = create_tool_executor(bindings, sink=sink, approval_gate=gate,
                                ledger=ledger, counter=counter, extra_guardrails=(scope_rail,))
scheduler = create_cron_scheduler(engine=engine, …, sink=sink, approval_gate=gate,
                                  ledger=ledger, counter=counter, extra_guardrails=(scope_rail,))
```

**"Share the same singletons" across two PROCESSES means share-by-storage, and the platform
already built that:** the spend ledger + rate counter are the NE-070H Postgres-backed pair —
"ATOMIC + SHARED across ALL workers" (factory.py:169-171; proven under 16 concurrent workers);
the approval gate's state is `SqlAlchemyApprovalStore` rows on the platform
engine, so an approval CREATED by the worker's executor is LISTED by ne-api's
`/v1/approvals/pending` and RELEASED by ne-api's approve route — the cross-process HITL loop is
the same rows (this is the single most valuable e2e test, §6.3-T4); the audit sink appends to
the ONE hash-chained `ne_audit_log`; the session store is the same tables. Within each process
there is exactly one instance of each (ne-api spec §3.2 discipline); across processes the
DISCIPLINE is shared durable state — no new mechanism needed, only correct wiring.

**Shared-root extraction (anti-drift):** the composition steps are ~identical in both
processes. Extract them into `ne/api/platform.py :: build_platform(settings) -> Platform`
during NE-104a-follow-up or the first NE-103c unit, and have BOTH `ne.api.app` and
`ne.worker.main` call it (`ne-worker` declares a dependency on `ne-api` — same image installs
both anyway). One recipe, two processes: the strongest structural guarantee that
the worker cannot silently compose a weaker stack.

### 6.2 config_hash — frozen once in the worker, and it MUST equal ne-api's

`freeze_platform_config` covers `RuntimeSettings` (factory.py:134-137), and `RuntimeSettings`
**includes `durability`** (config.py:100-101). If ne-api ran `durability='off'` while the
worker ran `'dbos'`, the two processes would stamp **different composed config_hashes** on
their audit events — splitting the ADR-0003 "one platform hash" and the NE-070 config-bundle
stamp. Therefore, normative:

- **Both `ne-api` and `ne-worker` run `NE_RUNTIME_DURABILITY=dbos` in the pilot profile.**
  Safe for ne-api because: (i) the decorators fire at decoration/import time (durable.py:15-21)
  and ne-api's import graph contains NO `@ne_workflow`-decorated module —
  `ne.worker.workflows` is never imported by ne-api (it enqueues by NAME, §2.1), and
  `ne.runtime.approvals_durable` is not re-exported by `ne.runtime.__init__` (verified — no
  import there); (ii) only the worker calls `launch_durable_runtime` (durable.py:90-105).
  A grep/import-linter guardian pins (i) permanently: `ne.api` may not import
  `ne.worker.workflows` or `ne.runtime.approvals_durable`.
- Boot cross-check: both processes log the frozen hash; the pilot e2e test asserts a
  worker-emitted audit event and an api-emitted audit event carry the SAME `config_hash`.

### 6.3 Proof obligations (tests the build must land)

- **T1 — grep guardian, extended:** in `ne-worker` src there is no `GovernedToolExecutor(`
  construction (the class, middleware.py:114, is constructed only inside
  `ne.runtime.factory` seams — factory.py:216-222, 366-368, 430-432), no tool-handler module
  imports outside the bindings list, and no second `AuditSink`/gate construction — the ne-api
  spec §4.1 invariant verbatim, now quantified over both processes.
- **T2 — composition test:** boot the worker root; assert one sink/gate/ledger/counter
  instance; assert `get_config_hash()` frozen before any event; assert the executor's gate IS
  the gate whose store rides the platform engine.
- **T3 — the durability proof (promote `test_dbos_live.py`):** new gated
  `packages/ne-worker/tests/test_worker_dbos_live.py`, gating flipped from opt-in
  (`NE_LIVE_DBOS=1`, test_dbos_live.py:46-56) to **CI-on** in the live-Postgres CI lane that
  NE-070 established (live outbox tests already run in CI). Assertions:
  (1) enqueue `chat.turn` from the test process via `DBOSClient` (workflow_id=T) against a
  worker **subprocess** (`python -m ne.worker`, stub gateway — the stub is the CI boundary,
  test_dbos_live.py:16-17); (2) the turn's step blocks on a DB flag; **SIGKILL the worker
  mid-step; restart it; assert the workflow resumes and completes** — turn-side-effect row
  count proves at-least-once step / exactly-once completion (`retrieve_workflow(T).get_result()`
  returns once, re-enqueue of T is a no-op); (3) the frame stream contains exactly one `done`
  after recovery and the SSE-visible payload sequence is contract-legal; (4) `test_dbos_live.py`
  + `test_approvals_dbos_live.py` themselves run in the same lane (delete the module-level
  skip in favor of the `NE_TEST_POSTGRES_URL` gate).
- **T4 — cross-process HITL e2e:** worker turn hits a tier-2 tool → pending row appears via
  the gate's `list_pending` on a SECOND process's gate instance (simulating ne-api) → approve
  there → worker turn releases and `done` arrives. This is the no-bypass composition proof
  ACROSS the process boundary — the ne-api spec §5.3-4 test upgraded to two processes.
- **T5 — passthrough parity (upgrade-in-place validation):** the SAME workflow function run
  with `durability='off'` (plain call, no DBOS) yields the same frames/output — proving the
  owner's "upgrade-in-place, nothing wasted" claim is a tested property, not a
  slogan.

---

## 7. Cron — the NE-034 scheduler in the same worker

Same process, after `DBOS.launch` — exactly the composition the runtime README documents
(README.md:438-444) and the scheduler deliberately left DBOS-import-free (scheduler.py:38-40):

```python
@ne_workflow(name="cron.tick")
async def cron_tick() -> None:
    await worker_platform().scheduler.run_due_jobs()      # scheduler.py:290
# driven by DBOS.scheduled("* * * * *") in dbos mode; a plain asyncio ticker in 'off' mode
```

- The scheduler is built by `create_cron_scheduler` with the SAME sink/gate/ledger/counter/
  scope-rail as the turn executor (§6.1) — cron tool calls ride the one governed chokepoint
  and the one cap pool (NE-CKD1, factory.py:400-415).
- Multi-worker double-fire safety is already in the store (CAS due-claim, scheduler.py:35-37);
  DBOS.scheduled adds exactly-once-per-interval workflow semantics on top. Job mutations and
  runs stay dual-appended (outbox + hash chain in one txn — scheduler.py:18-28).
- The outbox **exporter loop** the outbox has been waiting for ("ne-worker owns the loop, a
  later unit", outbox.py:599) gets its process here: a worker background task ticking
  `drain_once(engine, consumer, handler)` (outbox.py:591-636). v1 wires the loop with a no-op/
  OTel-export handler behind a setting; competing replicas are already safe (SKIP LOCKED claim,
  outbox.py:423-428). If §4.3 lands on fallback impl B, the frame-table TTL sweeper is a
  scheduled tier-0 job in this same scheduler.

---

## 8. Compose / topology — one image, different command

Per NE-103a's locked pattern ("one first-party image `ne-app`, multi-role by command"; owner
decision (ii) local-digest pin):

```yaml
# deploy/compose/pilot/compose.yaml — fills the reserved "agents" profile (pilot README.md:27)
ne-worker:
  image: ${NE_APP_IMAGE}            # the SAME locally-built digest-pinned ne-app image as ne-api
  profiles: ["agents"]
  command: ["python", "-m", "ne.worker"]
  depends_on:
    postgres: {condition: service_healthy}
    opa:      {condition: service_started}     # PDP is a tool-call-time dependency
  environment:                       # secrets via the NE-070 SecretStore bundle, never literal
    NE_RUNTIME_DURABILITY: "dbos"
    NE_RUNTIME_DBOS_DATABASE_URL: …  # dedicated `ne_dbos` database on the platform Postgres
    NE_RUNTIME_SESSION_DATABASE_URL: …
  # NO ports, NO Caddy vhost — the worker has no inbound HTTP surface at all
  healthcheck: {test: ["CMD", "python", "-m", "ne.worker", "--health"]}   # DB ping + launched-marker
  # + the pilot tier's standard hardening (resource limits, cap-drop — NE-070 conventions)
```

- **DBOS's Postgres:** a dedicated `ne_dbos` database on the same pilot Postgres instance
  ("DBOS owns its Postgres schema") — created by the pilot initdb
  (`deploy/compose/pilot/initdb/`); DBOS runs its own system-schema migrations at `launch()`.
  Keeping it a separate database keeps workflow/queue state out of the platform schema's
  backup/migration story while staying on one instance at pilot scale.
- `ne-api` service (from NE-103a) gains `NE_API_TURN_EXECUTION=enqueue` +
  `NE_RUNTIME_DURABILITY=dbos` (§6.2) + the DBOS system-DB URL for its `DBOSClient`.
- Guardian + air-gap wiring per the NE-103 plan: add `ne-worker` to the pilot-stack guardian's
  `PROFILE_MATRIX` and to `test_airgap_phase_a.py`'s healthy-container tuple
  with the image built/loaded in the sealed-netns staging window.
- Scale-out: `ne-api` replicas are free (stateless); `ne-worker` replicas are ALSO safe by
  construction (DBOS queue partitioning + NE-070H shared caps + CAS cron claims) but v1 ships
  ONE worker — multi-worker soak is a stated non-goal until a load test exists.

---

## 9. Deliverables + decomposition (coordinate the ne-runtime lane)

| # | Deliverable | Package(s) |
|---|---|---|
| D1 | `packages/ne-worker` skeleton: `main.py` (root, launch, cron, drain loop), `workflows.py`, `relay.py` (writer), `settings.py` (`NE_WORKER_*`), `--health` | ne-worker (new) |
| D2 | Shared `build_platform` extraction | ne-api (refactor-only) |
| D3 | Enqueue mode + `TurnFrameReader` relay + timeout policy in the chat router | ne-api |
| D4 | Compose service + guardian + air-gap wiring | deploy, tools/ne-cli tests |
| D5 | Test promotion + the new live CI lane assertions (§6.3 T1-T5) | ne-runtime (tests only), ne-worker, CI |
| D6 | Short ADR: turn-enqueue + frame-relay as sanctioned database-mediated cross-process comm (SPEC §4.1 deviation clause); frames-are-not-events rationale | docs/adr |

**Two units, serial** (the pipeline's review-granularity precedent — one governance story each):

- **NE-103c-1 — the durable worker exists and is proven** (D1 minus streaming, D2, D4, D5-T1/2/3,
  D6): worker boots, launches DBOS-Postgres, runs cron + the outbox drain loop under the shared
  governed stack, and the crash-recovery CI proof is green. Chat turns still run inline in
  ne-api. Review focus: composition/no-second-executor + the live durability proof.
- **NE-103c-2 — turns move to the worker + cross-process streaming** (D3, workflows/relay in
  D1, D5-T4/5): enqueue mode on, relay loop, cross-process HITL e2e, SSE byte-identity tests
  (vendored sse.ts grammar rules, ne-api spec §5.3-5). Review focus: the streaming mechanism's
  failure modes + principal-carriage no-bypass.

**Lane coordination:** ne-runtime source is intentionally UNTOUCHED (the decorators, factories,
session, scheduler already expose every needed seam) except test-gating changes (D5) — so the
ne-runtime lane stays free for parallel work; NE-103c-1/2 live in the ne-worker + ne-api +
deploy lanes. If build-time verification demands any ne-runtime seam change (e.g. §10-Q2's
approval dedupe), it ships as its own small ne-runtime unit, not smuggled into this one.

---

## 10. Open questions / risks (none may be silently hand-waved)

- **Q1 — DBOS API verification (top build-gate).** `DBOSClient` enqueue-by-name,
  `Queue`, `write_stream`/`close_stream`/`read_stream`, async workflow support, stream/workflow
  retention (GC), and system-DB migration behavior must be pinned against the LOCKED dbos
  version in the first build round. Fallbacks are specified for the
  two riskiest surfaces: enqueue (§2.2 claim table) and streams (§4.3 impl B) — both drop-in
  behind the seams. If BOTH fail, the design degrades to (b)+(claim table) entirely on our
  schema with unchanged semantics.
- **Q2 — approval re-prompt on turn re-execution** (§5.2-2). Fail-safe direction, crash-rate
  frequency, accepted v1; mitigation path = approval-request idempotency key
  `(turn_id, action, args_hash)` in the gate — an ne-runtime unit if the pilot finds it noisy.
- **Q3 — turn-granularity durability** (§5.2). "Resumes the workflow" at v1 = re-runs the turn,
  not resumes mid-token. Honest in all docs/demos; per-model-call step decomposition rides the
  same future seam as spec §7-R7 token streaming. Do not let sales language outrun this.
- **Q4 — long-blocking step under HITL.** `execute_turn_step` can pend hours inside
  `await_outcome` (SLA 4 h, config.py:43). Verify DBOS imposes no step wall-clock limit and
  that queue concurrency slots held by blocked turns can't starve the queue (mitigation:
  per-queue concurrency ≥ expected concurrent HITL waits; or the ne-runtime seam later swaps
  `time.sleep`-style polling for `DBOS.sleep`, approvals_durable.py:21-22 anticipates this).
- **Q5 — config_hash parity** (§6.2). The `durability` field sits inside the hashed
  `RuntimeSettings`; the both-processes-`dbos` rule is load-bearing and must be enforced by the
  pilot config bundle + the boot cross-check test, or audit correlation silently forks.
- **Q6 — no cancel surface.** An abandoned browser leaves a durable turn running to completion
  (bounded by guardrail caps + `max_requests`, config.py:99). Frozen console contract has no
  cancel; decide post-pilot whether `DBOSClient.cancel_workflow` gets an ops surface.
- **Q7 — orphaned-output visibility.** A timed-out/aborted turn's output lands in session
  history (agent.py:133) but the frozen 19-endpoint contract has no history GET — the user
  cannot see it until a v1.1 additive endpoint exists. Same residual as ne-api spec §7-R9,
  now with a better story (the work isn't lost) — surface in the runbook.
- **Q8 — upgrade-in-place claim validation.** Decoration-time durability (durable.py:15-21)
  means a process cannot flip modes without restart, and mixed-mode fleets (api `off`, worker
  `dbos`) violate Q5. "Upgrade-in-place" = same CODE both modes (proven by §6.3-T5), achieved
  operationally by a coordinated env change + restart — say exactly that in the runbook.
- **Q9 — DBOS system-DB ops.** Backup/retention of `ne_dbos` joins the pilot backup story
  (NE-069 lane); unbounded completed-workflow growth is a real leak if retention (Q1) is
  misconfigured — the worker `--health` check should surface system-DB row-count watermarks.
