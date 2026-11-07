# 0004 — PolicyDecision decided tier

## Status

Accepted (2026-07-06; Checkpoint-D boundary-audit finding #3 — HITL BYPASS —
orchestrator owner-delegated decision; committed by NE-CKD3).

## Context

SPEC §4.2 defines a four-rung approval ladder (tier 0-3). The OPA Rego bundle
(`packages/ne-govern/policies/ne/govern/tiers.rego`) is the authoritative source
of an action's tier at decision time: it computes `class_tier` from the action
class and lets a caller-declared tier ESCALATE it (stricter-of). The designed
escalation mechanism for raising an action's HITL gating — e.g. promoting an
action to Tier-3 (named approver + distinct second check) — is a Rego-bundle
edit.

The Checkpoint-D boundary audit (finding #3) found that this escalation is
silently dropped at the PDP→middleware seam, a CONFIRMED HITL bypass:

1. **`PolicyDecision` has no tier field.** The frozen contract carried
   `effect / policy_version / reason / obligations` only.
2. **`GovernedPdp` audits `raw.tier` but drops it.** The OPA client parses and
   validates the decision's first-class `tier` key, and the PDP records it in the
   audit `detail`, but never returns it on the `PolicyDecision`.
3. **The runtime middleware recomputed the gate tier from a STATIC Python
   mirror** of `tiers.rego` (`ne.runtime.actions.tier_for_action`) and ignored the
   OPA decision. So a Rego edit escalating an action to Tier-3 silently degraded
   to Tier-2 single-approver gating: no named approver, no distinct second check.

The fix must not break the Checkpoint-A freeze of `ne-contracts` v1.0.0: every
existing constructor, wire producer, and contract model stays byte-for-byte
compatible.

## Decision

Add a **single additive optional field** to `PolicyDecision` — purely additive:

- `tier: int | None = None` — the tier the policy bundle DECIDED for this
  request (SPEC §4.2 ladder 0-3). The default is `None`, so every existing
  constructor and every pre-1.2.0 wire producer that omits it stays valid, and
  `None` unambiguously means "the bundle did not classify" (deny paths) or "a
  pre-1.2.0 producer built this decision".

Consumers compose **stricter-of**: the decided tier is a gating FLOOR, never a
ceiling. The runtime gate takes `max(decided_tier, spec.tier, python-mirror
tier)` plus obligation floors, so the decided tier can only ESCALATE gating,
never lower it. `GovernedPdp` populates the field from the `raw.tier` it already
audits (no audit-shape change — the value already travelled in `detail`), and the
middleware honors it as the authority the mirror can never under-tier.

`obligations` (already present since 1.0.0) is left untouched and continues to
carry the second-check / named-approver / per-item flags the gate also honors.

### Alternatives rejected

- **Carrying the tier only inside `obligations`.** Rejected: `obligations` is an
  untyped, unvalidated `AnyMap`; the OPA decision document already exposes `tier`
  as a first-class, `OpaClient`-parsed-and-validated `int | None` key. Threading
  it through an untyped map would discard that validation and duplicate an
  existing typed signal.
- **Making `tier` a required field.** Rejected: it is not additive — it would
  break every existing `PolicyDecision(...)` constructor and every pre-1.2.0 wire
  producer, violating the Checkpoint-A freeze discipline.

## Consequences

- **Purely additive MINOR bump 1.1.0 → 1.2.0.** No existing field is removed,
  renamed, or reordered; no existing API signature or observable behavior
  changes. `PolicyDecision` stays `frozen=True` (Checkpoint-A immutability), and
  all `config_hash` invariants are untouched (no `config.py` change). This honors
  the Checkpoint-A freeze discipline (ADR-0001 freeze scope, SPEC §6):
  additive-only + ADR + version bump.
- **The HITL bypass closes.** Once producers populate `tier`, the runtime gate
  can never under-tier what the bundle decided; a Rego escalation to Tier-3 now
  reaches the human gate as Tier-3.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the unit's
diff surface is `packages/ne-contracts/**`. The orchestrator may index it from the
repo-wide `docs/adr/README.md` on merge (and reconcile the `0004` sequence number
if another in-flight unit claims it).
