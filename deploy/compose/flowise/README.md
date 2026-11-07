# deploy/compose/flowise — Flowise optional add-on (NE-088)

An **OFF-BY-DEFAULT** integration profile that lets a customer optionally run
[Flowise](https://github.com/FlowiseAI/Flowise) — the OSS visual LLM-flow builder —
as a separate add-on service **whose platform tool calls are GOVERNED by OUR PDP**.
This is NOT a fork or a vendored copy of Flowise: it is a digest-pinned upstream
image plus a thin governed endpoint, so a Flowise flow is just another
OIDC-authenticated caller of the platform's C6 governed tool surface.

> ⚠️ **SECURITY DRIFT FLAG (read the "Security history" section below):** Flowise
> carries **CVE-2025-59528**, a CVSS 10.0 unauthenticated RCE via the CustomMCP
> node that went into **active in-the-wild exploitation ~April 2026** (KEV-tracked).
> The pinned `3.1.4` is patched (fix floor `3.0.6`) and this profile applies
> **defense-in-depth hardening** (owner decision (b), SPEC §11): off-by-default,
> an explicit **accept-the-risk enable-gate** (`NE_FLOWISE_ACCEPT_CVE_RISK=true`),
> the **full code-execution node class disabled** (H1, not just customMCP),
> loopback-only publish, in-network, digest pin, and a Helm **egress clamp** (H2).
> The load-bearing mitigation remains that **this add-on ships OFF by default and
> cannot start by accident**. Turn it on only with a conscious decision.

---

## (a) What / why

SPEC §8 explicitly lists a "visual workflow-DSL builder" under **"do NOT build"**,
with a single exception: a **Flowise optional add-on ONLY**. This unit is that
exception — a thin, opt-in profile, never a first-party builder. The default
Neutron Enterprise pilot deploy contains **no** flowise service (proven by
`tools/ne-cli/tests/test_flowise_addon.py::test_default_pilot_compose_has_no_flowise`,
and the existing pilot exact-match guardian
`tools/ne-cli/tests/test_pilot_stack.py` is untouched).

Flowise runs as its **own compose project** (`name: ne-flowise`) with its **own
`flowise` profile gate**, in a **separate file** that never edits the pilot
compose. It joins the pilot network so the governed endpoint is reachable
in-network, and publishes only on loopback.

## (b) Bring-up runbook

Prerequisite: the pilot stack is up and healthy (this creates the
`ne-pilot_default` network the overlay rides):

```
ne up --profile core          # pilot stack (ingress + identity + PDP + DB + ne-api)
```

Then bring up the Flowise overlay (secrets resolved from your secret store into a
secure env file — see `.env.example`; C12). The secure env file **must also carry
the explicit risk acknowledgment** `NE_FLOWISE_ACCEPT_CVE_RISK=true` (see "Risk
acceptance" below) — without it the overlay refuses to even `config`:

```
# secure-env must contain the five NE_FLOWISE_* secrets AND:
#   NE_FLOWISE_ACCEPT_CVE_RISK=true
docker compose --env-file <secure-env> \
  -f deploy/compose/flowise/compose.yaml \
  --profile flowise up -d
```

Reach the UI over an **SSH tunnel** (the service publishes only on
`127.0.0.1:3100`; it is never off-box):

```
ssh -L 3100:127.0.0.1:3100 <pilot-host>
# then open http://127.0.0.1:3100 in your browser
```

The first-run admin account is created in the Flowise UI on first load.

## (c) Governance (C6) — how a flow calls a platform tool

**Every platform tool call from a flow goes through OUR governed executor**, so a
Flowise-initiated call gets the SAME `PDP decide → approval-tier → audit → execute`
path as a first-party chat turn. There is **no ungoverned side-channel**.

Wire a flow's **HTTP / Requests tool node** (NOT the CustomMCP node — it is
disabled; NOT a Custom Tool JS block for platform calls) to:

```
POST https://<pilot-host>/api/v1/flowise/tools/execute
Authorization: Bearer <OIDC access token for the flowise-svc client>
Content-Type: application/json

{ "tool": "read.knowledge.search", "args": { "query": "..." } }
```

(ne-api is served on Caddy's **443** vhost — `:8443` is Keycloak only. Because
the Flowise container rides `ne-pilot_default`, an in-network alternative is
`http://ne-api:8000/api/v1/flowise/tools/execute`, which avoids the internal-CA
trust step for the HTTP node.)

Discover the governed tool catalog (specs only — names, tiers, input schemas;
never handlers) with:

```
GET https://<pilot-host>/api/v1/flowise/tools
```

Governance semantics (identical to a first-party turn):

- A **PDP-denied** tool returns **403** — there is no bypass.
- A **tier-2/tier-3** tool **BLOCKS** until a named human approves it in the
  console (`/v1/approvals/pending` → approve). Set **generous HTTP-node timeouts**
  on the flow's request node so it waits for the human decision.
- An **unknown** tool returns **404** (and is still audited as `tool.unknown`).
- A handler failure returns **502** (the raw handler error is never leaked).
- **Every** call is audited: the governed `pdp.decide` / `tool.approval` /
  `tool.execute` / `tool.unknown` events plus an `api.flowise.execute` surface
  record.

Direct **model** calls from a flow (LLM nodes) should target the in-network
**ne-gateway**, not an external provider — keep egress on the governed/allowlisted
path (C2).

### Keycloak `flowise-svc` client runbook (operator step)

The bearer above is a **client-credentials** token for a confidential service
client. In the `ne-pilot` realm (manual operator step — live verification parked):

1. Create a **confidential** client `flowise-svc` with **service accounts
   enabled** (Standard flow off; Service accounts on).
2. Add an **audience mapper** that adds `ne-console` to the token's `aud`
   (ne-api validates `aud == ne-console`).
3. Obtain a token via the **client-credentials** grant and present it as the
   `Authorization: Bearer` on the HTTP tool node.

> This runbook is **documentation-only** in this unit. If a live pilot later needs
> realm JSON changes, that is a separate wiring rider — the pilot realm file is
> guardian-covered and is NOT touched here.

## (d) EE exclusion / license boundary (C4)

Flowise is **dual-licensed**. Quoting `LICENSE.md` at tag `flowise@3.1.4`:

> All content that resides under `.../packages/server/src/enterprise` directory
> and files with explicit copyright notice such as `IdentityManager.ts` are
> licensed under Commercial License. … Content outside of the above mentioned
> directories or restrictions above is available under the "Apache 2.0" license.

**What this profile does:**

- We run the **OSS Apache-2.0 core** as a **separate service** — a legitimate use
  of Apache-2.0 software.
- The **commercial enterprise-edition surface** is **EXCLUDED**: the EE features
  activate **only** when one of `FLOWISE_EE_LICENSE_KEY` / `LICENSE_URL` /
  `OFFLINE` is set, and this profile **never sets any of them** (env-var names
  verified against `docker/.env.example` at the pinned tag). This is
  **guardian-enforced** by
  `tools/ne-cli/tests/test_flowise_addon.py::test_flowise_ee_surface_excluded`
  (no EE env keys, and no `FLOWISE_EE` text anywhere in the overlay).
- We **neither fork nor vendor** Flowise: it is a **digest-pinned upstream image**
  (C11), out-of-process, **never in `uv.lock`** (it is a Node app, not a Python
  dependency).
- **No EE feature is used or resold.**

## (e) Security history (dependency-intake) — KEV re-checked **2026-08-05**

`3.1.4` is the **latest** Flowise release (published 2026-07-29) and patches
**every** known Flowise RCE plus the GHSA-8gj2-2cvc-6xx7 TTS credential-abuse bug
(the reason for this pin). Every CVE row below lives inside the code-execution
node class that H1 disables anyway (see "Disabled node class"); the GHSA row is a
**whitelisted REST-controller bug H1 cannot reach** — the 3.1.4 version floor is
its only real fix.

| CVE | What | CVSS | Status at `3.1.4` |
| --- | --- | --- | --- |
| **CVE-2025-59528** | Unauthenticated RCE via the **CustomMCP** node (GHSA-3gcm-f6qx-ff7p; `>=2.2.7-patch.1 <3.0.6`). VulnCheck saw **active in-the-wild exploitation** (~12–15k exposed instances, Apr 2026); **CISA-KEV**. Same failure class SPEC §2 cited when banning Langflow. | 10.0 | **Patched** (fix floor 3.0.6) + node disabled |
| **CVE-2026-40933** | CustomMCP **stdio → OS command** execution. | — | **Patched 3.1.0** + node disabled |
| **CVE-2026-41264** | **CSV-Agent** LLM-generated-Python RCE. The `CSVAgent` node **no longer exists** in the components tree at the `3.1.3` tag. | 9.2 | **Patched 3.1.0** (node removed) |
| **CVE-2026-58057** | **Windows-only, authenticated** case-sensitive `NODE_OPTIONS` denylist bypass in CustomMCP stdio. **exploit-db 52623 is titled "Flowise 3.1.3 — arbitrary code execution", but 3.1.3 is the FIXED release** (the title names the patched version, not a vulnerable one). | 2.3 | **Patched IN 3.1.3** + node disabled |
| **GHSA-8gj2-2cvc-6xx7** | **Unauthenticated `/api/v1/text-to-speech/generate`** — the unauthenticated fallback path fetches any chatflow by UUID and **skips the `isPublic` check**, so an attacker who knows a chatflow UUID can burn that chatflow's stored OpenAI/ElevenLabs **TTS credential** (financial abuse / quota-exhaustion DoS). **NOT covered by H1** (whitelisted REST controller, not a flow node) — the 3.1.4 bump IS the fix. No CVE id assigned (GH severity: medium). | 6.3 | **Patched 3.1.4** |

**CISA KEV** contains **no Flowise entry other than CVE-2025-59528** (recent KEV
adds cite Langflow, not Flowise) → **no new unpatched, actively-exploited KEV RCE
exists at 3.1.4**. No dependency-intake stop-condition.

- Flowise `3.1.x` also enables **HTTP SSRF security checks by default**, which may
  block a flow's HTTP node from reaching a **private** address (the in-network
  governed endpoint). If the governed endpoint is unreachable from a flow at live
  bring-up, follow the Flowise docs to allow the specific pilot host
  (live-parked detail).

## (f) Risk acceptance (owner decision (b), SPEC §11)

Because Flowise is a **CVSS-10 / KEV-history** component, the owner chose
**merge-with-hardening** over merge-as-is. The add-on therefore **cannot be
enabled by accident**: it requires an explicit acknowledgment env var,
`NE_FLOWISE_ACCEPT_CVE_RISK`, that must be **exactly the string `true`**, enforced
at **two** points:

1. **Compose config-time** — `NE_FLOWISE_ACCEPT_CVE_RISK` is a required `${…:?}`
   interpolation, so even `docker compose --profile flowise config` (no daemon)
   **fails loud** when it is unset.
2. **Container runtime** — the service `entrypoint` is a `/bin/sh -c` wrapper that
   compares the container-runtime env to `true`, prints a loud refusal and
   `exit 78` if it is anything else, and only then `exec`s the upstream
   `flowise start` (staying PID 1 so the healthcheck is unchanged).

> Note: the config-time `:?` catches an **unset** value; a value set to something
> other than `true` (with `restart: unless-stopped`) will **crash-loop by design**
> — that is the intended fail-closed behaviour, not a bug.

**Blast-radius argument for accepting the residual risk:** the add-on is
**off-by-default**, every flow tool call is forced through the **C6 governed
executor** (no ungoverned side-channel), the **entire code-execution node class is
disabled** (H1), publish is **loopback-only**, and — on Kubernetes — egress is
**clamped to ne-api only** (H2). Guardians: `test_flowise_accept_risk_gate_
required_interpolation`, `test_flowise_entrypoint_refuses_without_ack`,
`test_flowise_profile_without_ack_does_not_start`, `test_flowise_env_example_
documents_ack`.

## (g) Disabled node class (H1)

`DISABLED_NODES` disables the **full dynamic-code-execution / arbitrary-eval node
class** (23 identifiers, each `this.name` verified file-by-file at tag
`flowise@3.1.3`), not just `customMCP`, so a **future sibling-node CVE** in this
class is **pre-disabled**. Guardian:
`test_flowise_code_exec_node_class_fully_disabled`.

| Identifier | Source at `flowise@3.1.3` | Why disabled |
| --- | --- | --- |
| `customMCP`, `customMcpServerTool` | `tools/MCP/{CustomMCP,CustomMcpServerTool}` | Arbitrary MCP server config / stdio spawn (CVE-2025-59528 / 2026-40933 / 2026-58057) |
| `customTool` | `tools/CustomTool` | User-authored JS tool |
| `customFunction`, `ifElseFunction` | `utilities/{CustomFunction,IfElseFunction}` | User JS |
| `customFunctionAgentflow` | `agentflow/CustomFunction` | User JS (Agentflow v1) |
| `customDocumentLoader` | `documentloaders/CustomDocumentLoader` | User JS |
| `codeInterpreterE2B` | `tools/CodeInterpreterE2B` | Remote code exec via external E2B SaaS (also a C2 egress vector) |
| `chatPromptTemplate` | `prompts/ChatPromptTemplate` | Optional `messageHistoryCode` user JS |
| `openAPIToolkit` | `tools/OpenAPIToolkit` | `customCode` user JS |
| `agentAsTool`, `ChatflowTool` | `tools/{AgentAsTool,ChatflowTool}` | Interpolate chat input into sandbox-executed JS fetch templates (CVE-2026-41264-style shape). Note the literal capital-C `ChatflowTool`. |
| `seqAgent`, `seqCondition`, `seqConditionAgent`, `seqCustomFunction`, `seqEnd`, `seqExecuteFlow`, `seqLLMNode`, `seqLoop`, `seqStart`, `seqState`, `seqToolNode` | `sequentialagents/*` | The entire **deprecated** sequentialagents category — the executing members call `executeJavaScriptCode`; the rest are useless without them (categorical disable) |

**What REMAINS for flow authors:** **Agentflow v2** (the `conditionAgentflow` node
is declarative-only — `compareOperationFunctions`, no code sandbox — and is
deliberately **not** disabled), the **plain `promptTemplate`** node, and the
**HTTP / Requests** node pointed at the governed `ne-api` endpoint. This narrows
the visual authoring surface **on purpose** (owner chose maximal hardening); the
supported pattern is Agentflow v2 + HTTP node → `POST /api/v1/flowise/tools/execute`.

## (h) Network isolation & the enforcement boundary (H2)

Be **honest** about what each layer can and cannot enforce.

**What compose ENFORCES here (guardian-tested):**

- **Loopback-only publish** — exactly one published port, `127.0.0.1:3100:3000`;
  nothing off-box (`test_flowise_publishes_no_ports_beyond_loopback`).
- **No extra egress grants** in the overlay — no `extra_hosts` / `dns` /
  `network_mode`; the only network is the external `ne-pilot_default`
  (`test_flowise_no_egress_grants`).
- **A sealed, flowise-unaware pilot stack** — no `flowise` reference in the pilot /
  serve compose, Caddyfile(s), `opa/`, or `otel/` configs: no reverse-proxy,
  egress, or allowlist entry exists for the add-on
  (`test_sealed_stack_has_no_flowise_egress_lane`).
- **Profile + accept-risk gating** (see above).

**What compose CANNOT enforce (stated plainly):** per-container **egress deny**.
Docker attaches containers to a **NAT bridge** that grants **outbound Internet by
default**; compose has no portable per-container egress-firewall primitive. So this
overlay does **not** — and cannot honestly claim to — block a compromised Flowise
container from dialling out. Two real options close that gap:

1. **Operator-optional host firewall (NOT guardian-enforced).** Drop outbound from
   the Flowise container's subnet except to the ne-api / Caddy address, e.g.:

   ```
   # DOCKER-USER runs before Docker's own rules. Replace <flowise-subnet> with the
   # ne-pilot_default bridge subnet (docker network inspect ne-pilot_default) and
   # <ne-api-ip> with the ne-api/Caddy container address.
   iptables -I DOCKER-USER -s <flowise-subnet> -d <ne-api-ip> -j RETURN
   iptables -I DOCKER-USER -s <flowise-subnet> -j DROP
   ```

   This is **operator-optional** and lives outside the repo's guardian coverage.

2. **The ENFORCEABLE clamp is on Kubernetes** — the Helm NetworkPolicy
   **`flowise-egress-clamp`** (`templates/networkpolicy-flowise.yaml`, gated
   `networkPolicy.enabled` **AND** `profiles.flowise`) default-denies the
   `app=flowise` workload and permits egress **only** to `ne-api:8000` (DNS rides
   the chart's `allow-dns`). On a NetworkPolicy-enforcing CNI (Calico/Cilium/…)
   this is real, enforced egress isolation. The umbrella chart ships **no** Flowise
   workload today, so the policy **pre-clamps** any customer-deployed `app=flowise`
   pod. `kubectl port-forward` for the UI is proxied by the API server (not
   pod-to-pod), so it is unaffected by the ingress default-deny.

   A K8s connection is permitted only when **both** ends allow it, so the same
   template renders a second, additive policy **`allow-ne-api-from-flowise`** that
   opens the matching `ne-api` **ingress** from `app=flowise` on 8000. Without it
   the clamp's sole allowed destination would still be dropped at `ne-api`'s
   ingress — the chart's baseline `allow-ne-api` admits only the ingress-controller
   namespace. It is `Ingress`-only (it never touches `ne-api`'s egress) and rides
   the same two gates. Guardians:
   `test_flowise_helm_networkpolicy_clamps_egress_to_ne_api`,
   `test_flowise_helm_networkpolicy_completes_ne_api_ingress_lane`,
   `test_flowise_helm_networkpolicy_requires_both_gates`.

## (i) Image provenance (C11)

Digest-pinned; captured 2026-08-05 from Docker Hub
(`registry Docker-Content-Digest` for the multi-arch index).

| Service | Image tag | Digest | License | Source |
| --- | --- | --- | --- | --- |
| flowise | `flowiseai/flowise:3.1.4` | `sha256:3922767afb52a5777759fd8b28a3c9eee864daea96018a791f2429eae2a76571` | Apache-2.0 core (commercial EE surface EXCLUDED — see (d)) | hub.docker.com/r/flowiseai/flowise |
