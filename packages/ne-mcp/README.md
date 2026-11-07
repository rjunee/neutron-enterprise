# ne-mcp

A **thin, non-bypassable governed MCP adapter** over the NE-033 C6 chokepoint
(`ne.runtime.middleware.GovernedToolExecutor`). Two surfaces, both governed by
construction:

- **`GovernedMcpServer`** — an MCP *server* that exposes the platform's governed
  tools to MCP clients.
- **`ExternalMcpToolProvider`** — an MCP *client* that consumes tools from a private
  in-VPC catalog of trusted external MCP servers. Its streamable-HTTP transport
  (`mcp>=1.30.0`) follows a catalog endpoint's redirect only within that endpoint's
  origin (same scheme, host and port, or http→https on the default ports), so catalog
  endpoints must not redirect off their origin.

All `mcp` SDK imports are isolated behind `ne/mcp/_sdk.py` (the official
`mcp` SDK is pinned `>=1.30.0,<2`).

## C6 no-bypass posture (the crux)

The server holds **zero tool handlers and zero bindings**. Every inbound
`call_tool` — known tool, unknown tool, well-formed or malformed arguments — is
routed through the ONE `GovernedToolExecutor` (guardrails → PDP → HITL → audit).
There is no code path off the adapter that reaches a tool effect without the
executor.

- **`@call_tool(validate_input=False)` is load-bearing.** With the SDK default
  (`True`), the lowlevel server jsonschema-rejects malformed arguments *before* our
  governance runs — recreating the exact **unaudited pre-governance rejection** class
  that bounced NE-033 (the pydantic-ai `ToolManager` pre-validation). With `False`,
  every request reaches the audited governance path.
- **Unknown tools** get the NE-033 discipline: `tool.unknown` audit + guardrails +
  `pdp.decide` + terminal rejection — never executed.
- **Tier-3 over MCP still requires a named human approval.** A rejected approval
  surfaces as an MCP `isError` result with zero handler invocations.
- **No anonymous execution.** The principal is established at construction from the
  authenticated session (one adapter instance per authenticated caller) and passed
  keyword-only into every `execute` — a client-supplied argument can never become the
  principal.
- **Redaction-disciplined errors.** Ordinary governance/handler errors render as
  `isError` results whose text is `"{ErrorClass}: {msg}"` (tool names / tiers / statuses /
  error classes only). Fail-closed governance-**infra** faults
  (`RuntimeAuditError` / `AuditEmissionError`, `GuardrailEvaluationError`,
  `PolicyBackendError`) are instead **caught at the adapter** and rendered as status-only
  sanitized `isError` results with stable codes (`NE_MCP_AUDIT_UNAVAILABLE` /
  `NE_MCP_GUARDRAIL_UNAVAILABLE` / `NE_MCP_POLICY_UNAVAILABLE` /
  `NE_MCP_EXECUTION_UNAVAILABLE`) — the raw exception string never crosses the MCP
  boundary (the mcp 1.x lowlevel server would otherwise stringify a propagated
  exception into the client-visible error via
  `except Exception: return _make_error_result(str(e))`). The result remains withheld and
  the tool never executes; infra failures other than the audit sink itself are still
  audited by the engine/PDP before the adapter responds, and a final defense-in-depth
  `except Exception` guard closes the residual `str(e)` leak for any unexpected class.
  That final guard also logs the raw exception **server-side** (stdlib `logging`, logger
  `ne.mcp.server`, with traceback) for diagnosability, while the client still sees only
  `NE_MCP_EXECUTION_UNAVAILABLE` — the raw detail never crosses the boundary.
- **Catalog computed once, at construction.** The tool catalog advertised via
  `list_tools` is built once, at construction, into a frozen snapshot; the list-tools
  path serves a copy of that snapshot and is raise-free. This closes invariant-audit
  Finding D: the mcp SDK's tool-cache refresh re-invokes the list-tools handler
  *before* the sanitized `call_tool` chain above runs, inside its own `str(e)`
  catch-all, so a raising list-tools handler would otherwise leak a raw exception at
  that seam (pinned by `test_list_tools_catalog_is_stable_and_matches_specs_exactly`
  and `test_tool_cache_refresh_path_cannot_leak_raw_exception`).

## C2 in-VPC catalog rules

`validated_catalog(settings)` validates every catalog endpoint fail-loud at
composition time via `ne.net.validate_in_vpc_endpoint`: cloud-metadata hosts,
packed-integer IPv4, embedded credentials, and external hosts are **rejected**; private
IPs, `localhost` / `::1`, dotless compose service names, `.local` / `.localhost` /
`.internal` suffixes, and explicitly allowlisted private-DNS hosts are **accepted**.
Duplicate names and any tier below the Tier-2 floor are refused.

**Unforgeable by construction (Codex P2).** `validated_catalog` returns
`ValidatedCatalogEntry` values — a proof-carrying subclass whose own pydantic
before-validator runs `validate_in_vpc_endpoint`, so **every** construction path
(including direct instantiation) validates + normalizes the endpoint; an instance
holding an unvalidated endpoint is *unrepresentable* (mirroring NE-046's pattern).
`ExternalMcpToolProvider` accepts **only** a `ValidatedCatalogEntry` — a raw
`McpServerCatalogEntry` (or an element of raw `McpSettings.catalog`) is rejected at
`__init__` with a `TypeError` — and it **re-runs** the validator as defense-in-depth.
That re-run (NE-CLEAN1 hardening) consults **only** a `trusted_allowed_hosts`
allowlist supplied explicitly by the composition root (default: `()`, i.e. nothing
beyond the built-in in-VPC rules) — **never** the entry's own (`model_copy`-forgeable)
`allowed_hosts` field. So a `model_copy(update={"endpoint": ..., "allowed_hosts": ...})`
forgery that bundles its own matching allowlist alongside a smuggled external endpoint
is still caught: the forged field is never the thing being trusted. A composition root
that needs a per-server operator-declared host must pass its own settings-derived
`trusted_allowed_hosts` explicitly. C2 in-VPC validation is therefore unbypassable by
construction, not by caller discipline.

## External-tool namespacing and tier floor

Discovered external tools are surfaced ONLY as `ToolBinding` objects namespaced
`write.external.mcp.<server>.<tool>`. The `write.external.` prefix pins the class tier
to **2** (`ne.runtime.actions.tier_for_action`); a `.pii.` substring can only
*escalate*, never lower. Each binding carries `extract_egress_urls` returning the server
endpoint (bound via a closure default so nothing model-controlled can shadow it), so the
C12 egress-host allowlist can block an external tool before the remote is ever
contacted. The provider exposes **no** public call/execute method — its only output is
governed bindings.

## Tasks extension — DEFERRED to v1.1

Probe-verified 2026-07-06 on `mcp==1.28.1`:

- the task request/result types (`tasks/get`, `tasks/result`, `tasks/cancel`,
  `tasks/list`, `TASK_*` constants) exist in `mcp.types`;
- **but** the server-side implementation lives ONLY under
  `mcp.shared.experimental.tasks`;
- `mcp.server.tasks` is **absent**.

Building governed long-running semantics on an explicitly *experimental* API
contradicts the `<2` stability pin, so the Tasks extension is **not built** here.
Re-evaluate at the v2/stable re-pin.

## v2 migration containment

The ~2026-08 `mcp` v2 migration is contained to a single module: rewrite the imports in
`ne/mcp/_sdk.py`. This is enforced two ways: the grep guardian
(`tests/test_mcp_isolation_guardian.py`, scanning `packages/*/src` **and**
`packages/*/tests`) and the `c3-mcp-sdk-chokepoint` import-linter contract at
`/.importlinter` (sole ignored edge `ne.mcp._sdk -> mcp`).

## LangGraph drop-in (out-of-process, governed by construction)

NE-LG-DROPIN adds a runnable, tested demonstration that a customer's **LangGraph**
agent can be governed by Neutron **without Neutron ever linking against langchain**.
It lives entirely inside this package — no new workspace package.

**The seam (structural non-bypass).** `GovernedMcpServer` holds **zero handlers**, and
`@call_tool(validate_input=False)` is load-bearing: every inbound `call_tool` — known,
unknown, malformed — is forwarded straight to the C6 `GovernedToolExecutor`. A LangGraph
agent whose **entire** enterprise toolset is served by this server therefore has no
ungoverned path to any effect. Governance credentials, connector clients, and
source-system reach exist only on the server side of the MCP pipe; the agent process
holds none of them.

**Out-of-process by necessity, and that is the win.** langgraph depends on
langchain-core, and the platform's sovereignty guards bar every `langchain*`/`langgraph*`
name from the platform `uv.lock`. So the customer's agent runs in its **own isolated,
hash-pinned venv** (`langgraph_env/` payload; git-ignored `.langgraph-venv`) and connects
to `GovernedMcpServer` over MCP **stdio** as a client. The first build's instinct — "to
govern LangGraph we must import LangGraph" — was backwards: because the executor is the
only route to any effect, Neutron governs a runtime it never links against.

**Artifact map.**

- `src/ne/mcp/demo_server.py` — the governed composition root + stdio entry
  (`python -m ne.mcp.demo_server`); composes with existing factories only (no new
  governance code) and wraps the executor in a `ToolsetScopedExecutor` grant (C7).
- `src/ne/mcp/langgraph_demo.py` — the platform-side driver
  (`python -m ne.mcp.langgraph_demo [--auto] [--model]`): builds the sidecar venv via
  `ne.envbuild.ensure_sidecar_venv`, launches the agent, records the named approval,
  prints the C10 receipts, and verifies the chain.
- `langgraph_env/` — the isolated payload (`requirements.in/.txt`, `agent.py`, its own
  README with the trust-boundary statement and the run/compile recipe).
- Tests: `test_langgraph_isolation.py` (lock sovereignty), `test_langgraph_env_lock.py`
  (envbuild delegation shape), `test_demo_server_composition.py` (in-memory C6/C12/C7
  non-bypass suite), `test_langgraph_dropin_live.py` (the two-process cross-venv proof;
  run-before-merge).

**Sovereignty invariant (headline).** Nothing matching `langgraph*`/`langchain*` may
enter the platform `uv.lock` — enforced by `tests/test_langgraph_isolation.py` (the NEW
`langgraph*` assertion is load-bearing: `langgraph-checkpoint`/`-sdk`/`-prebuilt` would
slip past a `langchain*`-only guard) and a redundant `langgraph*` assertion added to
`packages/ne-eval/tests/test_ragas_isolation.py`. The only platform-lock change this unit
makes is two workspace edges (`ne-mcp → ne-envbuild`, `ne-mcp → ne-audit`) with zero new
third-party names.

## CrewAI drop-in (second governed drop-in — NE-RTSWAP re-scoped phase (b))

NE-CREWAI-DROPIN adds a **second** governed out-of-process drop-in, for a customer's
**CrewAI** agent, mirroring the LangGraph unit's shape exactly. The seam is IDENTICAL and
`demo_server.py` is **REUSED byte-unchanged** — the same governed composition root is now
proven against a SECOND, independently-built out-of-process client. That is the stronger
"any stack" claim: the governance invariant holds across a genuinely different framework,
not two flavors of one binding shape.

**Why in-process CrewAI is dead (three independent ways, verified 2026-07-21).**

- crewai's base deps pull `chromadb~=1.1.0`, whose base deps pull **`posthog`** — barred
  from the platform lock forever by `packages/ne-memory-graph/tests/test_no_graph_closure_in_lock.py`
  (and, redundantly, by `tests/test_crewai_isolation.py`); crewai also pulls `lancedb` and
  `instructor`;
- **four hard pin conflicts** (`pydantic<2.13` vs locked 2.13.4, `regex~=2026.1.15` vs
  2026.6.28, `portalocker~=2.7.0` vs 3.2.0, `opentelemetry-*~=1.42.0` vs sdk 1.43.0) — the
  closure cannot even resolve into the workspace lock;
- CrewAI's own tool-interception layer has three NE-033-class hook gaps (ReAct hallucinated
  names, fuzzy tool-name matching, an LLM-layer execution path) — worse than pydantic-ai's.

**All three are irrelevant across the process boundary.** The customer's agent runs in its
**own isolated, hash-pinned venv** (`crewai_env/` payload; git-ignored `.crewai-venv`) and
connects to `GovernedMcpServer` over MCP **stdio** as a client. Its BaseTool proxies are
built against the mcp SDK **directly** (crewai-tools is excluded — AGPL-3.0 pymupdf in its
base deps). Whatever CrewAI's dispatch layer does locally, the agent has no ungoverned
capability: every enterprise effect crosses the MCP pipe into the C6 executor.

**Artifact map.**

- `src/ne/mcp/crewai_demo.py` — the platform-side driver
  (`python -m ne.mcp.crewai_demo [--auto] [--model]`), mirroring `langgraph_demo.py`;
  builds the sidecar venv via `ne.envbuild.ensure_sidecar_venv`, launches the agent,
  records the named approval, prints the C10 receipts, and verifies the chain.
- `crewai_env/` — the isolated payload (`requirements.in/.txt`, `agent.py`, its own README
  with the trust-boundary statement and the run/compile recipe).
- Tests: `test_crewai_isolation.py` (lock sovereignty), `test_crewai_env_lock.py` (envbuild
  delegation shape), `test_crewai_dropin_live.py` (the two-process cross-venv proof;
  run-before-merge). The existing `test_demo_server_composition.py` in-memory C6/C12/C7
  suite continues to cover the shared composition root unchanged.

**Sovereignty invariant (headline).** Nothing matching
`crewai*`/`chromadb*`/`posthog`/`lancedb*`/`instructor` may enter the platform `uv.lock` —
enforced by `tests/test_crewai_isolation.py` (with the `posthog` assertion deliberately
redundant with the ne-memory-graph guard). **This unit makes ZERO platform-lock changes**:
the `ne-mcp → ne-envbuild` / `ne-mcp → ne-audit` workspace edges already exist from
NE-LG-DROPIN, so `uv.lock` is untouched.

## Residual (out of scope for NE-049)

Per-channel provenance tagging of MCP-originated calls (adapter-level audit
enrichment — e.g. stamping "arrived via MCP") is deferred to the Phase-5/6 composition
root, where the authenticated MCP session and the platform config-hash freeze are wired.
