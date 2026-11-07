# NE-ACTION-REQID — deterministic idempotency identity for governed write-back actions (design)

**Status:** DESIGN — buildable
**Date:** 2026-07-21
**Finding under review:** the NE-050 `ActionDedupStore` idempotency key is derived solely from a
model-supplied `request_id`, so a coarse-grained crash-and-retry (a DBOS step re-running the whole
pydantic-ai loop) re-prompts the model fresh and can produce a DIFFERENT `request_id` — letting a
real source-system write fire twice past the dedup layer.
**Provenance note (honest):** the tasking cites a spec row `NE-ACTION-DETERMINISTIC-REQID`.
**That row does not exist** — `grep -rn 'DETERMINISTIC.REQID'` across the repo returns
nothing. This design therefore re-verified the claim entirely from code, treating the
tasking's summary as an unconfirmed hypothesis, not an accepted finding.

---

## 1. Verdict

**CONFIRMED as a framework-level design gap, with two refinements the original finding missed:**

1. **The core claim is true and unmitigated in code.** `request_id` is genuinely model-facing end to
   end; no seam anywhere strips, injects, or derives it; the dedup key is
   `sha256(f"{action}:{request_id}")` and nothing else. The existing test suite *asserts the
   vulnerable behavior as intended* ("a different request_id performs a second real write" —
   `packages/ne-runtime/tests/test_action_tools.py:214-218`). No test simulates a step retry.
2. **Refinement (severity down): the exposure is latent, not live.** No shipped composition today
   puts NE-050 ActionTools and DBOS step-retry in the same process (§2.5). And every ActionTool write
   sits behind the hard Tier-2 HITL floor, so a crash-retry double-write is never *silent* — it
   requires a human to approve a second, fresh approval request (§2.6). The direction of failure is
   "human asked twice, may unknowingly approve a duplicate write," not "unattended double-fire."
3. **Refinement (severity up): a second bug in the same key.** Because NOTHING turn-scoped enters the
   dedup key, two *different* turns/sessions whose models happen to choose the same low-entropy id
   (models are strongly biased toward ids like `req-12345678`, which is literally the pattern the
   existing tests model) collide **across turns**: same payload → the second turn's legitimate write
   is silently swallowed as `deduplicated`; different payload → a spurious
   `ActionIdempotencyError`. The original finding (double-write) and this one (over-dedup /
   false-conflict) are the two faces of the same root cause: **the dedup key contains no
   framework-controlled stable identity.**
4. **Bonus finding:** `docs/architecture/ne103c-durable-worker-design.md:383-384` (§5.2 point 2)
   claims re-executed "NE-050 write actions are protected by the `ActionDedupStore` idempotency
   keys." **That claim is wrong** under exactly the re-run it describes — the key is model-chosen and
   a re-prompted model need not reproduce it. The same design *did* catch the analogous
   non-determinism for approvals (the "human may be asked twice" note, workflows.py:16-18) but missed
   that the write dedup has the identical hole one layer down. The build unit must correct that
   paragraph.

---

## 2. Re-verification — the full trace (all file:line cites verified 2026-07-21)

### 2.1 Where `request_id` enters: it is model-facing, with no injection seam

- **Declaration:** `ActionInput.request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")` —
  `packages/ne-runtime/src/ne/runtime/action_tools.py:93`. The docstring (action_tools.py:85-88) is
  explicit: "``request_id`` is the caller-supplied idempotency key — retries MUST reuse it."
- **Model-visible schema:** all three reference actions list `request_id` in the `ToolSpec.input_schema`
  JSON the model sees — `reference_actions/email_draft.py:87`, `crm_note.py:69`,
  `ticket_update.py:83` — and allowlist it in the guardrail arg policies
  (`email_draft.py:105`, `crm_note.py:85`, `ticket_update.py:99`).
- **Schema → model, verbatim:** `build_governed_tool`
  (`packages/ne-runtime/src/ne/runtime/runner.py:106-112`) hands `spec.input_schema` straight to
  `pydantic_ai.Tool.from_schema`; the tool body is `**kwargs`-only and forwards the model's kwargs
  untouched (runner.py:98-104). The session builds its tool list directly from
  `executor.tool_specs` (`agent.py:281`, grounding branch `agent.py:207-210`).
- **Executor passes args through untouched:** `GovernedToolExecutor.execute`
  (`middleware.py:173-254`) reads `args` for guardrails/recipients/cost extraction and hands the SAME
  mapping to the handler (`middleware.py:254` → `_run_handler(binding, args, principal)` →
  `binding.handler(args)` at middleware.py:306). No mutation, no injection, anywhere between the
  model's JSON and `ActionInput.model_validate` (action_tools.py:341).
- **Key derivation:** `dedup_key = hashlib.sha256(f"{spec.name}:{validated.request_id}".encode()).hexdigest()`
  — `action_tools.py:349`. The model-supplied field is the ONLY variable component.

### 2.2 Every construction site checked — no mitigating wrapper exists

Exhaustive sweep for `ActionDefinition` / `build_action_binding(s)` / `create_governed_actions` /
`create_write_actions` constructors and callers (src, non-test):

| Site | What it does with `request_id` |
|---|---|
| `reference_actions/{crm_note,email_draft,ticket_update}.py` | expose it in the model schema + arg policy; nothing else |
| `factory.py:233-280` (`create_write_actions`) | pure composition (dedup store + bindings + executor); no id handling |
| `ne-mcp/src/ne/mcp/demo_server.py:264-275` | wires `crm_note` + `ticket_update` bindings into `GovernedMcpServer`; no id handling |
| `action_tools.py:467-480` (`create_governed_actions`) | facades only; no id handling |

**No composition root injects, derives, overrides, or validates `request_id` beyond the regex.** The
only caller in the repo that supplies one deliberately is the scripted LangGraph demo agent, which
hard-codes `"lg-demo-note-1"` / `"lg-demo-ticket-1"`
(`packages/ne-mcp/langgraph_env/agent.py:123,155`) — a scripted caller, not a mitigation; a real
LLM behind that MCP surface mints its own ids. (The `request_id` on
`ne-api/src/ne/api/wire.py:460` is the *approval* request id — an unrelated field.)

### 2.3 Existing tests pin the vulnerable semantics; the retry scenario is untested

`packages/ne-runtime/tests/test_action_tools.py`:

- `test_idempotent_retry_dedups_and_skips_client` (:189-230) — same id + same payload dedups; same id
  + different payload raises; and **:214-218 asserts a different `request_id` performs a second real
  write** — the exact behavior a crash-retry would trigger, pinned as correct.
- `test_concurrent_same_request_id_writes_exactly_once` (:232) — concurrency on the SAME id only.
- No test anywhere (incl. `test_reference_actions.py`, `test_dbos_live.py`,
  `ne-worker/tests/test_workflows_passthrough.py`) simulates "step re-run, model chooses a new id,
  same underlying turn." Git history (`git log -S 'request_id: str = Field'`) shows the field landed
  with NE-050 (c7b1154, PR #62) and has never been touched since — there was no earlier deterministic
  derivation that got lost.

### 2.4 The crash-retry surface is real and exactly as coarse as claimed

`packages/ne-worker/src/ne/worker/workflows.py:30-43`: `execute_turn_step` (`@ne_step`) wraps the
ENTIRE `session.run` pydantic-ai loop as ONE DBOS step; the module docstring (:12-18) states the
recovery semantics honestly — "a worker restart mid-turn re-runs the turn FROM THE TOP." A re-run is
a fresh model inference; LLM sampling is not reproducible in general (and even temperature-0 is not
guaranteed bit-stable across a worker restart onto a different vLLM replica). The re-prompted model
has no memory of its crashed attempt's `request_id`.

### 2.5 Deployment topology — why this is latent today (verified, honest)

- The ne-api/ne-worker platform executor (`ne-api/src/ne/api/platform.py:948-957`) contains chat /
  trace / verified / skills bindings plus at most `demo_writeback_binding` — **no NE-050
  ActionTool binding is wired into it**. (`demo_writeback_binding`,
  `demo_seams.py:110-125`, is a deliberately inert no-egress handler with no dedup at all — it
  would "fire" twice on a step retry, but it performs nothing.)
- The one composition that DOES run real ActionTools (`ne-mcp/demo_server.py`) has no DBOS anywhere
  — its caller is an external LangGraph process.
- So today no process contains both a durable step-retry and a deduped real write. **The gap becomes
  live the day `create_write_actions` bindings are merged into the platform executor** — which is the
  declared NE-077 direction ("governed write-back" is in the gate list) — and the
  ne103c design already (incorrectly) tells that future integrator the dedup store has them covered.

### 2.6 Severity backstop — the Tier-2 HITL floor

Every ActionTool declares tier ≥ 2 by construction (`action_tools.py:130-135`), and
`GovernedToolExecutor.execute` routes EVERY effective-tier ≥ 2 call to the named human gate
(`middleware.py:250-251`), dedup notwithstanding (dedup is post-governance; a retry re-enters the
approval gate, action_tools.py:38-40). So the worst case is: crash after write+finalize → re-run →
model mints new id → **second approval request lands on a human** → if approved, second live write.
Not silent — but the approver has no signal that this is a duplicate of an already-performed write,
and "the human may be asked twice" (workflows.py:17) was accepted for approvals precisely on the
assumption the write layer beneath was idempotent. It is not.

---

## 3. The design tension, resolved

The module docstring's intent (action_tools.py:24-31) is the classic **Stripe-style caller-chosen
idempotency key**: "the caller supplies a ``request_id``… retries MUST reuse it." That contract is
sound for a caller that can *remember its own id across the retry* — a programmatic caller, or a
model retrying **within one live reasoning session** ("my tool call seemed to hang; re-issue with
the same id"), which the in-flight/settled dedup states (action_tools.py:365-376) serve well.

The crash-retry breaks the contract's premise, not its logic: after a step re-run, the "caller" is a
**fresh inference with no memory**. There are therefore two distinct retry classes needing two
mechanisms:

| Retry class | Who remembers the identity? | Mechanism |
|---|---|---|
| In-session, model-driven | the model (its own context window) | caller-chosen id — works today, keep it |
| Cross-attempt, crash-driven | nobody in the model loop — only the durable layer | a framework-derived identity from stable inputs — missing today |

A fix that silently overrides every model-supplied id would destroy the first class (and break the
scripted LangGraph demo caller plus every existing test); a fix that ignores the second class leaves
the confirmed gap. The chosen design serves both.

### Rejected options

- **A. Always inject/override with `hash(workflow_id, step, ordinal, action)`.** Rejected: (i) kills
  the legitimate explicit-id contract for programmatic callers (`GovernedAction.execute` app code,
  the LangGraph demo's hard-coded ids — `extra="forbid"` would then have to *reject* what today is
  the documented interface); (ii) an *ordinal* is not stable across re-prompts — a re-run model may
  reorder or interleave calls, so ordinal-pairing can mis-match two genuinely different writes and
  fail them closed as payload conflicts; (iii) invalidates every existing dedup test and any
  in-flight dedup row at upgrade time. Strictly worse than the hybrid for no additional safety.
- **B. Validate the model-supplied id is "stable."** Not viable, as suspected: on attempt 2 there is
  nothing to validate *against* — attempt 1's id lives only in the crashed context and (if the write
  finalized) in a dedup row that can only be found by… already knowing a stable key. Validation
  collapses into derivation.
- **C-weak. Only scope the existing key by turn id (`sha256(action:scope:request_id)`).** Fixes the
  §1.3 cross-turn collision but NOT the core finding (a re-run model still mints a different id
  inside the same scope). Insufficient alone; its scope component is subsumed by the chosen design.

---

## 4. Chosen mechanism: derived-by-default identity, explicit id preserved

**Rule:** the dedup identity for a write is `request_id` when the caller explicitly supplies one
(byte-identical to today), and otherwise a **framework-derived deterministic token** =
`sha256(scope : action : payload_hash)` where `scope` is a contextvar-carried stable turn identity
(the DBOS workflow id / `turn_id`) and `payload_hash` is the existing canonical input hash. The
model-facing schemas stop advertising `request_id`, so the agentic path becomes derived-by-default.

Concretely, in the `build_action_binding` handler:

```python
# action_tools.py — replaces line 349
payload_hash = _payload_hash(validated)          # unchanged, moves above the key
if validated.request_id is not None:
    # Explicit caller-chosen id: EXACTLY today's key (no ':' can appear in a legal
    # request_id, so the two namespaces cannot collide).
    dedup_key = hashlib.sha256(f"{spec.name}:{validated.request_id}".encode()).hexdigest()
else:
    scope = current_action_scope()               # correlation.py contextvar
    if scope is None:
        raise ActionValidationError(
            f"{spec.name!r} was called without a request_id and outside any action "
            "idempotency scope; a governed write needs a stable idempotency identity "
            "(fail closed)"
        )
    dedup_key = hashlib.sha256(
        f"{spec.name}:derived:{scope}:{payload_hash}".encode()
    ).hexdigest()
```

Why this is the soundest shape:

- **Crash-retry closes.** The re-run step re-establishes `scope = turn_id` (stable: DBOS re-invokes
  the step with the same serialized `TurnRequest`; `workflow_id = turn_id` is minted once by ne-api
  — ne103c design §"Correlation handle", :112-114). The re-prompted model re-issues the same
  intended write → same canonical payload → same key → `deduplicated`, connector never re-called.
  The model's inability to reproduce an id becomes irrelevant because no id is asked of it.
- **In-session retry gets STRONGER, not weaker.** Today it depends on the model correctly reusing
  its id; derived keys make the common retry (re-issue same args) dedup automatically, and the
  in-flight PENDING state (:365-372) still fails a racing duplicate closed. The one capability lost
  in-scope: a model deliberately issuing two byte-identical writes in one turn now dedups the
  second. For governed Tier-2+ writes that is the safe default (a byte-identical duplicate inside
  one turn is presumptively a loop bug); a programmatic caller that truly needs it passes two
  explicit `request_id`s.
- **Cross-turn collision (§1.3) closes** on the derived path (scope is in the key) and becomes
  documented caller responsibility on the explicit path (unchanged from today's contract).
- **Explicit path is byte-identical.** Same field, same regex, same key derivation, same
  divergent-payload integrity error, same tests. `langgraph_env/agent.py`'s hard-coded ids, all of
  `test_action_tools.py` / `test_reference_actions.py`, and any in-flight dedup rows survive the
  upgrade untouched. No migration.
- **Fail-loud, not fail-open.** No id + no scope raises `ActionValidationError` (already in
  `_BLOCKING_ERRORS` via `ToolExecutionError` wrapping — surfaces as an audited BLOCKED result, no
  write). A composition that exposes derived-id actions without establishing a scope is told so on
  the first call, in CI.

### Residual risk, stated honestly (do not paper over)

**Payload drift on re-prompt.** If the re-run model *rephrases* the write (different body text), the
canonical payload differs → different key → second write reaches the source (after a second human
approval, §2.6). No idempotency layer can equate two semantically-similar-but-byte-different writes;
the honest guarantee this design delivers is **"same turn + same action + same bytes never writes
twice, no matter what id the model does or doesn't invent."** The full fix for drift is
per-tool-call step decomposition (already tracked as ne103c §10-Q3 future work) — out of scope here,
and this unit's scope seam (`action_scope`) is exactly what that future decomposition will reuse.
The corrected ne103c §5.2 paragraph must state this residual explicitly.

---

## 5. Exact change points

1. **`packages/ne-runtime/src/ne/runtime/correlation.py`** — add, mirroring `run_trace_scope`
   (:30-58): `_action_scope: ContextVar[str | None]`, `current_action_scope()`, and
   `@contextmanager action_scope(scope: str)` (nested scopes: innermost wins — a sub-agent
   dispatched inside a turn inherits the ambient scope unless the dispatcher sets its own). Export
   from `ne/runtime/__init__.py`.
2. **`packages/ne-runtime/src/ne/runtime/action_tools.py`**
   - `:93` — `request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")`.
   - `:349` — the branch shown in §4 (explicit key byte-identical; derived key
     `sha256(action:derived:scope:payload_hash)`).
   - `:24-40` + `:85-88` — rewrite the "Idempotency (A6)" docstring section and the `ActionInput`
     field docs to state the two-class contract (explicit caller id vs derived turn-scoped
     identity) and the payload-drift residual. Per house rule
     [[written-rules-disarm-review]]: every normative sentence added here must trace to a test in
     §6, none may stand on argument alone.
   - `_payload_hash` (:316-319) is unchanged — with `request_id=None` excluded via
     `model_dump(exclude_none=True)`? **No** — keep `model_dump(mode="json")` as-is and let
     `request_id: null` serialize; it is a constant on the derived path and present on the explicit
     path exactly as today. Zero behavior change either way; do not touch it.
3. **`packages/ne-runtime/src/ne/runtime/reference_actions/{crm_note,email_draft,ticket_update}.py`**
   — remove `"request_id"` from the three `input_schema["properties"]` dicts
   (email_draft.py:87, crm_note.py:69, ticket_update.py:83) so the model is never invited to mint
   ids. **Keep** the `request_id` entries in the three `*_ARG_POLICY` allowlists (a programmatic /
   scripted caller passing an explicit id must still clear guardrails; an allowlisted-but-absent
   arg costs nothing).
4. **`packages/ne-worker/src/ne/worker/workflows.py`** — in `execute_turn_step`, wrap the session
   run in the scope (alongside the existing `TURN_PRINCIPAL` pattern, :47):
   `with action_scope(req.turn_id): result = await session.run(...)`. Also fix the module
   docstring's recovery-semantics paragraph (:12-18) to name the write-dedup behavior. The cron
   tick (`main.py:424`) gets `action_scope(f"cron:{job}:{fire_ts}")` **only if** cron agents ever
   gain write actions — v1: leave cron out, note it in the docstring.
5. **`docs/architecture/ne103c-durable-worker-design.md:383-384`** — correct the false "protected by
   the ActionDedupStore" claim to the two-class truth + residual (§4 above).

Nothing else moves. `middleware.py`, `runner.py`, `agent.py`, `tools.py`, `factory.py`,
`demo_server.py` are untouched — the fix lives entirely at the dedup boundary plus one contextvar
seam, preserving the single-chokepoint C6 shape (no per-caller branching in the executor).

---

## 6. Regression test plan (the proof, not the argument)

`packages/ne-runtime/tests/test_action_tools.py` additions (same in-memory-SQLite harness):

1. **The finding's exact scenario, closed** — `test_step_retry_with_fresh_model_id_dedups`:
   under `action_scope("turn-1")`, call the binding with args WITHOUT `request_id`, payload P →
   real write. Simulate the DBOS re-run: a second call under `action_scope("turn-1")`, same P (the
   "re-prompted model" — which today would have minted a different id) → assert `perform` was
   called EXACTLY once and the second result carries `detail="deduplicated"`.
2. **Scope separation** — same P under `action_scope("turn-2")` → a second real write (no
   cross-turn over-dedup).
3. **Fail-loud** — no `request_id` + no ambient scope → `ActionValidationError`, `perform` never
   called, audited as an error execute.
4. **Distinct in-turn writes still work** — same scope, two different payloads → two real writes.
5. **Explicit-path byte-compatibility** — the existing tests (:189-230, :232, :281, :312) pass
   UNMODIFIED (this is the load-bearing assertion that the explicit key derivation did not change),
   plus one new case: explicit `request_id` given under an active scope → key ignores the scope
   (documented Stripe semantics: an explicit id must survive a scope change, e.g. an app-level
   retry from a different turn).
6. **Schema hygiene (structural, guardian-style)** — the three reference `input_schema` dicts do
   NOT contain `"request_id"`; `ActionInput.model_fields["request_id"].default is None`.
7. **`packages/ne-worker/tests/test_workflows_passthrough.py`** addition — a stub binding records
   `current_action_scope()` at handler time; drive `execute_turn_step` (passthrough durability,
   fake platform per existing harness) and assert the scope seen equals `req.turn_id`, and is reset
   after the step.

`packages/ne-runtime/tests/test_reference_actions.py`: existing explicit-id flows (:121, :173,
:219) must pass unmodified (they exercise the guardrail-allowlisted explicit path).

---

## 7. Buildable unit brief — NE-ACTION-REQID-1

- **Goal:** governed write-back idempotency survives a coarse-grained durable-step re-run: a
  re-prompted model that re-issues the same write in the same turn scope can never reach the source
  system twice, while explicit caller-chosen `request_id` semantics stay byte-identical.
- **Deliverables:**
  1. `action_scope` / `current_action_scope` contextvar seam in `correlation.py` (+ `__init__.py`
     export).
  2. `ActionInput.request_id` optional; two-namespace dedup-key derivation in
     `build_action_binding` (§4 rule, fail-loud on no-id-no-scope); docstring contract rewrite.
  3. `request_id` removed from the 3 reference-action model-facing schemas (arg policies kept).
  4. Worker turn step establishes `action_scope(req.turn_id)`; workflows.py docstring corrected.
  5. ne103c §5.2 point-2 correction (the false safety claim) + residual-risk sentence.
  6. Tests per §6 (items 1-7).
- **mayTouch:**
  `packages/ne-runtime/src/ne/runtime/action_tools.py`,
  `packages/ne-runtime/src/ne/runtime/correlation.py`,
  `packages/ne-runtime/src/ne/runtime/__init__.py`,
  `packages/ne-runtime/src/ne/runtime/reference_actions/crm_note.py`,
  `packages/ne-runtime/src/ne/runtime/reference_actions/email_draft.py`,
  `packages/ne-runtime/src/ne/runtime/reference_actions/ticket_update.py`,
  `packages/ne-worker/src/ne/worker/workflows.py`,
  `packages/ne-runtime/tests/test_action_tools.py`,
  `packages/ne-runtime/tests/test_reference_actions.py`,
  `packages/ne-runtime/tests/test_correlation.py`,
  `packages/ne-worker/tests/test_workflows_passthrough.py`,
  `docs/architecture/ne103c-durable-worker-design.md`.
  (Downstream-assertion sweep done per [[unit-briefs-must-cover-downstream-assertions]]: the only
  asserting suites over `request_id` / the schemas are the four test files above;
  `test_action_write_guardian.py` scans for `.perform(` and structural C6 shape only — untouched by
  this diff — and `test_middleware_c6.py` / `test_redteam_c6.py` don't reference `request_id`.)
- **Out of scope (explicit):** per-tool-call step decomposition (ne103c §10-Q3); approval-request
  dedup across re-runs (ne103c §10-Q2); wiring NE-050 actions into the platform executor; cron-lane
  scoping.
- **Checks:** `uv run pytest packages/ne-runtime/tests/test_action_tools.py
  packages/ne-runtime/tests/test_reference_actions.py packages/ne-runtime/tests/test_correlation.py
  packages/ne-worker/tests/test_workflows_passthrough.py` + the standard full-package CI lanes.
- **Size:** **M** — ~120 LOC of source across 7 files (the load-bearing diff is ~25 LOC in
  `action_tools.py` + ~30 LOC in `correlation.py`), ~200 LOC of tests, one doc correction. Single
  builder, one pipeline pass.
