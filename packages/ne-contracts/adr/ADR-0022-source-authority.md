# 0022 — Source-authority and immutable-artifact contract

## Status

Accepted (2026-08-21; PRE-DEMO-REHEARSAL-015A0-CONTRACT, SPEC §13).

## Context

Knowledge, graph, verified-row, Tool, model, approval, dispatch and Workflow
consumers must not treat an immutable historical artifact as a current disclosure
decision. They need one generation-bound source identity and retained immutable
admission receipt, while an independently composed authority service owns the
current head, current ACL/ReBAC decision and reconciliation health. Existing
`KnowledgeRead`, `KnowledgeWrite` and `ChunkStore` Protocols are frozen and must
not acquire source-authority methods.

The browser, model and generic connector payload are not trusted source-authority
producers. Mutable labels, delimiter-joined ids, caller-created continuations, a
receipt carrying only content hash, or a Reader that also exposes reconciliation
writes would each permit authority to be reassigned or bypassed.

## Decision

1. Add strict frozen source identity, contract identity, immutable artifact,
   reconciliation, authority receipt/stamp, verified contributor, protected
   reference/resolution and bounded authorized-page/continuation value types.
   Identity hashes use domain-separated, individually length-prefixed raw UTF-8
   fields. They do not strip, case-fold, delimiter-join or Unicode-normalize.

2. `SourceIdentity` binds deployment construction, source kind, server-issued
   connector instance, typed native point components and generation.
   `SourceContentArtifactIdentity` additionally binds source/content snapshot,
   captured ACL, exact source contract and artifact schema. Current head, lease,
   fence, attempt and time are structurally absent from the immutable artifact id.

3. `SourceAuthorityStamp` retains the exact selected artifact/head, ACL and
   contract epoch plus immutable authority receipt identity/hash. It records
   historical authenticity, not a mutable allow decision. Every disclosure or
   action resolves it again through `CurrentSourceAuthorityReader`.

4. Split Reader and Writer capabilities. Add separate immutable artifact writer,
   authorized selector, target-specific result resolver, protected output writer,
   protected execution writer and continuation codec Protocols. API projection
   receives no authority Writer. Existing frozen Knowledge Protocols are unchanged.

5. Authorized selection distinguishes `complete`, `partial` and `exhausted`.
   Counts describe only bounded work and disclosed page entries, never protected
   corpus totals. Incomplete pages carry only an opaque authenticated continuation;
   raw query/filter bytes are absent from token and replay types. Token decode
   returns only a strictly parsed key-id/MAC locator; canonical continuation state
   remains in its private durable row and is never returned by the codec seam. The
   selector authenticates the MAC only after claim/loading that exact private row.

6. Immutable artifact create/reuse first retains an in-flight reference. Only a
   newly-created artifact returns a creator token; idempotent reuse never acquires
   deletion ownership. A future implementation may release only with that exact
   creator token, zero references and current-head CAS.

7. Ship stdlib-only conformance-kit bases for all eight seams. Concrete producer,
   store and runtime units must subclass them with real adapters and add their own
   production-composition and mutation proof.

8. Because `DataContract` has no identity/version field, ne-connect owns the exact
   M365 `SourceContractIdentity` factory. It retains the canonical M365 source kind
   while hashing the actual configured source alias, owner, canonical schema,
   typed permission model, PII, fixed freshness SLA and mapper/config revisions.
   Credential and rotation-only secret material is not accepted by its API.

9. This is an additive MINOR contract bump from 1.18.0 to 1.19.0. The authority
   store/evaluator, M365/T3 admission producers and runtime enforcement are later
   dependency-ordered units and cannot claim existence from these types.

## Rejected alternatives

- Add methods to `KnowledgeRead`, `KnowledgeWrite` or `ChunkStore`: rejected;
  it breaks the frozen seam and grants unrelated implementations excess authority.
- Put current authorization in the immutable artifact or stamp: rejected; ACL,
  contract, deletion, generation and health can change after production.
- Encode source identity by delimiter joining or normalization: rejected;
  component boundaries and raw source-native identity must remain collision-safe.
- Return current corpus totals or collapse incomplete empty scans to complete:
  rejected; both leak hidden population truth and manufacture an empty answer.
- Embed query/filter state in the continuation token: rejected; private state is
  retained server-side and the token is only its authenticated lookup key.
- Give reuse callers a creator token: rejected; it permits deleting an artifact
  another operation created, selected or retained.

## Consequences

Later storage and enforcement units now have one shared additive contract and can
be built without package-local identity forks. The types do not authenticate a
source producer, persist an authority head, implement HMAC token storage, perform
ACL/ReBAC evaluation, or protect runtime bytes by themselves. Those claims remain
blocked until the named producer, authority and enforcement units are implemented
and production-composed.
