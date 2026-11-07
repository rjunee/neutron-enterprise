# 0030 — PolicyDecision approval window, and the standing-approval lifetime

## Status

Accepted (2026-09-02; SPEC §12 item 5 — "approval window unusably short";
owner requirement 2026-08-30, stated twice: the approval window is policy
configuration, a per-action setting, never tied to auth-token lifetime).

**AMENDED 2026-09-08 (contracts 1.29.0, bundle 1.5.0) — the SECOND clock.**
The 1.27.0 field made the window honest; it did not make a long window
POSSIBLE, because the run owner's frozen authorization snapshot still expired
underneath it (the "deliberately out of scope, and still open" item at the
bottom of the original Consequences). Option F1 —
`docs/architecture/approval-horizon.md`, SPEC amendment shipped — closes that
by letting an approver's DECISION outlive the attempt it was asked for, and a
carryover needs its own bounded lifetime. That lifetime is the same KIND of
fact as the window (a property of the action, declared beside its tier), so it
is resolved by the same rule set, carried on the same surfaces, and validated
by the same guard. Everything below is unchanged; the amendment is additive and
described in **"Amendment: `standing_approval_seconds`"** at the end.

## Context

`tiers.rego` resolves `approval_window_seconds` per action (exact action
override → per-tier override → built-in floor of 4 h for tier 2 / 24 h for
tier 3, from `data.ne.config.approval_windows` — floors that are NOT honourable
on a shipped deployment; see the consequences note at the end), and the runtime
carries the
value through `approval_window_scope` to the durable approval row's
`expires_at`. But between those two working halves the value never travelled:

1. **The runtime read the wrong place.** `middleware.py`'s
   `_approval_window_s` read `decision.obligations["approval_window_seconds"]`,
   while the live decision document carried only
   `{"per_item_human_approval": true}`. `approval_window_seconds` was a SIBLING
   rule in a different document, and the OPA client evaluates only
   `/v1/data/ne/govern/decision` — so the fallback to the deployment-wide
   `cfg.expiry_s` fired on every path that ever executed.
2. **The obvious fix is already known-broken.** Emitting the window into
   `obligations` was tried (`a0672c1b`) and REVERTED (`5186d579`):
   `obligations` is the Rego/Cedar PARITY SURFACE — `test_cedar_parity`
   compares the two engines' decision documents bit-for-bit, and the Cedar
   bundle carries obligations as STATIC `@ne_obligations` annotations parsed
   per rule at load time. A window resolved from `data` PER ACTION cannot be a
   static annotation, so an obligation-borne window breaks parity BY
   CONSTRUCTION, not by oversight.

The revert itself named the correct home: "a PolicyDecision FIELD, mirrored in
the Cedar evaluator — exactly the path `tier` took when it was added in
contracts 1.2.0."

## Decision

Add a **single additive optional field** to `PolicyDecision`:

- `approval_window_seconds: float | None = None` — the policy-resolved
  per-action approval window in seconds. The default is `None`, so every
  existing constructor and pre-1.27.0 producer stays valid, and `None`
  unambiguously means "the policy named no window; the deployment default
  applies". It MUST NEVER be read as "expire immediately".

Producers:

- **OPA**: `decision.rego` emits `approval_window_seconds` first-class on the
  four `require-approval` rules (the `tiers.rego` resolution result — always
  defined there, because the built-in floor covers tiers 2 and 3) and `null`
  on every `allow`/`deny` rule (an autonomous action has no approval to
  time-box; an answer where none is meaningful is noise). `OpaClient` parses
  and validates it (number or null; bool rejected) and `GovernedPdp` threads
  it onto the decision.
- **Cedar**: the value cannot live in the static bundle, so
  `CedarEvaluator` resolves it in its Python marshaller — the same component
  that already mirrors `tiers.rego`'s declared-tier / provisional-context /
  extraction-mode marshalling bit-for-bit — using the identical
  most-specific-first order over an `approval_windows` mapping with the same
  shape as `data.ne.config.approval_windows`, falling back to the identical
  built-in floors. `CedarGovernedPdp` threads it onto the decision.

Consumers: `middleware.py`'s `_approval_window_s` reads the FIELD (not
obligations), normalises zero/negative/non-finite to `None`, and binds the
value in `approval_window_scope` around the park, where the durable gate
already consumes it.

### Parity

The decision documents stay bit-for-bit comparable: with no deployment
override configured, both engines resolve the built-in floors and produce
identical documents (pinned by the parity suite, which now compares seven
fields). A deployment that overrides windows configures OPA via its `data`
document and the Cedar evaluator via its constructor mapping; the resolution
MECHANISM is identical on both engines, and feeding them the same mapping is a
deployment-consistency requirement of exactly the same kind as keeping the two
bundles' `policy_version` in step (a divergently-configured pair is a
deployment error, not an engine divergence — and the parity suite proves the
mechanism agrees when the mappings agree).

### Alternatives rejected

- **Carrying the window in `obligations`.** Rejected — tried and reverted;
  breaks Rego/Cedar parity by construction (see Context).
- **The runtime reading the sibling `tiers.rego` document in the same
  round-trip.** Rejected: it widens the OPA transport contract (a second
  document path, or evaluating `/v1/data/ne/govern` wholesale), has no Cedar
  analogue at all — Cedar has no addressable sibling document, so parity would
  be structurally unverifiable — and moves policy semantics into transport
  plumbing.
- **Resolving the window at the runtime from `data.ne.config.approval_windows`
  directly.** Rejected: the runtime has no access to OPA's data documents
  except by asking OPA, so this means duplicating the resolution AND the
  config in a second place the policy engine never sees — a Python mirror
  PLUS a config mirror, with tier escalation (`final_tier`) re-derived
  runtime-side. The mirror already burned this codebase once (ADR-0004: the
  static Python tier mirror silently under-tiered a Rego escalation).

## Consequences

- **Purely additive MINOR bump 1.26.0 → 1.27.0.** No existing field removed,
  renamed, or reordered; `PolicyDecision` stays `frozen=True`; no `config.py`
  change, so all `config_hash` invariants are untouched.
- The policy bundle version bumps 1.2.0 → 1.3.0 (both engines), since the
  decision document grows a key — the NE-T2X-5 precedent.
- The window now reaches the durable row's `expires_at` on both park paths
  (conversation and canonical Run).
- **Was out of scope here; BUILT SINCE — and it changes what these defaults
  mean.** The run owner's frozen authorization snapshot expires independently of
  the approval window, so a 4 h window on a 15-minute session was a promise the
  platform made silently. `ne.runtime.middleware._require_honourable_approval_window`
  now compares the two before `create_wait` and REFUSES the park with
  `ApprovalWindowUnhonourableError` when the window outruns the owner's horizon
  **and this deployment DECLARED that window**
  (`approval_windows.actions`/`.tiers`, carried on the additive
  `PolicyDecision.approval_window_declared` bit added in contracts 1.28.0 /
  bundle 1.4.0). On the built-in floor, and on the `ApprovalSettings.expiry_s`
  fallback, it emits a **log warning** on the `ne.runtime.middleware` logger —
  deliberately not an audit row (a fail-closed sink would reintroduce the very
  refusal this branch removes), so there is nothing to find in the audit chain —
  and lets the park proceed: the refusal
  buys legibility, not safety — a park that outruns the horizon executes nothing
  unsafe and terminalizes the already-handled `authorization_expired` at worst —
  and a floor is not a promise anyone made. ⭐ It is the DECLARATION, not the
  check, that fixes the measured failure: 180 s in the durable row's
  `expires_at` turns a stranded four-hour promise into a visible approval
  expiry. ⚠️ On the CANONICAL RUN park only: the conversation park has no
  durable resume seam for the horizon to collide with — it is dispatched as a
  background `asyncio.create_task` by `ne.api.workflows` (NOT awaited inline in
  the request, as earlier revisions of this ADR said), so the HTTP request
  returns while `_park` polls, and nothing there re-binds the owner's admitted
  context to enforce the horizon. Pre-existing residual, not introduced here: an
  approved conversation effect can fire past the owner's authorization expiry.

  Two consequences for anyone reading this ADR for the floors:

  1. **The 4 h / 24 h built-in floors are not deliverable on any deployment
     shipped today.** A deployment that does not override them is not refused —
     the guard warns and parks on a floor — it silently promises four hours and
     strands the run as `authorization_expired` fifteen minutes in, which is the
     measured 12.7% failure. ⚠️ An earlier revision of this bullet said such a
     deployment "has every tier-2/3 Run park refused at park time"; that was the
     unconditional first cut, and it refused parks that succeed on `main`. This repo's
     own document (`deploy/opa/data/ne/config/data.json`, staged into the served
     bundle by `ne.cli.opa_bundle.stage_govern_policies`) declares **180 s for
     `write.external.email.send`** — an `approval_windows.actions` override, NOT
     a per-tier one, because the derivation covers exactly one situation (the
     pre-park stretch of a Run executed by an agent holding a tier-2 write
     grant) and that action is today the only tier-2-or-higher grant any agent
     in `deploy/agents/registry.toml` holds. Every other action keeps the
     built-in floor: a per-tier entry would have cut the conversation park —
     which has no durable resume seam for the horizon to collide with — from
     4 h/24 h to three minutes for a reason that says nothing about it. Derived from three ENFORCEMENT sites: the console's 420 s run-credential
     renew trigger, minus the 230 s enforced per-attempt wall clock (the
     LARGEST `max_steps` x `_SECONDS_PER_STEP` among the agents whose
     `tool_grants` hold a tier-2-or-higher action — the only agents that can
     park — today just `agt_01k3customersupport000000000`, 23 x 10), minus the
     10 s of clock skew OIDC verification budgets. It is the largest window that holds when
     the console's renew **succeeds** — not a worst-case bound, because that
     renew is fail-open and binds only the console — and it is pinned by
     `packages/ne-runtime/tests/test_approval_window_is_honourable.py`, which
     reads every input at its site rather than retyping it. The gap
     between the 4 h promise and the 180 s deliverable is stated openly in
     `docs/architecture/approval-horizon.md`; closing it needs durable run-scoped
     authorization, which contradicts the specification ("only a new
     authenticated retry may run"), and is an owner
     decision plus a SPEC amendment — never a token-lifetime change.
  2. **The deployment-consistency contract below covers the provenance bit too.**
     Configuring one engine's windows and not the other's diverges in BOTH the
     window and its `approval_window_declared` provenance: the configured engine
     answers a declared 180 s (refusable if a horizon is shorter still), the
     unconfigured one answers the 4 h floor and parks on a promise it cannot
     keep. ⚠️ An earlier revision said the unconfigured engine "refuses every
     tier-2/3 canonical Run park" — under the declared-only scope the divergence
     runs the other way. `CedarGovernedPdp.from_settings` builds a bare
     `CedarEvaluator()`, so Cedar is the currently unconfigured side; OPA is what
     this deployment runs.

  ⭐ **F1 does not retire this check, and does not make it vacuous.** The
  standing approval carries an ANSWER past the horizon; it does not carry the
  OPPORTUNITY TO ANSWER. `_mint_standing_approval` mints from exactly two
  sources — a named human who decided past the horizon, or a row already
  `approved` and unconsumed — and mints nothing when nobody answered, while the
  terminalization marks the approval `expired` either way. So a DECLARED window
  that outruns the horizon still promises an approver seconds they do not have.
  F1 lowers the COST of that incoherence; it does not remove it. The two
  mechanisms are complementary: the check refuses a promise this deployment
  wrote down and cannot keep, and F1 makes a decision that WAS made survive the
  attempt that asked for it.

---

## Amendment: `standing_approval_seconds` (2026-09-08, contracts 1.29.0)

### Context

Under F1, when a parked attempt terminalizes `authorization_expired` its
decided-but-unconsumed approval becomes a **standing approval** bound to
`(owner principal id, action_hash, policy_hash, protected_invocation_hash)`.
A carryover with no bound would be a claim the platform cannot back — "this
human's answer is good forever" — so SPEC requires "a policy-configured
standing lifetime resolved beside `approval_window_seconds`".

"Beside" is literal. The two are the same kind of fact and must be configured
in one place by one mechanism, or a deployment ends up declaring one and
inheriting the other from a default nobody chose.

### Decision

A **second additive optional field**, validated by the SAME validator:

- `standing_approval_seconds: float | None = None` — how long the decision
  stays usable after the attempt it was asked for terminalized. Default `None`,
  so every pre-1.29.0 producer stays valid.

**`None` means MINT NO STANDING APPROVAL.** This is the one asymmetry with the
window and it is deliberate: the window's `None` falls back to a deployment
default, because an approval must have *some* deadline. A standing lifetime has
no such floor to fall back to — the honest answer to "policy named none" is
that no decision carries forward, which is exactly the pre-F1 behaviour. An
absent value must never be able to widen into an unbounded carryover.

Producers mirror the window exactly: `tiers.rego`'s
`standing_approval_seconds` (exact action → per tier → built-in floor, over
`data.ne.config.standing_approvals`); `decision.rego` emits it first-class on
the four `require-approval` rules and `null` elsewhere; `OpaClient` parses and
validates it; both PDPs thread it; `CedarEvaluator` mirrors the resolution from
a SEPARATE `standing_approvals` constructor argument.

The separate constructor argument is load-bearing. Reading the standing
lifetime out of the `approval_windows` mapping would have passed every existing
parity test while giving two independent policy answers one value, so
`test_cedar_parity` §6 proves the two documents are resolved independently and
that configuring one does not move the other.

### The built-in floors are asymmetric, and that is the governance statement

7 days for tier 2, **2 days** for tier 3 — SHORTER for the more dangerous tier,
which is the opposite of the window's 4 h / 24 h shape. The window grows with
tier because a Tier-3 ceremony needs two named humans and more time. The
standing lifetime shrinks with tier because what a carryover concedes is that
the decision may be OLDER than the execution it authorizes, and that concession
must be smallest where the effect cannot be undone.

### Consequences

- **Purely additive MINOR bump 1.28.0 → 1.29.0.** No field removed, renamed or
  reordered; `PolicyDecision` stays `frozen=True`; no `config.py` change, so
  every `config_hash` invariant is untouched.
- The policy bundle version bumps 1.4.0 → 1.5.0 (both engines, plus the bundle
  `.manifest` revision), since the decision document grows a key.
- The value is frozen at PARK into the insert-only
  `ne_agent_run_approval_joins.standing_lifetime_s`: the seam that mints a
  standing approval is a terminalization, which has no live PDP call to ask.
  A later edit to the deployment data document therefore cannot retro-extend a
  lifetime already recorded, and SPEC's "extending the standing lifetime at
  consumption is independently RED" holds structurally rather than by
  convention.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) with its
predecessors.
