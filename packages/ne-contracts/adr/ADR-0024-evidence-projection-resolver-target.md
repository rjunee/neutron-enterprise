# 0024 — Evidence-projection resolver target

## Status

Accepted (2026-08-22; PRE-015A1, SPEC §13).

## Context

`SourceBoundResultResolver` (ADR-0022) exposes five target-specific protected-result
methods: model send, deterministic direct publication, approval decision, sub-agent
dispatch and Workflow-state projection. PRE-015A0-ENFORCEMENT's `ne-runtime` delivery
guard binds each of them to an exact attempt and refuses a resolution answered about a
different target.

SPEC §13's 015A1 evidence projection is a sixth consumption path. An owner reopen,
export or deep link resolves the Run's assistant-target `ProtectedSourceOutputRef` and,
for a source-delivered claim, the `ProtectedClaimSliceRef` whose span it slices — and
`ProtectedClaimSliceRef` reaches no resolver method at all today. A projector therefore
cannot make a single authorized read without either resolving evidence through a
delivery target that never happened, or reaching around the guard into the authority
store. The first mints a served receipt bound to a fictitious model call, publication,
approval or dispatch attempt; the second is the disclosure bypass ENFORCEMENT exists to
prevent.

`SourceBoundResultResolver` is also already structurally implemented and composed
(`RunStoreSourceBoundResultResolver`, `UnavailableSourceBoundResultResolver`, wired in
`ne.api.platform`). Adding a method to that Protocol in place makes every existing
implementation stop satisfying it, which is a breaking change to a frozen seam.

## Decision

1. Add `SourceBoundEvidenceProjectionResolution`: a frozen, extra-forbidden envelope
   over the shared resolution base carrying exactly two opaque target identifiers,
   `run_id` and `evidence_projection_attempt_id`. It adds no field beyond that target
   identity — no span, offset, claim, title, hash or second text channel — so the sixth
   target names a projection attempt and opens no new content surface. Disclosure still
   travels only the shared, decision-gated `materialized_text`/`stamps` channel that all
   six targets share and that the ENFORCEMENT guard already substitutes for.

2. Add `SourceBoundEvidenceProjectionResolver`, a `Protocol` that **extends**
   `SourceBoundResultResolver` with one method, `for_evidence_projection`. Its
   `reference` parameter is the closed union `ProtectedSourceOutputRef |
   ProtectedClaimSliceRef`: both are stamped protected references, and neither an open
   string nor a generic member is accepted. Extension rather than in-place mutation
   keeps the five-target seam satisfiable by the already-composed implementations, while
   a projector types against the six-target Protocol and gets all six.

3. There is no `other`/`custom`/`extra` target and no widened target type. A seventh
   consumption path is a future additive change with its own ADR.

4. Add `SourceBoundEvidenceProjectionResolverContractKit`, extending the five-target kit
   with the evidence scenario and a foreign-principal negative. Both kits carry a
   coverage map from resolver target to the kit scenario that exercises it, checked
   against the Protocol class hierarchy itself rather than a copied list, so a target
   added later cannot reach a binding without a scenario behind it.

5. This is an additive MINOR contract bump from 1.20.0 to 1.21.0.

## Rejected alternatives

- Resolve evidence through `for_model_send` or `for_workflow_projection`: rejected; the
  served receipt would be bound to an attempt that never occurred, and the guard's
  target-binding proof would be satisfied by a fabricated identifier.
- Read the authority store directly from the projector: rejected; that is the
  disclosure bypass ENFORCEMENT was built to close.
- Add `for_evidence_projection` to `SourceBoundResultResolver` in place: rejected;
  every already-composed structural implementation would stop satisfying the frozen
  seam, which is a breaking change dressed as an additive one.
- Add a generic `for_target(kind, ...)` or a widened target union: rejected; a member
  that admits arbitrary targets defeats the closed union and lets a future consumer
  self-issue a target name the guard never reviewed.
- Give the envelope a span/slice field so the projector need not re-derive it:
  rejected; it would carry evidence content in a target-identity type and create a
  second disclosure channel outside the one the guard substitutes for.

## Consequences

015A1 can resolve evidence through ENFORCEMENT without bypassing it and without
inventing a target. Nothing existing changes behaviour: no consumer is touched by this
unit, and every current five-target implementation and composition remains valid. The
types do not by themselves project evidence, slice a claim span, authorize a graph hop
or implement the runtime guard method that will call this target; those remain 015A1's
work and cannot claim existence from this contract.
