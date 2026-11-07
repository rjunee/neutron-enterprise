# 0019 — `ApprovalOutcome.approval_id`

## Status

Accepted (2026-08-03; NE-OBS-U5 / B14, the execution→approval join —
filed as a SPEC §11 follow-up by NE-DEMO-WRITE-LIVE).

## Context

The C6 executor (`packages/ne-runtime/src/ne/runtime/middleware.py`) routes a
require-approval tool call through the HITL gate and, on grant, executes the
handler and appends a `tool.execute` AuditEvent. Before this ADR that event
carried no reference to the approval that authorised it: the join from an
executed write back to its approval was trace + seq adjacency — a guess a
reader had to make, in the one product whose claim is that it fabricates
nothing. SPEC §11 records this as "the exact join a bank's model-risk
reviewer asks for first."

The executor cannot stamp what it does not hold. `ApprovalGate.
request_approval` returns an `ApprovalOutcome`, and that model carried the
decision facts (`status`, `approver`, `decided_at`, `decision_event_id`) but
not the durable request id of the approval itself. The id exists at every
gate site — `submit` mints it, `_grant`/`reject` decide it, `_to_outcome`
rehydrates the row that holds it — it simply was never threaded onto the
outcome.

## Decision

1. **`ApprovalOutcome` gains `approval_id: str | None = None`.** Additive,
   defaulted, frozen-model discipline — the exact shape of the 1.14.0
   `decision_event_id` bump (ADR-0017).

2. **Every `DurableApprovalGate` path that builds an outcome from a known
   request populates it**: `_grant`, `reject`, `_to_outcome` (all three
   branches), and `await_outcome`'s inline EXPIRED outcome. This deliberately
   differs from ADR-0017's producer contract for `decision_event_id` (which
   only the appending call may populate): `approval_id` is stable ROW state
   — which request this outcome decided — not a claim about which append a
   call performed, so rehydration paths state it honestly. The distinction
   matters because the executor's normal path (`submit` → `await_outcome`)
   terminates in `_to_outcome`, where `decision_event_id` is correctly `None`
   but the request id is exact.

3. **A producer with no durable request stamps nothing.** Protocol-typed
   in-memory gates (test fakes, pre-1.16.0 producers) leave the default
   `None`; any consumer forwarding the id (the B14 `tool.execute` stamp)
   must OMIT its key then — an absent join renders as absent, never as an
   inferred or invented one.

4. **MINOR version bump, 1.15.0 → 1.16.0.** Purely additive: one defaulted
   field; no Protocol change; a pre-1.16.0-shaped payload still validates.

## Rejected alternatives

- **Middleware calls `submit` + `await_outcome` itself to learn the id.**
  Works without a contract change, but copies the gate's composition into
  the chokepoint and leaves every other `ApprovalOutcome` consumer (the
  approvals API, receipts) without the id. The outcome is the record of
  "which approval decided this, and how" — the id belongs on it.
- **A gate-side ambient "last decided id".** The exact wrong implementation
  the B14 two-leg guard exists to kill: under concurrent decisions the
  granted leg stamps the deny leg's id. Rejected on arrival.

## Consequences

`packages/ne-contracts/tests/test_approval_outcome_approval_id.py` pins
default/round-trip/frozen behavior. The threading itself and the two-leg
adversarial guard live in
`packages/ne-runtime/tests/test_approval_execution_join.py`.
