# Architecture Decision Records (ADRs)

This directory holds the platform's Architecture Decision Records.

## Convention (MADR-lite)

- Filename: `NNNN-slug.md` (zero-padded sequence + kebab-case slug), e.g.
  `0001-topology.md`.
- Sections, in order: **Status** · **Context** · **Decision** · **Consequences**.
- Status is one of: `Proposed`, `Accepted`, `Superseded by NNNN`, `Deprecated`.

## When an ADR is required

Per SPEC section 6: any change to an `ne-contracts` Protocol (a seam another
module consumes) requires an ADR here **and** a contract version bump. Units may
not silently change a Protocol another module depends on.

## Index

- `0001-topology` — modular-monolith topology and the three deployable
  processes (`ne-api`/`ne-worker`/`ne-ingest`). **Accepted**, committed with
  NE-003.
- `0002-event-outbox-in-ne-events` — the ADR-001 §4.1 Postgres outbox transport
  lives in a new `ne-events` package (not frozen `ne-contracts`, not `ne-obs`),
  and the frozen event surfaces are sufficient so `ne-contracts` is untouched.
  Non-contract decision (no version bump). **Accepted**, committed with NE-005.
- `0003-composed-platform-config-hash` — additive `compute_platform_config_hash` /
  `freeze_platform_config_hash` over a mapping of all module settings, so the SPEC §4
  replay hash covers every module's config (audit #7). Modules passed in →
  `ne-contracts` stays decoupled. Contract change → `ne-contracts` 1.0.0→1.1.0.
  **Accepted**, committed with NE-CKC5. *(Lives package-local at
  `packages/ne-contracts/adr/ADR-0003-composed-platform-config-hash.md` — a
  contract-local ADR co-located with the frozen package; indexed here for discovery.)*
- `0005-graph-aware-citations` — additive graph-aware citation types
  (`GraphEntityRef` / `GraphHop` / `GraphPath`) + a defaulted
  `RetrievalResult.graph_paths`, so a multi-hop Tier-2 answer cites its traversal
  (trimmed by construction). `Citation` and every existing Protocol are untouched.
  Contract change → `ne-contracts` 1.2.0→1.3.0. **Accepted**, committed with
  NE-082. *(Lives package-local at
  `packages/ne-contracts/adr/ADR-0005-graph-aware-citations.md`; indexed here for
  discovery.)*
- `0006-durable-turn-enqueue-and-frame-relay` — the ADR-0001 sanctioned deviation
  for the durable `ne-worker`: turn-enqueue rides DBOS's own Postgres schema in a
  dedicated `ne_dbos` database; token frames are ephemeral transport (NOT typed
  events, never `ne_outbox_events`); the relay is DBOS workflow streams with a
  poll-tail table fallback behind Writer/Reader seams (LISTEN/NOTIFY rejected); and
  `WorkerSettings` is deliberately unfrozen (one-platform-hash parity). Non-contract
  decision (no version bump). **Accepted**, committed with NE-103c.
- `0011-skill-substrate` — additive Skill Forge contract surface: `ne.contracts.skills`
  (`SkillPack`/`WorkflowSnapshot`/`WorkflowStep`/`SkillInvocation`/`AuthoringStandard`,
  the two canonical hashes — `content_hash` labels-in, `routing_hash` labels-out —
  `SnapshotPage`/`InvocationPage`, and the `SkillSubstrate` Protocol), the normative
  `ne.plugins.skill_substrate` entry-point slot name (no registration code — slots
  are group names), and the `SkillSubstrateContractKit` conformance kit. Frozen
  decisions: `acl_labels` is the shared ne-permissions ACL (NOT a scope map),
  procedural-only v1 (no executable-artifact field), two hashes now (D-12). Contract
  change → `ne-contracts` 1.7.0→1.8.0. **Accepted**, committed with NE-SKILL-0.
  *(Lives package-local at
  `packages/ne-contracts/adr/ADR-0011-skill-substrate.md`; indexed here for discovery.)*
- `0012-context-budget-contracts` — additive context-budget primitive layer:
  `ContextBudgetExceededError` (counts-only, C12; one class distinguished by
  `stage`) plus a NEW pure-stdlib `ne.contracts.tokens` (`TokenEstimator` Protocol,
  `ByteTokenEstimator`, frozen `ContextBudget`). The `errors.py` family docstring is
  rewritten to cover operationally-raised governed refusals, with an AST guardian
  test pinning that no production code broad-catches `NeContractsError`. A vendored
  multi-script oracle (checked-in REAL Qwen3 + `o200k_base` counts) REFUTED the
  design-time `bytes_per_token=3.0` default and lowered it to `1.5` for one-sided
  conservatism on demo-shaped `urn:ne:entity:*` rows — downstream units inherit
  `DEFAULT_BYTES_PER_TOKEN` from the module, not the prose. Contract change →
  `ne-contracts` 1.8.0→1.9.0. **Accepted**, committed with NE-CTXB-1.
  *(Lives package-local at
  `packages/ne-contracts/adr/ADR-0012-context-budget-contracts.md`; indexed here
  for discovery.)*
- `0013-grounded-provenance-strictness-stamp` — additive
  `GroundedProvenance.strictness: str = "strict"` tier stamp. Default
  `"strict"` preserves every pre-1.10 constructor/payload (no silent
  posture loosening); plain `str` (not `Literal`) because the enumerable
  tier set is an `OntologySettings` config-seam concern, not a frozen-model
  concern; stamped ONLY by the G3 runtime from deployment config, never
  caller/request-selectable (B-P0-1); kept a separate field from `gate`
  because `gate` is rulebook identity while `strictness` is deployment
  posture (D-P2-2). Contract change → `ne-contracts` 1.9.0→1.10.0.
  **Accepted**, committed with NE-HGATE-0. *(Lives package-local at
  `packages/ne-contracts/adr/ADR-0013-grounded-provenance-strictness-stamp.md`;
  indexed here for discovery.)*
- `0014-graph-assurance-label` — additive `assurance: str = "source"` on
  `GraphEntityRef`/`GraphHop` (three-value provenance-assurance label:
  source | extracted | confirmed) so every consumer can distinguish an
  LLM-extracted graph element from a backbone one (NE-T2X floor F2).
  Contract change → `ne-contracts` 1.10.0→1.11.0. **Accepted**, committed
  with NE-T2X-0. *(Lives package-local at
  `packages/ne-contracts/adr/ADR-0014-graph-assurance-label.md`; indexed
  here for discovery.)*
- `0015-graph-entity-source-ref` — additive `source_ref: str = ""` on
  `GraphEntityRef` — the provenance ref evidencing THIS entity (mirrors
  `GraphHop.source_ref` for the edge), threaded from
  `ne.brain_graph.model.EntityNode.source_ref` through the wire so a
  console chip for an extracted ENDPOINT can show that endpoint's own
  evidencing ref instead of an unrelated edge's (design §6.1's
  one-click-evidence property; the datum was not reachable at the wire
  boundary without this field, so it is threaded, not derived). Contract
  change → `ne-contracts` 1.11.0→1.12.0. **Accepted**, committed with
  NE-T2X-5-ENDPOINT-EVIDENCE-REF. *(Lives package-local at
  `packages/ne-contracts/adr/ADR-0015-graph-entity-source-ref.md`; indexed
  here for discovery.)*
- `0021-inference-target-operator` — additive closed inference lifecycle seam:
  strict target-scoped status/start/stop request models, safe manifest and
  observation values, `InferenceTargetOperator`, and a mutation-discriminating
  conformance kit. Executable paths/digests, argv/env, endpoints, credentials,
  raw output and free-form errors are structurally absent. Contract change →
  `ne-contracts` 1.17.0→1.18.0. **Accepted**, committed with
  PRE-DEMO-INFERENCE-LIFECYCLE Unit 0. *(Lives package-local at
  `packages/ne-contracts/adr/ADR-0021-inference-target-operator.md`; indexed here
  for discovery.)*
- `0022-source-authority` — additive generation-bound source identity,
  immutable artifact/authority receipts, bounded authorized selection and
  target-specific protected-source contract family with eight least-privilege
  Protocols/conformance kits. Existing Knowledge Protocol method sets remain
  unchanged. Contract change → `ne-contracts` 1.18.0→1.19.0. **Accepted** with
  PRE-DEMO-REHEARSAL-015A0-CONTRACT. *(Lives package-local at
  `packages/ne-contracts/adr/ADR-0022-source-authority.md`; indexed here for
  discovery.)*
- `0023-authenticated-request-context` — additive defaulted verifier-only
  `TokenClaims.verified_expires_at` plus the strict, minimal, hash-bound
  `AuthenticatedRequestContext`; the frozen `TokenVerifier.verify` return type is
  unchanged and delayed-work dependencies fail closed for legacy claims without
  expiry. Contract change → `ne-contracts` 1.19.0→1.20.0. **Accepted** with
  PRE-015A0-ENFORCEMENT. *(Lives package-local at
  `packages/ne-contracts/adr/ADR-0023-authenticated-request-context.md`; indexed
  here for discovery.)*
- `0024-evidence-projection-resolver-target` — additive sixth closed resolver target
  for owner-scoped evidence projection: a content-free
  `SourceBoundEvidenceProjectionResolution` carrying only `run_id` +
  `evidence_projection_attempt_id`, and a `SourceBoundEvidenceProjectionResolver`
  Protocol extending the frozen five-target seam with `for_evidence_projection` over
  the closed `ProtectedSourceOutputRef | ProtectedClaimSliceRef` union. Existing
  five-target implementations and compositions are untouched. Contract change →
  `ne-contracts` 1.20.0→1.21.0. **Accepted** with PRE-015A1. *(Lives package-local at
  `packages/ne-contracts/adr/ADR-0024-evidence-projection-resolver-target.md`; indexed
  here for discovery.)*
- `0026-salesforce-source-authority` — additive Salesforce source point bound to
  provider-issued organization id plus object and record identity. Host/config/
  credential-derived organization identity and silent org rebinding are refused.
  Contract change → `ne-contracts` 1.22.0→1.23.0. **Accepted**. *(Lives
  package-local at `packages/ne-contracts/adr/ADR-0026-salesforce-source-authority.md`.)*
- `0028-completion-finish-reason` — additive `Completion.finish_reason` (the
  provider's stop reason, carried verbatim instead of being read once and
  discarded) plus the derived `Completion.truncated`. A completion cut off at the
  output budget but still carrying content was previously served through a
  `Completion` indistinguishable from a whole answer, and its success audit row
  said nothing either; the partial answer is still served, but it can no longer be
  mistaken for a complete one. Contract change → `ne-contracts` 1.24.0→1.25.0.
  **Accepted** with NE-FRONTIER-OUTPUT. *(Lives package-local at
  `packages/ne-contracts/adr/ADR-0028-completion-finish-reason.md`.)*
- `0029-turn-context-budget` — additive OPTIONAL `TurnContextBudget` capability
  Protocol (`async context_budget_for_turn(task, *, requested_model, lane) ->
  TurnModelBudget | None`, where `TurnModelBudget` is the frozen pair
  `(model: str, budget: ContextBudget)` naming WHICH model the enforced window
  belongs to), so the Layer-2 trim guard sizes itself to the model IN FORCE for
  the turn — the governed per-run selection, else the FIRST candidate the
  task's route yields under the caller's lane pin, per the CURRENT route
  table — instead of to the boot-composed default it could not re-derive.
  `lane` and the async `TurnModelBudget`-answering form were amended in the
  same 1.26.0 cycle, before the version shipped (no second bump). `ModelGateway` is untouched and stays frozen: this is a separate
  capability a concrete gateway MAY also satisfy, tested for with `isinstance`,
  with the caller's composed budget as the documented fallback. Retires the
  `ne.api.model_routing` input-budget floor, whose stated premise was exactly
  that the budget could not be re-derived live. Every wrapping gateway on a
  governed path forwards the capability EXPLICITLY (method-presence Protocols
  fail silently through a decorator that omits the method). Contract change →
  `ne-contracts` 1.25.0→1.26.0. **Accepted** with NE-CTX-IN-FORCE. *(Lives
  package-local at `packages/ne-contracts/adr/ADR-0029-turn-context-budget.md`.)*
