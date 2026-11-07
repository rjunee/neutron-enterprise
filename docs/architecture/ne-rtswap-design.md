# NE-RTSWAP — CrewAI as the second runtime: feasibility review (design)

**Status:** FEASIBILITY REVIEW — verdict at end (§10): **PARTIALLY FEASIBLE, re-scoped**
**Date:** 2026-07-21
**Question under review:** can CrewAI serve as the NE-RTSWAP second-runtime target, and is the
NE-RTSWAP plan feasible as scoped or in a revised form? The plan under review is the epic AS IT
STOOD ON 2026-07-21 — phases (a)–(e), backlogged and owner-gated, at `faa52ed8`. Its spec row
no longer reads that way: the 2026-07-22 re-scope THIS REVIEW PRODUCED
replaced it in place, and the current row authorizes and closes a three-phase plan. See §2.
**Method:** every normative claim below traces to a file:line read during this review or to
PyPI metadata fetched fresh on 2026-07-21 (`https://pypi.org/pypi/crewai/json` and the
closure walk in §4). Nothing is carried forward from prior conversation assertions.
**Predecessor precision bar:** `docs/architecture/ne-lg-dropin-design.md` — the LangGraph
finding ("langgraph requires langchain-core, which the sovereignty guard permanently bans
from the platform lock — full stop") is the shape a blocking finding must take. This review
applies the same bar to CrewAI, and finds the in-process phases blocked by the same *class*
of fact — different package names, identical structure.

---

## 1. The stakes and the trap this review must avoid

The spec's NE-RTSWAP row (corrected after NE-LG-DROPIN) already
names CrewAI as the candidate target. The evidence the row gave for that is **not in today's
row and never was in this file as quoted below** — the 2026-07-22 re-scope (`83d0bcf8`) rewrote
the row, and the wording used here when this review was filed was a light paraphrase. It is
therefore quoted from the row **as it stood at `faa52ed8`** and, like any quote of a superseded
revision, is deliberately not attributed to today's spec: *"re-verified
2026-07-22 via PyPI metadata: v1.15.5, explicitly 'independent of LangChain or other agent
frameworks,' zero `langchain*`/`langgraph*` in `requires_dist` — MIT-licensed, no
sovereignty-guard conflict"* (that row closes `requires_dist` with a stray `"` instead of a
backtick; corrected here, and the nested double quotes are rendered as single quotes).

That statement is true at the top level of `requires_dist` and **wrong one level down** —
exactly the failure mode that burned the first LangGraph build (a feasibility claim checked
at the surface, falsified by the dependency closure). The row itself flags this: *"CrewAI
has not yet been verified against every RTSWAP phase's requirements … that verification is
unit-0 work."* This document is that unit-0 work.

## 2. What the epic asks (the frozen phases)

The spec row as it stood at review time — `⬜ NE-RTSWAP (epic)`, BACKLOG, scoped
2026-07-21, do NOT build yet (owner-directed), at `faa52ed8`. ⚠️ Today that row is the one the
re-scope this review produced put there: `✅ NE-RTSWAP (epic, RE-SCOPED 2026-07-22, CLOSED
2026-07-22)`, whose plan is three phases and is authorized, not withheld. The five phases below
are the frozen pre-re-scope ones — the question this review was asked, deliberately not
presented as today's row:

- **(a)** conformance kits for every runtime-facing `ne-contracts` slot (AgentSession,
  sub-agent dispatcher, tool registry, scheduler, memory provider, durable-execution) —
  the C5 `SkillSubstrateContractKit` is the template
  (`packages/ne-contracts/src/ne/contracts/testing/kits/skill_substrate.py:41`);
- **(b)** a second binding per cheap slot;
- **(c)** a second **durable-execution** engine binding (**Temporal**) passing a durability
  kit (crash→resume, HITL-survives-restart, exactly-once steps, shared audit/approval
  ledger);
- **(d)** a runtime-level dual-binding swap test running ONE governed turn through both
  runtimes, asserting identical governed behavior;
- **(e)** proof that **C6** (no PDP/audit bypass) and **C12** (a hijacked agent cannot do a
  Tier-3 action) hold IDENTICALLY on the new runtime.

## 3. The frozen contract surface a second runtime must bind

Read in full for this review — `packages/ne-contracts/src/ne/contracts/runtime.py`:

| Protocol | Methods (frozen) | Reference implementation |
| --- | --- | --- |
| `AgentSession` (runtime.py:205-222) | `session_id`, `agent_name`, async `run(prompt, *, principal, context) -> AgentRunResult`, async `close()` | `DepartmentAgentSession` (`packages/ne-runtime/src/ne/runtime/agent.py:97`) |
| `ToolRegistry` (runtime.py:225-232) | sync `register`/`get`/`list_tools` over `ToolSpec` | in-memory registry; `GovernedToolExecutor.tool_specs` is the governed catalog (`middleware.py:168-171`) |
| `SubAgentDispatcher` (runtime.py:235-238) | async `dispatch(SubAgentSpec) -> SubAgentResult` | `GovernedSubAgentDispatcher` (`packages/ne-runtime/src/ne/runtime/dispatch.py`) — server-side depth/concurrency caps, lane-clamped routing |
| `Scheduler` (runtime.py:241-252) | sync `schedule`/`cancel`/`list_jobs` | `CronScheduler` (`packages/ne-runtime/src/ne/runtime/scheduler.py`) — cron-as-isolated-agent, dual-append audit |
| `MemoryProvider` (runtime.py:255-267) | async `append`/`recall` | `SqlAlchemyEpisodicStore` (`packages/ne-runtime/src/ne/runtime/memory_store.py`) — append-only, per-agent isolated |

Two load-bearing facts about this surface, both decisive later:

1. **The C6 chokepoint is runtime-external.** `GovernedToolExecutor`
   (`packages/ne-runtime/src/ne/runtime/middleware.py:139-254`) is "the ONLY object in the
   platform that holds a tool `handler`" (middleware.py:5-6). The reference runtime
   (pydantic-ai) never reaches a handler: every pydantic-ai tool the session builds has
   exactly one body — `executor.execute(...)` — with the tool name bound by closure and
   `**kwargs` as the only parameter (`runner.py:83-112`), and a model-emitted UNKNOWN name
   is routed through the same executor by the `on_unknown_tool` hook at the **model
   adapter** boundary (`model.py:23-30`, `runner.py:115-137`) before pydantic-ai's
   ToolManager can reject it — the NE-033 gap-closure. Any second runtime must reproduce
   this *pair* of properties: proxy-only tools AND unknown-name governance.
2. **Durability is contractually NOT a slot.** The frozen contract module says so in its
   docstring, runtime.py:8-13: *"these Protocols are durability-engine agnostic. DBOS
   Transact is the durable-execution engine … A future Temporal move is an engagement-time
   port, not a plugin slot; there is deliberately no durability slot in the plugin
   registry."* §7 takes this up — it falsifies the epic's phase (c) framing.

## 4. Finding 1 — in-process CrewAI is ruled out (hard, structural, three independent ways)

CrewAI 1.15.5 (PyPI, fetched 2026-07-21): base `requires_dist` includes — non-optional —
`chromadb~=1.1.0`, `lancedb>=0.29.2,<0.30.1`, `instructor>=1.3.3`, `openai>=2.30.0,<3`,
`mcp~=1.28.1`, `crewai-core==1.15.5`, `crewai-cli==1.15.5`, `pydantic>=2.11.9,<2.13`,
`regex~=2026.1.15`, `portalocker~=2.7.0`, `opentelemetry-{api,sdk,exporter-otlp-proto-http}~=1.42.0`,
plus openpyxl/pdfplumber/cel-python/json-repair/json5/jsonref/aiofiles/aiosqlite/appdirs/
python-dotenv/pyyaml/tokenizers/tomli/tomli-w/httpx/click/pydantic-settings/pyjwt.

It is TRUE that no `langchain*`/`langgraph*` name appears anywhere in the base closure
(walked one level down across crewai-cli, crewai-core, instructor 1.15.4, chromadb 1.1.1,
lancedb 0.30.0, pdfplumber, cel-python; sole appearance in the wider ecosystem is
`langchain-apify` behind `crewai-tools`' `apify` **extra**, and `crewai-tools` is not needed
for the core loop — custom tools subclass `crewai`'s own base tool class). So the
langchain sovereignty guard (`packages/ne-eval/tests/test_ragas_isolation.py:28-34`, plus
the NE-LG-DROPIN `langgraph*` extension and its ne-mcp sibling
`packages/ne-mcp/tests/test_langgraph_isolation.py` on origin/main) is **not** the blocker
here. Three other things are.

### 4.1 Blocker A — `crewai → chromadb → posthog` collides with an existing merge-blocking sovereignty guard

- `chromadb~=1.1.0` is a **base** (non-optional) dependency of `crewai` 1.15.5. The only
  releases inside that pin are 1.1.0/1.1.1.
- chromadb 1.1.1's own base `requires_dist` includes **`posthog>=2.4.0,<6.0.0`** — its
  embedded telemetry client — plus `onnxruntime>=1.14.1`, `kubernetes>=28.1.0`,
  `grpcio>=1.58.0`, `uvicorn[standard]`, and an OTLP-**gRPC** exporter, all base.
- The platform has a SPEC-mandated lock guard that permanently bans `posthog` from the
  platform lock: `packages/ne-memory-graph/tests/test_no_graph_closure_in_lock.py:20-25`
  (`_FORBIDDEN = {"graphiti-core", "graphiti", "neo4j", "posthog"}`) with
  `test_uv_lock_has_no_graph_closure` (lines 35-39) asserting against the repo-root
  `uv.lock` name set. The same intent is enforced at import level by the
  `memory-graph-sidecar-out-of-process` import-linter contract (`.importlinter:385-427`),
  which names `posthog` as out-of-process-only for every `ne.*` root package.

**The finding, stated with the langchain-finding's precision:** *CrewAI requires chromadb
(base pin `~=1.1.0`); chromadb requires posthog (base pin `>=2.4.0,<6`); and posthog is
permanently barred from the platform `uv.lock` by a merge-blocking C4 sovereignty guard.
There is no way to `import crewai` in-process without pulling posthog into the lock. Full
stop.* This is not a policy judgment to argue with — it is an existing, tested invariant,
the exact structural twin of the `langgraph → langchain-core → test_ragas_isolation`
collision that killed the first LangGraph build.

### 4.2 Blocker B — the closure cannot even resolve against the platform lock

Independent of Blocker A, four of CrewAI's tight pins are in outright resolution conflict
with the current platform lock (versions read from the repo-root `uv.lock`):

| CrewAI base pin | Platform `uv.lock` has | Conflict |
| --- | --- | --- |
| `pydantic>=2.11.9,<2.13` | pydantic **2.13.4** | hard — and pydantic is the substrate of every frozen contract model (`ne.contracts.*`); downgrading platform-wide pydantic to admit a runtime binding is a non-starter |
| `regex~=2026.1.15` (⇒ <2026.2) | regex **2026.6.28** | hard |
| `portalocker~=2.7.0` (⇒ <2.8) | portalocker **3.2.0** | hard |
| `opentelemetry-api/sdk/exporter~=1.42.0` (⇒ <1.43) | opentelemetry-sdk **1.43.0** | hard |

The uv workspace produces ONE lock for `packages/*` (root `pyproject.toml`
`[tool.uv.workspace]`), so "put crewai in its own package" does not escape this: any
workspace member's dependency lands in the same `uv.lock`. `uv lock` would simply fail to
resolve — before the posthog guard even gets its chance to go red in CI.

### 4.3 Blocker C — sovereignty/SBOM costs that would remain even if A and B were waived

Recorded for completeness, because they independently disqualify the in-process path even
under hypothetical future pin relaxations:

- **Phone-home channels in base:** `crewai-core` ships `opentelemetry-exporter-otlp-proto-http`
  in its base deps — CrewAI's own telemetry channel (env-suppressible via
  `CREWAI_DISABLE_TELEMETRY` / `OTEL_SDK_DISABLED`, but structurally present in-process);
  chromadb adds posthog + an OTLP-gRPC exporter. A C2-hardened platform would carry two
  latent egress channels inside its own trust boundary and hold them shut with env vars —
  the opposite of the platform's fail-closed posture, where banned closures are kept
  *outside the lock*, not disabled inside it.
- **Attack-surface bloat:** chromadb's base pulls a full Kubernetes client, grpcio,
  onnxruntime, and a uvicorn server stack into the SBOM for what would be, for us, an
  unused embedded vector store (the platform's memory/knowledge plane is its own).
  `crewai-cli` additionally vendors the ~35 MB `uv` binary as a *runtime* dependency and a
  full TUI framework (`textual`).
- **C4 license-gate noise:** all four crewAIInc wheels (`crewai`, `crewai-core`,
  `crewai-cli`, `crewai-tools`) ship **zero license metadata on PyPI** (null license field,
  no classifier — repo is MIT-badged). `cel-python` likewise. The license gate would report
  unknown-license for the runtime's own framework. And `crewai-tools` (not needed, but the
  natural next ask) has **`pymupdf` — AGPL-3.0 — in its BASE deps**: adopting the companion
  package at all is a copyleft event.

### 4.4 Correction this review makes to the SPEC row

The row's "no sovereignty-guard conflict" claim was assessed at `requires_dist` top level.
The closure walk defeats it: the conflict is real, it is just against the **posthog/graph**
guard rather than the **langchain** guard. The SPEC row should be updated to record CrewAI
as ruled out for the in-process phases on Blockers A+B (owner edit; this review does not
touch the spec).

**Consequence:** the two leading enterprise agent frameworks — LangGraph and CrewAI — are
now BOTH proven structurally inadmissible as in-process platform dependencies, each by a
different pre-existing sovereignty guard. That is strong evidence about the *category*:
modern agent frameworks ship batteries (vector stores, telemetry, provider SDKs, TUIs)
that a sovereignty-guarded lock is designed to reject. The "in-process second
`AgentSession` binding via a major framework" framing of NE-RTSWAP phase (b) should be
retired, not retried against a third framework.

## 5. Finding 2 — tool-execution interception (the NE-033 question, asked of CrewAI)

*(Verified against CrewAI source, `crewAIInc/crewAI` main @ 2026-07-21 — the 1.15.x-era
code: `lib/crewai/pyproject.toml` there depends on `openai>=2.30,<3` with litellm as an
extra, matching the 1.15.5 wheel. This section is written even though §4 already rules out
the in-process binding: it governs the out-of-process drop-in design (§8), and it records
whether a future in-process attempt could ever clear the C6 bar if the dependency picture
changed. Short answer: not cleanly — CrewAI has the NE-033 gap, plus two of its own.)*

### 5.1 What CrewAI actually provides

- **Real, blocking tool hooks exist.** `crewai.hooks` (`lib/crewai/src/crewai/hooks/`)
  ships `@before_tool_call` / `@after_tool_call` (plus `before_llm_call`/`after_llm_call`
  and an `@on(InterceptionPoint.PRE_TOOL_CALL)` dialect). `before_tool_call` returning
  `False` blocks the tool (the model sees `"Tool execution blocked by hook"`);
  `after_tool_call` can replace the result. The `ToolCallHookContext` carries
  `tool_name` / mutable `tool_input` / `tool` / agent / task / crew. Hooks fire across the
  standard paths: the default (experimental) `AgentExecutor`, the deprecated
  `CrewAgentExecutor`, planning-mode `StepExecutor`, `LiteAgent`, standalone
  `Agent.kickoff()`, and crew-injected delegation tools.
- **Two dispatch modes.** Native function-calling when
  `llm.supports_function_calling()`, else ReAct text parsing
  (`crewai/agents/parser.py`) — with a silent **mid-run downgrade** to ReAct when a
  native-unsupported error occurs (`_downgrade_to_text_tool_calling`).
- **Task guardrails are output-only** (`task.py` `process_guardrail` over the finished
  `TaskOutput`, retry loop) — they never see individual tool calls; not a C6 mechanism.

### 5.2 The gaps — CrewAI has the NE-033 hole, and it is worse

The NE-033 precedent: pydantic-ai's ToolManager rejected unknown tool names BEFORE the
governed executor saw them — an unaudited path the platform closed by owning the model
adapter (`GatewayModel.on_unknown_tool`, model.py:23-30). CrewAI's equivalents, from
source:

1. **ReAct path, hallucinated name → NO hook fires.** In
   `lib/crewai/src/crewai/utilities/tool_utils.py` (both sync/async), the tool lookup is
   an exact-match dict get; on miss, the i18n `wrong_tool_name` error string is returned
   to the model loop from OUTSIDE the `if tool:` branch that wraps both
   `run_before_tool_call_hooks` and `run_after_tool_call_hooks`. A hallucinated call on
   this path is invisible to the hook layer — only internal event-bus events see it. And
   because native mode can silently downgrade to ReAct mid-run, "we always run native"
   is not an invariant a binding can hold by construction.
2. **Fuzzy tool-name matching.** `ToolUsage._select_tool`
   (`lib/crewai/src/crewai/tools/tool_usage.py:759`) maps a near-miss name onto a real
   tool via `SequenceMatcher` ratio > 0.85 — a model-emitted *wrong* name can silently
   become a *different real* tool. (In current wiring the exact-match wrapper usually
   gates this; any path reaching `ToolUsage.use` directly gets fuzzy semantics.)
3. **An LLM-layer execution path that bypasses hooks entirely.**
   `BaseLLM._handle_tool_execution` (`lib/crewai/src/crewai/llms/base_llm.py:~714`)
   executes `available_functions[name](**args)` INSIDE the LLM layer, with no
   `crewai.hooks` involvement. The framework's own executors pass
   `available_functions=None` (so the executor runs tools itself), but the ungoverned
   path exists for any code that hands `llm.call` a functions dict.

Mitigations exist — proxy-only tools (every registered CrewAI tool's `_run` body being
`executor.execute(...)`, the runner.py:83-112 closure pattern transplanted) give C6 on
KNOWN tool calls structurally, and a `crewai_event_bus` subscription
(`ToolSelectionErrorEvent`) could route hallucinated names to audit. But the platform's
C6 bar is **fail-closed audit** — `RuntimeAuditError` kills the run if the audit append
fails (middleware.py:368-374) — and an event-bus listener bolted onto a framework's
internal error path cannot make that guarantee by construction; it is exactly the
"specifically closed" NE-033 gap, except here the closure would live in OUR shim atop
THEIR internals (three separate paths' worth), re-verified at every CrewAI release.
**Verdict for this axis alone:** interception is *engineerable but not structural* — it
would have been a major (likely blocking) finding for the in-process phases even if §4's
dependency blockers did not already decide the question. For the out-of-process drop-in
none of this matters: the agent process is outside the trust boundary, and every
enterprise effect crosses the MCP pipe into `GovernedToolExecutor` regardless of what
CrewAI's dispatch layer does locally (the `ne-lg-dropin-design.md` §3 trust-boundary
statement applies verbatim).

## 6. Finding 3 — model-call routing (C3)

The platform rule is enforced twice:

- **Import-linter** (`.importlinter:39-88`, contract `c3-provider-sdk-chokepoint`):
  `litellm`, `openai`, `anthropic`, and every other provider SDK are forbidden imports for
  ALL `ne.*` root packages, with the single exception `ne.gateway._litellm -> litellm`.
- **The model adapter**: `ne-runtime`'s only model path is `GatewayModel`
  (`packages/ne-runtime/src/ne/runtime/model.py:3-9`), a pydantic-ai `Model` over the
  frozen `ne.contracts.models.ModelGateway` Protocol. Decision D3 (model.py:12-31):
  `Completion` has NO `tool_calls` field — tool-calling is done entirely in-band via a
  prompted JSON line, parsed by the adapter, with unknown names routed through the
  governed executor before the framework sees them.

What this means for CrewAI (verified from source, same tree as §5):

- CrewAI's default `crewai.llm.LLM` is a `__new__` **factory** routing to native provider
  SDK classes (`crewai.llms.providers.{openai,anthropic,azure,gemini,bedrock,...}`); the
  OpenAI provider imports the official `openai` SDK directly
  (`llms/providers/openai/completion.py:11`). litellm is only an optional extra in 1.15.x.
  So an in-process binding would put a second, actively-used provider-SDK call site inside
  the platform — outside `ne.gateway._litellm`, against the C3 contract's whole point.
  The import-linter would not literally fire (it scans `ne.*` sources, and `crewai`'s own
  internal `openai` import is not our import), which makes this *worse*, not better: the
  bypass would be invisible to the existing guard, and a new lock/grep guard would be
  needed just to see it.
- A clean delegation *is* structurally available in CrewAI's design: `BaseLLM`
  (`llms/base_llm.py:311`, abstract `call(messages, tools, callbacks,
  available_functions, ...)`) supports full custom LLMs, and `LLM(base_url=...,
  custom_openai=True)` / `OPENAI_BASE_URL` point the native OpenAI provider at any
  OpenAI-compatible endpoint. A hypothetical in-process binding would implement a
  `GatewayBaseLLM` over `ne.contracts.models.ModelGateway` — noting one C6-relevant
  subtlety verified in §5.2: the custom `call()` must return the raw tool_calls list
  (never execute `available_functions` itself) so dispatch stays in the executor where
  the hooks live.
- **For the out-of-process drop-in this becomes a customer-side concern only**: the
  sidecar agent's model traffic is the customer's business (their key or a local
  endpoint), outside our trust boundary, exactly like the LangGraph drop-in's `--model`
  mode (`ne-lg-dropin-design.md` §8, §10.1). No platform C3 surface is touched.

## 7. Finding 4 — durable execution is a SEPARATE axis, and the epic bundles it wrongly

Three code-grounded facts:

1. The frozen contracts say durability is not a slot (runtime.py:8-13, quoted in §3):
   *"A future Temporal move is an engagement-time port, not a plugin slot; there is
   deliberately no durability slot in the plugin registry."* Phase (c) — "a second
   durable-execution engine binding (Temporal)" presented as a swappable binding with a
   conformance kit — contradicts the platform's own frozen-contract stance. Either the
   contracts docstring is wrong (an ADR-level decision to reverse, owner call, out of scope
   here) or the epic's phase (c) is mis-framed. As the repo stands, (c) is not buildable
   *as specified* without first amending a frozen contract's documented design decision.
2. The reference runtime itself is not durably wrapped at the loop level.
   `packages/ne-runtime/src/ne/runtime/durable.py:5-13` (honesty note): *"the v1 agent
   loop is NOT itself DBOS-decorated — composing DBOS around a live pydantic-ai run needs
   serializable-args turn functions … a composition root wraps ITS turn functions."* The
   durability seam is `@ne_workflow`/`@ne_step` around **composition-root turn functions**,
   not around framework internals. Demanding that a *second* runtime be internally durable
   would hold the challenger to a bar the champion does not meet.
3. Phase (d)'s swap test includes a "durable resume" leg, which silently presupposes (c).
   Cut (c) and the honest dual-runtime swap test asserts identical **governed** behavior
   (C6/C12/C10/C7) — which is the load-bearing invariant (e) actually names.
4. CrewAI's own execution model confirms the axes are independent. `Crew.kickoff()` is a
   single blocking call (`crew.py:980`; `kickoff_async` is just `asyncio.to_thread`;
   `akickoff` is native-async but still run-to-completion) — there is **no public API to
   pump the loop one step at a time**, so external durability could only wrap whole turns
   (exactly the platform's existing composition-root pattern) or interception points.
   CrewAI does ship its own snapshot **checkpointing** (`kickoff(from_checkpoint=...)`,
   `state/checkpoint_config.py`; `Crew.replay(task_id)`) — event/task-boundary resume,
   NOT exactly-once durable execution — which is a customer-side convenience, not a
   Temporal-class engine, and does not change the analysis.

**Conclusion:** orchestrator-swap and durability-swap are independent axes. The epic
should be split: NE-RTSWAP keeps the orchestrator/governance axis; Temporal moves to a
separate, owner-gated epic (call it NE-DURSWAP) whose unit-0 is the ADR that reconciles
runtime.py:8-13 — or it stays an engagement-time port and is simply dropped from the
roadmap. This mirrors how NE-LG-DROPIN was right-sized from L to M once the real
mechanism was found; here the right-sizing is XL → (M + M + S) with the Temporal L/XL
lifted out entirely.

## 8. What IS feasible — the re-scoped plan

The epic survives, re-scoped. Two things killed the *original* framing (in-process second
`AgentSession` binding, bundled with a Temporal durability swap) — neither kills the
underlying claim NE-RTSWAP exists to prove: that the platform's governance holds
identically regardless of what agent framework is producing the tool calls.

**Retained, re-sized XL → S + M:**

- **Phase (a), unchanged and immediately buildable — conformance kits for the
  runtime-facing `ne-contracts` slots** (`AgentSession`, `ToolRegistry`,
  `SubAgentDispatcher`, `Scheduler`, `MemoryProvider`). Pure `ne-contracts` testing-kit
  work; the `SkillSubstrateContractKit` (`packages/ne-contracts/src/ne/contracts/testing/kits/skill_substrate.py:41`)
  is a directly reusable template. This proves the seams are real independent of which
  second runtime ever occupies them. **Size: S.**
- **REVISED phase (b)/(d) — a second GOVERNED runtime, out-of-process, mirroring
  NE-LG-DROPIN exactly.** §5.2's own finding makes this the right mechanism, not a
  fallback: *"For the out-of-process drop-in none of this matters: the agent process is
  outside the trust boundary, and every enterprise effect crosses the MCP pipe into
  `GovernedToolExecutor` regardless of what CrewAI's dispatch layer does locally."*
  CrewAI's in-process disqualifications (§4: posthog/chromadb sovereignty conflict, hard
  pin conflicts; §5: the NE-033-class tool-interception gaps, worse than pydantic-ai's)
  are **irrelevant** out-of-process — the sidecar venv absorbs the whole closure (posthog,
  chromadb, mismatched pydantic pin, all of it) without it ever touching the platform
  lock, and the agent's entire toolset originates from `GovernedMcpServer`, so there is no
  ungoverned path to reach regardless of CrewAI's internal hook gaps. **Size: S/M** — the
  composition-root/sidecar-venv/guardian-test pattern is now a proven, reusable template
  (NE-LG-DROPIN, merged PR #276); this is substantially "do it again for a second
  framework," not new invention.
- **REVISED phase (d)/(e) — the dual/triple-binding swap test, redefined around the
  invariant that's actually load-bearing.** Drop the "durable resume" leg (it presupposed
  Temporal, §7). Keep: run ONE governed scenario (a multi-step task with a Tier-2
  write-back) through **pydantic-ai** (in-process, the default) **and** the two
  out-of-process governed clients (**LangGraph** via NE-LG-DROPIN, **CrewAI** via the new
  unit below), asserting **identical** governed behavior — C6 non-bypass, C7 trimming,
  C10 audit-chain integrity, C12 (no Tier-3 action without a human) — across all three.
  This is arguably a *stronger* proof of "any stack" than the original single-swap-test
  framing: it demonstrates the governance invariant holds across a genuinely different
  delivery mechanism (in-process contract binding) AND two independently-built
  out-of-process clients, not just two flavors of the same binding shape.

**Cut entirely, not deferred:**

- **Original phase (c), Temporal-as-a-plugin-slot** — contradicts the frozen contract's
  own documented design decision (runtime.py:8-13: "deliberately no durability slot in
  the plugin registry"). Building it as scoped requires an ADR-level reversal of that
  decision first — an owner call, orthogonal to whether CrewAI or LangGraph can be
  governed. If ever wanted, it is its own epic (`NE-DURSWAP`), unit-0 = the ADR, not a
  NE-RTSWAP phase.
- **In-process "second `AgentSession` binding via a major agent framework,"** as a
  category. Evidenced twice now, independently (LangGraph → langchain-core → the
  langchain guard; CrewAI → chromadb → the posthog guard): modern agent frameworks ship
  batteries a sovereignty-guarded platform lock is specifically designed to reject. Retire
  this framing rather than retry it against a third framework (smolagents, AutoGen, etc.)
  on the same untested hope.

**The concrete first unit, ready to brief exactly like NE-LG-DROPIN was:**

`NE-CREWAI-DROPIN` — CrewAI-native governed drop-in, out-of-process via `ne-mcp`'s
`GovernedMcpServer`. Deliverables, entirely inside `packages/ne-mcp` (no new package,
mirroring the LangGraph unit's shape): a CrewAI-side sidecar agent in an isolated
hash-pinned venv (`crewai_env/`, built via `ne.envbuild.ensure_sidecar_venv`, the same
helper NE-LG-DROPIN used) whose tools are CrewAI `BaseTool` subclasses proxying to the
platform's governed MCP tools via a direct `mcp` client. **Correction verified 2026-07-22
(WebFetch against CrewAI's own docs, not assumed):** CrewAI's `MCPServerAdapter` lives in
the `crewai-tools` companion package, which §4.3 already ruled out (AGPL-3.0 `pymupdf` in
its base deps) — do NOT add `crewai-tools` to the sidecar. Build the `BaseTool` proxy
directly against the `mcp` SDK instead; CrewAI's own base closure already pins
`mcp~=1.28.1` (§4.1), matching the platform's own `mcp>=1.28.1,<2` pin, so this needs no
extra dependency beyond what CrewAI already carries. The same structural C6/C12
non-bypass argument as LangGraph's (§5.2's own conclusion) applies; and the mandatory
two-process live proof mirrors `test_langgraph_dropin_live.py`. **Size: S/M.**

## 9. What was ruled out and why (summary)

| Candidate mechanism | Verdict | Blocking fact |
| --- | --- | --- |
| CrewAI as in-process `AgentSession` binding (phases (b)/(d) as scoped) | **RULED OUT** | Blocker A: `crewai → chromadb~=1.1.0 (base) → posthog (base)` vs. the merge-blocking posthog ban (`test_no_graph_closure_in_lock.py:20-39`); Blocker B: four hard pin conflicts incl. `pydantic<2.13` vs. locked 2.13.4 — the closure cannot resolve into `uv.lock` at all |
| LangGraph as in-process binding | **RULED OUT** (previously, NE-LG-DROPIN) | `langgraph → langchain-core` vs. `test_ragas_isolation.py` langchain ban |
| Temporal as phase-(c) "second durable-execution binding" inside this epic | **RULED OUT as bundled** | runtime.py:8-13: durability is deliberately not a plugin slot; separate epic or engagement-time port (§7) |
| `crewai-tools` companion package (any venv that feeds our SBOM) | **RULED OUT** | AGPL-3.0 `pymupdf` in base deps; not needed for the core loop |
| CrewAI governed out-of-process via `GovernedMcpServer` | **FEASIBLE** (§8) | no platform-lock contact; same seam as NE-LG-DROPIN (merged, PR #276) |
| Conformance kits for runtime slots (phase (a)) | **FEASIBLE** (§8) | pure `ne-contracts` testing kits; template exists (`skill_substrate.py:41`) |

## 10. Verdict

**PARTIALLY FEASIBLE, RE-SCOPED.**

CrewAI is **ruled out** as an in-process `AgentSession` binding — hard, structural, two
independent blockers (§4.1, §4.2), the same class of fact that killed LangGraph in-process,
against a *different* pre-existing sovereignty guard (posthog/graph-closure, not
langchain). It would also have failed the C6 tool-interception bar on its own merits
(§5.2) had the dependency question not already closed it. The original epic's phase (c) —
Temporal as a plugin slot — is separately ruled out as *bundled*: it contradicts the
frozen contract's own documented stance that durability is deliberately not a slot
(runtime.py:8-13), and belongs in its own owner-gated epic if pursued at all.

What survives, and is genuinely buildable now: **(a)** the runtime-slot conformance kits
(S), and **(b)** CrewAI governed **out-of-process** via `GovernedMcpServer` — the exact
mechanism NE-LG-DROPIN proved for LangGraph, now extended to a second framework (S/M). A
re-scoped dual/triple-runtime governance swap test (pydantic-ai in-process + LangGraph and
CrewAI out-of-process, asserting identical C6/C7/C10/C12 behavior) is the honest successor
to the original phase (d)/(e), and is arguably the stronger proof: it is now evidenced
across three independently-delivered mechanisms, not two flavors of one binding shape.

**Recommendation:** proceed to build. First unit: `NE-CREWAI-DROPIN` (§8), briefed and
ready. The spec's NE-RTSWAP row needs an owner/orchestrator edit reflecting this
re-scope (§4.4) — this review does not touch the spec.
