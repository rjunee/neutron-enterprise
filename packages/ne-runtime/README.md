# ne-runtime

The governed, durable agent runtime — and **THE C6 chokepoint**: every agent
tool call passes the deterministic guardrails (NE-032) **and** the PDP (NE-031)
**and** (for require-approval) a human gate, then emits an audit event, with
**no bypass path**. C6 is made airtight here by construction, not by convention.

## The C6 chokepoint (in text)

```
agent turn
  │
  ▼
GatewayModel.request ──► ModelGateway (frozen ne.contracts seam; NO provider SDK, C3)
  │   prompted tool-calling (D3): one-line {"tool_call": {...}} JSON in a system message
  ▼
pydantic-ai loop picks a tool ─────────────────────────────────────────────┐
  │                                                                          │
  ▼                                                                          │
GovernedToolExecutor.execute(tool, args, principal)   ◄─ the ONLY path to a handler
  │                                                                          │
  ├─(1) GuardrailEngine.enforce  ── C12, runs FIRST ── block  ─► audit + raise (unwrapped)
  │                                                                          │
  ├─(2) GovernedPdp.decide       ── C6, audits by construction ── DENY ─► ToolDeniedError
  │                                                                          │
  ├─(3) REQUIRE_APPROVAL ─► ApprovalGate ── not APPROVED / no named approver ─► block + audit
  │                                        (Tier-2/3 NEVER auto-approve, structurally)
  │                                                                          │
  └─(4) handler(args) ─► audit tool.execute[success|error] ─► result ───────┘
```

`GovernedToolExecutor` is the **only** object that holds a tool `handler`, and
its constructor **requires** `pdp`, `guardrails`, `approval_gate`, and `sink`
(all keyword-only, no defaults, none may be `None`). There is no sink-less /
PDP-less / gate-less way to build one, so:

- the PDP audit coupling is free (`create_pdp(settings, sink=...)` audits every
  decision before returning);
- guardrail-block auditing is free (`GuardrailEngine` requires the sink);
- the middleware's **own** events fail closed — if the audit append fails,
  `RuntimeAuditError` is raised and the result is **withheld**.

The per-run pydantic-ai tool functions take `**kwargs` **only**, with the
`ToolSpec` name captured by **closure**. pydantic-ai calls tool functions
keywords-only and *skips* schema validation, so any *named* parameter would be
model-shadowable — a crafted argument key (e.g. `_spec`) could rebind an internal
carrier and crash the run **before** the governed executor. The closure binding
makes such a key ordinary governed **data** instead of a pre-governance crash;
enforced by the three shadow tests in `tests/test_c6_no_bypass_guardian.py`.

The permanent, un-skippable structural enforcement lives in
`tests/test_c6_no_bypass_guardian.py` (build-orchestration plan §2.1): it scans
that `execute` is the only public callable, that `.handler(` is called only in
`middleware.py`, that no provider model is imported, and that every executed call
was preceded by a `pdp.decide` event. **Do not weaken it.**

## Provisional-context escalation (NE-T2X-5, design §6.1/§6.3)

`ne.runtime.provisional` is a turn-scoped accumulator — modeled on
`correlation.run_trace_scope` (inherit-if-ambient `ContextVar`) and
`grounding.GroundingRecorder` (fresh per turn) — answering ONE question for
`GovernedToolExecutor`'s PDP-input-threading seam: did this turn's retrieval
touch any unconfirmed, LLM-extracted graph fact?

- `provisional_context_scope()` — a context manager `DepartmentAgentSession.run`
  enters alongside `run_trace_scope` for every turn (fresh accumulator; a prior
  turn's provisional-ness must never taint this turn); a sub-agent dispatched
  INSIDE a provisional turn inherits the ambient accumulator (poisoned context
  follows the call tree).
- `current_provisional_recorder()` — the active accumulator's `.record(*,
  provisional_source_count, provisional_source_refs=(), extraction_mode)`,
  the seam a verified-read tool handler binds AS DATA (the
  `current_answer_recorder` precedent). **Counts SUM** across retrievals in one
  turn (fail toward flagging); refs are ordered-deduped; the mode-conflict rule
  prefers any non-`"autonomous"` mode over `"autonomous"` once one has been
  recorded.
- `current_provisional_context()` — a `ProvisionalContext` snapshot (`None`
  outside any scope), whose `.provisional` bool and `.extraction_mode` thread
  onto the PDP's `PolicyRequest.context` in `middleware.execute()`:
  `context["context_provisional"]`/`context["extraction_mode"]`, added ONLY
  when a scope is active — with no active scope the context dict is
  **byte-identical** to before this unit (`{"tier": ...}` alone).

**Wired in production (`NE-T2X-5-RECORDER-WIRE`).** The production caller is
`ne.api.tools.build_chat_bindings`'s `read.knowledge.search` handler (the
`_search` closure): it calls `current_provisional_recorder()` INSIDE the
handler, at request time, and — when the retrieval touched any
assurance-labeled graph element — records the count/refs into the turn's
accumulator. The count/refs are derived from the ONE pinned
`ne.api.wire.provisional_summary` traversal (never a second, hand-rolled
count), and `extraction_mode` is threaded explicitly from the deployment's
configured `BrainGraphSettings.extraction_mode` at the composition root
(`ne.api.platform.build_platform`), never guessed at the binding. The
end-to-end proof — a real SSE chat turn, the real in-process Cedar PDP, the
real `DurableApprovalGate` — lives in
`packages/ne-api/tests/test_provisional_recorder_wiring.py`: an
`assurance="extracted"` retrieval followed by a `write.internal.*` call
requires a named human approval; the byte-identical sequence over
`assurance="source"` facts does not. Previously (Codex cross-review, prior to
this unit) the binding was missing entirely: the turn accumulator was always
empty, `context_provisional` was always `false`, both PDP backends returned
Tier-1 **allow**, and a `write.internal.*` following a retrieval of
unconfirmed extracted facts executed with no human approval — precisely the
A-P1-5 risk design §6.3 exists to close. The earlier "one-line follow-up"
framing was accurate about the SIZE and wrong about the CONSEQUENCE; both are
now closed.

Everything downstream (the accumulator, the middleware threading, the PDP
escalation policy in `ne-govern`, and the approval-queue read-model below) is
fully exercised against a synthetic recorder by
`tests/test_provisional_seam.py` and the §6.3 guardian,
`tests/test_provisional_escalation_guardian.py` (drives the REAL in-process
Cedar engine — a stubbed PDP there would prove the executor obeys a double,
not that the policy itself escalates).

**Approval payload (C12):** when the turn's context was provisional,
`_require_named_approval`'s payload gains `provisional_source_count` /
`provisional_source_refs` (the opaque `llm:<sha16>:<ref>` identifiers, never
fact text) — see "Event / audit catalog" below for how `PendingApproval`
surfaces the same fields read-side to the console queue.

**Payload disclosure (owner ruling 2026-07-31, NE-APPROVAL-PAYLOAD):** an
approver who cannot see what they are approving is not approving anything —
`_require_named_approval` also embeds a `disclosure` object into the payload
(`target_resource`, `description`, the RAW tool-call `args` as
`material_parameters`, and a tier-derived `reversibility` label from
`reversibility_label(tier)`). This material is at rest in
`ne_approval_requests.payload`, but it is served **exclusively** through
`DurableApprovalGate.read_payload(request_id, *, approver)` — the queue read
(`list_pending` / `PendingApproval`) stays byte-identical payload-free; that
redaction is unchanged and remains a real security property.

`read_payload`'s policy (single owner of the disclosure decision, enforced in
the gate, not the console): for Tier-3, `approver` must be in
`ApprovalSettings.tier3_named_approvers` — **both** dual-flow approvers of a
Tier-3 request qualify (the same list gates the first *and* second `approve`
check), and an empty configured list refuses everyone (fail closed). For
Tier < 3 the gate performs no further check — `ne-api`'s `approver_roles`
fail-closed pre-check is documented as the Tier-2 authorization layer (see
`ne.api.routers.approvals`'s module docstring), so any caller who cleared that
role gate may read. Every successful read audits `approval.payload_read`
**before** returning (fail-closed C6: a sink failure raises
`RuntimeAuditError` and withholds the disclosure); the audit detail carries
only `tier`/`action` — never a material value. See
`tests/test_approval_payload_read.py` (gate policy, THE load-bearing case is
the refusal of a non-named approver, proven on the exception's message
content) and `tests/test_middleware_with_durable_gate.py::test_submitted_row_carries_disclosure`
(the real persistence path, end-to-end from a governed tool call).

## Prompted tool-calling (decision D3)

`ne.contracts.models.Completion` has no `tool_calls` field (ne-contracts is
FROZEN). Rather than edit the contract, `GatewayModel` implements a **prompted**
protocol entirely in-band:

- the tool catalog + a strict instruction are rendered into ONE system message;
- to call a tool the model replies with EXACTLY one line of JSON
  `{"tool_call": {"name": "<tool>", "arguments": {...}}}`; anything else (prose,
  malformed JSON, wrong shape, extra keys, non-dict arguments) is treated as the
  **final answer** — parsing never raises;
- `tools=` / `tool_choice=` are **never** placed in `CompletionRequest.params`.

An unknown tool name still parses to a tool call — and is routed through
`GovernedToolExecutor` **before** pydantic-ai's tool resolution, via the
`GatewayModel`'s **required** `on_unknown_tool` hook. Every parsed tool call whose
name is unregistered produces a `tool.unknown` AuditEvent, an audited guardrail
verdict (rule `toolargs.unknown-tool`), and an audited PDP **DENY** (unclassified
action), then is terminally refused — the unknown tool never executes. There is no
hook-less `GatewayModel` constructor, so this governance cannot be bypassed.

**Fast-follow ADR (reported to the orchestrator):** add an *additive*
`Completion.tool_calls` field to `ne.contracts.models` so native tool-calling
providers can be used without the prompted shim. ne-contracts was NOT edited
here (frozen); the prompted protocol is the v1 boundary.

## Message-shape contract (C3, NE-AGENT-MSG-ORDER)

The outgoing `CompletionRequest.messages` sequence carries **at most one
system-role message, and it is always first**. A strict chat template (e.g.
Qwen3.6-35B-A3B-FP8's, served via vLLM with `--tool-call-parser
qwen3_coder`) HTTP-400s `"System message must be at the beginning."` on any
system message that is not first — the shipped shape (instructions as a
leading system message, the D3 tool catalog appended as a *second, trailing*
system message) failed exactly this way. Model independence is a headline
claim of the platform, so this is normalized at the boundary, not
special-cased per provider.

`GatewayModel._assemble` (used identically by `request()` and the
context-budget `_trim()` estimator, so the trim estimate stays byte-identical
to what is actually sent) folds every system-role message — mapped
instructions, mid-history `SystemPromptPart`s, and the in-band tool catalog —
into ONE leading system message via `_merge_system_head`: an order-stable,
lossless `"\n\n"`-joined merge (nothing is dropped, nothing is reordered
relative to other system content). `ne-gateway`'s `LiteLLMGateway` applies the
same contract, independently derived (never imported — `ne-runtime` and
`ne-gateway` do not import each other), at its own chokepoint (`complete()`
and `_stream()`) so every caller and every provider candidate gets a
conforming sequence.

Pinned by `tests/test_message_order_contract.py` (built from the real agent
path, not a hand-written message list) and
`packages/ne-gateway/tests/test_message_normalization.py`. There is no
per-model/per-provider conditional anywhere in this normalization — a
conditional would reproduce the defect for the next strict template.

## HITL approval gates (NE-035)

When the PDP returns `require-approval` (Tier-2/3, SPEC §4.2), the executor routes
to the production **`DurableApprovalGate`** — the object that FULFILS what the PDP
requires. A tool call awaiting approval does **not** execute until a **named**
human approves; requests are persisted (`SqlAlchemyApprovalStore`, SQLite-CI /
Postgres-parity, mirroring `store.py`) and **survive restarts**.

### Gate tier authority (NE-CKD3)

The gate tier is the **stricter-of** `max(PDP-decided tier, spec.tier, tiers.rego
python mirror in actions.py)` with obligation floors (`second_check` /
`named_approver` ⇒ Tier-3; `per_item_human_approval` ⇒ Tier-2). The decided tier
can only **escalate** gating, never lower it — so a Rego-bundle edit escalating an
action to Tier-3 gates at Tier-3 (named approver + distinct second check), not
Tier-2 single-approver (Checkpoint-D finding #3). An **ALLOW** at effective tier
≥ 2 **still requires the human**: the Python mirror is a defense-in-depth floor,
never a ceiling, so even a hijacked/rogue bundle cannot execute a Tier-2/3 action
without a human (C12). A CI **tier-parity guardian** cross-evaluates `tiers.rego`
(`opa eval`) against the mirror and fails the build on any divergence, so the
mirror can never silently drift below the policy.

### Lifecycle

```
submit ─► pending ─────────────────────────► approved   (Tier-2: one named approver)
             │   └► pending_second ─────────► approved   (Tier-3: named + distinct 2nd)
             ├─(now ≥ SLA)──► escalated  (a FLAG — still approvable; notify via events)
             ├─(reject)─────► rejected   (terminal, denied)
             └─(now ≥ expiry)► expired    (terminal, denied — a re-request is required)
```

### Never auto-approve — four enforcement layers

Tier-2/3 can **never** auto-approve. An approver-less `APPROVED` is impossible at
every layer, so a single regression cannot open a bypass:

1. **table `CheckConstraint`** (`ck_approval_named_approver`) — an `approved` row
   with no `first_approver` (or, at Tier-3, no distinct `second_approver`) is
   *unrepresentable* on SQLite **and** Postgres;
2. **`store.decide`** — raises `ApprovalIntegrityError` **before** any SQL when an
   `approved` decision carries no named approver;
3. **gate policy** — a non-empty approver is required; Tier-3 additionally requires
   membership in `approvals.tier3_named_approvers` **plus** a distinct second
   approver, and an **empty** configured list makes Tier-3 **unapprovable** (fail
   closed — 'no delegation below the named approver');
4. **`_to_outcome`** — refuses to construct an `APPROVED` outcome with an empty
   approver (belt-and-braces; also the existing middleware `ApprovalIntegrityError`).

### Idempotent submission (NE-INV-APPROVAL-SCOPE)

`DurableApprovalGate.submit()` is idempotent per `(principal_id, action, tier,
ambient action_scope, args_hash)`: inside a scoped turn, a DBOS step re-run
reattaches to the existing non-terminal request (invariant-audit Finding A)
instead of minting a duplicate row that would otherwise orphan forever in the
queue with no janitor sweep — a re-run supplies the SAME args, so it hashes to
the SAME key and reattaches. `tier` is part of the key — a same-action request
at a different tier never reattaches to a lower-tier pending row and inherits
its weaker approval policy (Codex review finding C1).

`args_hash` is a per-invocation discriminator (round 2, Argus re-check P1):
`pydantic-ai` dispatches a turn's tool calls concurrently via
`asyncio.create_task` with no sequential guard, so two GENUINELY DISTINCT
concurrent calls (same action/tier/principal/scope, different arguments) would
otherwise collapse onto ONE approval row and let a single human decision
authorize both external writes — the action-dedup `payload_hash` layer
(`action_tools.py`) does not catch this (different payloads, both writes
execute once approved). `args_hash` is the canonical-json (`sort_keys=True`)
sha256 hex digest of the RAW tool-call args, computed by the middleware
*before* validation (same shape as the action-dedup `payload_hash`, see below)
and threaded OUT-OF-BAND — never added to the frozen `ApprovalRequest` contract
(the payload carries a `disclosure` block readable only by a named approver (C12: the queue listing stays payload-free)) — through
`request_approval(..., args_hash=...)` → `submit(..., args_hash=...)` →
`store.create(..., args_hash=...)`. A scoped caller that omits `args_hash`
falls back to hashing its own (already-redacted) payload, which preserves
round-1 collapse semantics for legacy/direct callers and guarantees a scoped
submit never binds on a NULL hash. Outside any ambient scope, behavior is
unchanged — a fresh `uuid4()` per call, as before. Pinned by
`tests/test_approval_scope.py` and
`tests/test_middleware_with_durable_gate.py::test_concurrent_distinct_calls_get_independent_approvals`.

### §4.2 timeout / escalation (settings)

| Setting                     | Meaning                                            | Default |
| --------------------------- | -------------------------------------------------- | ------- |
| `approvals.tier2_sla_s`     | pending > SLA (Tier-2) → escalate + notify         | 14400   |
| `approvals.tier3_sla_s`     | pending > SLA (Tier-3) → escalate + notify         | 14400   |
| `approvals.expiry_s`        | hard expiry → `EXPIRED` terminal; re-request req.  | 86400   |
| `approvals.poll_interval_s` | await-loop poll cadence (tests inject 0)           | 0.5     |
| `approvals.tier3_named_approvers` | exhaustive Tier-3 approver list (**empty ⇒ never**) | `()` |

Escalation emits its notification via the **event spine** — an
`ApprovalEvent(escalated)` (outbox) plus an `approval.escalated` AuditEvent — which
console/Slack surfaces (NE-062/063) consume. An expired request can **never** be
granted; `await_outcome` and `approve` both check expiry before acting.

### Atomic transitions + persisted durable expiry

- Every status transition is an atomic **compare-and-set** (`UPDATE ... WHERE status
  still pending` + a `rowcount` check) inside the one outbox transaction — exactly
  one terminal transition ever wins under concurrent approvers (read-committed safe,
  Postgres and SQLite alike; a racing approve can never overwrite a deny). The loser
  raises `ApprovalAlreadyDecidedError` with **no** duplicate outbox event or evidence
  row.
- The DBOS `await_approval_workflow` persists terminal `expired` via
  `expire_approval_step` (CAS + outbox `ApprovalEvent(expired)` + evidence row, one
  transaction) **before** returning — the row is never left `pending` while the
  workflow reports `expired`, so an expired request is never grantable and drops out
  of the pending queue.
- **Actual-terminal-state reporting (NE-070H).** The store CAS already totally
  orders the terminal transition; the gate now reports the **actual** landed state
  when its expiry CAS loses. If `await_outcome` (or `approve`) drives an expiry that
  loses to a concurrent approve/reject, it re-reads the row and returns/raises the
  real terminal status — a just-approved call is **never** misreported as `EXPIRED`.
  Proven on live Postgres: barrier-raced approve-vs-expiry converge to exactly one
  terminal state, and every approve-win is reported `APPROVED`
  (`tests/test_approvals_concurrency_postgres.py`).

### Event / audit catalog

- **AuditEvents** (config_hash-stamped, frozen `AuditSink`, fail-closed):
  `approval.requested` · `approval.first-check` · `approval.granted` ·
  `approval.rejected` · `approval.escalated` · `approval.expired` ·
  `approval.promotion.evaluated`. The middleware additionally emits
  `tool.approval[success|denied]` and `tool.execute`.
- **ApprovalEvents** (ne-events outbox, atomic with the row write):
  `requested` · `approved` · `rejected` · `escalated` · `expired`.
- **Decision-record linkage (ADR-0017).** Every decision `AuditEvent`'s
  provenance carries the originating request's `approval_id` (mirroring
  `approval.requested`'s existing key), and `approve()`/`reject()` return
  `decision_event_id` on the `ApprovalOutcome` — the `event_id` of the
  decision record actually appended to the chain.
- **Execution→approval join (ADR-0019, B14).** Every `ApprovalOutcome` the
  gate builds carries `approval_id` (the durable request id — populated on
  rehydration too, since it is row state). The C6 executor stamps that id as
  `provenance["approval_id"]` on the `tool.execute` record (success AND
  error) and on both `tool.approval` branches, at the execution site — so an
  executed write names the approval that authorised it, and a refused call
  names the approval that refused it. No approval (tier 0/1, or a gate with
  no durable id) ⇒ the key is omitted, never inferred. Adversarial guard:
  `tests/test_approval_execution_join.py` (two-leg fixture; kills a stamped
  constant and last-id-wins).

**`PendingApproval` (the console-queue read-model, NE-T2X-5):** gains additive,
defaulted `provisional_source_count: int = 0` /
`provisional_source_refs: tuple[str, ...] = ()`. `DurableApprovalGate.list_pending`
parses each row's stored `payload` JSON tolerantly (missing/malformed keys ->
the defaults, refs filtered to `str`, count coerced to a non-negative `int`) —
this is a **display-only** channel: the tier already escalated at PDP-decide
time, so nothing about the GATE's behavior depends on this parse succeeding.

### Metrics

`ApprovalMetrics` records the histogram **`ne.approval.latency`** (unit `s`) with
attributes `ne.approval.tier`, `ne.approval.decision`, and `ne.config_hash` — the
ne-obs pattern (OTel metrics **API** only; the GenAI semconv stays exclusive to
ne-obs, C3). The meter is injectable for in-memory assertions.

### Promotion-on-evidence (the policy boundary)

`PromotionTracker` records the 'assist → execute → own' evidence trail: one
`ne_approval_evidence` row per terminal decision, and `evaluate_and_record` emits a
**logged** `approval.promotion.evaluated` AuditEvent (rate ≥ 0.95, zero violations,
≥ `promotion_min_instances` over the window per §4.2). It is provably incapable of
changing a tier or policy — it has no write path to any policy state. **Promoting an
action stays a human policy-bundle change.** The counter-model second check (a
distinct second-approver adapter beyond the named list) is named here as a future
adapter, not built.

### Composition root

```python
from ne.runtime import create_tool_executor, create_approval_gate

gate = create_approval_gate(sink=audit_sink, settings=settings)  # persisted store + outbox
executor = create_tool_executor(bindings, sink=audit_sink, approval_gate=gate)
```

### Live-park: real DBOS-Postgres durable approval

CI covers passthrough + fake-DBOS decoration of the `approvals_durable` steps
(`tests/test_approvals_durable.py`). The real durable run is parked
(`tests/test_approvals_dbos_live.py`):

```
ne dev up
NE_LIVE_DBOS=1 NE_RUNTIME_DURABILITY=dbos \
    uv run pytest packages/ne-runtime/tests/test_approvals_dbos_live.py -q
```

## Durability (`@ne_workflow` / `@ne_step`)

Thin wrappers over DBOS Transact, read from `NE_RUNTIME_DURABILITY` at decoration
time:

- `off` (default) — transparent passthroughs preserving name / signature /
  sync-vs-async; no DBOS instance is constructed.
- `dbos` — the real `DBOS.workflow(name=...)` / `DBOS.step(name=...)` are applied.

Honesty note: the v1 agent loop is **not itself** DBOS-decorated (composing DBOS
around a live pydantic-ai run needs serializable-args turn functions and a
launched instance). A composition root wraps ITS turn functions:

```python
from ne.runtime import ne_workflow, ne_step, launch_durable_runtime

@ne_step
def charge(amount_cents: int) -> str: ...

@ne_workflow(name="daily-turn")
def run_turn(prompt: str, principal_id: str) -> str: ...

# at process start, in dbos mode:
launch_durable_runtime(name="ne-runtime", database_url="postgresql://.../ne")
```

### Live-park: real DBOS-Postgres

CI covers passthrough + fake-DBOS decorator application only. To exercise real
durability (`tests/test_dbos_live.py`):

```
ne dev up            # brings up Postgres
NE_LIVE_DBOS=1 NE_RUNTIME_DURABILITY=dbos \
    uv run pytest packages/ne-runtime/tests/test_dbos_live.py -q
```

### Live-park: real-frontier agent run

The stub `ModelGateway` is the CI boundary. A real end-to-end run through a live
`ne-gateway` (provider creds) is parked with no test file — run a
`DepartmentAgentSession` against a real gateway manually. The prompted
tool-calling protocol is unproven on real frontier models until that clears.

## Sessions

`DepartmentAgentSession` implements the frozen `ne.contracts.runtime.AgentSession`
Protocol and persists state per `(agent_name, department)` over
`SqlAlchemySessionStore` (portable SQLAlchemy-Core — SQLite in CI, Postgres in
prod, parity by construction). A principal whose `claims['department']` does not
match the session is rejected with `SessionDepartmentMismatchError` **before** any
model or tool activity — no cross-department state bleed.

### Bounded history window (P1-3)

A per-department session is shared by every user in that department, and each turn
loads its history. Without a bound that history grows linearly forever, and once it
exceeds the model context window **every** subsequent turn raises
`context_length_exceeded` **permanently** for that whole department (there is no
delete/reset API — it takes manual DB surgery). Two things close this:

- **`load_history(session_id, *, limit=N)`** applies the window in **SQL** —
  `ORDER BY seq DESC LIMIT N`, then re-reversed to chronological — so a turn can never
  load unbounded rows (it is the SQL LIMIT, not a post-hoc slice of a full read). With
  `limit=None` it keeps the full ascending read (back-compat). `DepartmentAgentSession.run`
  always passes `limit=max(1, settings.session_history_max_turns)` (default **40**
  `append_turn` batches), so the model receives at most the last-N batches, oldest-first.
- **`reset_history(session_id)`** is the governed ops escape hatch that replaces manual
  DB surgery: it clears the session's message rows **and** its `ne_agent_session_seq`
  counter in one transaction, so a subsequent `append_turn` re-seeds at seq 1. The
  `ne_agent_sessions` row (and the session id) survive, so `open_session` still returns
  the same id (console/API exposure is a follow-up).

All session I/O routes through `ne.audit.engine.call_db`, so on non-sqlite dialects the
blocking SQLAlchemy body runs via `asyncio.to_thread` and never pins the event loop
(P2-16); SQLite stays on-loop by design (thread-bound pysqlite connections).

### Multi-worker atomicity (NE-070H)

The pilot tier is the first deployment that runs **multiple workers against one
Postgres**, so the session write paths are atomic by construction:

- **`open_session`** is a speculative `INSERT ... ON CONFLICT DO NOTHING` on the
  `(agent_name, department)` unique constraint followed by a read of the winner. Two
  workers opening the same session both return the **same** id with exactly one row
  (on Postgres the speculative insert blocks on an in-flight sibling until it commits,
  then DO NOTHING + the read-committed SELECT sees the winner).
- **`append_turn`** claims its `seq` from a per-session counter table
  (`ne_agent_session_seq`) via an atomic `UPDATE ... SET last_seq = last_seq + 1`
  under the row lock, so N concurrent appenders to one session produce a **gapless,
  contiguous, unique** seq. The counter is seeded **once** from the legacy `max(seq)`
  (upgrade-safe; a fresh session starts at 1); a `uq_agent_session_messages_seq`
  unique index backstops the invariant. A legacy DB carrying duplicate `(session_id,
  seq)` rows fails **loudly** at `ensure_runtime_schema` — a fail-closed surfacing of
  pre-NE-070H corruption. Proven on live Postgres with 8 workers × 25 appends →
  `{1..200}` (`tests/test_store_concurrency_postgres.py`).

### Verified-answer pre-step (NE-VAF-U3)

`DepartmentAgentSession` takes an optional, keyword-only `router:
VerifiedRouter | None = None`. When set, `VerifiedRouter.route(...)` runs
**after** `load_history` and **before** the model/tools are built for the
turn — on a ROUTE/CLARIFY/REFUSE outcome the model is **never instantiated**.
Default `None` keeps every pre-1.6.0 caller byte-identical (a composition
root, U4, turns it on).

- **Injected-Protocol composition (C3).** `ne.runtime` imports **nothing**
  from `ne.brain_ontology`: the U1 deterministic matcher, the routing-row
  table, and the answer renderer are handed to `VerifiedRouter` as plain
  callables + structural `Protocol`s (`RoutingRowLike`, `RouteDecisionLike`).
  ROUTE executes **only** via the injected `ToolExecutorLike.execute(...)` —
  the identical governed chokepoint `/v1/verified/invoke` uses (guardrails →
  PDP → HITL tier gate → handler → C7 scoped read → audited
  `knowledge.verified_read`) — never a bypass. Enforced three ways: no import
  in `verified_router.py`, the `.importlinter` `runtime-verified-lane`
  contract, and an AST guardian test.
- **Marker namespace.** All CLARIFY-counter state is derived from `history` —
  there is no new DB column. `ModelResponse.model_name` markers
  (`ne-verified-router:route:<domain>` / `:clarify:<domain>` /
  `:refuse:<domain>`) are unforgeable: only this module ever stamps them, the
  gateway stamps its own `ne-gateway:*` marker, and a user prompt containing
  the literal marker text lands as inert `UserPromptPart` content (it never
  increments the clarify counter).
- **Fail-closed exception policy.** Every exception on the ROUTE execution
  path (PDP deny, a wrapped scope/principal failure, an approval denial, a
  parse/render failure) converts to the Layer-2 **audited** refusal — never
  free text, never a raised error frame. The sole exceptions that propagate
  are `RuntimeAuditError` / `AuditEmissionError` (an un-auditable call must
  kill the run) — mirroring `ne.runtime.middleware` /
  `ne.runtime.runner._BLOCKING_ERRORS`'s own posture. A `GuardrailViolation`
  raised **by** the executor's own engine is rendered directly (no
  double-audit); the router's own `guardrail_engine.enforce(...)` call is used
  only for a REFUSE the router itself decides (bound-exhausted CLARIFY, PDP
  deny, wrapped failures, tier-gated HITL defense) and normally emits exactly
  **one** `guardrail.block`. If that call's own guardrail instead raises
  internally, `GuardrailEvaluationError` is raised after exactly one
  `guardrail.error` is audited — already-audited, so it is suppressed the
  same way `GuardrailViolation` is, and the Layer-2 refusal still renders
  rather than escaping as an exception.
- **HITL non-blocking defense.** Before awaiting `executor.execute`, a
  static tier check (`max(spec.tier, tier_for_action(spec.name)) >= 2`)
  refuses up front rather than hanging a chat turn on a human approval gate;
  a PDP-time escalation is still covered by the fail-closed exception arm
  (defense-in-depth). `accounts-directory` is `TIER_0`, so the happy path
  never reaches this branch.
- **Every short-circuit persists.** ROUTE/CLARIFY/REFUSE all call
  `store.append_turn` with the synthetic `ModelRequest(UserPromptPart)` +
  `ModelResponse(TextPart, model_name=<marker>)` exchange — even on refusal —
  so the next model turn's history always contains it and can never
  contradict a verified answer it never saw.

See `packages/ne-runtime/tests/test_verified_router.py` for the executable
contract.

### Grounded-agent post-step (NE-GA-G3)

`DepartmentAgentSession` also takes an optional, keyword-only `grounding:
GroundingPolicy | None = None` — the symmetric **mirror** of the
verified-answer pre-step above, running **after** the model produces its
draft instead of before the model is even built. Default `None` keeps the
model path byte-identical (the load-bearing parity invariant); a composition
root (G5) turns it on.

- **The trio.** `ne.runtime.grounding` ships `GroundingRecorder` (a per-run
  wrapper that captures this turn's `VerifiedAnswer`-shaped tool returns —
  the FULL shape, deliberately aligned with the G1 corpus gate, so a captured
  fetch always also grounds the corpus; `BLOCKED: ...` strings and any other
  non-shaped return are ignored),
  `GroundingPolicy` (the granted verified-tool names, plus INJECTED
  `render_fallback` / `guardrail_engine` / `sink` / `pdp`), and
  `run_grounding_gate` (the post-step itself: builds a `GroundingCorpus` from
  the recorder's fetches, runs the real `ne.govern.grounding.check_grounding`
  gate, and either passes the draft through unchanged or fails closed).
- **Per-run recorder freshness.** A `GroundingRecorder` is constructed
  **fresh inside `run()` for every call** — never once in the factory — so a
  prior turn's fetched answers can **never** ground the current turn even
  when a session object is reused across turns.
- **Fail-closed.** An ungrounded draft is **withheld entirely**: it never
  reaches `output` and never enters session history. Exactly **one** audited
  `guardrail.block` fires via the injected, sink-bound engine (genuine
  `grounded_gate=True`/`grounded=False` booleans, reason **classes** + a
  finding **count** only — never the ungrounded text); the draft is then
  replaced by `policy.render_fallback(fetched)`, a deterministic table of the
  verified data. `GuardrailViolation`/`GuardrailEvaluationError` from that
  `enforce` call are suppressed (already audited); `AuditEmissionError`
  propagates and kills the run (mirrors the verified-router's own H6
  posture). An empty corpus (zero fetches this turn) means every content
  token of the draft is ungrounded, so a turn that never fetched anything
  always falls back.
- **The draft never persists.** On FAIL, `append_turn` persists exactly
  `[ModelRequest(UserPromptPart(prompt)), ModelResponse(TextPart(fallback),
  model_name="ne-grounding-gate:fallback")]` — no draft text, no failed
  turn's tool-call/return parts — so no fragment of the ungrounded exchange
  can ever launder into a later turn's history.
- **C3 injection.** `ne.runtime.grounding` imports **nothing** from
  `ne.brain_ontology`: `render_fallback` and the guardrail engine are
  injected via `GroundingPolicy` (G5 builds them from
  `ne.brain_ontology.verified.answers.render_answer_text` and G2's
  `GroundingGuardrail`); the G1 gate functions
  (`GroundingCorpus.from_answers` / `check_grounding` / `GATE_VERSION`) are
  imported directly from `ne.govern.grounding` — an allowed lane. Enforced by
  the existing `.importlinter` `runtime-verified-lane` contract (already
  covers all of `ne.runtime`) plus an AST guardian test.
- **Tool restriction rides the existing scoped executor.** `create_department_session`
  wraps the executor in `ToolsetScopedExecutor(executor, granted=grounding.granted_tools,
  ...)` when `grounding` is set — the SAME handler-less, audited wrapper NE-034
  ships for cron/sub-agent toolset gating (C6: zero new bypass).
- **Bounded model-assist (NE-GA-G4, design §4).** `GroundingPolicy` also
  carries an INJECTED, optional `assist: Callable[..., Awaitable[str]] |
  None = None` — a plain gateway completion wrapper (no tool catalog),
  task-tagged `grounding-assist`, wired by the composition root (G5).
  `None` (the default) disables it entirely: `run_grounding_gate` is
  byte-identical to G3. When injected, it runs **at most once per turn**,
  on either branch, and its output is **re-checked by the same
  `check_grounding` gate against the same corpus+prompt** before it may
  replace anything — there is no other channel to `output` (the single
  emit point; an AST + behavioral guardian test enforce it mechanically).
  On PASS it may only re-word the already-passing draft (re-check fails ->
  the original draft is kept); on FAIL it sees the rendered tables and the
  failure's reason **classes** only — **never** the withheld draft text
  (structural C12) — and a second FAIL still falls to the deterministic
  table. A raising or degrading assist can never make the outcome worse
  than the pure hard-gate baseline.
- **Tiered strictness + gate profile (NE-HGATE-2, design
  `docs/architecture/anti-hallucination-grounding.md` §7).** `GroundingPolicy` carries two
  more frozen, deployment-config fields — `strictness: StrictnessTier =
  STRICT` and `profile: GateProfile = PROFILE_NG1_FROZEN` (both from
  `ne.govern.grounding_policy`) — **never** caller/request-selectable. Their
  defaults reproduce today's gate **byte-for-byte** (a default-constructed
  policy makes the same withhold decisions, stamps the same `gate` string, and
  dumps the same provenance). `run_grounding_gate` decides withhold via
  `apply_strictness(verdict, policy.strictness)` — **not** `verdict.passed`
  (E-P1-1) — so at `standard` an ungrounded non-factual vocabulary token in a
  no-fact sentence is *advisory* (audited, never withholds) while every fact
  class still withholds at every tier; the G4 assist re-check applies the
  **same** tier + profile symmetrically. Provenance stamps
  `gate = policy.profile.version` and `strictness = policy.strictness.value`;
  the FAIL audit payload gains `strictness` plus an `enforced_reasons` /
  `advisory_reasons` split (legacy `reasons` / `finding_count` unchanged). On a
  **non-withholding pass-through** that still carried advisory findings — where
  no `guardrail.block` is emitted — those counts are instead recorded by one
  counts-only `grounding.advisory` event (`{strictness, advisory_reasons,
  advisory_count}`, no text) so advisory is *recorded, never dropped* (design
  §2.5); it fires only when advisory findings exist, so a default `strict`
  policy never emits it. Each assist invocation emits **one** counts-only
  audited `grounding.assist` event via `policy.sink` — `{phase, adopted,
  error_class}`, no draft/candidate text (C12), fail-closed like
  `toolset._emit`.

See `docs/architecture/anti-hallucination-grounding.md` for the
design and `packages/ne-runtime/tests/test_grounding_runtime.py` for the
executable contract.

## Composition example

```python
from ne.runtime import (
    create_tool_executor, create_approval_gate, create_department_session, ToolBinding,
)

gate = create_approval_gate(sink=audit_sink, settings=settings)  # NE-035 durable HITL gate
executor = create_tool_executor(
    bindings,          # ToolBinding[]: ToolSpec + handler + guardrail extractors
    sink=audit_sink,   # REQUIRED — C6 audit coupling
    approval_gate=gate, # REQUIRED — HITL (production DurableApprovalGate)
)
session = await create_department_session(
    "assistant", "eng", store=store, gateway=gateway, executor=executor,
)
result = await session.run("draft the Q3 note", principal=principal)
```

`ApprovalGate` is a **required** constructor argument; ne-runtime ships **no**
auto-approving implementation anywhere. The production gate is the
`DurableApprovalGate` (see **HITL approval gates (NE-035)** above).

### Shared guardrail state + injected rails (NE-CKD1)

A composition root that runs **more than one** executor (the session executor +
the sub-agent dispatcher + the cron scheduler) MUST share **one** spend ledger +
**one** rate counter across all three, and ride ne-identity's
`AgentScopeGuardrail` (the sole `CredentialScope.tools` enforcer, NE-036) on **all
three** paths via `extra_guardrails`. Otherwise (boundary-audit findings #1/#7/#8)
each executor tallies caps in **private** state — dispatch + cron multiply the
effective spend/rate cap ~N× — and the least-privilege scope check is wired into
**zero** roots.

```python
from ne.runtime.factory import create_shared_guardrail_state
from ne.identity.agents.guardrail import AgentScopeGuardrail  # composition root/test
#   MAY import ne-identity; ne-runtime SRC MUST NOT (the seam is Protocol-typed).

ledger, counter = create_shared_guardrail_state()
scope_rail = AgentScopeGuardrail(credential_service)

executor = create_tool_executor(
    bindings, sink=sink, approval_gate=gate,
    ledger=ledger, counter=counter, extra_guardrails=(scope_rail,),
)
dispatcher, _ = create_subagent_dispatcher(
    ..., ledger=ledger, counter=counter, extra_guardrails=(scope_rail,),
)
scheduler = create_cron_scheduler(
    ..., ledger=ledger, counter=counter, extra_guardrails=(scope_rail,),
)
```

`extra_guardrails` is typed by the frozen `ne.contracts.governance.Guardrail`
Protocol precisely so injecting a cross-package rail never forces ne-runtime to
import its package (the C3 import ban stays intact — only the composition
root/test imports both). Executable proof:
`packages/ne-runtime/tests/test_composition_guardrail_seams.py`.

**Durable, atomic, shared state (NE-070H).** Pass the pilot tier's platform
Postgres URL to make the caps/limits **durable + shared across all workers** and
survive restarts:

```python
ledger, counter = create_shared_guardrail_state(database_url=platform_postgres_url)
```

`None` → the in-memory pair (test/dev). A URL → a `SqlAlchemySpendLedger` +
`SqlAlchemySlidingWindowCounter` over one engine (the ne-audit transactional-SQLite
recipe is applied here, in ne-runtime, so ne-govern's `src` never imports
`ne.audit`). The spend cap is now enforced by an **atomic conditional charge** and
the rate limit by an **atomic conditional record** — under K concurrent
multi-worker chargers the cap C can never be overshot (the loser is audited +
denied). Proven on live Postgres — 16 workers charging \$1 against a \$10 cap admit
**exactly 10** (`packages/ne-govern/tests/test_guardrail_concurrency_postgres.py`);
restart-durability is proven in `test_guardrail_state_durable.py`.

## Sub-agent dispatch (NE-034)

`GovernedSubAgentDispatcher` implements the frozen
`ne.contracts.runtime.SubAgentDispatcher` protocol. Each dispatched sub-agent runs
as an **isolated** governed agent (`run_isolated_agent`) over a
`ToolsetScopedExecutor` gated to its `AgentDefinition.toolset` — so **every**
sub-agent tool call still routes through the one `GovernedToolExecutor` (C6). An
out-of-set call is audited (`tool.toolset_denied`) + PDP-consulted + terminally
refused; the handler is never reached.

**Server-side caps** (a lying spec can only *narrow*, never *widen*):

| Cap | Source of truth | Spec narrowing | Rejection |
| --- | --- | --- | --- |
| depth | `DispatchSettings.max_depth` (dispatcher `_budget`) | `child_for(spec)` min-composes `spec.max_depth-1` | `subagent.cap_rejected` `reason='depth'` `scope='server'` |
| global concurrency | `ProcessRegistry` live count vs `max_concurrent_subagents` | — (registry-counted, never spec-trusted) | `reason='concurrency'` `scope='server'` |
| local fan-out | dispatcher `_active_local` | `max(1, spec.max_concurrency)` | `reason='concurrency'` `scope='spec'` |

**Memory isolation** is by construction: a sub-agent runs with an **empty message
history** and no reference to any `SqlAlchemySessionStore`, so it can neither read
nor write parent-session state. The **only** sanctioned parent→child channel is
`SubAgentSpec.context`, rendered as a JSON block into the child prompt. (A future
`MemoryProvider` wiring is NE-037's seam — untouched here.)

```
model emits out-of-set tool ─► on_unknown_tool hook ─► scoped.execute
   └─► tool.toolset_denied (audit, fail-closed) ─► pdp.decide (record) ─► ToolsetViolationError
       (handler UNREACHABLE — the scoped wrapper holds zero handlers)
```

`child_for(spec)` is the **only** dispatcher a nested integration may hand to a
sub-agent (it steps depth down and min-composes the budget). v1 ships no
in-product dispatch-as-tool, so this recursion seam is honor-system; global
concurrency stays registry-enforced regardless. The future dispatch-as-tool unit
**must** use `child_for()`.

### Per-child model routing (NE-161)

A dispatched sub-agent can route to a **different** gateway task/lane than its
parent. The binding is keyed **only** on the TRUSTED, server-registered
`AgentDefinition` — never on the model-emitted, untrusted `SubAgentSpec`:

| `AgentDefinition` field | Meaning | `None` behavior |
| --- | --- | --- |
| `route_task` | The gateway routing task (`CompletionRequest.task`) — a **different namespace** from `SubAgentSpec.task`, which is prompt work-text rendered into the child's instructions. | Inherit the parent `RuntimeSettings.task`. |
| `route_lane` | The gateway routing lane (`'frontier-api'` \| `'private-vllm'`), validated against `ne.contracts.types.Lane` **at registration** (boot-time fail-loud: a typo raises `ValidationError` when the `AgentDefinition` is constructed, not at first dispatch). | Inherit the parent `RuntimeSettings.lane`. |

**`SubAgentSpec.route_lane_hint` is narrow-only** (`ne-contracts` 1.5.0,
ADR-0008). It is a signal from the (untrusted) planner and may only pull a
child **toward** `Lane.PRIVATE_VLLM`; any attempt to widen toward
`Lane.FRONTIER_API` is deterministically clamped back to the
`AgentDefinition`-derived lane — the same MIN-compose discipline as
`max_depth`/`max_concurrency`. No code path reads a lane/task widen from the
spec: allowing one would be a straightforward frontier-exfil upgrade path for
a hijacked planner.

Every dispatch **always** emits a `subagent.route` `AuditEvent` (before the
child run starts, riding the same `_emit` path as `subagent.dispatch` — an
append failure raises `RuntimeAuditError` and withholds the child, C6
fail-closed). Its `detail` carries identifiers/enums/booleans only, never
prompt text:

```json
{
  "agent": "adjudicator",
  "route_task": "ap.adjudicate",
  "route_lane": "frontier-api",
  "tier": "frontier",
  "task_source": "definition",
  "lane_source": "definition",
  "hint_clamped": false,
  "pdp_effect": "allow",
  "policy_version": "1.2.0"
}
```

`tier` is `'frontier'` | `'private'` | `'unpinned'`; `task_source`/`lane_source`
are `'definition'` | `'settings'` | `'spec-hint'` (the last only for a
successful narrowing hint); `hint_clamped` is `true` exactly when a
`FRONTIER_API` widen attempt was rejected. The event's `trace_id` is stamped
from the ambient `run_trace_scope` (NE-166) — it always equals the sibling
`subagent.dispatch` event's `trace_id`, no extra correlation plumbing needed.

The child's `RuntimeSettings` is a **per-child copy** —
`settings.model_copy(update={'task': route_task, 'lane': route_lane})` — handed
to `run_isolated_agent`. The dispatcher's own `self._settings` (the shared
parent object) is **never** mutated or replaced.

**Fail-loud, never a silent default:** if a definition's `route_task` names a
task the gateway doesn't have configured, the real `LiteLLMGateway` raises
`UnknownTaskError` from `_candidates`; the dispatcher's existing
error-class-only handling turns that into a failed `SubAgentResult` (`output`
== `"ERROR: UnknownTaskError"`, `succeeded=False`) — it never silently falls
back to the default task.

**RUNTIME GATE (NE-162 dependency) — code-enforced, not just documented:**
`GovernedSubAgentDispatcher` takes an `allow_frontier_subagent_routing: bool =
False` constructor flag. When a dispatch resolves to the `'frontier'` tier
(via a `route_lane='frontier-api'` `AgentDefinition`) and this flag is
`False` — the default, inherited by every `child_for()` descendant — the
dispatcher audits a `subagent.route` **denial** (`detail.reason ==
"frontier_routing_disabled"`) and raises `FrontierRoutingDisabledError`
**before the child ever calls the model**. No shipped composition root passes
`allow_frontier_subagent_routing=True`, so a `route_lane='frontier-api'`
`AgentDefinition` is inert by construction today, not merely by convention.
Flip the flag on for a specific composition **only** once NE-162's pre-egress
redaction is wired in for that path.

**PDP route authority (NE-169):** the PDP is consulted right at the routing
decision — immediately after the child's lane is resolved and **before** any
child dispatch — via `PolicyRequest(action="subagent.route", context={
"route_task", "route_lane", "route_tier"})`. The decision's `effect` and
`policy_version` are threaded into the **same** `subagent.route` audit event
shown above (never a second event). Any effect other than `DecisionEffect.ALLOW`
— a `deny`, or a `require-approval` (routing has no human-in-the-loop leg) — is
audited as a `subagent.route` **denial** (`detail.reason ==
"route_policy_denied"`) and raises `SubAgentRoutePolicyDeniedError` **before**
the child ever calls the model. The `ne.govern` bundle (1.2.0) classifies
`subagent.route`: a private or unpinned resolved lane is tier-0 **allow**; a
frontier-resolved lane is a total **deny** — an explicit grant mechanism (a
principal attribute or a policy tier) is a deliberate, named follow-up
**parameter**, not invented in this unit. The PDP consult runs **before** the
RUNTIME GATE described above, which remains as independent defense-in-depth: a
PDP allow does not by itself open frontier egress.

## Cron-as-isolated-agent scheduling (NE-034)

`CronScheduler` implements the frozen `Scheduler` protocol. Each scheduled job
runs as its **own** isolated governed agent under a `ToolsetScopedExecutor` gated
to that job's **per-job granted toolset** (the OpenClaw cron-as-isolated-agent
pattern) — gating is enforced, never advisory. Cron expressions are evaluated in
**UTC** via `cronsim`; `CronSim` yields the next match strictly after the
reference time, so `run_due_jobs(now=...)` computes the next run from *now* and
**collapses any number of missed windows into a single run** (no catch-up storm).

The sync `schedule()` / `cancel()` write the job row **and** its `AuditEvent` to
the ne-events **outbox in one transaction** (the NE-035 precedent — this is how a
sync protocol audits without `asyncio.run`). The due-job claim is a
compare-and-set advance of `next_run_at` (rowcount-checked, single-writer-safe);
the finish writes the `ne_job_runs` row + `job.run` event in one transaction, and
an audit-append failure rolls the finish back (the run row stays honestly
unfinished — fail closed).

**Dual-append into the tamper-evident chain (Checkpoint-D finding #4).** Every cron
lifecycle event — `job.schedule` / `job.cancel` / `job.run` — lands in **both**
stores on that one transaction: the **ne-events outbox** (mutable export transport)
**and** the trigger-protected, hash-chained **`ne_audit_log`** (the authoritative,
`verify_chain()`-covered store) via `ne.audit.append_record`. The *same* event
object goes to both, so both carry the same `event_id` and composed `config_hash`
(alignment, not duplication). Writing only to the mutable outbox meant these
governance facts could be silently mutated/deleted while `verify_chain()` stayed
green; the chain append closes that gap. It is **fail-closed**: a failure on either
append raises `RuntimeAuditError` and rolls the whole cron mutation back — nothing
persisted without its chain record. `ensure_scheduler_schema` therefore also runs
`ensure_audit_schema` on the scheduler engine, and the composition root **must** hand
the **same** engine to the `CronScheduler` and its `PostgresAuditSink` so the chain
is one store (matches the `test_middleware_to_audit_sink_real.py` composition).

The live DBOS-Postgres tick is **live-parked to NE-070** — wrap `run_due_jobs`
with the existing `@ne_workflow` and drive it from `DBOS.scheduled`:

```python
@ne_workflow
async def _tick() -> None:
    await scheduler.run_due_jobs()   # DBOS.scheduled('* * * * *') at the composition root
```

Multi-worker double-fire safety of the CAS claim under concurrent Postgres workers
is likewise NE-070 (single-writer CI is authoritative for this unit).

## Process registry (NE-034)

`ProcessRegistry` tracks live sessions, sub-agents, and cron jobs with lineage
(`parent_id`, `depth`) and a bounded finished-history ring. It is the **atomic**
authority for the global sub-agent concurrency cap: `register(..., concurrency_cap=n)`
checks the live count and inserts with no `await` between check and insert, so a
recursive agent cannot exceed the cap by lying. A multi-process registry is NE-070.

## Run correlation (NE-166)

`ne.runtime.correlation` establishes an explicit, run-scoped `trace_id`
(`uuid4().hex`, a `ContextVar`-backed scope) at every agent RUN ROOT — a
`DepartmentAgentSession.run`, a `run_isolated_agent` call (cron / standalone),
or a `GovernedSubAgentDispatcher.dispatch` — and threads it as ordinary DATA
through the rest of the run, so every event the run produces shares ONE
non-null id even under the **default no-op OTel tracer** (which would
otherwise leave every event's `trace_id` `None`).

- `run_trace_scope()` mints a fresh id, or — if a run is already ambient
  (a sub-agent dispatched from inside a parent run) — INHERITS it unchanged,
  so the correlation spine follows the call tree rather than process/session
  boundaries.
- `GatewayModel.request` stamps `current_run_trace_id()` onto every
  `CompletionRequest.trace_id`, the sole cross-process data carrier into
  `ne-gateway`.
- `dispatch._emit` / `middleware._emit` stamp the same ambient id onto every
  `subagent.dispatch` / `subagent.complete` / `tool.execute` audit event.
- Outside any scope (or for a pre-NE-166 caller), every emitter behaves
  exactly as before: `trace_id=None`.
- Scheduler `job.run` events are deliberately left unstamped in v1 — a cron
  job's child run (via `run_isolated_agent`) still gets its own scoped id;
  only the scheduler's own bookkeeping event is out of scope for this unit.

## Run principal attribution (NE-COST-PRINCIPAL)

`ne.runtime.correlation` also establishes `run_principal_scope`, a distinct
`ContextVar`-backed scope carrying the calling principal's opaque identifier
from a governed run root down to the gateway, so `CostEvent.actor` (ne-gateway)
can attribute usage to the actual calling principal instead of the gateway's
own service identity.

- `DepartmentAgentSession.run` and `run_isolated_agent` (standalone, cron, and
  every sub-agent dispatched through it) each enter
  `run_principal_scope(principal.id)` with the VERIFIED `Principal` argument
  they already hold — never minted, never inherited by default (unlike
  `run_trace_scope`; every governed run root sets its own principal).
- `GatewayModel.request` stamps `current_run_principal_id()` onto every
  `CompletionRequest.principal_id`.
- Outside any scope — a boot-time warmup, a background sync, any caller that
  predates this unit — `current_run_principal_id()` is `None`, the resulting
  `CompletionRequest.principal_id` is `None`, and the gateway falls back to
  its own service actor: honest attribution for a call with no calling
  principal, never a stale or inherited one.
- This is a distinct seam from `current_tool_principal`
  (`ne.runtime.middleware`), whose narrow read surface (skill bindings only)
  is not widened by this addition.
- Background emitters outside the agent-run call tree — the brain enricher,
  brain-graph extraction, the eval judge, the memory-graph facade — enter no
  scope and stay service-attributed by construction; this is intentional, not
  a residual gap.

## Credits

The cron scheduling design (cron factored as schedule / store / run-log, each job
as an ephemeral isolated session, per-job toolset gating, and a sub-agent registry
with state / lineage / depth) follows the **OpenClaw** (MIT) pattern. OpenClaw is a
TypeScript codebase; **zero code was copied** — the adoption is pattern-only, so no
NOTICE entry is required.

## Agent memory v1 (NE-037)

An **append-only**, per-agent **episodic** store of agent experience
(`decision`, `outcome`, `steward-verdict`, `learned-fact`, plus the
`promotion-proposed` / `promotion-approved` bookkeeping events), each stamped
with a timestamp + provenance (agent id, session, source).

- **Frozen seam, no new ABC.** `ne.contracts.runtime.MemoryProvider` and
  `MemoryEvent` already ship in the frozen contracts, so `AgentMemory` implements
  the Protocol *exactly* (pyright-checked in
  `tests/test_memory_provider_contract.py`). Semantic + bi-temporal recall
  (NE-083) swaps in behind this **same** Protocol with no interface change.
- **Audit-grade append-only.** The store's entire public mutation surface is a
  single-row `INSERT` (`SqlAlchemyEpisodicStore.append`) — there is no
  update/delete/edit/purge/clear method anywhere. Promotion state changes are
  themselves *appended* events, never row mutations. Portable SQLAlchemy-Core
  types + UTC normalization give SQLite-CI/Postgres parity by construction.
- **Per-agent isolation** (NE-034/NE-036 posture). Every query is
  `agent_id`-scoped; `get()` on another agent's event raises
  `MemoryEventNotFoundError` so existence never leaks. The only cross-agent
  visibility is the explicit append-time `shared_with` allowlist, read back via
  `recall_shared(owner_agent_id=..., requesting_agent_id=...)`.
- **Time-scoped recall.** `recall(agent_id, kinds=..., since=..., until=...,
  limit=...)` with an **inclusive** window, newest-first.

### Curation-gated promotion into the brain

Durable learnings reach the brain **only** through a two-stage, curation-gated
flow — never direct-to-index, and `ne-brain` is **not imported**:

```
agent propose(event_ids)        # learned-fact only; CANNOT reach the writer
      │  → audit: memory.promotion.proposed  (config_hash-stamped)  ── AUDIT FIRST
      │  → append-only promotion-proposed row (LAST)
      ▼
curator promote(proposal_id,    # approved_by MUST be a distinct named principal
        approved_by=...)        #   (never the proposing agent)
      │  → KnowledgeWrite.upsert([...])   # the ONE brain call — frozen NE-023 seam
      │        provenance = "learned-by-agent", item_id = "learned-<event_id>"
      │        → brain-side shape-validation / accept-or-quarantine / provenance
      │  → audit: memory.promotion.promoted (config_hash-stamped)
      │  → append-only promotion-approved row (LAST)
      ▼
   UpsertReceipt (accepted + quarantined surfaced, not hidden)
```

Ordering is **audit-integrity fail-closed** (Checkpoint-D boundary-audit #6): a
promoted/retrievable fact can **never** exist without its
`memory.promotion.promoted` audit record. `promote()` runs durable `upsert` →
`memory.promotion.promoted` audit → `promotion-approved` row **last**; if the
audit append **fails**, the accepted chunks are **UNWOUND** through the governed
`KnowledgeWrite.quarantine` seam (park-before-delete, so payloads are never lost
and the unwind is itself audited) and `MemoryPromotionAuditError` is raised —
leaving the proposal cleanly **retryable** (no approval row, guard not armed).
`propose()` is **audit-first** because the episodic store is append-only (no
unwind possible): an orphan `proposed` audit event is fail-safe over-audit, while
an unaudited row is the finding.

**Composition root** injects NE-023's `KnowledgeWriter` as the frozen
`KnowledgeWrite` and freezes the config hash at process start. The provenance
STRING sets the stamp source; the queryable provenance CLASS comes from the
writer's default — construct the promotion writer with
`ProvenanceClass.LEARNED_BY_AGENT`:

```python
knowledge_writer = KnowledgeWriter(
    store, quarantine, contracts, default_shape=..., actor=...,
    default_provenance_class=ProvenanceClass.LEARNED_BY_AGENT,  # REQUIRED — class-queryable promotions (NE-037 P2)
    audit_sink=audit_sink,          # REQUIRED — no governed write without audit (Checkpoint-D #5)
    violation_sink=violation_sink,  # REQUIRED — shape-violation transport (Checkpoint-D #5)
)
memory = create_agent_memory("agent-a", settings=settings, session_id=session_id)
promoter = create_memory_promoter(
    writer=knowledge_writer,  # REQUIRED — the NE-023 governed write path
    sink=audit_sink,          # REQUIRED — config_hash-stamped audit on each stage
    settings=settings,
)
proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[eid])
receipt = await promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
```

## Notes

- **config_hash** is frozen at a composition root via
  `freeze_platform_config(settings=..., govern_settings=..., extra_modules=...)`
  ONCE at boot (ADR-0003, NE-CKC12) — the COMPOSED platform hash covering every
  module's config (ne-runtime + ne-govern + caller-passed modules), not the NE-033
  single-module subset. Per-test fixtures still freeze the module subset via
  `freeze_config_hash(RuntimeSettings())` — same frozen-slot mechanics (one frozen
  hash per process).
- **C1 (no phone-home):** pydantic-ai-slim installs with no provider extras and
  no logfire; DBOS Conductor is not configured.
- **Tests** use `asyncio.run` (pytest-asyncio is forbidden in this repo) and sync
  SQLAlchemy-Core engines with async Protocol methods (the ne-audit pattern).

## Governed write-back actions (NE-050)

The write path is **first-class**. A typed, tier-declared write action is an ordinary
governed tool: it becomes a `ToolBinding` inside the ONE `GovernedToolExecutor`, so a
source-system write can **only** happen after guardrails → PDP → typed-input pre-gate
validation → stricter-of tier gating → the HITL approval gate → a `config_hash`-stamped
audit event. The `GovernedAction` facade holds no `perform` callable and no connector
client — its `execute` can only delegate to the executor. The crux invariant (C7-write)
is structural, proven by `tests/test_action_write_guardian.py`.

**Typed input is validated BEFORE the human approval gate
(NE-ACTION-VALIDATE-BEFORE-GATE).** `build_action_binding` sets the binding's
`validate_args` — a preflight the executor runs immediately after guardrails+PDP and
**before** `DurableApprovalGate.submit`. A call whose typed input can never execute
(a missing required field, an undeclared key, a missing idempotency identity) is
refused right there: audited as `tool.validate[denied]`, and it **never creates an
approval row and never consumes a human decision** — a decision spent on a call that
cannot run is not a decision. The governed handler re-validates the identical way
(defense in depth, in case it is ever reached with bad input regardless); both sites
call the SAME `_validate_input` / `_dedup_key_for` helpers — one implementation, not
two. Proven by `tests/test_action_validate_before_gate.py`.

**The tier ladder (2 / 3 / 2).** Every write declares tier >= 2 (the framework floor,
registration-rejected below); the declared tier may only *escalate* the Rego class
tier, never relax it:

- `crm_note_definition` → `write.internal.crm.note`, **Tier-2** (a single named approval);
- `ticket_update_definition` → `write.external.ticket.update`, **Tier-3** (customer-visible:
  named approver **plus** a distinct second check via the real `DurableApprovalGate`);
- `email_draft_definition` → `draft.email.create`, **Tier-2** — a DRAFT is never sent, so
  it proves declared-tier escalation over a Rego Tier-0 name (a SEND would be
  `write.external.*` and Tier-3).

**Idempotency (two-class identity, NE-ACTION-DETERMINISTIC-REQID).** `request_id` is
**optional**. When a caller explicitly supplies it (a programmatic caller — the
model-facing reference schemas no longer advertise the field at all), the dedup key
is `sha256(action:request_id)` — byte-identical to the original contract, and
**scope-independent** (Stripe semantics: an explicit id survives an ambient scope
change, e.g. an app-level retry issued from a different turn). When it is absent (the
agentic path), the key is instead **derived**:
`sha256(action:derived:scope:payload_hash)`, where `scope` is the ambient
`ne.runtime.correlation.action_scope` (the DBOS-stable `turn_id`) and `payload_hash`
is the canonical input hash — so a re-prompted model dedups a re-issued write
regardless of what id (if any) it would have minted. **No `request_id` and no ambient
scope fails LOUD** with `ActionValidationError` before any claim (a governed write
always needs a stable idempotency identity). Either key feeds the same store: the
governed handler **atomically claims** it (a single unique-PK INSERT) *before*
touching the source system, so only the caller that wins the primary-key race
performs the write. A concurrent same-key call loses the INSERT and is turned away
**before** the connector is reached — two simultaneous retries can no longer both
write (no read-then-write TOCTOU). A settled retry with the same key + payload
returns `detail="deduplicated"` (the connector is never re-called). The same key
reused with a *different* payload — or one whose first write is still in flight — is
a typed `ActionIdempotencyError` (fail closed, no second write). Dedup is
**post-governance** and the claim boundary is **non-transactional across the HTTP
seam**: a crash between the source write and `finalize` leaves a PENDING claim, so a
later retry is turned away as in-flight and the write is **not repeated** (crash-safe
on this side; the un-finalized row is a janitor-cleanup concern) — documented
honestly rather than papered over. A Tier-3 retry still re-enters the approval gate.
**Honest residual:** payload DRIFT on re-prompt (a re-run model rephrasing the write)
changes the derived key, so a drifted retry is **not** deduplicated and can reach a
second approval + write — full closure needs per-tool-call step decomposition
(`ne103c` §10-Q3, out of scope here).

**Claim/release race (P2-12).** `claim` **never** reports ownership without a persisted
PENDING row. If a losing INSERT's post-`IntegrityError` re-read finds **no** row (the
winner raced a concurrent `release()`), returning `None` would be misread as "we own the
claim" though nothing is persisted — letting a third caller `perform` concurrently (two
source writes for one `request_id`). Instead `claim` **retries the INSERT** (bounded by
`_CLAIM_MAX_ATTEMPTS`, then raises `ActionIdempotencyError` on pathological contention).
`finalize` asserts it matched **exactly one** row and raises on `rowcount != 1` — so a
vanished claim can never be silently recorded as settled and later re-performed. This is
the NE-050 exactly-once guarantee under the two-connection interleave test.

**Composition.**

```python
gate = create_approval_gate(sink=sink, settings=settings)
ensure_action_dedup_schema(engine)
dedup = ActionDedupStore(engine)
bindings = (
    *root_read_bindings,
    *build_action_bindings(
        [crm_note_definition(sf_write), ticket_update_definition(sql_write),
         email_draft_definition(graph_write)],
        dedup=dedup,
    ),
)
executor = create_tool_executor(
    bindings,
    sink=sink,
    approval_gate=gate,
    govern_settings=GovernSettings(guardrails=GuardrailSettings(tools={
        "write.internal.crm.note": CRM_NOTE_ARG_POLICY,
        "write.external.ticket.update": TICKET_UPDATE_ARG_POLICY,
        "draft.email.create": EMAIL_DRAFT_ARG_POLICY,
        # ...root read-tool policies...
    })),
)
# Optional typed facades over the same executor:
actions = create_governed_actions(definitions, executor)
```

One executor per composition root: the bindings join the root's SINGLE
`GovernedToolExecutor` next to its read tools (C6 — one chokepoint, one guardrail
state, one agent-facing catalog). There is deliberately no factory returning a
dedicated action-only executor — `create_write_actions` was removed (NE-SCOPE-SEAM,
2026-07-22) because a standalone-executor shape cannot compose into the root's single
catalog, which is why its one would-be consumer hand-rolled this recipe instead.

Worked example: `packages/ne-mcp/src/ne/mcp/demo_server.py` (`build_demo_server`),
pinned by `packages/ne-mcp/tests/test_demo_server_composition.py`.

The `*_ARG_POLICY` constants **must** be merged into `GovernSettings.guardrails.tools`
alongside the root's read-tool policies — a tool absent from that map is rejected by
the deterministic guardrails (fail closed), so an un-merged action blocks as a toolargs
violation before any decision
(`packages/ne-govern/tests/test_tool_args.py::test_unknown_tool_denied`).

Because actions are ordinary governed tools, they are automatically servable through the
NE-049 governed MCP server with **zero extra wiring** — no separate write path exists.

## Context-budget trim guard (NE-CTXB-3)

`GatewayModel` enforces the Layer-1 context-budget invariant. It gains two
keyword-only constructor params:

- `budget: ContextBudget | None = None` — when `None` (the default), `request()` is
  **byte-identical to the legacy path**: no trim, no audit, no estimator. The
  production composition root (`ne.api.platform`) **always** passes it, derived
  **once** from the composed `GatewaySettings` for the routed runtime task's
  **primary** model (design §2.2 — not min-over-candidates; the gateway
  per-candidate pre-flight is the belt for fallback rolls). The estimator params
  (`bytes_per_token` / `per_call_overhead`) ride the same platform `model_copy`, so
  the runtime trim and the gateway pre-flight derive their arithmetic from **one**
  object and can never split-brain (the cross-package lockstep test pins it).
- `sink: AuditSink | None = None` — **required whenever `budget` is set** (a
  budget without a sink is a construction-time `ValueError`): a trimming model that
  cannot audit is structurally forbidden (C10).

**Trim algorithm (pairing-safe whole-turn, design §3.5).** Before `_map_history`,
`request()` partitions the pydantic-ai `messages` into a **protected head** (the
first request's `SystemPromptPart`s + the mapped `instructions` message), a
**protected suffix** (everything from the last `UserPromptPart`-bearing
`ModelRequest` onward — the current turn's prompt and **all** of this run's
tool-call/return/retry exchange), and a **droppable middle** of prior history
grouped into **whole turns**. Oldest turns are dropped first until the
lockstep-estimated assembled size (head + kept + suffix + the in-band tool catalog)
fits `budget.input_budget`. Whole-turn drops keep every `ToolCallPart`/
`ToolReturnPart` pair together — no dangling halves.

**Governed refusal.** If the head + suffix alone still overflow (the current turn's
tool returns are structurally un-trimmable), `request()` raises a typed
`ContextBudgetExceededError(stage="runtime-trim")` — a **hard, honest refusal**, no
fallback render in v1. It propagates as a session error and surfaces on the SSE
relay as a **named** `ContextBudgetExceededError` error frame (counts never leak to
the browser).

**Mandatory audit (C10/C12).** A **counts-only** `context.budget` event
(`{estimated_tokens, input_budget, context_window, dropped_turns, stage}` — never
message text) is emitted on **every** drop and **every** raise, on the session's
own hash-chained sink; a sink-append failure fails **closed** (`RuntimeAuditError`).

**`model_settings.max_tokens`.** Honored into the outbound params **and** the
effective-reserve arithmetic (widening the input budget); every other pydantic-ai
`ModelSettings` key is explicitly ignored, with a guardian test enumerating the
supported set so a new upstream key is a loud supported-vs-ignored decision.

Adding the `context_budget` / `bytes_per_token` / `per_call_overhead` fields to
`RuntimeSettings` moves the ne-runtime module-subset `config_hash` — expected (the
`DispatchSettings` precedent).
