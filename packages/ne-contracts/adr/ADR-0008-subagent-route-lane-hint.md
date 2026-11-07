# 0008 — SubAgentSpec.route_lane_hint (narrow-only)

## Status

Accepted (2026-07-16; NE-161 — Per-child model routing keyed on
AgentDefinition).

## Context

NE-161 lets a governed sub-agent route to a **different** gateway task/lane
than its parent. The routing binding itself lives on `AgentDefinition`
(`packages/ne-runtime/src/ne/runtime/dispatch.py`) — a **trusted,
server-registered** model, frozen and `extra='forbid'`, populated only by a
composition root. `SubAgentSpec`, by contrast, is **model-emitted**: its
fields are the (possibly hijacked) planner's own request, carried across the
prompt boundary. This is the same trust inversion the dispatcher already
enforces for `max_depth` / `max_concurrency` (`dispatch.py` `child_for` /
`_dispatch_reserved`): a spec value may only **narrow** a server-side bound,
never widen it — a lying spec can shrink its own budget but can never exceed
the platform cap.

Model routing needs the identical discipline, with a sharper consequence if
it is skipped: a spec field that could **widen** a child's lane toward
`FRONTIER_API` would be a straightforward **frontier-exfil upgrade path** —
a hijacked planner could simply ask a normally private/local sub-agent to run
its next turn on a frontier API vendor, no code path required to key that
authority off the trusted `AgentDefinition` at all.

The requirement, then, is a `SubAgentSpec` field that lets a spec pull a
child **toward** the cheaper/more-local `PRIVATE_VLLM` lane (a legitimate,
narrow "please run this cheaply" request) while a deterministic,
non-model-judgment guardrail in the dispatcher clamps any attempt to widen
toward `FRONTIER_API` back to whatever the `AgentDefinition` already binds
(§10.2, C12 — the clamp runs in code, before any model call, so a fully
hijacked planner cannot move a child onto the frontier lane by asking).

This must not touch `config_hash`: `SubAgentSpec` is a per-request model, not
platform configuration, matching the ADR-0007 `CompletionRequest.trace_id`
precedent.

## Decision

Add a **single additive optional field** to `SubAgentSpec`:

- `route_lane_hint: Lane | None = None` — a **narrow-only** hint from the
  (untrusted) planner. Typed `Lane` (not `str`), matching the
  `CompletionRequest.lane` precedent: pydantic coerces a lane-shaped string
  (e.g. `'private-vllm'`) and rejects anything else with a `ValidationError`
  at spec-parse time (fail-loud). Default `None` keeps every pre-1.5.0
  `SubAgentSpec` constructor valid.

The dispatcher-side clamp (same unit, `ne-runtime`, not this package) is the
enforcement point: `GovernedSubAgentDispatcher._resolve_route` mirrors the
existing `max_depth`/`max_concurrency` MIN-compose discipline —

- `route_lane_hint is Lane.PRIVATE_VLLM` → the hint narrows the child's lane
  to `PRIVATE_VLLM`, recorded `lane_source='spec-hint'`.
- `route_lane_hint is Lane.FRONTIER_API` and the `AgentDefinition`-derived
  lane is not already `FRONTIER_API` → the widen attempt is **clamped**: the
  child's lane is left at the `AgentDefinition`-derived value, and the
  `subagent.route` audit records `hint_clamped=true`.

No code path anywhere reads a lane/task widen from the spec; only the
dispatcher's `AgentDefinition` fields (`route_task`/`route_lane`, themselves
server-side and boot-time validated) can ever place a child on the
`FRONTIER_API` lane.

### Alternatives rejected

- **Untyped `str` hint.** Rejected: matches the rejected-alternative
  reasoning in ADR-0007 for `trace_id` via `params` — an untyped surface is
  easy to typo and not visibly part of the frozen contract the way a named,
  `Lane`-typed field is.
- **Letting the hint apply unconditionally (no clamp).** Rejected outright:
  this is exactly the frontier-exfil upgrade path §10.2 identifies as
  unacceptable — a model-emitted field must never be able to widen egress
  authority.
- **Putting the clamp logic in `ne-contracts` (e.g. a validator on
  `SubAgentSpec` that also sees the definition).** Rejected: `SubAgentSpec` is
  constructed and validated independent of any `AgentDefinition` lookup (the
  dispatcher resolves the definition by name only after the spec is already a
  validated object), so the clamp can only be applied once both are in hand —
  that is dispatcher logic, not a contracts-level constraint.

## Consequences

- **Purely additive MINOR bump 1.4.0 → 1.5.0.** No existing field is removed,
  renamed, or reordered; no existing API signature or observable behavior
  changes. `SubAgentSpec` stays `frozen=True` (Checkpoint-A immutability), and
  `config_hash` is entirely untouched — this is a request-side model, not
  platform config.
- **The narrow-only discipline is uniform across the dispatcher.** Depth,
  concurrency, and now lane routing all follow the same shape: the spec may
  only ask for *less* than the server already permits, never more.
- **The clamp is enforced in `ne-runtime` code, not this ADR.** This package
  only fixes the contract shape (`Lane`-typed, optional, additive); see
  `packages/ne-runtime/src/ne/runtime/dispatch.py` (`_resolve_route`) and its
  README section ("Per-child model routing (NE-161)") for the enforcement
  detail and the `subagent.route` audit schema.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the
unit's contract diff surface is `packages/ne-contracts/**`. It is indexed from
the repo-wide `docs/adr/README.md` for discovery.
