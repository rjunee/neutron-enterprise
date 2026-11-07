# NE-LG-DROPIN — LangGraph-native governed drop-in (design)

**Status:** DESIGN — buildable, redesigned after the first build attempt was correctly BLOCKED
**Date:** 2026-07-21
**Predecessor event:** first build tried to bring `langgraph` + `langchain-core` into the
platform lock as an in-lock sibling package and collided with the sovereignty guard
(`packages/ne-eval/tests/test_ragas_isolation.py`). That collision is architectural, not
incidental — this design routes around it and turns the constraint into the headline win.

---

## 1. Goal and positioning

A customer keeps their existing `langgraph`-the-library (MIT) agents **unchanged as agents**
and drops Neutron AROUND them: Neutron is the open, self-hosted replacement for the paywalled
LangGraph Platform / enterprise layer, and it governs **every enterprise-effectful action**
their LangGraph agents take, non-bypassably.

Relationship to the rest of the roadmap:

- **This is NOT NE-RTSWAP.** NE-RTSWAP (epic, XL, BACKLOG, "do NOT build yet,
  owner-directed", scoped 2026-07-21) is "replace the entire agent runtime" — conformance
  kits, a second durable-execution binding (Temporal), a dual-runtime swap test. NE-LG-DROPIN
  is the **wedge in front of it**: the customer's LangGraph agent stays *their* runtime, in
  *their* process; Neutron supplies the governance plane. Nothing here forecloses or
  pre-builds RTSWAP phases (a)–(e).
- The spec already names "LangGraph server (commercial key)" as a rejected runtime
  lib precisely because of the paywalled control plane, and lists "customer's
  … LangGraph Platform" as a customer-purchasable alternate. NE-LG-DROPIN is the open path
  for the same audience.

### 1.1 Who this serves — correcting a common conflation

Two different things share the "LangGraph" name, and the distinction is the whole pitch:

- **`langgraph`, the OSS library (MIT, free)** — the agent framework. **The customer uses this.**
- **LangGraph Platform (ELv2 + a paid license key)** — LangChain's commercial deployment / governance / control-plane product. **Neutron replaces this.**

**The target customer is the one running FREE OSS langgraph who does NOT want to buy LangGraph Platform.** That is exactly who NE-LG-DROPIN serves. Neutron does **not** require the customer to hold the commercial Platform — it is the open, self-hosted *alternative* to it. What Neutron adds is precisely what LangGraph Platform charges for: the non-bypassable governance chokepoint (every action -> PDP + guardrails + immutable C10 audit), RBAC / permission-trimming, HITL approval tiers, the knowledge/brain layer, eval + flywheel, and deployment — all inside the customer's own VPC, no license key, no phone-home.

**Keeping `langgraph` out of Neutron's *own* lockfile is a sovereignty feature, not a restriction on who we serve.** The customer still runs free OSS langgraph; it simply runs in *their* process, and Neutron governs it from the outside (their process, our chokepoint, MCP between — see §3). Because Neutron never links against the framework: (1) our sovereign supply chain / SBOM / license gate stays clean of the langchain closure; (2) we govern *any* langgraph version the customer runs without re-releasing Neutron; (3) their framework's churn and CVEs stay outside our security perimeter. For the customer it is also *less* invasive than an in-process binding — no agent rewrite, no coupling to Neutron internals.

**Accuracy note for the positioning docs (reconcile these):** the whitepaper (§3.5 / §14) and master-positioning framing that "LangGraph fits the contract-swappable orchestrator slot" describes the *in-process* AgentSession-swap aspiration (NE-RTSWAP). For LangGraph specifically, the langchain-isolation sovereignty guard (`test_ragas_isolation.py`) makes an in-process swap infeasible without taking the langchain closure into the platform lock — so the **delivered** mechanism is **out-of-process governance via MCP** (this document). Any positioning that implies an in-process LangGraph binding should be reconciled to this out-of-process reality.

## 2. The collision that forced this design (verified)

- `packages/ne-eval/tests/test_ragas_isolation.py:28-34`
  (`test_uv_lock_has_no_ragas_scipy_or_langchain`) asserts, against the repo-root `uv.lock`,
  that `ragas`/`scipy` are absent and `langchain* == []` — the **entire** langchain closure
  is barred from the platform lock. The docstring names it the "Sovereignty guard … grep-0
  invariant": the heavy closure lives ONLY in an isolated hash-pinned dev venv
  (`packages/ne-eval/ragas_env/`, venv at `packages/ne-eval/.ragas-venv`, git-ignored via
  `packages/ne-eval/.gitignore`).
- `langgraph` depends on `langchain-core`. Therefore `langgraph` **cannot enter the platform
  `uv.lock`**, and no `packages/*` member of the uv workspace
  (root `pyproject.toml` `[tool.uv.workspace] members = ["packages/*", "apps/*", "tools/*"]`)
  may declare it. Full stop.
- Additional guard the first attempt would also have hit: langchain's own `langchain-core`
  0.3-line carries two HIGH advisories (CVE-2026-34070, CVE-2026-26013), which is why
  `packages/ne-eval/ragas_env/requirements.in` floors the isolated closure at
  `langchain-core>=1.2.22,<2`. Any langchain closure we ship anywhere must inherit that floor.

**Design consequence:** langgraph/langchain may exist in this repo only as an isolated,
hash-pinned sidecar-venv payload — exactly the ragas/docling/presidio/codegen pattern — and
the governed integration must happen over a **process boundary**, not an import.

## 3. The architecture: their process, our chokepoint, MCP in between

The platform already ships the seam. `packages/ne-mcp/README.md:3-8` describes `ne-mcp` as
"a thin, non-bypassable governed MCP adapter over the NE-033 C6 chokepoint
(`ne.runtime.middleware.GovernedToolExecutor`)" whose server surface, **`GovernedMcpServer`**,
"exposes the platform's governed tools to MCP clients."

Verified properties of that seam (all read from code, not the README alone):

- **Zero handlers on the adapter.** `packages/ne-mcp/src/ne/mcp/server.py:5-8`: the adapter
  "holds ZERO tool handlers and ZERO tool bindings. Its ONLY route from an inbound MCP
  `call_tool` to a tool effect is `await self._executor.execute(name, arguments,
  principal=self._principal)`" — the single NE-033 governed pipeline
  (guardrails → PDP → HITL → audit).
- **No pre-governance rejection.** `server.py:188` registers
  `@server.call_tool(validate_input=False)` ("LOAD-BEARING (C6)") so malformed/unknown calls
  still reach the audited governance path.
- **Unknown tools are audited-denied, never executed** (`ToolNotRegisteredError` is in
  `_GOVERNED_ERRORS`, `server.py:83-91`; exercised by
  `packages/ne-mcp/tests/test_server_governed.py`).
- **Tier ≥ 2 blocks on a named human approval** even over MCP (`server.py:20-22`; the
  executor routes through the HITL gate; a rejection surfaces as MCP `isError` with zero
  handler invocation).
- **No anonymous execution.** The principal is fixed at construction from the authenticated
  session and passed keyword-only into every `execute` (`server.py:148-164`) — a
  client-supplied argument can never become the principal.
- **Fail-closed infra faults** render as status-only sanitized `NE_MCP_*` codes; the raw
  exception never crosses the MCP boundary (`server.py:93-223`,
  `tests/test_server_fail_closed.py`).
- **Per-agent tool trimming exists**: `ToolsetScopedExecutor`
  (`packages/ne-runtime/src/ne/runtime/toolset.py:54-79`) is accepted by
  `GovernedMcpServer.__init__` (`server.py:150`), so the tool catalog advertised to a given
  LangGraph agent can be a fail-closed granted subset (empty grant = zero tools).
- **A real end-to-end template exists**: `packages/ne-skills/tests/test_mcp_end_to_end.py`
  drives a real `GovernedToolExecutor` + `GovernedMcpServer` through an in-process MCP client
  (`create_connected_server_and_client_session` from `ne.mcp._sdk`) and asserts the audit
  events land.

### The thesis, validated

**The customer's LangGraph agent runs in ITS OWN process/venv (with its langchain closure —
never in our lock) and connects to Neutron's `GovernedMcpServer` as an MCP client. Its entire
enterprise toolset is the platform's governed tools served over MCP.** Every tool call it
makes therefore routes through the C6 `GovernedToolExecutor` (deterministic guardrails → PDP →
HITL → audit) **by construction**: there is no tool for the agent to call that is not an MCP
proxy to the chokepoint, and the chokepoint's own non-bypass properties are already merged and
tested. Governance credentials, connector clients, and source-system reach exist **only on the
server side** of the pipe.

```
customer venv (isolated, hash-pinned;            platform venv (uv.lock; NO langchain*)
langgraph + langchain-core + adapters)
┌──────────────────────────────────┐   stdio    ┌───────────────────────────────────────┐
│ LangGraph StateGraph / agent     │  (MCP 1.x) │ GovernedMcpServer (ne-mcp, 0 handlers)│
│ tools = MCP proxies ONLY         │═══════════▶│  └─▶ GovernedToolExecutor (C6)        │
│ (langchain-mcp-adapters client)  │            │       guardrails → PDP → HITL(Tier-2/3)│
│ no connector creds, no ne.* code │            │       → C7 toolset trim → C10 audit   │
└──────────────────────────────────┘            └───────────────────────────────────────┘
```

### Honest trust-boundary statement (carry this into every customer doc)

The customer's agent **process** is outside our trust boundary. Neutron does not govern what
that process does locally (it can write its own files, call its own LLM). The claim — and the
thing the tests prove — is scoped and strong: **every action against enterprise systems**
(connectors, write-backs, knowledge plane, anything with credentials or effect) is governed,
because those capabilities exist only behind the C6 chokepoint and the agent's only route to
them is the governed MCP pipe. In a hardened deployment C2 egress policy additionally pens the
agent's runtime environment, but that is deployment posture, not a property this unit builds.

## 4. Decision 1 — artifact shape: NOT a new platform package

**NE-LG-DROPIN is an integration pattern + runnable demo + guard tests, delivered inside
`ne-mcp`. No new workspace package.** Honest reading of the code: the governance is already
built (`ne-mcp` + `ne-runtime`); what is missing is (a) a runnable composition root that
serves `GovernedMcpServer` over a real transport — `run_stdio` (`server.py:229-237`) is
currently a "LIVE-PARKABLE" production entry with **zero callers** outside tests — (b) the
isolated customer-side venv + agent, and (c) the proofs. That is M-sized glue, not an
L-sized package.

Concrete artifact layout:

| Artifact | Path | Venv it runs in |
| --- | --- | --- |
| Demo composition root + stdio entry | `packages/ne-mcp/src/ne/mcp/demo_server.py` (runnable as `python -m ne.mcp.demo_server`) | platform `.venv` |
| Demo driver (orchestrates the whole flow) | `packages/ne-mcp/src/ne/mcp/langgraph_demo.py` (runnable as `python -m ne.mcp.langgraph_demo`) | platform `.venv` |
| Isolated venv payload: pins + agent | `packages/ne-mcp/langgraph_env/{requirements.in, requirements.txt, agent.py, README.md}` | sidecar venv |
| Sidecar venv (git-ignored) | `packages/ne-mcp/.langgraph-venv/` + `packages/ne-mcp/.gitignore` (mirror `packages/ne-eval/.gitignore`: `.ragas-venv/` + `.ragas-venv.lock`) | — |
| Lock-sovereignty guard test | `packages/ne-mcp/tests/test_langgraph_isolation.py` | platform CI, always |
| Composition/non-bypass tests | `packages/ne-mcp/tests/test_demo_server_composition.py` | platform CI, always |
| Live end-to-end (skip-if-unavailable) | `packages/ne-mcp/tests/test_langgraph_dropin_live.py` | platform CI `live` lane |

Why inside `ne-mcp` and not a top-level `demo/` dir: pytest only collects
`packages/*/tests` and `tools/*/tests` (root `pyproject.toml` `testpaths`), so the guard and
live tests must live in a package; `ne-mcp` is the package whose seam this exercises, and the
`langgraph_env/` payload directory mirrors `packages/ne-eval/ragas_env/` exactly (that
directory is neither `src/` nor `tests/`, so it is outside the mcp-SDK grep guardian's scan
globs — see §6 — and outside ruff/pyright `src` scopes, same as `ragas_env/cross_check.py`,
which freely imports the ragas/langchain closure today).

`demo_server.py` composes with existing factories only (no new governance code):
`freeze_platform_config(...)` once at boot, `create_shared_guardrail_state()`,
`create_approval_gate(...)` over a durable SQLite engine, and
`create_tool_executor(bindings, sink=..., approval_gate=..., ledger=..., counter=...)`
per the normative recipe in `packages/ne-runtime/src/ne/runtime/factory.py` (docstring
lines 6-47 — the factory internally constructs PDP + guardrails over the ONE sink, so the
executor "cannot be assembled without its full governance stack (C6)"). The tool catalog it
registers:

- `read.kb.search` — a demo-local Tier-0 read binding over a seeded in-memory corpus
  (handler lives server-side in `demo_server.py`; a handler behind the executor is the
  governed place for handlers).
- `write.internal.crm.note` — the existing Tier-2 reference action
  (`packages/ne-runtime/src/ne/runtime/reference_actions/crm_note.py`: "declared at the
  Tier-2 write floor — … a single named approval clears it"), injected with a demo
  `CrmNoteClient` stub that records + prints the applied write (clearly labeled a demo
  connector stub — honesty bar).
- `write.external.ticket.update` — the Tier-3 reference action
  (`reference_actions/ticket_update.py`) for the "rejected approval" beat.

Wrapped in `ToolsetScopedExecutor` with an explicit grant so the demo also shows C7 trimming
(a tool the agent was not granted is invisible in `list_tools` and fail-closed on call).

## 5. Decision 2 — MCP wiring on the customer side

**`langchain-mcp-adapters` (MIT, langchain-ai) in the isolated venv; stdio transport in v1.**

- Client side (customer venv): `langchain_mcp_adapters` converts MCP tools into LangChain
  tools consumable by a LangGraph graph (`MultiServerMCPClient` / `load_mcp_tools`). It is
  the canonical way a LangGraph agent consumes an MCP server, it depends on
  `langchain-core` + `mcp` — both of which are fine **in the sidecar venv only** — and using
  it (rather than the raw MCP SDK) is exactly what a real customer would do, which is the
  point of the demo. Exact versions are chosen at `uv pip compile` time with the
  `langchain-core>=1.2.22,<2` security floor carried over from
  `packages/ne-eval/ragas_env/requirements.in` (same advisories, same closure). Verify the
  adapter's current API at pin time; it is an external fact, not a repo fact.
- Server side (platform venv): `GovernedMcpServer.run_stdio()` already exists
  (`server.py:229-237`) and `ne.mcp._sdk` already exports `stdio_server`
  (`_sdk.py:43`). Per MCP stdio convention the **client spawns the server command**: the
  agent's MCP client config points at `<repo>/.venv/bin/python -m ne.mcp.demo_server`
  (command + args + env). The two closures never share an interpreter; the wire protocol is
  the only contract. The sidecar venv resolves its own `mcp` client version — protocol
  interop, not a shared pin, is what couples them, so the platform's `mcp>=1.28.1,<2` pin
  (`packages/ne-mcp/pyproject.toml`) is untouched.
- **Streamable-HTTP is deliberately v1.1, not v1.** `ne.mcp._sdk` exports the *client*-side
  `streamablehttp_client` (`_sdk.py:41`) but does NOT export the server-side streamable-HTTP
  machinery, and adding it means widening the single-module SDK chokepoint
  (`ne/mcp/_sdk.py`, guarded by `tests/test_mcp_isolation_guardian.py` and the
  `c3-mcp-sdk-chokepoint` import-linter contract, README:107-112) plus building session auth
  → principal mapping on ne-api. That is the real "customer agents connect over the network"
  deployment shape, and it is honest to defer it: v1 proves governance; transport hardening
  is a follow-on unit (see §10 Open decisions).

## 6. Decision 3 — isolation mechanics (mirror the ragas pattern exactly)

- **Payload:** `packages/ne-mcp/langgraph_env/requirements.in` pins `langgraph`,
  `langchain-mcp-adapters`, and (for the optional model-driven demo mode) a chat-model
  binding; `requirements.txt` is generated with
  `uv pip compile packages/ne-mcp/langgraph_env/requirements.in --universal
  --python-version 3.12 --generate-hashes -o packages/ne-mcp/langgraph_env/requirements.txt`
  (the exact recipe documented in `packages/ne-eval/ragas_env/README.md`).
- **Builder:** the driver calls `ne.envbuild.ensure_sidecar_venv` (
  `packages/ne-envbuild/src/ne/envbuild/sidecar.py:176` — cross-process filelock, atomic
  marker, authoritative under-lock recheck) with `env_dir=packages/ne-mcp/langgraph_env`,
  `venv_dir=packages/ne-mcp/.langgraph-venv`, full-closure install (`--require-hashes`, no
  `--no-deps` — the ragas shape asserted in
  `packages/ne-eval/tests/test_ragas_env_lock.py:63-66`), and the same fail-closed `AIR_GAP`
  `pre_lock_check` hook the ragas oracle passes (`test_ragas_env_lock.py:185-193`). Add a
  small `test_langgraph_env_lock.py` asserting the delegation shape, mirroring
  `test_ne_eval` ragas' NE-CKC13 test (never spawns real `uv`).
- **Never in the SBOM/lock:** like ragas ("never enters the platform `uv.lock`/SBOM and
  never trips the C4 license gate", `ragas_env/README.md`), the sidecar closure is not a
  platform dependency. All three top pins are MIT regardless.
- **The guard test (mandatory, platform CI):**
  `packages/ne-mcp/tests/test_langgraph_isolation.py`, a structural sibling of
  `test_ragas_isolation.py`, asserting against the repo-root `uv.lock` name set:
  1. no name starts with `langgraph` — **this is NEW coverage**: the existing ragas guard
     asserts `langchain*` only; `langgraph`, `langgraph-checkpoint`, `langgraph-sdk`,
     `langgraph-prebuilt` would all slip past it today;
  2. `langchain* == []` (redundant with ne-eval's guard by design — two packages now
     depend on this invariant, each must fail loudly on its own);
  3. `packages/ne-mcp/pyproject.toml` declares no `langgraph*`/`langchain*` dependency.
- **mcp-SDK chokepoint (C3) interplay:** `tests/test_mcp_isolation_guardian.py:28-35` scans
  `packages/*/src/**/*.py` and `packages/*/tests/**/*.py` for `import mcp` — the new
  `demo_server.py`/`langgraph_demo.py` (in `src/`) therefore MUST touch the SDK only via
  `ne.mcp._sdk`/`GovernedMcpServer`, which they naturally do. `langgraph_env/agent.py` is
  outside both scan globs (same standing as `ragas_env/cross_check.py`); it should still
  import only `langgraph` + `langchain_mcp_adapters` (never bare `mcp`, never `ne.*`) — and
  the live test asserts the `ne.*`-free property behaviorally by running it in a venv where
  no `ne` distribution is installed.

## 7. Decision 4 — the C6/C12 non-bypass proof (headline deliverable)

**Structural argument** (each leg traces to a merged, tested property):

1. The agent's tool universe is exactly `list_tools` from `GovernedMcpServer`, which
   advertises "SPECS ONLY … exactly the executor's registered specs" (`server.py:175-186`).
   The agent process contains no connector clients, no credentials, no `ne.*` imports.
2. Every `call_tool` — known, unknown, malformed — reaches
   `GovernedToolExecutor.execute` (`server.py:5-8, 188-227`; `validate_input=False`
   load-bearing). There is no second path: the adapter holds zero handlers.
3. Tier-2/3 actions block on the real `DurableApprovalGate`
   (`packages/ne-runtime/src/ne/runtime/approvals.py:94`, `request_approval`/`approve` with
   a named approver); a rejection is an `isError` with zero handler invocations.
4. Every decision and effect lands in the hash-chained C10 audit log; `ne audit verify`
   detects tamper/deletion/reorder (C10, whitepaper constraint table row C10).
5. Therefore a LangGraph agent wired this way has **no ungoverned path to an enterprise
   effect and no path to a Tier-3 action without a named human** (C12) — not because the
   agent is trusted, but because the only capabilities it has are governed proxies.

**Concrete tests** (behavioral — per the repo rule that normative claims trace to a TEST,
not an argument):

- `test_demo_server_composition.py` (platform CI, in-memory MCP transport via
  `create_connected_server_and_client_session`, no langchain anywhere — the
  `test_mcp_end_to_end.py` template):
  - `list_tools` over the composed demo server == the granted toolset, nothing more
    (C7 trim visible: an ungranted registered tool is absent and fail-closed on call);
  - `call_tool("shell.exec", ...)` (unregistered) → `isError` +
    `ToolNotRegisteredError` surface + audit events present, zero effects;
  - `call_tool("write.internal.crm.note", ...)` with a rejecting gate → `isError`, the
    recording `CrmNoteClient` stub saw **zero** calls, `approval.requested` +
    rejection audited;
  - same call with a named approval → executes once, `pdp.decide` → `approval.requested`
    → `tool.approval` → `tool.execute` all present with a trace id.
- `test_langgraph_dropin_live.py` (skip-if-unavailable, mirroring
  `packages/ne-eval/tests/test_ragas_oracle.py`'s live-oracle pattern): builds the sidecar
  venv, runs the REAL `agent.py` (deterministic mode, §8) as a subprocess, which spawns the
  real stdio server; then asserts from the platform side, against the shared SQLite
  audit/approval stores: the agent's discovered tool count == granted specs; the scripted
  rogue call was audited-denied; the Tier-2 write executed only after the driver's named
  approval; `AuditSink.verify` returns valid over the produced chain. This is the
  **cross-venv, two-process** proof — the thing no in-memory test can claim.
- Per the repo's parked-live-check rule: this live test (or `python -m ne.mcp.langgraph_demo`
  end-to-end) must be RUN and shown green **before** the unit's PR is called ready-to-merge —
  green CI on the always-on lane alone does not prove the sidecar venv resolves and the
  two-process wiring works.

## 8. Decision 5 — the runnable demo scenario

`python -m ne.mcp.langgraph_demo` (platform venv) does, in order:

1. **Build** the sidecar venv (`ensure_sidecar_venv`, §6) — first run only.
2. **Launch** `agent.py` inside `.langgraph-venv`, passing the server command
   (`<platform-python> -m ne.mcp.demo_server`) and the shared state dir (SQLite audit +
   approval stores) via argv/env. The agent's MCP client spawns the governed server over
   stdio.
3. **Agent runs a multi-step task** (a real LangGraph `StateGraph`): research a customer
   account (`read.kb.search`, Tier-0 — executes, audited) → compose a CRM note → call
   `write.internal.crm.note` (Tier-2).
4. **The write BLOCKS on HITL**: the executor submits to `DurableApprovalGate`; the driver
   polls `list_pending()` on the shared store, prints the approval card (action, args
   summary, tier), and prompts the operator. Approval is recorded with a **named approver**
   via `gate.approve(request_id, approver=...)` (`approvals.py:153`). The blocked
   `await_outcome` resolves; the note lands in the (clearly labeled) demo connector stub.
5. **Rogue beat**: the graph's final node deliberately calls `shell.exec` (not a
   registered tool) and, in a second leg, `write.external.ticket.update` (Tier-3) which the
   operator REJECTS — both surface as `isError` to the agent, both are audited, neither
   executes anything.
6. **Receipts**: the driver prints the audit trail for the run
   (`pdp.decide` / `approval.requested` / `tool.approval` / `tool.execute` /
   unknown-tool denial) and runs chain verification (C10) over the produced log.

Two agent modes, one graph shape:

- **Deterministic mode (default; what the live test runs):** the LangGraph nodes call tools
  directly with scripted inputs — no LLM at all. Hermetic, CI-safe, zero egress; proves the
  governance wiring, which is the point of the unit.
- **Model mode (`--model`, the "wow" path):** the same toolset behind
  `langgraph.prebuilt` react-style agent with a chat model. Which model endpoint the demo
  venv talks to (the demo rig's local OpenAI-compatible serving endpoint vs. a frontier key
  from the customer side) is an open decision (§10) — it changes the C2 egress story of the
  *demo environment*, not the governance claim.
- Wiring into the `ne demo` narrated arc (`tools/ne-cli/src/ne/cli/demo/arc.py` speaks only
  HTTP to ne-api) is explicitly out of scope for v1 — the arc is HTTP-only by design and
  this demo is a process-spawning flow; a later rider can add an arc beat once the
  streamable-HTTP transport exists.

## 9. Buildable unit plan

**Unit:** NE-LG-DROPIN — size **M** (down from the original L: governance, MCP server,
approval gate, sidecar-venv builder, and isolation guards all already exist and are cited
above; the unit is composition + payload + proofs).

**Deliverables**

1. `packages/ne-mcp/src/ne/mcp/demo_server.py` — governed composition root + stdio
   `__main__` (freeze → shared guardrail state → gate → `create_tool_executor` →
   `ToolsetScopedExecutor` grant → `GovernedMcpServer.run_stdio`).
2. `packages/ne-mcp/src/ne/mcp/langgraph_demo.py` — driver (venv ensure, agent launch,
   approval prompt loop, audit receipt + chain verify).
3. `packages/ne-mcp/langgraph_env/` — `requirements.in` (langgraph,
   langchain-mcp-adapters, optional model binding; `langchain-core>=1.2.22,<2` floor),
   hash-pinned `requirements.txt`, `agent.py` (deterministic + `--model` modes),
   `README.md` (the customer-facing integration pattern, with the §3 trust-boundary
   statement verbatim).
4. `packages/ne-mcp/.gitignore` — `.langgraph-venv/` + `.langgraph-venv.lock`.
5. Tests: `test_langgraph_isolation.py` (lock guard incl. NEW `langgraph*` assertion),
   `test_langgraph_env_lock.py` (envbuild delegation shape),
   `test_demo_server_composition.py` (in-memory non-bypass suite),
   `test_langgraph_dropin_live.py` (skip-if-unavailable two-process proof).
6. `packages/ne-mcp/README.md` — new "LangGraph drop-in" section pointing at
   `langgraph_env/README.md`.
7. `packages/ne-mcp/pyproject.toml` — ONE new dependency edge: `ne-envbuild`
   (workspace leaf, sole third-party dep `filelock>=4.2.1,<5`, already in the platform
   lock via ne-brain/ne-govern/ne-schema/ne-eval), needed by `langgraph_demo.py` for
   `ensure_sidecar_venv`. Plus the corresponding `uv.lock` regeneration (workspace edge
   only — **zero new third-party names** enter the lock).

**mayTouch globs**

```
packages/ne-mcp/src/ne/mcp/demo_server.py
packages/ne-mcp/src/ne/mcp/langgraph_demo.py
packages/ne-mcp/langgraph_env/**
packages/ne-mcp/.gitignore
packages/ne-mcp/pyproject.toml
packages/ne-mcp/tests/test_langgraph_isolation.py
packages/ne-mcp/tests/test_langgraph_env_lock.py
packages/ne-mcp/tests/test_demo_server_composition.py
packages/ne-mcp/tests/test_langgraph_dropin_live.py
packages/ne-mcp/README.md
uv.lock
docs/architecture/ne-lg-dropin-design.md
```

Downstream-assertion sweep (per the unit-brief rule): the `uv.lock` touch adds only the
`ne-mcp → ne-envbuild` workspace edge, so the name-set sovereignty guards
(`test_ragas_isolation.py`, and this unit's own `test_langgraph_isolation.py`) stay green —
the lock guard asserts package NAMES, not edges; the mcp guardian
(`test_mcp_isolation_guardian.py`) scans the two new `src/` modules — they import the SDK
only via `ne.mcp._sdk`, so it stays green; no dev-group additions (the in-memory suite
needs nothing new). If the builder finds any other guardian asserting on
`packages/ne-mcp` file lists or on `uv.lock` shape, grep first and extend mayTouch before
dispatch.

**Invariants (merge-blocking)**

- **C6:** every agent-originated call reaches `GovernedToolExecutor.execute`
  (in-memory suite + live test).
- **C12:** the scripted hijack legs (unknown tool, unapproved Tier-2, rejected Tier-3)
  produce zero effects and full audit (both suites).
- **C10:** chain verification passes over the demo-produced log (live test / demo step 6).
- **C7:** the ungranted-tool leg is invisible and fail-closed (in-memory suite).
- **Langchain sovereignty:** `langgraph*` AND `langchain*` absent from platform `uv.lock`
  (new guard test — the load-bearing new assertion is `langgraph*`).
- **C3 (mcp chokepoint):** no new direct `mcp` import in `packages/*/src|tests`
  (existing guardian, unmodified).

**Out of scope (honest)**

- Temporal durability / any NE-RTSWAP phase (conformance kits, second durable-execution
  binding, dual-runtime swap test) — RTSWAP is backlog, owner-gated.
- Streamable-HTTP server transport + session-auth→principal mapping on ne-api (v1.1 unit;
  requires widening `ne/mcp/_sdk.py` + the import-linter contract).
- LangGraph checkpoint/persistence governance (their checkpointer is their state, outside
  the trust boundary — say so, don't govern it).
- `ne demo` arc integration and console approval-card wiring for this flow (the driver's
  terminal approval is a real named approval through the same `DurableApprovalGate`; the
  console surface is a rider once HTTP transport exists).
- Any change to `packages/ne-eval` (its guard stays authoritative for its own package).

## 10. Open decisions (orchestrator/owner before build)

1. **Demo model endpoint for `--model` mode:** demo-rig local serving endpoint
   (sovereign, no egress) vs. customer-side frontier key (realistic for the audience,
   needs the C2 framing sentence). Default if undecided: local endpoint; `--model` off in
   CI either way.
2. **Live-lane CI wiring:** should `test_langgraph_dropin_live.py` run in the `live` CI job
   (needs network for the one-time sidecar-venv build, like the ragas oracle) or stay
   dev-run-before-merge only? Default: mirror whatever `test_ragas_oracle.py` does today.
3. **Naming/positioning doc:** whether `langgraph_env/README.md` graduates into a
   customer-facing "replace LangGraph Platform" one-pager (whitepaper §-level claim needs
   owner wording review — it is a competitive claim, not just a technical one).
4. **v1.1 streamable-HTTP unit** (`NE-LG-DROPIN-HTTP`): green-light criteria — the mcp v2
   migration window (~2026-08, README:107-112) may argue for doing transport work after the
   re-pin rather than before.

## 11. Key design win (the honesty bar)

The wedge is proven **without taking langchain into the sovereign core**. The first build's
instinct — "to govern LangGraph we must import LangGraph" — was exactly backwards: because
`GovernedMcpServer` holds zero handlers and the executor is the only route to any effect,
Neutron can govern an agent runtime **it never links against**. The sovereignty guard that
blocked the first attempt is not an obstacle the design works around; it is the property
that makes the pitch credible: the customer's framework churn (langgraph versions, langchain
CVEs, the mcp v2 migration on *their* side) stays in their venv, while the governance plane —
lock, SBOM, license gate, audit chain — is structurally unable to absorb it. Every normative
claim above traces to a file read during this design (§2, §3, §6, §7 citations).
