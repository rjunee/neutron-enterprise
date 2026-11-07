# NE-ACTION-SCOPE-WIRING — one action_scope pattern for every entrypoint, and the write-action composition gap (design)

**Status:** DESIGN — buildable
**Date:** 2026-07-22
**Questions under review:** (1) why are the NE-050 governed write actions not live in the real
chat app — intentional sequencing or an unfinished gap? (2) what is the ONE lean pattern for
establishing the NE-ACTION-DETERMINISTIC-REQID `action_scope` at every entrypoint that can reach
a governed write action, instead of three ad-hoc wraps?
**Method:** every claim below was re-verified against code/git during this pass (file:line cites
as of `ne/design-action-scope-wiring`, base commit 25948b2). The tasking's summaries
were treated as hypotheses, not facts; one was sharpened (§1.1) and one common assumption was
disproven (§3.2 — MCP *does* carry a usable turn signal).

---

## 1. The finding, stated plainly

**`create_write_actions` — the factory presented as THE NE-050 composition seam — is called by
nothing. Not by any production composition root, not by the demo, not even by a test.**

Verified by exhaustive grep (every `.py` in the repo):

- Definition: `packages/ne-runtime/src/ne/runtime/factory.py:233`. The only other Python
  occurrences are its own docstring example (factory.py:258), the public export
  (`packages/ne-runtime/src/ne/runtime/__init__.py:122,241`), and a docstring mention
  (`action_tools.py:529`). Zero call sites.
- The real ne-api/ne-worker platform (ONE recipe, two processes —
  `packages/ne-worker/src/ne/worker/main.py:99,128` imports `ne.api.platform.build_platform`)
  builds its executor via `create_tool_executor` with chat/trace/seams/verified/demo/skills
  bindings only (`packages/ne-api/src/ne/api/platform.py:948-966`). **No NE-050 ActionTool
  binding is in the tuple.** The only "write-back" there is `demo_writeback_binding`
  (platform.py:928-932) — a deliberately inert, no-egress, no-dedup demo stand-in.
- The ONLY place the reference write actions run end-to-end is the drop-in demo root,
  `packages/ne-mcp/src/ne/mcp/demo_server.py:259-275` — and it wires them **by hand**
  (`ensure_action_dedup_schema` + `ActionDedupStore` + `build_action_bindings` at :268, fed
  into `create_tool_executor`), bypassing `create_write_actions`.

### 1.1 Why even the one real composition bypasses the factory (sharper than "sloppy")

`create_write_actions` returns a **dedicated executor over only the action bindings**
(factory.py:244: `-> tuple[GovernedToolExecutor, dict[str, GovernedAction]]`). Every real
composition root needs the action bindings **interleaved into its ONE C6 executor** next to its
read tools (the demo needs kb-search + actions in one catalog; the platform needs
chat/trace/verified + actions in one catalog — single executor, single guardrail state, single
agent-facing toolset). The factory's shape cannot do that, so the only real consumer hand-rolled
the three-line recipe instead. The factory isn't just unused — **its shape is why it's unused.**
The composable seam that actually exists and is actually exercised is
`build_action_bindings(definitions, dedup=...)` (action_tools.py; used at demo_server.py:268).

### 1.2 The archaeology — deliberate deferral that decayed into an unscheduled gap

Traced through git and the logs; each step has a stated reason, and the composition step fell
between them:

1. **2026-07-07 — NE-050 merges as a framework, "live-parked"** (c7b1154, PR #62;
   "Live source-system writes parked"). At that date this was not a choice about the app —
   **there was no app**: ne-api did not exist and was only discovered missing on 2026-07-08
   ("`ne-api` … was NEVER BUILT"). There was literally nothing to wire actions into.
2. **ne-api is then built against the mocked console contract** (~19 endpoints the SPA was coded
   against). Write actions were never part of that contract, so NE-104a/NE-103c
   composed only the read-side bindings. Every later touch of the executor construction
   (NE-103c 496b6e8, NE-137 0dac037, NE-GA-G5 39a80d4) added its own bindings; none added NE-050.
3. **The demo arc's write-back beat got a stand-in**: `demo_writeback_tool`
   (`packages/ne-api/src/ne/api/settings.py:205`, binding at platform.py:928-932) — inert
   no-egress handler, no dedup — which satisfied the visible deny→HITL→approve arc and removed
   the day-to-day pressure to wire the real actions.
4. **The drop-in track intentionally proved governance parity out-of-process first**
   (NE-LG-DROPIN, NE-CREWAI-DROPIN): real NE-050 actions over MCP with an
   external LangGraph/CrewAI agent. Sequencing here WAS deliberate and is now done (two proof
   points recorded).
5. **No SPEC §5 unit ever schedules "compose NE-050 into the platform executor."** NE-076
   and NE-077 both *require* "governed write-back through Tier-2
   **console** approval" and both dep on 050 — the wiring is an implied, unscheduled
   prerequisite of the v1 gate. This is the same class of gap that produced ne-api itself
   (it fell between the topology table and the phased plan).

**Verdict: no stated safety or compliance blocker exists anywhere** (spec,
build record, design logs, code comments — searched for any "parked pending X" note about app wiring; none).
"Live-parked" at NE-050 time meant *live connector writes* (no dev tenants existed until
2026-07-19), not "keep the framework out of the app." The honest answer to the owner's
question — *"we haven't built out the inline chat functionality? why not?"* — is: **the deferral
was rational when made (no app existed; demo-first parity proof was deliberate), but the
wire-into-the-real-app step was never scheduled afterward, and the demo stand-in plus a
dead factory have been masking that.** NE-077 cannot pass without it.

### 1.3 The scope gap in the real chat surface (verified)

`packages/ne-api/src/ne/api/routers/chat.py` establishes **no `action_scope` in either mode**:

- **inline** (default): `_streamer` sets `TURN_PRINCIPAL` + `TURN_TOOL_TRANSITS` contextvars
  (chat.py:150-153) then runs `matched.run(body.prompt, principal=principal)` (chat.py:156)
  with no scope. The moment a write action lands in the platform executor, the first inline
  derived-path write raises `ActionValidationError` (fail-loud by design — correlation.py:72-87,
  action_tools derived branch). Good floor, foreseeable trap.
- **enqueue**: mints `turn_id = f"turn-{uuid.uuid4()}"` (chat.py:191) and hands off to the
  worker, which already wraps correctly: `with action_scope(req.turn_id)` around `session.run`
  (`packages/ne-worker/src/ne/worker/workflows.py:86-87`). Covered.

The MCP surface wraps every `call_tool` in ONE per-connection scope
(`packages/ne-mcp/src/ne/mcp/server.py:183,217` — `mcp:<uuid4>` minted in `__init__`): fails
safe (over-dedup within one long session, never a double write; regression-pinned at
`packages/ne-mcp/tests/test_demo_server_composition.py:298`) but coarser than a turn.

---

## 2. Recommendation: build the wiring, not just the scopes

Given §1.2 (no real blocker; NE-076/NE-077 require it; owner leaning "build it"), the primary
recommendation is **W — wire the NE-050 write actions into the real platform executor**, with
the scope pattern (§3) landing first as its prerequisite. The one genuinely live-blocked slice
is narrow and does not block the wiring: the M365/Salesforce **tenant write scopes** are not yet
granted (READ verified 2026-07-19; write adds tracked in the NE-102 dev-tenant guides). The SQL
ticket-update action writes to the deployment's own SQL and is fully exercisable today; the
CRM-note/email-draft legs compose now and go live when the owner grants the scopes — every leg
sits behind the hard Tier-2 HITL floor + PDP + the C2 egress allowlist regardless.

What W looks like (concrete; unit brief in §5):

- **Where:** `build_platform` (platform.py), behind a default-off `ApiSettings` block —
  `write_actions_enabled: bool = False` plus the connector-client config it needs. Adding the
  field is an EXPECTED release-level `config_hash` schema change (the `demo_writeback_tool` /
  `demo_delegation` precedent verbatim, settings.py:210-214 — PR-flagged, never mid-process).
- **How:** `ensure_action_dedup_schema` on the platform engine + `ActionDedupStore` +
  `build_action_bindings([...])` appended into the SAME executor tuple at platform.py:948 —
  exactly the demo_server recipe, exactly how every other feature's bindings joined
  (verified/demo/skills). **Not** via `create_write_actions` (§1.1). Merge
  `CRM_NOTE_ARG_POLICY` / `TICKET_UPDATE_ARG_POLICY` / `EMAIL_DRAFT_ARG_POLICY` into
  `govern_settings.guardrails.tools` (an un-merged action fails closed as a toolargs
  violation — factory.py:264-268).
- **Clients:** composition roots may import ne-connect (ne-api already depends on it,
  `packages/ne-api/pyproject.toml:15`; ne-runtime src never does). `SqlWriteClient`
  (`ne/connect/sql/write.py:35`) constructs from the platform/Acme SQL config always;
  `SalesforceWriteClient` (:30) / `GraphWriteClient` (m365/write.py:34) construct only when
  their SecretStore creds are configured — **enabled-without-creds is a loud boot ValueError**
  per action, never a silently absent tool.
- **Exposure:** the grounded agent is already structurally excluded — `create_department_session`
  wraps it in `ToolsetScopedExecutor(granted=grounding.granted_tools)` (verified reads only,
  factory.py:489-496; chat.py:75-87). Every other chat agent sees the write tools, and **every
  call is Tier-2/3 HITL through the console approval queue** — which is precisely the NE-076/077
  beat. If the owner wants narrower default exposure (per-agent/department), that is existing
  PDP policy / `ToolsetScopedExecutor` configuration, not new mechanism — decide at dispatch,
  don't build new code for it.
- **Owner decisions actually remaining** (narrow): (a) default exposure — recommend "all
  non-grounded chat agents, HITL-gated" (the gate IS the control); (b) granting the tenant
  write scopes for the live SF/M365 legs (existing NE-102 owner action); (c) §5-U3's
  delete-vs-keep on the dead factory (recommend delete).

---

## 3. The pattern: one scope per logical turn, established at the turn root

No new abstraction is needed. `action_scope(token)`
(`packages/ne-runtime/src/ne/runtime/correlation.py:72-87`) **is** the single reusable
mechanism — a plain contextmanager over a contextvar, PII-disciplined (opaque tokens only,
never task/prompt text). What was missing is the *convention*, applied uniformly:

> **Every entrypoint that owns a logical turn establishes exactly one `action_scope` around the
> turn's execution, using its own native stable turn identity as the token. Nothing below the
> entrypoint (executor, middleware, session) ever mints a scope.**

The "below the entrypoint" clause is load-bearing: an executor that self-minted a fallback scope
would silently re-create the ad-hoc-granularity problem this design exists to remove, and would
defeat the fail-loud floor (`ActionValidationError` on no-id-no-scope) that turns any missed
entrypoint into a first-call CI failure instead of a silent mis-dedup. That floor — not a
registry of entrypoints — is the enforcement mechanism; per-entrypoint tests pin each token.

| Entrypoint | Turn identity (token) | Status |
| --- | --- | --- |
| Durable worker (`workflows.py:86-87`) | `req.turn_id` (DBOS-stable, minted by ne-api) | ✅ correct today — untouched |
| Inline chat (`chat.py` `_streamer`) | mint `turn-<uuid4>` per message — the SAME shape enqueue mode already mints at chat.py:191, so both modes get byte-identical per-turn scoping | ❌ build (U1) |
| MCP `call_tool` (`server.py`) | client-signaled turn id via `_meta`, namespaced under the connection scope; per-connection fallback | ⚠️ refine (U2) |
| Cron / sub-agent dispatch | inherit ambient scope (correlation.py:78-81); cron gets its own token **only if** cron agents ever gain write actions | ✅ no-op (per REQID design §5.4) |

### 3.1 U1 — inline chat (trivial by design)

In `_streamer`, mint `turn_id = f"turn-{uuid.uuid4()}"` and wrap the turn in
`with action_scope(turn_id):` — set before the turn task is created, exactly the existing
`TURN_PRINCIPAL`/`TURN_TOOL_TRANSITS` pattern at chat.py:150-153 (ContextVars copy into the
task). ~4 lines. This is W's hard prerequisite: without it, flipping write actions on makes
every inline write fail loud.

### 3.2 U2 — MCP: the protocol DOES offer a turn signal (verified, not assumed)

The prior round's comment "MCP call_tool carries no turn boundary" (server.py:174-179) is true
of the bare `(name, arguments)` handler signature but **false of the protocol**. Verified in the
locked SDK (mcp 1.28.1, platform lock; sidecar venvs pin `~=1.28.1`):

- `RequestParams.Meta` is `extra="allow"` — clients may attach arbitrary namespaced `_meta`
  fields to any request (`mcp/types.py:61-71`; `CallToolRequestParams` inherits it, :1348).
- The client API forwards it: `ClientSession.call_tool(name, args, *, meta={...})`
  (`mcp/client/session.py:386-404`).
- The server exposes it per-request: `self._server.request_context.meta` (contextvar set per
  inbound request, `mcp/server/lowlevel/server.py:240-244,759`).

So: define `TURN_META_KEY = "example.host/turn-id"` (MCP reserves un-prefixed keys; custom keys
take a domain prefix — ours is example.host). In `_call_tool`, read
`request_context.meta` (guard `LookupError` → no context), extract the key, validate it opaque
and bounded (`^[A-Za-z0-9._:-]{1,128}$`), and scope as:

```python
scope = self._session_scope                      # today's per-connection scope (fallback)
if turn_token is not None:
    scope = f"{self._session_scope}:turn:{turn_token}"   # strictly tighter
with action_scope(scope):
    ...
```

The connection-scope prefix is load-bearing: a client can only subdivide its OWN connection's
dedup space, so no client-chosen token can collide across sessions/principals. An absent or
invalid token falls back to today's exact behavior (fail-safe direction; the :298 regression
test passes unmodified) — a malformed token additionally logs server-side. `progressToken` is
deliberately NOT repurposed (per-request semantics, wrong granularity). The in-memory-transport
tests exercise the seam through the real protocol, so this is not another uncalled seam; the
drop-in demo agents adopting `meta=` per scripted task is a cheap optional follow-on, noted in
the unit.

### 3.3 U3 — retire the dead factory

Delete `create_write_actions` (+ its export) and bless the recipe the one real consumer already
uses — `build_action_bindings` into the root's single executor — as the documented NE-050
composition seam (factory.py / action_tools.py docstrings, ne-runtime README). Rationale beyond
dead code: its docstring presents it as the normative "Composition pattern" (factory.py:255-262),
which is exactly the [[written-rules-disarm-review]] hazard — the next integrator (unit W!)
would reach for the wrong-shaped seam because the docs bless it. Zero callers ⇒ zero migration.
(Owner alternative if deletion feels premature: keep it and add the missing test + a docstring
note pointing single-executor roots at `build_action_bindings`. Recommend deletion.)

---

## 4. Non-goals (explicit)

- **No change to dedup-key derivation.** `sha256(action:derived:scope:payload_hash)` and the
  explicit-`request_id` namespace (action_tools.py) are correct and untouched. This design is
  solely about WHO establishes the scope and WHEN.
- **No new abstraction layers.** No decorator, no middleware auto-scoping, no entrypoint
  registry — the contextmanager + convention + fail-loud floor is the whole pattern (§3).
- **No per-tool-call step decomposition** (payload-drift residual) — already tracked, ne103c
  §10-Q3; see the REQID design §4 for the honest residual statement.
- **No live SF/M365 writes before the owner grants tenant write scopes** — W composes the legs
  fail-loud; going live on those two is the existing NE-102 owner action, not code.
- **No cron/dispatch scoping** — inherit-ambient is correct today (correlation.py:78-81).

---

## 5. Buildable units

Format per `docs/architecture/ne-action-deterministic-reqid-design.md` §7. Order: U1 → W;
U2, U3 independent and parallel. Downstream-assertion sweeps done per
[[unit-briefs-must-cover-downstream-assertions]]; noted per unit.

### U1 — NE-SCOPE-CHAT: inline-chat turn scope (S)

- **Goal:** both chat execution modes carry byte-identical per-turn `action_scope` semantics;
  flipping write actions on can never fail-loud (or mis-dedup) on the inline path.
- **Deliverables:** §3.1 wrap in `_streamer`; test: a stub executor tool records
  `current_action_scope()` during a driven inline turn — asserts a `turn-`-prefixed scope is
  set, unique per message, and reset after the stream (client-abort path included).
- **mayTouch:** `packages/ne-api/src/ne/api/routers/chat.py`,
  `packages/ne-api/tests/test_chat_action_scope.py` (new).
  (Sweep: no existing suite asserts on scope absence in the inline path; `test_sse.py` drives
  `turn_event_stream` directly and is unaffected — additive contextvar only.)
- **Checks:** `uv run pytest packages/ne-api/tests/test_chat_action_scope.py
  packages/ne-api/tests/test_sse.py packages/ne-api/tests/test_chat_enqueue.py` + standard lanes.

### U2 — NE-SCOPE-MCP: per-turn MCP scope via `_meta` (M)

- **Goal:** an MCP client that signals turn boundaries gets true per-turn dedup scoping; one
  that doesn't keeps today's per-connection fail-safe behavior byte-identically.
- **Deliverables:** §3.2 (`TURN_META_KEY`, validated extraction, connection-prefixed
  composition, `LookupError`/invalid-token fallback + server-side log); docstring rewrite of
  server.py:53-58 + :172-183 to the two-level contract (normative sentences trace to the tests,
  per [[written-rules-disarm-review]]); tests over the in-memory transport: (1) two writes,
  same payload, different `_meta` turn ids → two real writes; (2) same turn id → deduplicated;
  (3) no meta → existing behavior (the :298 test passes UNMODIFIED — load-bearing); (4) invalid
  token → fallback, call still governed. Optional follow-on (may defer): drop-in demo agents
  pass `meta=` per scripted task.
- **mayTouch:** `packages/ne-mcp/src/ne/mcp/server.py`,
  `packages/ne-mcp/tests/test_server_governed.py`,
  `packages/ne-mcp/tests/test_demo_server_composition.py`.
  (Sweep: :298 is the only suite pinning scope behavior; parity/live suites don't send `_meta`.)
- **Checks:** `uv run pytest packages/ne-mcp/tests/test_server_governed.py
  packages/ne-mcp/tests/test_demo_server_composition.py
  packages/ne-mcp/tests/test_server_fail_closed.py` + standard lanes.

### U3 — NE-SCOPE-SEAM: retire `create_write_actions`, bless the real seam (S)

- **Goal:** the documented NE-050 composition seam is the one real roots actually use; no
  wrong-shaped blessed factory left to mislead unit W's builder or review.
- **Deliverables:** delete `create_write_actions` + exports; move the normative composition
  recipe (dedup schema + store + `build_action_bindings` into the root's ONE executor, ARG_POLICY
  merge requirement) into the `build_action_bindings`/factory docstrings + ne-runtime README;
  structural assertion that `ne.runtime.__all__` matches (existing `test_import.py` home).
- **mayTouch:** `packages/ne-runtime/src/ne/runtime/factory.py`,
  `packages/ne-runtime/src/ne/runtime/__init__.py`,
  `packages/ne-runtime/src/ne/runtime/action_tools.py`, `packages/ne-runtime/README.md`,
  `packages/ne-runtime/tests/test_import.py`, `packages/ne-runtime/tests/test_factory.py`.
  (Sweep: zero callers repo-wide — §1; no test imports the symbol.)
- **Checks:** `uv run pytest packages/ne-runtime/tests/test_factory.py
  packages/ne-runtime/tests/test_import.py packages/ne-runtime/tests/test_action_tools.py`
  + standard lanes.

### W — NE-ACTION-WIRE: write actions into the real platform (M; **depends on U1**; owner go on §2 decisions (a))

- **Goal:** the NE-050 reference actions are live, governed tools in the real ne-api/ne-worker
  executor — the NE-076/NE-077 "governed write-back through Tier-2 console approval" beat runs
  in the real app, not a demo seam.
- **Deliverables:** §2 wiring (default-off `ApiSettings` block; dedup schema/store on the
  platform engine; `build_action_bindings` appended at platform.py:948; ARG_POLICY merge;
  fail-loud enabled-without-creds boot; SQL leg live now, SF/M365 legs cred-gated); tests:
  flag-off byte-identity (config_hash schema change PR-flagged per the settings.py:210-214
  precedent), flag-on catalog + a full inline AND enqueue write→HITL-approve→audit→dedup pass
  over the platform harness, grounded-agent exclusion assertion, boot-error cases. Decide at
  dispatch whether `demo_writeback_tool` retires into this (recommend: separate follow-up).
- **mayTouch:** `packages/ne-api/src/ne/api/platform.py`,
  `packages/ne-api/src/ne/api/settings.py`, `packages/ne-api/tests/test_write_actions_platform.py`
  (new), `packages/ne-api/tests/test_no_bypass.py`.
  (Sweep at dispatch: any golden test pinning the ApiSettings field set / config_hash schema —
  the `demo_delegation` PR is the template for which suites moved.)
- **Checks:** `uv run pytest packages/ne-api/tests/` + the ne-worker passthrough suite +
  standard lanes; the live console-approval beat lands in the NE-076 demo arc run.

---

**Net:** U1 (S) + U2 (M) + U3 (S) close the scope-wiring question with zero new abstractions;
W (M) closes the owner's actual question — it turns the write path on in the real product,
which the v1 gate already requires and nothing was ever actually blocking.
