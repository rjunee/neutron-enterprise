# 0018 — `CompletionRequest.principal_id`

## Status

Accepted (2026-07-31; NE-COST-PRINCIPAL — Fable arbiter ruling on the console
Models section's per-principal usage requirement).

## Context

Every `CostEvent` emitted by `LiteLLMGateway` already carries `model` /
`provider` / `lane` / `input_tokens` / `output_tokens` / `cost_usd` / `task` /
`config_hash` / `trace_id` on every terminating call — the fact table for
"which models are being used and by whom, aggregate spend across models" (the
owner's stated requirement for the console's Models section) already exists.
The one field that is wrong is `actor`: it is stamped with the gateway's own
service-level actor (`LiteLLMGateway.__init__`'s `actor: str = "ne-gateway"`,
threaded onto every `new_cost_event(actor=self._actor, ...)` call), never the
principal who caused the call.

The consequence: a per-user usage breakdown drawn from the `CostEvent` outbox
would be fabricated — every row would attribute to the service, regardless of
which user or agent actually drove the completion. On the one screen whose
entire purpose is to prove measurement discipline, that is the worst possible
defect.

The principal is already available at every governed run root
(`DepartmentAgentSession.run`, `run_isolated_agent`) as a VERIFIED
`Principal` argument — the PDP sees it on every call (C6). What is missing is
a DATA carrier from the agent-runtime process into the gateway process (the
only channel a `ModelGateway` implementation sees is the frozen
`CompletionRequest` itself) — exactly the gap `trace_id` closed for
correlation (ADR-0007/NE-166).

## Decision

Add a **single additive optional field** to `CompletionRequest`, mirroring the
`trace_id` precedent:

- `principal_id: str | None = None` — an OPAQUE principal IDENTIFIER matching
  the shape already used for `Principal.id` and the audit-chain actor (e.g.
  `"user:ada"`, `"agent:demo-delegate"`), carried AS DATA from the
  agent-runtime process into the gateway process. The default `None` keeps
  every existing constructor and every pre-1.15.0 wire producer valid.

The companion `ne-runtime` change (same unit) adds a run-scoped ambient
carrier — `run_principal_scope` / `current_run_principal_id`
(`ne.runtime.correlation`) — entered ONLY at run roots from the VERIFIED
`principal` argument (never from request/message data), and threaded through
`GatewayModel.request` and the `ne-api` grounding-assist closure onto
`CompletionRequest.principal_id`.

On the gateway side, `LiteLLMGateway` stamps `CostEvent.actor` with
`request.principal_id if request.principal_id else self._actor` (a small
`_cost_actor` helper), computed FRESH from the request on every call — never
cached on the instance — so a principal-less call can never inherit an
earlier request's principal.

`principal_id` carries **no PII** — it is an opaque identifier only, the
IDENTICAL shape the audit chain already stamps for actors
(`approvals.py:280`, `dispatch.py:225`, `agent.py:336`), so a `CostEvent` and
an `AuditEvent` for the same call are joinable on principal without
introducing a new identifier class.

### Decided divergence from `trace_id`: empty string is "no principal"

`trace_id`'s contract (ADR-0007) is **explicit-wins**: an explicit empty
string `""` is preserved, not overwritten by the span fallback, because `""`
is a legitimate (if degenerate) opaque correlation value.

`principal_id` is **decided differently**: an empty string is treated as "no
principal" (truthiness — `request.principal_id if request.principal_id else
self._actor`), falling back to the service actor exactly like `None`. An
empty string is not a principal identifier — no `Principal.id` in this
platform is ever `""` — so attributing a `CostEvent` to `""` would not be
"the calling principal", it would be a blank masquerading as an identity.
Honest service attribution is preferred over a value that looks like an
identifier but names no one.

### Alternatives rejected

- **Widening `current_tool_principal`** (the existing middleware-scoped
  principal carrier) instead of adding a new ambient carrier. Rejected: its
  docstring explicitly restricts its read surface to skill bindings (plan
  §2.9 ambient-authority discipline); widening it to a second, unrelated
  reader (the gateway model adapter) blurs that boundary for no benefit — a
  narrow, purpose-built carrier mirroring `action_scope`'s exact semantics
  (always-set, no minting, no ambient-inherit ambiguity) is simpler to
  reason about and does not touch an existing seam's contract.
- **Attributing a principal-less call to the last-seen principal on the
  gateway instance.** Rejected outright — this is the exact defect the
  cross-attribution mutation probe (M1/M2 in the unit's PR body) is built to
  catch. A background/system call (boot-time warmup, a cron sync) must
  remain honestly attributable to the service, never silently inherit a
  prior caller's identity.
- **Making `principal_id` required.** Rejected: not additive — it would
  break every existing `CompletionRequest(...)` constructor and every
  pre-1.15.0 wire producer, violating the Checkpoint-A freeze discipline
  (same rejection as ADR-0007's equivalent alternative for `trace_id`).
- **Carrying the principal only in `params` (the untyped `AnyMap`).**
  Rejected for the same reason ADR-0007 rejected it for `trace_id`: `params`
  is forwarded toward litellm call kwargs; an ad-hoc key there is
  unvalidated and not visibly part of the frozen contract surface.

## Consequences

- **Purely additive MINOR bump 1.14.0 → 1.15.0.** No existing field is
  removed, renamed, or reordered; no existing API signature or observable
  behavior changes. `CompletionRequest` stays `frozen=True` (Checkpoint-A
  immutability).
- **Existing outbox rows are not rewritten.** Every `CostEvent` emitted
  before this change (and every principal-less call after it) keeps the
  gateway's service actor — history is never rewritten, and the service
  actor remains the honest attribution wherever no principal caused the
  call.
- **Gateway `AuditEvent`s deliberately keep the service actor.** They
  record the gateway's own egress action (a C10-conservative choice, pinned
  by test) — joinable to the causing principal via `trace_id`, not by
  widening `actor`. This ADR changes an events/outbox payload VALUE, not the
  audit hash chain (C10).
- **Per-principal usage aggregation becomes honestly answerable** once a run
  root threads its verified principal through — the prerequisite for the
  console Models section's "usage by whom" requirement.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`), mirroring
ADR-0007 and ADR-0017 — the unit's contract diff surface is
`packages/ne-contracts/**`.
