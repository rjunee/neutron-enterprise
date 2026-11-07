# ne-gateway

The single model seam. `LiteLLMGateway` embeds a **LiteLLM `Router` (library
only — never the proxy)** behind the frozen `ne.contracts.ModelGateway`
Protocol, adding per-task routing, privacy lanes, fail-loud fallback chains, and
a cost-event pipeline.

## C3 — model independence (the `_litellm.py` chokepoint)

`ne.gateway._litellm` is the **only** module in the entire repository permitted
to `import litellm`. Every other gateway module imports `litellm` / `Router`
*from* it. No provider SDK (`openai`, `anthropic`, …) is imported anywhere in
this package. Until NE-012 lands an import-linter contract, the
`test_only_litellm_module_imports_litellm` grep test enforces this.

The chokepoint also forces telemetry **off** — `litellm.telemetry = False`
(the effective control in 1.90.3), all callback lists cleared, and
`LITELLM_LOCAL_MODEL_COST_MAP=True` / `LITELLM_TELEMETRY=False` set pre-import so
there is no cost-map fetch or telemetry egress at import time.

## Message-shape contract (C3, NE-AGENT-MSG-ORDER)

`LiteLLMGateway.complete()` / `_stream()` normalize every incoming
`CompletionRequest.messages` to **at most one system-role message, always
first**, *before* token estimation, NE-162 redaction, and every provider
attempt — so every caller and every provider candidate reaches a conforming
sequence, regardless of what was assembled upstream. This is the C3
chokepoint's other half: model independence means a strict chat template
(Qwen3.6-35B-A3B-FP8's, which HTTP-400s `"System message must be at the
beginning."` on a non-leading system message) never sees a sequence a
permissive template would have tolerated by accident.

The merge (`_normalize_system_first`) is order-stable and lossless — every
system message's content is collected in encounter order and joined with
`"\n\n"` into one leading system message; an already-conforming request
passes through byte-identical. Derived independently from the same contract
as `ne-runtime`'s `GatewayModel._merge_system_head` (never imported —
`ne-gateway` and `ne-runtime` do not import each other). Pinned by
`tests/test_message_normalization.py`; no per-model/per-provider conditional
exists in this normalization.

## Configuration (`ne.toml` / `NE_GATEWAY_` env)

`GatewaySettings` is a layered `NeSettings` (precedence: init kwargs >
`NE_GATEWAY_` env > `ne.toml` > defaults). Routing is validated at **boot**: a
task pointing at an unconfigured model, a duplicate model name, or an empty
model list raises `GatewayConfigError` with the exact offending key path — never
a silent mis-route.

Per the repo-wide `NeSettings` convention, fields live at the **TOML root** of
the gateway process's config file — there is no `[gateway]` section table.
Select a per-process/per-customer file via `NE_CONFIG_FILE` or
`GatewaySettings.from_config_file(path)`.

```toml
num_retries = 2
timeout_seconds = 120.0

[[models]]
name = "frontier"            # routing alias
litellm_model = "anthropic/claude-3-5-sonnet-20241022"
provider = "anthropic"
lane = "frontier-api"
context_window = 200000      # REQUIRED — the SERVING window (see below)
# api_base / extra_params (e.g. api_key) are optional per-model litellm_params
# max_output_tokens is optional (defaults to min(4096, context_window // 8))

[[models]]
name = "private"
litellm_model = "hosted_vllm/meta-llama/Llama-3.1-70B-Instruct"
provider = "vllm"
lane = "private-vllm"
api_base = "https://vllm.internal.example/v1"
context_window = 32768       # the deployment's vLLM --max-model-len

[tasks.draft]
model = "frontier"
fallbacks = ["private"]     # tried in order if the primary attempt fails
params = { temperature = 0.2 }
```

Env layering example: `NE_GATEWAY_NUM_RETRIES=5` overrides the retry count;
nested fields use the `__` delimiter.

## Context budget (NE-CTXB-2a)

`ModelDef` is `frozen` + `extra="forbid"`, so an unknown key in
`NE_GATEWAY_MODELS` env JSON (e.g. a typo'd `context_windw`) now fails boot
LOUDLY instead of being silently dropped.

- **`context_window: int` (REQUIRED, `> 0`)** — the *serving* window, i.e. the
  deployment's vLLM `--max-model-len`, **not** the model-card advertised max (the
  RunPod incident proved the card value is wrong at runtime). `ModelBinding` may
  only **narrow** it (a served deployment can be smaller than the registry entry,
  never larger).
- **`max_output_tokens: int` (optional)** — defaults to
  `min(4096, context_window // 8)`; a supplied value must be
  `< context_window // 2`.

Before every provider attempt the gateway runs a deterministic **budget
pre-flight** (order: egress-guard → pre-flight → redaction): it estimates the
un-redacted prompt and skips any candidate whose estimate exceeds
`context_window − effective_max_tokens − overhead_margin`, rolling to the next
(larger-window) candidate. Every completion also gets `max_tokens` defaulted from
the model (an explicit caller value is never overridden).

**Single ownership of the estimator (design §2.3).** The estimator parameters
live ONCE on `GatewaySettings`:

- `bytes_per_token: float` (env `NE_GATEWAY_BYTES_PER_TOKEN`) — defaults to
  `ne.contracts.tokens.DEFAULT_BYTES_PER_TOKEN` (the empirically-corrected value,
  **not** the design-prose 3.0).
- `per_call_overhead: int` (env `NE_GATEWAY_PER_CALL_OVERHEAD`) — defaults to
  `ne.contracts.tokens.DEFAULT_PER_CALL_OVERHEAD`.
- `overhead_margin` policy is `max(1024, ceil(0.05 × context_window))`, owned by
  `overhead_margin_for(...)`.

The runtime trim (NE-CTXB-3) derives its `ContextBudget` from this same object so
the two layers never disagree.

## Lanes

`CompletionRequest.lane` (`frontier-api` | `private-vllm`) filters the task's
candidate list to models serving that lane. If no candidate for the task serves
the requested lane, `LaneMismatchError` names the task, the requested lane, and
every candidate's lane. A request with `lane = None` considers all candidates.

## Cost events

Every **successful** call emits one `ne.schema.CostEvent`
(`ne.events.factory.new_cost_event`) carrying model / provider / lane / task /
token counts / cost, stamped with the frozen process `config_hash`. The
production sink, `OutboxCostSink`, writes it through the ne-events Postgres
**outbox** in a single transaction (the C8 event spine). `kpi_tag` travels as a
**reserved request-param key** (`params={"kpi_tag": "..."}`) — it is stamped
onto the event and is **never** forwarded to litellm (the frozen
`CompletionRequest` has no `kpi_tag` field). Streaming emits **exactly one** cost
event on **every terminating stream** — clean exhaustion, a mid-stream provider
failure, *and* a consumer abandon (stop-button / `GeneratorExit`) — with the
token/cost fields None-guarded best-effort off the last seen chunk (the cost
event is emitted synchronously *before* the audit append, so it lands even if the
audit await is cancelled mid-cancellation). NE-014 additionally attaches token
usage, the response model, wall-clock latency, and the task as **post-hoc span
attributes** (`ne.gateway.instrumentation.annotate_span_after_call`) once the
provider call returns.

**Attribution (NE-COST-PRINCIPAL).** `CostEvent.actor` is the CALLING
PRINCIPAL's opaque identifier — `request.principal_id` — when the request
carries one; otherwise it is the gateway's own service actor (the constructor's
`actor: str = "ne-gateway"`). An explicit empty string is treated the same as
no principal (falls back to the service actor — ADR-0018's decided divergence
from `trace_id`'s explicit-empty-wins contract). This is computed fresh from
each request, never cached, so a principal-less call can never inherit an
earlier call's principal. Gateway `AuditEvent`s (below) are **not** affected:
they always keep the service actor, deliberately — they record the gateway's
own egress action, joinable to the causing principal via `trace_id`.

## Instrumentation (NE-014)

Every **successful** `complete()` / `stream()` call produces a trio that all
carry the **identical** frozen `config_hash` (each independently defaults to
`ne.contracts.get_config_hash()`):

- one **OTel GenAI span** — opened by `ne.obs.genai_span` (operation / provider /
  `gen_ai.request.model` / `ne.lane` / `ne.config_hash` at open time), then
  enriched post-call by `ne.gateway.instrumentation.annotate_span_after_call`
  with `ne.task`, `ne.gateway.latency_ms`, `gen_ai.response.model`, and
  `gen_ai.usage.input_tokens` / `output_tokens` (token/response-model attrs are
  omitted on the streaming establish-phase span, which closes before the stream
  is drained);
- one **`CostEvent`** (the pre-existing NE-010 path);
- one **`AuditEvent`** (`ne.events.new_audit_event`) — a call-usage audit of
  model usage (distinct from the C6 PDP path), emitted through the frozen
  `ne.contracts.AuditSink` on both the success and the all-candidates-failed
  path (`outcome=error`, no `CostEvent` on failure). A **streaming** call also
  audits its abnormal terminations: a mid-stream provider failure or a consumer
  abandon each emits an `outcome=error` audit (the abandon detail marks the
  `GeneratorExit` interruption) alongside its cost event, so no path yields
  provider data without an accounting trail. As of **NE-163a**, every
  **success-path** `AuditEvent` provenance carries, in addition to the
  pre-existing `task` / `lane` / `model_name`: `cost_usd`, `input_tokens`,
  `output_tokens`, and `response_model` (the actually-served model reported by
  litellm) — all stringified, each **omitted** (never the literal string
  `"None"`) when the value is unknown, and always numerically equal to the
  sibling `CostEvent`'s typed fields for the same call. This makes per-trace
  cost/tokens queryable straight off the existing tamper-evident audit chain
  via `GET /v1/audit/replay?trace_id` — no new cost store, no separate
  per-trace-cost route (absorbs NE-165). The all-candidates-failed audit
  carries none of these keys: no successful call means no cost to report.

On the **success paths** the cost and audit events carry a `trace_id`. As of
NE-166, `CompletionRequest.trace_id` — an explicit, opaque run-scoped
correlation id threaded in AS DATA by the caller (e.g. `ne-runtime`'s
`ne.runtime.correlation`) — is the primary carrier and always WINS when
present; live-span derivation (`opentelemetry.trace.format_trace_id` off the
current GenAI span) is used only as an ENRICHMENT FALLBACK when the request
supplies no explicit id, which is exactly the pre-NE-166 behavior (`None`
under the default no-op tracer). Failure-path audit details and
`ModelCallFailedError` messages are **credential-redacted** through
`redact_url_for_logs`, so a provider exception embedding a credentialed
`api_base` can never leak a cleartext secret into a log line or audit record.

The audit event flows through an optional constructor seam:

```python
from ne.gateway import LiteLLMGateway

gw = LiteLLMGateway(settings, cost_sink, audit_sink=my_audit_sink)
```

`audit_sink: AuditSink | None = None` is a deliberate no-op when `None` — exactly
like `egress_guard=None`, this keeps unit-test ergonomics simple. Production
composition roots MUST pass a real `AuditSink` (e.g. `ne.audit.PostgresAuditSink`)
so model-usage is durably audited.

### Traces in the dev stack

ne-gateway calls `trace.get_tracer("ne.gateway")` and therefore emits real spans
into **whatever global `TracerProvider` the hosting process has configured** — it
does not, and by design should not, bootstrap an OTel SDK itself (it is a
library, not a process entrypoint). To see its spans in NE-006's dev-stack
`otel-collector` (`deploy/compose/dev/otel/otel-collector-config.yaml`, OTLP on
`4317`/grpc and `4318`/http, printed via `docker compose logs otel-collector`),
run the hosting process under `opentelemetry-instrument` (or programmatically
install a `TracerProvider` + `BatchSpanProcessor(OTLPSpanExporter(...))`) with:

```bash
export OTEL_SERVICE_NAME=ne-gateway
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
```

No runtime composition root exists **yet** in this repo to host that SDK
bootstrap end-to-end, so the live-collector round trip is **live-parked** for
this unit. The in-memory-exporter tests in `tests/test_instrumentation.py` are
the CI-provable proof that span construction is correct.

## API keys

litellm reads provider credentials from the usual provider environment variables
(or from per-model `extra_params`, e.g. `api_key`). Wiring credentials from the
`SecretStore` is a deployment concern owned by NE-007, not this package.

## Model registry (NE-011)

`ne.gateway.registry.InMemoryModelRegistry` structurally implements the frozen
`ne.contracts.ModelRegistry` Protocol over the frozen `ModelEntry` type. Every
entry is tagged:

- **license** — one of `KNOWN_MODEL_LICENSES` (`MIT`, `Apache-2.0`,
  `Modified-MIT` — the Kimi K2.x caveat class, `proprietary-api` — frontier-API
  models whose weights are not distributed). Non-commercial / research-only
  classes (`cc-by-nc*`, `mrl`, `research-only`, `llama-4-community`) are
  **banned** at registration (C4).
- **origin** — the model developer's HQ, an uppercase ISO-3166 alpha-2 code
  (`CN`, `US`, `FR`, `EU`, `GB`, `CA`, `DE`).
- **lane** — `frontier-api` / `private-vllm` (one or more).
- **capabilities** — e.g. `streaming`, `structured-output`, `tool-use`,
  `reasoning`, `vision`, `long-context`, plus a required `context_window`.

### Named menus

Two default menus ship as typed data (`ne.gateway.menus`), both served
in-VPC (`lanes=(Lane.PRIVATE_VLLM,)`):

- **`capability-first`** — GLM-5.2 (MIT, CN), Qwen3.5-397B-A17B (Apache-2.0,
  CN), Qwen3.6-35B-A3B (Apache-2.0, CN), DeepSeek-V4-Flash (MIT, CN).
- **`western`** — Mistral-Large-3 (Apache-2.0, FR), gpt-oss-120b (Apache-2.0,
  US).

Origin tags exist so a customer can filter by procurement constraint: some
buyers object to procuring CN-origin model weights regardless of license, so
the Western menu is the alternate offering for that constraint. Use
`registry.filter_models(origin=...)` / `filter_models(license=...)` for this —
a separate method from the frozen `list_models(lane=..., capability=...)` so
Protocol conformance is never at risk of accidental signature drift.

License/context-window figures are a spec-time snapshot (verified 2026-07-04
against each model's HuggingFace page); re-check per SPEC §3.3 before a menu
refresh. GLM-5.x license (SPEC §12 verification item assigned to this unit) is
confirmed **MIT** via the `zai-org/GLM-5.2` HuggingFace model page.

### Validation

`register()` validates every entry eagerly (fail-loud at registration, never
at lookup) with errors that name the exact offending `model_id` and field:
empty/unknown/banned license (banned licenses cite C4 explicitly), unknown
origin, empty lanes, a `proprietary-api`-licensed entry on the `private-vllm`
lane (weights-restricted models cannot be self-hosted), unknown/empty
capabilities, and a missing/non-positive `context_window`. An unregistered
`model_id` lookup raises `UnknownModelIdError` naming every registered
alternative — there is no silent any-provider fallback.

### Registry -> gateway bridge

`build_gateway_settings` constructs NE-010's `GatewaySettings`/`ModelDef` from
registered entries plus deployment bindings (the registry is the source of
truth for model identity/tags; NE-010's `GatewaySettings.models` was a minimal
stand-in pending this unit):

```python
from ne.gateway import ModelBinding, TaskRoute, build_gateway_settings, load_menu
from ne.gateway.menus import MENU_CAPABILITY_FIRST

registry = load_menu(MENU_CAPABILITY_FIRST)
settings = build_gateway_settings(
    registry,
    bindings={
        "glm-5.2": ModelBinding(
            litellm_model="hosted_vllm/glm-5.2",
            api_base="http://vllm:8000",
        ),
    },
    tasks={"draft": TaskRoute(model="glm-5.2")},
)
```

An unbound task reference still fails at NE-010's own boot validator (the
`tasks.<task>.model` exact-key-path `GatewayConfigError`) — this bridge does
not duplicate that validation.

## Egress control & AIR_GAP (NE-012)

`ne.gateway.egress.EgressPolicy` is the real implementation of NE-010's
`egress_guard` seam — invoked before **every** provider attempt, it enforces the
two privacy lanes and proves the C2 sovereignty claim: the only sanctioned
egress is frontier-model APIs through the gateway, and `AIR_GAP` provably kills
even that.

Per lane the guard enforces:

- **frontier-api** — the resolved egress host (the model's `api_base` host, or
  `KNOWN_FRONTIER_PROVIDER_HOSTS[provider]` when no `api_base` is set) must be on
  the per-deployment allowlist `allowed_frontier_hosts`. Anything not on the
  allowlist is blocked (`EgressBlockedError`, fail-closed — an empty allowlist
  blocks everything). The error names the host, the allowlist, and the config
  key to fix. An unknown provider with no `api_base` is blocked (no host can be
  resolved).
- **private-vllm** — must stay in-VPC. `api_base` is required, is SSRF-validated,
  and an IP host must be private/loopback. A hostname must first be an admitted
  in-VPC shape (`localhost`, a dotless service name such as `vllm`, or a
  `.local` / `.internal` / `.localhost` name) or be explicitly listed in
  `allowed_private_vllm_hosts`; the guard then resolves it on **every provider
  attempt** and requires a non-empty answer containing only private/loopback
  addresses. Resolution failure, a public address, or a mixed private/public
  answer is denied. Public/special IP literals cannot appear in the private
  allowlist at all (a redundant private-literal entry is tolerated for generated
  config). An external GPU must be reached through its RFC1918/ULA overlay
  address (for example WireGuard), never its provider public IP or public DNS.
  This genuinely private lane remains available under `AIR_GAP`.

  DNS validation is fail-closed but is not connection pinning: the provider
  client resolves again when it connects. A deployment must still enforce the
  network egress boundary underneath the process to close DNS rebinding. The
  shipped GPU rig endpoint commands avoid that residual by emitting a private
  IP literal only.

`AIR_GAP` (SPEC C2): when engaged, a frontier-lane call raises
`AirGapViolationError` **before litellm is ever reached** — proven by tests that
monkeypatch `Router.acompletion` and assert it is never invoked. The bare global
`AIR_GAP=1` environment flag (accepts `1`/`true`/`yes`/`on`) is read **per call**
and ORed with `NE_EGRESS_AIR_GAP` / `air_gap` in settings, so it can only ever
**add** restriction — it always wins toward blocking.

Config surface (`NE_EGRESS_` env / `ne.toml`, layered `NeSettings`):

```toml
air_gap = false
allowed_frontier_hosts = ["api.anthropic.com", "api.openai.com"]
allowed_private_vllm_hosts = ["vllm.prod.acme.com"]  # private-DNS name; every
                                                       # answer must be private
```

Env list form uses pydantic-settings JSON:
`NE_EGRESS_ALLOWED_FRONTIER_HOSTS='["api.anthropic.com"]'`. Host entries are bare
hostnames only — a scheme/port/path entry raises `GatewayConfigError` at
construction, and `allowed_private_vllm_hosts` additionally rejects every
public/special/packed-IP spelling. Hosts are normalized (lowercased, trailing
dot stripped) so `API.OpenAI.com.` matches `api.openai.com`.

Wiring:

```python
from ne.gateway import LiteLLMGateway, build_egress_guard

gw = LiteLLMGateway(settings, cost_sink, egress_guard=build_egress_guard())
```

**Deliberate design decisions:** `egress_guard=None` remains a **no-op** by the
frozen NE-010 seam contract (its unit tests depend on it) — production
composition roots (NE-013/014) MUST pass the real guard via
`build_egress_guard()`; `None` is never a silent open-egress default in
production. And there is **no silent cross-lane fallback** under `AIR_GAP`: a
frontier denial propagates unwrapped (a policy denial is not a provider failure —
no fallback, no `CostEvent`); air-gapped deployments route tasks to the private
lane explicitly (`request.lane = Lane.PRIVATE_VLLM`).

## Pre-egress redaction (NE-162)

Every `FRONTIER_API` candidate attempt is redacted, fail-closed, before litellm
is ever reached — the frontier lane's outbound body is redacted-by-construction,
never opt-in.

```python
from ne.gateway import LiteLLMGateway

gw = LiteLLMGateway(settings, cost_sink, redactor=my_redactor)
```

`redactor: ne.contracts.governance.Redactor | None = None` is a keyword-only
injection, syntactically identical to `egress_guard` — but **the opposite
default safety**. `egress_guard=None` is a documented no-op (see above);
`redactor=None` is a **REFUSAL**: any `FRONTIER_API` model configured in
`settings.models` raises `RedactionRequiredError` at construction, and a
per-attempt runtime check re-raises it defensively (in case `settings.models`
is mutated after construction). There is no code path where a frontier body
egresses unredacted because a redactor happened to be omitted. `PRIVATE_VLLM`
candidates are never touched — redacting the sovereign lane would degrade
quality for zero sovereignty gain.

Redaction runs on a **per-attempt copy** of the full outbound body (every
message plus every merged param), never the shared `messages`/`params` objects
built once per request — a multi-candidate fallback chain redacts *only* its
frontier hop(s), and a multi-turn conversation is fully re-redacted on *every*
frontier egress (no incremental/cached redaction). Every string leaf is walked
— system prompt, all message contents, retrieved/RAG context embedded as
message content, tool schemas, tool-call arguments, tool results, and every
string-valued merged param (`ne.gateway.redaction.redact_payload`) — except a
message's top-level `role` (the wire-protocol enum), which is preserved
verbatim. This is deliberately **not** `redactor.redact(json.dumps(body))`:
serializing first risks corrupting the JSON structure with a replacement
string, and a `messages[].content`-only walk would silently leave tool
schemas/arguments/results unredacted while claiming redaction ran.

A `RedactionError` (or any redactor exception) **aborts the attempt
unwrapped** — exactly like an `egress_guard` denial, it propagates past the
provider try/except so it can never be swallowed into the fallback list and
silently rolled to another frontier candidate. A frontier provider failure is
independently sanitized: its exception text is reduced to
`FrontierProviderError(type(exc).__name__)` — the class name only, never
chained via `__cause__` — before it can reach `_redacted_attempts` or any
audit detail, since provider 4xx text can echo prompt substrings. Private-lane
provider failures are unaffected (full original exception text, as before).

A `redact.pre_egress` `AuditEvent` is emitted per frontier attempt through the
injected `audit_sink` — `outcome=success` with the request-wins `trace_id` and
a **hit count + per-category counts only** (`provenance["redaction_hits"]`,
`provenance["redaction_categories"]`), or `outcome=denied` with the exception
class name only, on a redaction failure. **Never** the raw or redacted body
text, and never backend error text (e.g. Presidio stderr, which may itself
carry PII) — an audit chain is append-only, so a leaked false-negative there
is permanent.

**Honest-scope caveat:** this is *structured* PII/secret reduction via the
injected `Redactor` (Presidio by default, `ne-govern`'s
`create_redactor`/`RedactionStage`) — it is **not** a zero-PII proof. It
catches what the backend's recognizers are configured to catch; it does not
guarantee the complete absence of every conceivable PII pattern in free text.

### SSRF endpoint validation

`ne.gateway.ssrf.validate_endpoint_url` guards admin-registered vLLM/Ollama
endpoints (applied lane-aware inside `entry_to_model_def` at registration). It
rejects cloud-metadata IPs (`169.254.169.254`, `metadata.google.internal`),
**packed-integer IPv4 forms** (`inet_aton`-resolvable decimal / hex / octal, e.g.
`http://2852039166` == `169.254.169.254` — rejected unconditionally, even on the
private lane), link-local / multicast / reserved / unspecified IP literals (the
metadata range is blocked even in-VPC), credentials-in-URL, query/fragment
components, and non-`http(s)` schemes — always; private ranges, loopback, and
`*.local`-style DNS are additionally rejected unless the private-vllm lane
(`allow_private`). It **complements** the egress allowlist. Its companion
`redact_url_for_logs` masks embedded credentials before any endpoint string is
echoed into an error or log — including **non-`//netloc` credentialed URLs**
(scheme-less `user:pass@host/v1` or single-slash `https:/user:pass@host`, which
`urllib` does not expose as `parsed.username`/`password`), which are regex-masked
from the raw string (fail-closed: over-masking is accepted, under-masking is
not). This code is derived from ABI
(<https://github.com/jupyter-naas/abi>) `provider_runtime.py` endpoint
validation — MIT License, Copyright (C) 2025 Naas.ai (see `NOTICE`).

### C3 import-linter contract

The C3 model-independence chokepoint is now enforced mechanically: a `forbidden`
import-linter contract at [`/.importlinter`](../../.importlinter)
(`c3-provider-sdk-chokepoint`) forbids importing `litellm` / provider SDKs
anywhere across the 20 `ne.*` portions except the sole ignored edge
`ne.gateway._litellm -> litellm`. It runs in CI as the **Import contracts (C3)**
step (`uv run lint-imports --config .importlinter --no-cache`); the existing
`test_c3_chokepoint` grep test stays as belt-and-braces.

## Future seams (only the hooks exist here)

- **NE-013** private vLLM lane — a real `private-vllm` serving path.
