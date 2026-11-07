# LangGraph governed drop-in — isolated demo venv (NE-LG-DROPIN)

This directory is the **isolated, hash-pinned virtualenv payload** for the
customer-side LangGraph agent in the Neutron governed drop-in. It mirrors the
isolation pattern `packages/ne-eval/ragas_env/` uses for ragas: the langgraph +
langchain closure is installed into a dedicated, git-ignored venv
(`packages/ne-mcp/.langgraph-venv`) and run as a subprocess, so it **never enters
the platform `uv.lock`/SBOM** and never trips the C4 license gate.

> **Positioning (pending owner wording review — do NOT graduate this out of the env
> README).** A customer keeps their existing **LangGraph-the-library** (MIT) agents
> unchanged *as agents* and drops Neutron **around** them. Neutron is the open,
> self-hosted replacement for the paywalled **LangGraph Platform / enterprise layer**,
> and it governs **every enterprise-effectful action** those agents take,
> non-bypassably. The customer's framework churn (langgraph versions, langchain CVEs,
> the mcp v2 migration on *their* side) stays in *their* venv; the governance plane —
> lock, SBOM, license gate, audit chain — is structurally unable to absorb it.

## What this is

- `requirements.in` — the top-level pins: `langgraph`, `langchain-mcp-adapters`, the
  `langchain-core>=1.2.22,<2` **security floor**, and (for `--model` mode only)
  `langchain-openai`.
- `requirements.txt` — fully resolved, `--generate-hashes`, installed with
  `--require-hashes`. Regenerate with (NEVER hand-edit `requirements.txt`):

  ```
  uv pip compile packages/ne-mcp/langgraph_env/requirements.in --universal \
      --python-version 3.12 --generate-hashes \
      -o packages/ne-mcp/langgraph_env/requirements.txt
  ```

- `agent.py` — the customer-side LangGraph agent. Runs ONLY inside `.langgraph-venv`;
  imports ONLY langgraph / langchain-mcp-adapters / langchain-core (+ langchain-openai
  under `--model`) + stdlib — never `ne.*`, never bare `mcp`. Its **entire** enterprise
  toolset is the platform's governed MCP tools.

The platform-side driver is `ne.mcp.langgraph_demo` (imports nothing from langgraph);
the governed server is `ne.mcp.demo_server`.

## The architecture: their process, our chokepoint, MCP in between

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

Every tool call the agent makes routes through the C6 `GovernedToolExecutor`
(deterministic guardrails → PDP → HITL → audit) **by construction**: there is no tool
for the agent to call that is not an MCP proxy to the chokepoint, and `GovernedMcpServer`
holds zero handlers with `@call_tool(validate_input=False)` load-bearing, so unknown /
malformed / rogue calls all reach the audited governance path. Governance credentials,
connector clients, and source-system reach exist **only on the server side** of the pipe.

## Honest trust-boundary statement (design §3, carried verbatim)

The customer's agent **process** is outside our trust boundary. Neutron does not govern
what that process does locally (it can write its own files, call its own LLM). The
claim — and the thing the tests prove — is scoped and strong: **every action against
enterprise systems** (connectors, write-backs, knowledge plane, anything with
credentials or effect) is governed, because those capabilities exist only behind the C6
chokepoint and the agent's only route to them is the governed MCP pipe. In a hardened
deployment C2 egress policy additionally pens the agent's runtime environment, but that
is deployment posture, not a property this unit builds.

## How to run

```
ne dev up                                   # bring up OPA (localhost:8181)
uv run python -m ne.mcp.langgraph_demo --auto   # unattended: approves the CRM note, rejects the ticket
uv run python -m ne.mcp.langgraph_demo          # interactive: prompts the operator at each approval
```

The driver builds this venv on first run (`ne.envbuild.ensure_sidecar_venv`, the same
mechanism as the ragas oracle), launches `agent.py` inside it, prints each approval card
plus the C10 audit receipts, and verifies the hash chain.

`--model` drives the same governed toolset through a react-style langgraph agent against
a **LOCAL** OpenAI-compatible serving endpoint (`NE_LG_MODEL_BASE_URL`) — owner/demo-rig
only, never CI, no frontier keys.

## Why the langchain-core security floor

`langchain-core` 0.3.x is affected by two HIGH advisories (per OSV/GHSA):

- **CVE-2026-34070** (GHSA-qh6h-p6c9-ff54) — path traversal in `load_prompt`, fixed in
  `langchain-core >=1.2.22`.
- **CVE-2026-26013** (GHSA-2g6r-c272-w58r) — fixed in `langchain-core >=1.2.11`.

Both remediations land only on the langchain **1.x** line, so `requirements.in` floors
the whole closure at 1.x (`langchain-core>=1.2.22,<2`). Same advisories, same floor as
`packages/ne-eval/ragas_env/requirements.in`.

## Transport: v1 is stdio; streamable-HTTP is deferred to v1.1

v1 proves governance over the MCP **stdio** transport (the client spawns the server).
The real "customer agents connect over the network" shape — server-side streamable-HTTP
plus session-auth→principal mapping on ne-api — is a follow-on unit (`NE-LG-DROPIN-HTTP`):
it widens the single-module SDK chokepoint (`ne/mcp/_sdk.py`) and may be sequenced after
the ~2026-08 mcp v2 re-pin. Deferring it is honest: transport hardening is orthogonal to
the non-bypass governance claim this unit establishes.
