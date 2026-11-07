# 0017 — `ApprovalOutcome.decision_event_id`

## Status

Accepted (2026-07-31; NE-APPROVAL-LINKAGE — Fable 5 arbiter ruling on
approver decision receipts).

## Context

Every approval-lifecycle audit record is a config_hash-stamped `AuditEvent`
appended to the tamper-evident ne-audit chain via
`DurableApprovalGate._audit` (`packages/ne-runtime/src/ne/runtime/approvals.py`).
`approval.requested` (and `approval.escalated` / `approval.expired` /
`approval.reattached`) already stamp `provenance["approval_id"]` with the
request's id, so those records are trivially linkable back to the request
they describe. The three DECISION records — `approval.granted` (`_grant`),
`approval.rejected` (`reject`), and the Tier-3 first-check
`approval.first-check` — did not: their provenance carried only approver
ids. A design arbiter reasoning about a different problem (surfacing an
approver's own decision receipt) noticed the asymmetry: the chain contains
the request, contains the decision, and nothing ties them together short of
guessing from timestamps and approver ids.

The Fable arbiter's binding ruling on the receipt design itself: the
approve/deny response should return the decision record's REAL chain
coordinates (`audit_seq`, `record_hash`, `event_id`) read back from the
chain — no signing, no keys. That read-back needs a deterministic way to
find the decision record for a given approve/reject call. This ADR is the
prerequisite that makes that lookup possible; the receipt surface itself
(threading `audit_seq`/`record_hash` through, and any API/console
consumption) is a separate, later unit.

## Decision

1. **Close the provenance gap at all three decision call sites.** `_grant`,
   `reject`, and the Tier-3 first-check path in `approve` add
   `"approval_id": request_id` to their `_audit(...)` provenance, mirroring
   the exact key `approval.requested` already uses — no new key name
   invented. After this change every `approval.*` audit record in the
   family carries `approval_id`.

2. **`_audit` pre-generates and returns the appended record's `event_id`.**
   The event id is generated with `str(uuid.uuid4())` (matching
   `new_audit_event`'s own default and the `request_id` precedent) and
   passed explicitly to `new_audit_event(event_id=...)` — the factory
   already accepts one (`packages/ne-events/src/ne/events/factory.py`).
   `_audit`'s signature changes from `-> None` to `-> str`. The id is
   returned only after `self._sink.append(event)` succeeds; the existing
   `try/except -> RuntimeAuditError` fail-closed wrapper is untouched, so no
   id is ever returned for a decision whose chain record did not actually
   land (C6/C10).

3. **`ApprovalOutcome` gains `decision_event_id: str | None = None`.** An
   additive, defaulted field on the frozen model — the same discipline as
   the 1.10.0–1.13.0 bumps. `_grant` and `reject` thread the id `_audit`
   returned onto the `ApprovalOutcome` they build and return. The Tier-3
   first-check path returns `None` (no `ApprovalOutcome` exists on that
   leg), so its `_audit` call's returned id is intentionally unused there.
   `_to_outcome` (the `await_outcome` / polling rehydration path) is NOT
   changed — a polled outcome never fabricates a `decision_event_id`; it
   stays `None`. This is a deliberate PRODUCER CONTRACT: the field is
   populated only by the specific gate call that performed the append, not
   by any code path that reconstructs an `ApprovalOutcome` from stored row
   state after the fact.

4. **MINOR version bump, 1.13.0 → 1.14.0.** Purely additive: one new
   defaulted field, no signature change to any existing export, no change
   to the `ApprovalGate` Protocol.

## Rejected alternatives

- **A separate lookup call (e.g. `gate.find_decision_record(request_id)`)
  instead of returning the id inline.** Rejected: it would require a second
  round-trip to the chain store and reopens a TOCTOU window between the
  decision and the lookup (a concurrent, unrelated append could shift what
  "the decision record for this request" would resolve to if resolved by
  anything other than the id captured at append time). Returning the id
  actually produced by the append that just happened is unambiguous and
  free of races.
- **A dashed vs. hex `event_id` format.** An orchestrator note floated
  `uuid4().hex` (32-char, no dashes) instead of `str(uuid.uuid4())`
  (dashed). Rejected: `new_audit_event`'s own default and the sibling
  `request_id` precedent both use the dashed form; no consumer parses the
  format; and inventing a second id shape for the same unique `event_id`
  column would itself be the kind of un-mirrored invention this unit's
  governing rule (match precedent, don't invent) rules out.
- **Populating `decision_event_id` from `_to_outcome`/polling by looking up
  the most recent decision AuditEvent for the request.** Rejected: it would
  make the field's presence depend on a fresh chain query on every poll
  (performance + coupling), and — more importantly — would blur the
  producer contract this ADR establishes (only the call that appended the
  record knows its own event_id for free; a lookup-based reconstruction is
  a different, weaker guarantee and does not need to exist for this unit's
  purpose, which is the immediate approve/reject response).

## Consequences

- **Purely additive MINOR bump 1.13.0 → 1.14.0.** Every pre-1.14 producer
  and consumer of `ApprovalOutcome` is unaffected; old serialized payloads
  (no `decision_event_id` key) validate with the field `None`.
- **`uv.lock` regenerates the `ne-contracts` package entry only** — via
  plain `uv lock`, never hand-edited (repo shared-file policy §1.4).
- **The fail-closed decision append is unchanged and newly pinned on the
  decision path.** `_audit`'s try/except still raises `RuntimeAuditError`
  before any id could escape a failed append; a new test
  (`test_decision_audit_append_failure_still_fails_closed`) exercises this
  specifically for `approve`/`reject`, alongside the existing
  `submit`-path fail-closed coverage.
- **Enables, but does not itself ship, the decision-receipt surface.**
  Threading `decision_event_id` (or a richer receipt carrying
  `audit_seq`/`record_hash`) through the `approve`/`reject` HTTP responses
  and console is left to a follow-up unit; this ADR only makes the decision
  record deterministically findable.
- **No change to `ne_audit_log` schema, hashing, or canonical-bytes
  machinery.** `approval_id` is a new key inside the existing JSON
  `provenance` map on the payload column — payload-additive only.
