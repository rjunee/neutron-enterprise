# CrewAI governed drop-in — isolated demo venv (NE-CREWAI-DROPIN)

This directory is the **isolated, hash-pinned virtualenv payload** for the
customer-side CrewAI agent in the Neutron governed drop-in — the **second** governed
out-of-process drop-in (NE-RTSWAP re-scoped phase (b)), mirroring `langgraph_env/`
file-for-file. It mirrors the isolation pattern `packages/ne-eval/ragas_env/` uses for
ragas: the crewai closure is installed into a dedicated, git-ignored venv
(`packages/ne-mcp/.crewai-venv`) and run as a subprocess, so it **never enters the
platform `uv.lock`/SBOM** and never trips the C4 license gate.

> **Positioning (pending owner wording review — do NOT graduate this out of the env
> README).** A customer keeps their existing **CrewAI-the-library** agents unchanged
> *as agents* and drops Neutron **around** them: Neutron governs **every
> enterprise-effectful action** those agents take, non-bypassably, without Neutron ever
> linking against crewai. The customer's framework churn stays in *their* venv; the
> governance plane — lock, SBOM, license gate, audit chain — is structurally unable to
> absorb it.

## What this is

- `requirements.in` — the top-level pins: `crewai` and `mcp` (declared explicitly
  because `agent.py` imports the mcp SDK directly; crewai's own base pin `mcp~=1.28.1`
  keeps the resolution on the 1.28.x line, matching the platform's `mcp>=1.28.1,<2`).
- `requirements.txt` — fully resolved, `--generate-hashes`, installed with
  `--require-hashes`. Regenerate with (NEVER hand-edit `requirements.txt`):

  ```
  uv pip compile packages/ne-mcp/crewai_env/requirements.in --universal \
      --python-version 3.12 --generate-hashes \
      -o packages/ne-mcp/crewai_env/requirements.txt
  ```

- `agent.py` — the customer-side CrewAI agent. Runs ONLY inside `.crewai-venv`;
  imports ONLY crewai / mcp / pydantic + stdlib — never `ne.*`. Its **entire**
  enterprise toolset is the platform's governed MCP tools, reached through CrewAI
  `BaseTool` proxies over a DIRECT mcp-SDK stdio client.

The platform-side driver is `ne.mcp.crewai_demo` (imports nothing from crewai); the
governed server is `ne.mcp.demo_server` (REUSED byte-unchanged from NE-LG-DROPIN — it is
already framework-agnostic, parameterised by its `NE_LG_DEMO_DB` / `NE_LG_DEMO_PRINCIPAL`
env contract, which this CrewAI side adopts verbatim with principal `user:crewai-demo`).

## The architecture: their process, our chokepoint, MCP in between

```
customer venv (isolated, hash-pinned;            platform venv (uv.lock; NO crewai*/chromadb*)
crewai + chromadb + posthog + lancedb + …)
┌──────────────────────────────────┐   stdio    ┌───────────────────────────────────────┐
│ CrewAI agent / BaseTool proxies  │  (MCP 1.x) │ GovernedMcpServer (ne-mcp, 0 handlers)│
│ tools = MCP proxies ONLY         │═══════════▶│  └─▶ GovernedToolExecutor (C6)        │
│ (direct mcp-SDK stdio client)    │            │       guardrails → PDP → HITL(Tier-2/3)│
│ no connector creds, no ne.* code │            │       → C7 toolset trim → C10 audit   │
└──────────────────────────────────┘            └───────────────────────────────────────┘
```

Every tool call the agent makes routes through the C6 `GovernedToolExecutor`
(deterministic guardrails → PDP → HITL → audit) **by construction**: there is no tool
for the agent to call that is not an MCP proxy to the chokepoint, and `GovernedMcpServer`
holds zero handlers with `@call_tool(validate_input=False)` load-bearing, so unknown /
malformed / rogue calls all reach the audited governance path. Governance credentials,
connector clients, and source-system reach exist **only on the server side** of the pipe.
CrewAI's own internal tool-hook gaps (ReAct hallucinated names bypassing hooks, fuzzy
tool-name matching, an LLM-layer execution path — real, and worse than pydantic-ai's per
ne-rtswap-design §5.2) are **irrelevant here**: they are on the OTHER side of the process
boundary, and the agent has no ungoverned capability for them to abuse.

## Honest trust-boundary statement (design §3, carried verbatim)

The customer's agent **process** is outside our trust boundary. Neutron does not govern
what that process does locally (it can write its own files, call its own LLM). The
claim — and the thing the tests prove — is scoped and strong: **every action against
enterprise systems** (connectors, write-backs, knowledge plane, anything with
credentials or effect) is governed, because those capabilities exist only behind the C6
chokepoint and the agent's only route to them is the governed MCP pipe. In a hardened
deployment C2 egress policy additionally pens the agent's runtime environment, but that
is deployment posture, not a property this unit builds.

## Sovereignty (the headline)

Nothing in this closure ever enters the platform `uv.lock`. **Three** independent
reasons, each guarded:

- crewai's base deps pull `chromadb~=1.1.0`, whose base deps pull **`posthog`** (its
  telemetry client) — permanently barred from the platform lock by
  `packages/ne-memory-graph/tests/test_no_graph_closure_in_lock.py` and, redundantly, by
  `packages/ne-mcp/tests/test_crewai_isolation.py`;
- crewai also pulls `lancedb` and `instructor` in its own base deps;
- **four hard pin conflicts** mean the closure could not even resolve into the workspace
  lock: `pydantic<2.13` (platform 2.13.4), `regex~=2026.1.15` (platform 2026.6.28),
  `portalocker~=2.7.0` (platform 3.2.0), `opentelemetry-*~=1.42.0` (platform sdk 1.43.0).

So the in-process CrewAI path is dead — but out-of-process none of it matters: the
sidecar venv absorbs the whole closure without it ever touching the platform lock.

## crewai-tools is EXCLUDED (never add it)

`crewai-tools` ships **`pymupdf` (AGPL-3.0) in its BASE deps** — adopting it at all is a
copyleft event. Its `MCPServerAdapter` is also unnecessary: `agent.py` builds the
`BaseTool`→mcp-client proxy directly. `test_crewai_isolation.py` asserts
`crewai-tools` never appears in `requirements.in`.

## Telemetry suppression (C2 demo hygiene)

The driver/live-test set `CREWAI_DISABLE_TELEMETRY=true`, `OTEL_SDK_DISABLED=true`, and
`ANONYMIZED_TELEMETRY=False` in the agent subprocess's child env **before** it starts
(authoritative), because crewai-core ships an OTLP exporter and chromadb ships posthog
inside the sidecar. This is demo hygiene; the sidecar is outside the trust boundary and a
hardened deploy additionally pens it with C2 egress policy (deployment posture).

## Why deterministic mode scripts `BaseTool.run()` (honesty note)

CrewAI has **no LLM-free agent loop and no public step-pump** — `Crew.kickoff()` is a
single run-to-completion call that requires a live model (ne-rtswap-design §7 fact 4). So
the hermetic, zero-egress CI path drives the **real** crewai `BaseTool` proxy objects
directly with scripted inputs rather than `Crew.kickoff`. The governance claim is
unaffected: the proxies' only capability is the governed MCP pipe. We do **not** claim an
LLM-driven Crew ran in CI.

## How to run

```
ne dev up                                   # bring up OPA (localhost:8181)
uv run python -m ne.mcp.crewai_demo --auto      # unattended: approves the CRM note, rejects the ticket
uv run python -m ne.mcp.crewai_demo             # interactive: prompts the operator at each approval
```

The driver builds this venv on first run (`ne.envbuild.ensure_sidecar_venv`, the same
mechanism as the ragas oracle and the langgraph drop-in), launches `agent.py` inside it,
prints each approval card plus the C10 audit receipts, and verifies the hash chain.

`--model` drives the same governed toolset through a real `Crew.kickoff` against a
**LOCAL** OpenAI-compatible serving endpoint (`NE_CREWAI_MODEL_BASE_URL`) — owner/demo-rig
only, never CI, no frontier keys.

## License note

crewai's PyPI wheels carry **null license metadata** (no `license` field, no classifier),
verified 2026-07-21 — the repo is MIT-badged. This is stated plainly in the package
`NOTICE`. crewai/crewai-core/crewai-cli are all MIT per the repo LICENSE; the closure's
other license-bearing components (chromadb Apache-2.0, lancedb Apache-2.0, instructor MIT,
openai SDK Apache-2.0, mcp MIT) never enter the platform SBOM regardless.
