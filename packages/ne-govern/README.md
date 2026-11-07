# ne-govern

The **C6 governance chokepoint** + the **C12 deterministic guardrails**: an
OPA-backed `PolicyDecisionPoint` (PDP) whose every decision is inseparably
coupled to a tamper-evident audit event, plus the pre-model, code-enforced
backstops (spend caps, rate limits, recipient/egress allowlists,
injection-resistant tool-arg validation, PII redaction) a fully prompt-injected
agent cannot talk past. Every agent tool call passes the PDP **and** the
guardrails **and** emits an `AuditEvent` — with no bypass path (SPEC C6/C12).

NE-033 wires all of this as non-optional middleware via the seams
`create_pdp(settings, sink=…)`, `create_guardrail_engine(settings, sink=…)`,
`create_redactor(settings)`, and `build_tool_call_payload(...)`.

## Architecture

```
                    (versioned Rego bundle: policies/, SPEC §4.2 tiers)
                                        │
 PolicyRequest ──► OpaClient ──HTTP──► OPA sidecar  POST /v1/data/ne/govern/decision
 (principal,          │                              └► {effect, tier, reason,
  action,             │ _RawOpaResult (NOT a               obligations, policy_version}
  resource,           │  PolicyDecision; unexported)
  context)            ▼
                 GovernedPdp.decide()
                    │  1. evaluate via OpaClient (undefined/HTTP error ⇒ fail closed)
                    │  2. build PolicyDecision
                    │  3. append AuditEvent to the AuditSink  ◄── MUST succeed
                    ▼      (append failure ⇒ AuditEmissionError, decision withheld)
                 PolicyDecision   ──► returned only after the audit append committed
                                      AuditEvent ──► ne-audit hash chain (NE-030)
```

The returned `PolicyDecision` now carries the bundle-**decided** `tier` (1.2.0,
ADR-0004) — the same int already recorded in the audit `detail` — so the runtime
HITL gate can compose stricter-of and never under-tier what the bundle decided
(NE-CKD3, Checkpoint-D finding #3). `obligations` (second_check / named_approver /
per-item flags) rides the same decision.

### No-bypass, by construction

Three structural facts make C6 non-optional rather than a convention:

1. **`OpaClient` cannot mint a `PolicyDecision`.** It returns a private,
   unexported `_RawOpaResult`; only `GovernedPdp` translates that into a
   `PolicyDecision`, and only along the audited path. `OpaClient` /
   `_RawOpaResult` are deliberately absent from `ne.govern.__all__`.
2. **Audit-append-before-return.** `GovernedPdp.decide()` appends the
   `AuditEvent` *before* returning; if `AuditSink.append` fails, it raises
   `AuditEmissionError` and no decision escapes.
3. **No sink-less path.** `GovernedPdp(opa, sink)` requires the sink positionally
   and `create_pdp(..., sink=…)` requires it as a keyword — there is no
   constructor that omits it.

Both failure surfaces **fail closed**: an OPA transport error or an `undefined`
result raises `PolicyBackendError` (after auditing an `error` outcome), and an
audit-sink failure raises `AuditEmissionError`. A broken or unloaded policy
bundle can never resolve to `allow` — OPA `undefined` is treated as an error.

## SPEC §4.2 approval tiers (the normative policy)

The Rego bundle under `policies/` encodes the tiers. Classification is anchored
on the **action class** — an action is classified only when it matches an
action-class prefix below. The caller-declared tier (`context.tier`, honored
only when it is an integer 0–3) can only **escalate** an already-classified
action (stricter-wins); it can never classify an otherwise-unknown action, so a
caller cannot self-classify a `mystery.action` into an allowed decision by
declaring a low tier. The **effective tier is the stricter of** the declared
tier and the action-class tier:

| Effective tier | Action-class prefixes             | Effect             | Obligations                          |
| -------------- | --------------------------------- | ------------------ | ------------------------------------ |
| 0              | `read.*`, `draft.*`               | `allow`            | (none)                               |
| 1              | `write.internal.*`                | `allow`            | `daily_digest`                       |
| 2              | `write.external.*`, `*.pii.*`     | `require-approval` | `per_item_human_approval`            |
| 3              | `finance.*`, `legal.*`            | `require-approval` | `named_approver`, `second_check`     |

Deny cases: a missing/invalid principal, or an action that classifies to no tier,
both `deny`. Example escalation: a caller declaring tier 0 on a `finance.*`
action is decided at **tier 3** (stricter wins). C12: tier-3 actions can never
resolve to `allow`.

### `subagent.route` routing authority (NE-169)

`packages/ne-govern/policies/ne/govern/subagent.rego` classifies the
`subagent.route` action — the frontier sub-agent routing AUTHORITY consulted by
`GovernedSubAgentDispatcher` at the point a child's lane is resolved (design
§10.2). `route_tier` in `input.context` is computed by the TRUSTED dispatcher
from the server-registered `AgentDefinition`, never from model input:

| `route_tier`            | Effect  | Tier   | Reason                                                              |
| ------------------------ | ------- | ------ | -------------------------------------------------------------------- |
| `private` / `unpinned`    | `allow` | 0      | `tier 0: read/internal draft — autonomous, logged`                    |
| `frontier`                | `deny`  | `null` | `subagent.route: frontier-lane routing requires an explicit grant — default deny` |
| unknown / missing         | `deny`  | `null` | `unclassified action`                                                 |

Frontier routing is a **total, explicit-grant-only deny** in v1 — the grant
mechanism (e.g. a principal attribute or a policy tier) is a deliberate, named
follow-up parameter, not implemented here (consistent with the NE-161 runtime
gate's own default-closed posture).

### Provisional-context escalation (NE-T2X-5, design §6.3)

Write-class (`write.*`) actions escalate **exactly one tier** when the
retrieval-derived `input.context.context_provisional` flag is `true` (the
JSON boolean — a string `"true"` or the number `1` does NOT escalate, on
either backend): Tier 1 → Tier 2, Tier 2 → Tier 3, so SPEC §4.2's
"never auto-approve" semantics apply to context the platform cannot yet
vouch for. Escalation clamps at Tier 3 by construction (an already-Tier-3
`finance.*`/`legal.*` action never escalates further — it reaches the
existing unescalated rule). `input.context.extraction_mode == "autonomous"`
(YOLO) explicitly opts OUT of escalation — the flag still threads into the
PDP input, it simply never fires — that is the DECLARED meaning of YOLO
(design §6.3), not an oversight. Absent/garbage `extraction_mode` fails
TOWARD escalation: only the literal string `"autonomous"` is exempt.

| Tier (unescalated) | Escalated tier | New reason                                                                          | New obligation           |
| ------------------- | -------------- | ------------------------------------------------------------------------------------ | ------------------------- |
| 1 (`write.internal.*`) | 2           | `tier 2: write on provisional context — escalated one tier, per-item human approval required` | `provisional_context: true` (+ `per_item_human_approval`) |
| 2 (`write.external.*`/`*.pii.*`) | 3 | `tier 3: write on provisional context — escalated one tier, named approver plus a second check` | `provisional_context: true` (+ `named_approver`, `second_check`) |

Implemented identically in **both** backends: `tiers.rego`'s `escalates`/
`final_tier` plus `decision.rego` rules `(e2)`/`(f2)`, and `decision.cedar`'s
`ESC` fragment ANDed into `(d)`/`(e)` (as `!(ESC)`) plus new rules
`(e2_tier2_provisional)`/`(f2_tier3_provisional)` — proven to agree via 9
vectors in `tests/vectors/tier_vectors.json` (prefixed `provisional-`) that
ride the SAME cedar-vs-oracle/live-opa/dual-PDP battery every other vector
does. The §6.3 guardian (a Tier-1 write with provisional context in
`read_flagged` mode cannot execute without a human approval) is proven
through the REAL in-process Cedar engine in
`packages/ne-runtime/tests/test_provisional_escalation_guardian.py`.

## Versioned policy bundle

The bundle version is stamped by the policy itself
(`policies/ne/govern/version.rego` → `policy_version := "1.2.0"`) and mirrored in
`policies/.manifest` (`revision`). A test enforces the two are equal; **git
history is the authoritative version log**. Every `PolicyDecision` carries the
`policy_version` that decided it (traceability).

**Bump procedure:** edit `version.rego`'s `policy_version` **and** `.manifest`'s
`revision` together (the equality test guards drift), then commit.

Build the deploy artifact (excludes `*_test.rego`) with `build_bundle`:

```python
from pathlib import Path
from ne.govern import build_bundle
build_bundle(Path("packages/ne-govern/policies"), Path("dist/bundle.tar.gz"))
```

## Testing

- **PDP logic + audit coupling** run in CI with the OPA HTTP wire stubbed
  (`httpx.MockTransport`) and the audit half against the **real** ne-audit
  `PostgresAuditSink` over in-memory SQLite (no mock past the seam under test).
- **Rego validation** (`opa test` + per-vector `opa eval`) runs on **every PR**:
  CI installs a SHA256-pinned `opa` 1.18.2 (matching the `deploy/compose` sidecar
  pin) and a dedicated anti-skip gate step fails the build if the Rego module ever
  skips again, so the real OPA evaluation of the tier policy is a per-PR gate. It
  still auto-skips on local hosts without the binary:

  ```
  opa test packages/ne-govern/policies -v
  ```

  Runs wherever the `opa` binary is on PATH; the shared decision vectors live in
  `tests/vectors/tier_vectors.json`.
- **Real-OPA end-to-end** (`test_opa_pdp_live.py`) runs wherever the `opa` binary
  is on PATH — including CI — and skips only where it is absent:

  ```
  uv run pytest packages/ne-govern/tests/test_opa_pdp_live.py -q
  ```

  ⛔ **No sidecar, no `ne dev up`, and no `NE_GOVERN_OPA_URL`.** The test STARTS ITS
  OWN OPA (`tests/conftest.py::disposable_opa_url`), serving the shipped Rego
  read-only from disk on a kernel-assigned loopback port, and tears it down. It
  uploads nothing to anything. It used to `PUT` the working tree's Rego into
  `NE_GOVERN_OPA_URL` and this README used to print
  `NE_GOVERN_OPA_URL=http://localhost:8181` as the recipe — on a box running the
  stack that port IS the deployed PDP, and running the recipe took governance down
  on 2026-08-30 (`eval_conflict_error` on every decision where the uploaded and
  deployed `tiers.rego` disagreed). Never point a test at a deployed engine;
  `packages/ne-mcp/tests/test_opa_pdp_mutation_guardian.py` now fails any test that
  tries. Every vector still runs through the real `GovernedPdp` + `OpaClient` over a
  real HTTP wire.

## Cedar PDP alternate (optional extra)

`GovernedPdp` (OPA) is the **default** `PolicyDecisionPoint` and is
byte-unchanged by this feature. `CedarGovernedPdp` is an **alternate**
in-process backend, backed by [cedarpy](https://github.com/k9securityio/cedar-py)
(Python bindings for AWS's Cedar Policy engine), registered under the SAME
`ne.plugins.policy_decision_point` entry-point slot as `opa`. It exists as the
platform's "no vendor bet" governance-plane proof: the SPEC §4.2 approval-tier
policy is expressed independently in two policy languages/engines, behind one
interface, with CI enforcing that the two NEVER diverge.

**Install:**

```
pip install 'ne-govern[cedar]'
```

`cedarpy` is an **optional extra** (`[project.optional-dependencies] cedar`)
plus a **dev dependency** (so the parity suite below runs in CI under
`uv sync --all-packages`) — it is **never** a `[project.dependencies]` runtime
dep. An OPA-only deployment never installs or imports `cedarpy`; `import
ne.govern` alone never imports `cedarpy` or `ne.govern.cedar` (test-enforced by
`test_cedar_is_lazy_and_unexported`).

**Select it:**

```python
from ne.govern.config import GovernSettings
settings = GovernSettings(pdp="cedar")   # or NE_GOVERN_PDP=cedar
```

`create_pdp(settings, sink=...)` then resolves `CedarGovernedPdp` instead of
`GovernedPdp`. Everything downstream (NE-033 middleware, the HITL gate,
audit consumers) is unaffected — both backends implement the identical
`PolicyDecisionPoint` Protocol and produce the identical `PolicyDecision`
shape.

**How it works:** `packages/ne-govern/src/ne/govern/cedar/decision.cedar`
mirrors the normative Rego bundle (`policies/ne/govern/{decision,tiers,version}.rego`)
policy-for-policy: eight mutually-exclusive, complete rules, the same
`@policy_version` (`"1.2.0"`), byte-identical reason strings/obligations/tier
semantics. The unexported `CedarEvaluator` probes each rule's boolean condition
through the real Cedar engine (`cedarpy.is_authorized`) and enforces, at
runtime, that **exactly one** rule matches any given input — any parser defect,
overlap, or gap fails CLOSED as `PolicyBackendError`, never open.
`CedarGovernedPdp` mirrors `GovernedPdp`'s C6 audit coupling file-for-file: no
sink-less constructor, audit-append-before-return, backend failure audits an
`error` outcome then re-raises.

**Governance-parity swap suite** (`tests/test_cedar_parity.py`) runs the FULL
vector battery (`tests/vectors/tier_vectors.json`) through:

- Cedar vs. the shared oracle (full-document equality — effect, tier, reason,
  obligations, `policy_version`) — **always** executes.
- Cedar vs. **live** `opa eval` (same full-document equality) — executes
  wherever the pinned `opa` binary is on PATH; CI installs `opa` 1.18.2, so
  this executes on **every PR**.
- Live `opa eval` vs. the shared oracle (proves the oracle itself).
- `GovernedPdp` vs. `CedarGovernedPdp` decision **and** audit-event parity
  (identical `PolicyDecision` fields, identical audit action/outcome/detail/
  provenance) for every vector.

Any divergence anywhere in that suite is a test failure.

**Honest Cedar divergence (NE-169):** the Cedar bundle deliberately does **NOT**
yet carry the `subagent.route` rule — `CedarEvaluator` hard-codes exactly eight
rules, and `subagent.route` is unclassified in Cedar today, so it **denies**
that action (the "unclassified action" outcome) rather than permitting a
private-lane route the way the Rego bundle now does. This is a conservative,
**deny-more** divergence — it never weakens enforcement, only under-permits
relative to Rego — and adding Cedar parity for `subagent.route` is a named
follow-up. Accordingly, `subagent.route` vectors are **intentionally not**
added to `tests/vectors/tier_vectors.json`: doing so would break the always-on
Cedar/OPA/oracle parity battery above for a rule Cedar doesn't have yet.

### Semantic-gap analysis (SPEC-required — conclusion: NO enforcement-weakening gap)

1. **Cedar's typed-entity model vs. OPA's raw JSON `input`.** All
   decision-relevant values travel in Cedar's `context` map under fixed,
   non-attacker-controlled entity UIDs (`NE::Principal::"input"` /
   `NE::Action::"decide"` / `NE::Resource::"input"`) — no untrusted string is
   ever spliced into Cedar surface syntax, mirroring OPA's `input` document.
   `resource` is never read by any rule in either engine.
2. **Cedar's binary Allow/Deny vs. the three-valued NE effect
   (allow/deny/require-approval).** The NE effect rides each rule's
   annotations, not Cedar's own decision. The Cedar **keyword** on every rule
   is chosen conservatively: only the two `allow` rules are Cedar `permit`;
   every `deny`/`require-approval` rule (including BOTH tier 2 and tier 3) is a
   Cedar `forbid`. A hypothetical plain-Cedar evaluation of this file (ignoring
   the annotations and the evaluator) could therefore only ever be MORE
   restrictive than the NE semantics — never less (C12 preserved by
   construction, and load-time-verified by the "conservatism guardian" check).
3. **Rego's `max()`/stricter-of tier aggregation has no direct Cedar
   aggregate.** It is compiled into six mutually-exclusive, complete rule
   conditions (equivalence argued rule-by-rule in `decision.cedar`'s header
   comment) and enforced at RUNTIME by the exactly-one-match invariant: any
   condition overlap or gap — a parser bug, a hand-edit mistake — fails CLOSED
   rather than silently picking a (possibly wrong) winner.
4. **Rego's `is_number`/`round` declared-tier semantics vs. Cedar's `Long`
   type.** `CedarEvaluator`'s marshalling admits only integers and
   integral floats within the i64 range as a declared tier (booleans, strings,
   fractional floats, and out-of-range values are OMITTED from Cedar's context
   entirely) — exactly mirroring `tiers.rego`'s `valid_declared` filter. The
   0..3 range check itself is enforced IN Cedar via each escalation fragment's
   `== k` equality.

Every candidate divergence above resolves to equal-or-stricter, and the
full-document (reason-included) parity suite enforces byte-equality on every
vector as the final backstop.

## C12 deterministic guardrails

The guardrails are **sync, deterministic, pre-model** checks — no model call, no
network, no LLM judgment anywhere in `ne.govern.guardrails`. Injection is
ASSUMED; every check is **reject-by-default** on an unknown tool, argument, or
shape. `GuardrailEngine` composes them and, mirroring the PDP's C6 coupling,
audits every BLOCK before raising.

Four guardrails ship:

| Guardrail                    | Blocks (rule prefix)                                        |
| ---------------------------- | ---------------------------------------------------------- |
| `SpendCapGuardrail`          | per-principal / per-department USD ceilings (`spend.*`)     |
| `RateLimitGuardrail`         | per-principal / per-tool sliding-window floods (`rate.*`)   |
| `RecipientAllowlistGuardrail`| non-allowlisted email recipients + egress hosts (`recipients.*`/`egress.*`) |
| `ToolArgGuardrail`           | unknown tool/arg, path traversal, SQL/command injection, oversized/binary (`toolargs.*`) |

A composition root MAY inject **additional** deterministic rails via
`create_guardrail_engine(..., extra_guardrails=(rail, …))`: they are appended
**after** the four base rails (which stay first and byte-identical) and run inside
the same audited engine, inheriting the block-audit / fail-closed coupling for
free. `extra_guardrails` is typed by the frozen
`ne.contracts.governance.Guardrail` Protocol precisely so injecting a
cross-package rail (canonically ne-identity's `AgentScopeGuardrail`, the sole
`CredentialScope.tools` enforcer — NE-036) never forces ne-govern (or ne-runtime)
to import the rail's package. Extras must be **additional** rails, not duplicates
of the base four (a second `SpendCapGuardrail` would be double-precharged —
over-restrictive, not a bypass).

`VerifiedDomainGuardrail` (`verified.unrouted`) is an opt-in FIFTH rail shipped
by this package but deliberately NOT assembled by `create_guardrail_engine` —
the verified-answer-first chat composition (NE-VAF-U3/U4) injects it via
`extra_guardrails`. It fails only when a payload carries `verified_domain`
truthy with `routed` falsy (a verified-domain query that did not resolve to a
verified template → audited refusal, never free text); ordinary tool-call
payloads never set `verified_domain`, so registering it is inert — zero false
blocks.

`GroundingGuardrail` (`grounding.ungrounded-claim`) is the grounded-agent
sibling of the same pattern — an opt-in rail shipped here but NOT assembled by
`create_guardrail_engine`; the grounded-agent runtime post-step (NE-GA-G3)
injects it via `extra_guardrails`. It fails only when a payload carries
`grounded_gate` truthy with `grounded` falsy (the grounding gate marked a
drafted turn ungrounded → audited refusal, never free text); per C12 the
payload/audit carry only the rule code and boolean flags — the gate reduces
findings to class+count, never finding values. Ordinary tool-call payloads
never set `grounded_gate`, so registering it is inert — zero false blocks.

A **PRESENT-but-malformed payload shape is itself a violation** — the field is
never coerced or skipped (C12 reject-by-default). A non-mapping `args` blocks with
`toolargs.malformed-args`; a `recipients`/`egress_urls` that is not a string or a
sequence of strings (a mapping, number, set, bytes, or a sequence with a non-str
item) blocks with `recipients.malformed-shape` / `egress.malformed-shape`. Only a
genuinely absent/None field is treated as no-args / no-outbound.

Malformed **rate rules** (`window_s <= 0` or non-finite, or `max_calls < 0`) are
rejected at construction with `GuardrailConfigError` — fail-closed, the same
name-the-bad-key pattern as spend caps, so a bad window can never silently
disable rate limiting (`max_calls == 0` stays legal: it blocks every call).
The **structured** tool-arg kinds (`identifier`/`path`/`email`/`url`) reject all
control characters incl. CR/LF/TAB (CRLF header/log-injection resistance) with
`toolargs.control-chars`; the free-text kind deliberately keeps `\t\n\r`.

### Payload contract

Every guardrail's `check(payload)` consumes one canonical `Mapping` — build it
with `build_tool_call_payload` (the NE-033 seam helper):

```python
from ne.govern import build_tool_call_payload, create_guardrail_engine

engine = create_guardrail_engine(settings, sink=audit_sink)  # sink REQUIRED
payload = build_tool_call_payload(
    principal=principal, tool="send_email",
    args={"to": "ok@corp.com", "body": "…"},
    recipients=["ok@corp.com"], egress_urls=[], estimated_cost_usd="1.00",
)
verdicts = await engine.enforce(payload)   # raises GuardrailViolation on any block
```

### Engine / audit semantics (C6 mirror)

* `sink` is a **required positional** ctor arg — there is no sink-less
  `GuardrailEngine`, so a block cannot escape un-audited.
* Any FAILED verdict → exactly one `guardrail.block` `AuditEvent`
  (`outcome=denied`) is appended **before** `GuardrailViolation` is raised.
* A guardrail that raises internally → a `guardrail.error` event + a
  `GuardrailEvaluationError` (fail closed).
* An audit-append failure → `AuditEmissionError` (chained); no decision escapes.
* `new_audit_event` construction stays **outside** the try, so
  `ConfigHashNotFrozenError` propagates.
* **Audit-redaction discipline:** raw arg / recipient / context VALUES never
  reach the audit `detail`/`provenance` — only rule names + arg keys + violation
  classes.
* `check` is a side-effect-free predicate; the rate-counter increment and
  pessimistic spend pre-charge happen in the engine on the **all-pass** path
  (NE-033 reconciles actuals via `CostEvent`s later).

### Atomic reservation under multi-worker Postgres (NE-070H)

`check()` stays the **side-effect-free fast pre-check**; the all-pass path is now
the **authoritative atomic reservation** (`_reserve_on_pass`), which closes the one
**fail-OPEN** race here — the check-limit-then-charge TOCTOU where concurrent tool
calls could both pass `check()` and overshoot a cap.

* **Spend** is charged by an atomic conditional `UPDATE ... WHERE total + amount <=
  cap` per scope, multi-scope **all-or-nothing** in one transaction. On Postgres the
  row lock serializes concurrent chargers, so a cap C can **never** be overshot; the
  loser's charge matches zero rows and the whole transaction rolls back.
* **Rate** is reserved by an atomic prune → count → conditional-append under a row
  `FOR UPDATE` (Postgres) / writer lock + in-process lock (SQLite).
* **Rate is reserved before spend** deliberately: if the spend charge then loses, the
  worst residue is a phantom rate event (fail-safe over-count), never phantom spend
  (`SpendLedger.charge` is all-or-nothing).
* A reservation loss rides the **same** audited-`guardrail.block` + `GuardrailViolation`
  path as a check-time block (same rule vocabulary `spend.cap-exceeded` /
  `rate.limit-exceeded`), and `AuditEmissionError` still fires if the block itself
  can't be audited — **C12 is strengthened, never weakened**.
* **Money is exact:** spend is stored as integer **nano-USD** (`BigInteger`) with
  pessimistic rounding (charges ceil, caps floor); `sa.Numeric` is deliberately
  avoided (SQLite degrades it to a binary `REAL`).
* **Durability + restart survival:** inject `SqlAlchemySpendLedger` +
  `SqlAlchemySlidingWindowCounter` via
  `ne.runtime.factory.create_shared_guardrail_state(database_url=...)`; caps/limits
  survive worker restarts and are shared across all workers. The counter uses a
  wall-clock epoch shared across workers — keep hosts NTP-synced; bounded clock skew
  only widens/narrows the effective window, never breaks the count-based cap.
* **Proven on live Postgres** (the fail-OPEN priority proof): 16 concurrent workers
  charging \$1 against a \$10 cap admit **exactly 10**, ledger total exactly \$10, the
  6 losers each audited; rate leg admits exactly `max_calls`
  (`tests/test_guardrail_concurrency_postgres.py`; a red-control shows the pre-fix
  check-then-record overshoots under the identical harness).

## Redaction (backend-agnostic, fail-closed)

`RedactionStage` implements the frozen `Redactor` protocol over a pluggable
backend and **fails closed**: if a backend raises for any reason it raises
`RedactionError` and the unredacted text never escapes.

* **Presidio, ADOPTED VIA ISOLATION (default).** Microsoft/​data-privacy-stack
  Presidio (MIT) is the industry-standard, NER-backed detector — the adopt-first
  default (SPEC §11 build-vs-adopt). Its spaCy + `en_core_web_lg` (~560 MB)
  closure is the heavy-scientific-Python class SPEC §11 keeps **out of** the
  platform `uv.lock` (C4 license-gate false-positive exposure + sovereign-deploy
  bloat), so it is confined to an **isolated, hash-pinned** venv
  (`packages/ne-govern/.presidio-venv`) and driven over a subprocess JSON
  protocol — the exact Docling `parser_env` pattern. **The platform package
  gains zero new third-party runtime dependencies** (the one first-party
  workspace edge to `ne-brain` is the interim sovereignty-primitive reuse noted
  below). Under `AIR_GAP` a missing/stale `.presidio-venv` fails closed with a
  typed `ne.brain.sovereignty.AirGapViolationError` before any build egress
  (C2/C12; interim first-party `ne-brain` edge for the sovereignty primitive,
  hoist deferred to the SSRF-consolidation unit).
* **Regex fallback = SUPPLEMENT only.** `RegexRedactor` is a stdlib-only
  air-gap fallback (email/phone/SSN/credit-card-with-Luhn/IP/IBAN), **never** the
  primary — reimplementing PII detection invites compliance-grade false negatives.

### Closure findings (adopt-vs-isolate, verified 2026-07-05)

`presidio-analyzer==2.2.363` / `presidio-anonymizer==2.2.363` (both MIT, released
2026-06-28) pull spaCy (`spacy==3.8.13` — the analyzer pin **excludes** the newest
`3.8.14`), thinc/blis/numpy, and the `en_core_web_lg==3.8.0` model wheel (MIT,
GitHub release). Presidio's repo moved `microsoft/presidio` →
`data-privacy-stack` org (license unchanged, MIT). All of this lives **only** in
`presidio_env/requirements.{in,txt}`, never the platform lock;
`test_presidio_env_policy.py` regression-guards the exclusion.

### Building / running the isolated env

```
# Recompile the hash-pinned pins (network OK; never hand-edit requirements.txt):
uv pip compile packages/ne-govern/presidio_env/requirements.in \
    --universal --python-version 3.12 --generate-hashes \
    --override packages/ne-govern/presidio_env/overrides.txt \
    -o packages/ne-govern/presidio_env/requirements.txt

# Live end-to-end test (builds the ~560 MB venv on first run):
NE_PRESIDIO_LIVE=1 uv run pytest packages/ne-govern/tests/test_presidio_live.py -q
```

The `--override` lifts presidio-anonymizer's stale `cryptography<47` ceiling so
the recompile takes the **GHSA-537c-gmf6-5ccf** fix (`cryptography>=48.0.1`,
resolves 49.0.0); see `presidio_env/README.md` and `presidio_env/overrides.txt`
for the full rationale and the unpin trigger.

The venv is (re)built lazily by `PresidioRedactor.redact` via `uv venv` +
`uv pip install --require-hashes --no-deps` when a stored requirements-sha marker
no longer matches (C11). (`--no-deps` is required: `requirements.txt` is the
complete compiled closure, and without it uv's hash-mode installer re-resolves
metadata and hard-fails on presidio's overridden `cryptography<47` ceiling — see
`presidio_env/README.md`.) The rebuild is **filelock-serialised** (NE-020 P2a): a sibling
`.presidio-venv.lock` (kept outside `venv_dir` so it survives the rebuild
`rmtree`) plus an in-lock double-checked marker fast-path means two concurrent
`redact()` calls perform exactly one build, never two installs at once. For
installed/production layouts outside a monorepo checkout, pass
`presidio_env_dir=`/`venv_dir=` or set
`NE_GOVERN_PRESIDIO_ENV_DIR`/`NE_GOVERN_PRESIDIO_VENV_DIR`.

## NE-033 wiring

```python
from ne.govern import create_pdp, create_guardrail_engine, create_redactor

pdp = create_pdp(settings, sink=audit_sink)               # sink REQUIRED — no sink-less path
engine = create_guardrail_engine(settings, sink=audit_sink)  # sink REQUIRED
redactor = create_redactor(settings)                      # defaults to Presidio (adopt-first)

verdicts = await engine.enforce(payload)  # guardrails: block in code (pre-model)
decision = await pdp.decide(request)      # PDP: gate tier-2/3 to a human
```

Together these make the **C12 compose invariant** hold: a fully hijacked agent is
either BLOCKED in code by the engine or GATED to a human by the PDP
(`REQUIRE_APPROVAL`) for every tier-3 action — proven by
`tests/test_redteam_c12.py`.

## Grounding gate core (NE-GA-G1)

`ne.govern.grounding` is the **pure, deterministic, model-free** containment
check that is THE guarantee of the grounded agent
(`docs/architecture/anti-hallucination-grounding.md`): every FACTUAL token
of a drafted answer must be traceable to THIS turn's fetched `VerifiedAnswer`
rows (`GroundingCorpus.from_answers`) or a pinned prose allowlist, or the
draft is withheld. `check_grounding(draft, corpus, prompt)` classifies every
token — numeral, IRI/URN, citation marker `[k]`, or prose — into one of six
finding classes: `numeral-ungrounded`, `numeral-unparseable`,
`iri-ungrounded`, `citation-out-of-range`, `vocab-ungrounded`, and
`phrase-recombination` (the anti-recombination rule that blocks minting
"John Smith" out of grounded cells "John Doe" + "Anna Smith"). A
`GroundingFinding` carries ONLY a reason class + a token count — the actual
ungrounded text never leaves the module (C12).

**Fail-closed posture:** an unparseable numeral is an explicit finding, never
silently skipped; a `truncated` answer excludes every DERIVED aggregate
(count/distinct-count/frequency/pair-frequency/sum/min/max) while raw cell
numerals and ordinals still ground; an empty corpus (zero fetches this turn)
refuses every content token; a non-`VerifiedAnswer`-shaped input (a
`BLOCKED: ...` string, a malformed mapping, a row/column length mismatch)
contributes zero facts; numerals are NEVER prompt-exempt; an IRI is checked
on the RAW draft substring so punctuation/Unicode folding can never hide one.

**Red-team hardening (§2.7), one-liners:**

* **F1 (arg-echo poisoning):** a cell whose column name (casefold-)matches
  one of the answer's own `args` keys is dropped from the corpus — a
  caller-echoed column is not graph data.
* **F2 (percent/currency laundering):** a trailing `%` or currency symbol is
  meaning-bearing — `canonical_numeral("50%")` is the DISTINCT token
  `"50%"`, never plain `"50"`, so it only grounds against a literal
  percent/currency cell. The shared tokenizer also binds a `%`/currency
  symbol detached from its numeral by whitespace (e.g. `"50 %"`, `"$ 50"`)
  to that same distinct token, so detached forms cannot launder past the
  check.
* **F3 (aggregate-provenance adjacency):** every grounded number carries its
  provenance `(kind, column)`; a numeral only grounds if the SAME sentence
  also contains a grounded token from that provenance (a `SUM(amount)`
  numeral needs an `amount`-labelled word in-sentence — `"4200 orders"`
  refuses, `"the amount total is 4200"` grounds).
* **Envelope facts (NE-CTXB-4, truncation honesty):** `returned_rows`/
  `total_rows`/`row_limit` mint a `NumberFact(kind="envelope")` ALWAYS —
  including when the answer is truncated (that is the point) — anchored
  F3-style to template-id + column vocabulary, so `"Returned 37 of
  5,000,000 accounts"` is now speakable while a pooled/foreign use of the
  same numeral refuses. Each field is parsed fail-closed per field (a
  `returned_rows` that contradicts `len(rows)` is muted, never guessed).
* **Single-cell identity (NE-CTXB-4):** a `cell` numeral's own-row identity
  is also satisfied by full containment of any single non-numeric, non-IRI
  sibling cell's COMPLETE token set (the name cell `{acme, corp}`), so a
  Layer-3 count sentence grounds without ever speaking the row's URN
  citation tokens. Degenerate identities (a subset of a sibling row's set
  for the same column) are dropped at build, and the wrong-row
  full-containment + foreign-sibling refusals are preserved. These two
  additions moved `GATE_VERSION` via the pinned recompute-list mechanism.

**`CONNECTIVE_ALLOWLIST`** is a pinned, versioned wordlist shipped as the
packaged resource `grounding_allowlist.txt` and folded into `GATE_VERSION`
(`ng-1/<sha256(allowlist bytes + pinned-recompute-list id)[:12]>`) — any
edit moves the gate version. It covers articles/pronouns/prepositions/
conjunctions/auxiliaries/discourse-markers plus a small closed set of
reporting verbs/nouns, and DELIBERATELY EXCLUDES every comparative/
superlative, modal, and quantifier/approximator (test-enforced) — the agent
cannot assert free-text comparisons, only present grounded numbers side by
side and let the reader compare.

**C3/C12:** this module imports STDLIB ONLY — no `ne.runtime`, no
`ne.brain_ontology` (an AST guardian test positively allowlists every
importable root module); its own copy of the U1 pinned `normalize` pipeline
(NFKC → strip Unicode `Cf` → casefold → delete apostrophes → punctuation-fold
→ split) stays in lockstep with `ne.brain_ontology.verified.routing.normalize`
by a fixture vector file, never by import. This is the corpus + check unit
only (G1) — the runtime wiring (tool restriction, the post-turn gate, the
audited refusal) is a later unit (G3).

### Configurable strictness — dual profiles + tiers (NE-HGATE-1)

`check_grounding(draft, corpus, prompt, *, profile=PROFILE_NG1_FROZEN)` takes a
frozen **`GateProfile`** rulebook. Two ship, DEFINED in `ne.govern.grounding`
(stdlib-only, C3-intact) and re-exported from `ne.govern.grounding_policy`:

* **`PROFILE_NG1_FROZEN`** — today's exact bytes + rules; its `version` is
  literally `GATE_VERSION` (`ng-1/<hash>`). Every existing caller defaults to
  it, so the gate is **byte-identical** to before (the entire legacy
  `test_grounding.py` / `test_grounding_redteam.py` stay untouched and green).
  It is the regulated escape hatch.
* **`PROFILE_NG2`** — the expanded default rulebook: a superset **core**
  allowlist (`grounding_allowlist_ng2.txt`), a STANDARD-only **extended**
  lexicon (`grounding_allowlist_extended.txt`, the comparatives/quantifiers/
  modals family), a derived **number-word** lexicon
  (`grounding_number_words.txt`: `one`→`1` grounds through the SAME
  `NumberFact`/F3 machinery as digits; bare magnitude words dozen/million/… are
  always `numeral-unparseable`; never prompt-exempt, never allowlistable),
  corpus-precedence anti-recombination runs, fact-sentence vocab classes, and
  **verbatim compound numerals**: a numeral `canonical_numeral` cannot parse
  (an ISO date `2026-05-15`, a hyphenated case id `DR-4830`, a dotted version,
  a range) grounds IFF that exact token appears verbatim in `corpus.tokens` —
  a QUOTATION, not an assertion — and it JOINS the anti-recombination run, so
  a date lifted from one chunk and pasted next to another chunk's prose is
  still `phrase-recombination`. Containment is EXACT (never the prose plural
  fold), so a corpus that says `2026-05-14` can never ground `2026-05-15`; a
  compound numeral absent from the corpus is still `numeral-unparseable`
  (fail-closed). The corpus build keeps a numeral-preserving phrase VARIANT
  alongside the `normalize` phrase so the compound token exists verbatim at
  all (`normalize` shreds it into `2026`/`05`/`15`); the variant is inert for
  ng-1, whose runs contain prose tokens only.
  Speakable identifiers required a matching fail-closed rule: an identifier is
  numeral-classified, so it is never a prose token and F3's own-row and
  foreign-row routes are both blind to it. **NE-GROUND-ROWID** therefore checks
  the compound numerals a sentence quotes against the fact's own row
  (`NumberFact.row_tokens`, the row's atomic token set): any quoted identifier
  the row does not carry refuses the numeral, so `"mer-dsp-001 has
  credit_amount_cents 20000"` cannot ground on mer-dsp-003's cell. A sentence
  naming two rows refuses both (the pairing is unverifiable). Table-level
  aggregates keep the declared no-row residual.
  Its `version` hashes all three resource bytes + the ng-2 rule id:
  `ng-2/<sha256(core‖ext‖numberwords‖rule-id)[:12]>` — any resource or rule
  edit moves it (steward-review discipline). **`grounding_allowlist.txt` is
  NEVER edited** — its bytes are the frozen ng-1 hash input.

The gate stays **tier-agnostic**: it computes every finding class for the
profile. `ne.govern.grounding_policy` owns the tier layer:

* **`StrictnessTier`** — `STRICT` / `STANDARD` (deployment posture, never
  caller-selectable).
* **`ENFORCEMENT`** — the finding-class × tier matrix. **Facts are verified at
  EVERY tier**: the five fact classes (`numeral-ungrounded`,
  `numeral-unparseable`, `iri-ungrounded`, `citation-out-of-range`,
  `phrase-recombination`) plus `vocab-ungrounded-factual` (an ungrounded token
  inside a fact-bearing sentence, where a fabricated entity does its damage)
  withhold unconditionally. The knob ONLY moves `vocab-extended` and
  `vocab-ungrounded` (enforced at STRICT, advisory at STANDARD). A guardian
  test iterating every tier makes tiering a fact class a CI failure.
* **`apply_strictness(verdict, tier)`** → `StrictnessDecision(withhold,
  enforced, advisory)`: partitions the findings by `ENFORCEMENT[tier]`
  (order-preserving); `withhold = bool(enforced)`. **Advisory means recorded,
  never dropped** — an advisory finding is carried (class+count, C12-shaped) as
  a curation signal, it simply does not withhold.

STANDARD has **declared residuals** (conscious posture costs, all pinned by
test): free relational phrasing is model-trusted (A-P2-6); a digit-free zero
claim like "Acme Corp has no open orders" passes advisory-only even when false
(A-P2-7); a number-word/corpus-name collision ("One World Corp" consuming the
word "one", A-P2-8). Each is refused at STRICT and visible in the advisory
counts.
