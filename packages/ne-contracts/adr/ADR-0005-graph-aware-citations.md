# 0005 — Graph-aware citations

## Status

Accepted (2026-07-09; Brain Tier-2 completion — NE-082; the ONLY `ne-contracts`
change of the tier, SPEC §5 row + `docs/architecture/brain-tier2-spec.md` §5.3).

## Context

Tier-2 answers multi-hop questions ("which customers are affected by the change
to product X via contract clause Y") by traversing the principal-scoped entity
graph (`ne.brain_graph.read.EntityGraph`). A multi-hop answer must cite the
TRAVERSAL, not just the endpoint chunks: a steward (and the end user) needs to
see WHICH relations the answer walked, and each relation must be
click-through-able to its source of record — exactly as a chunk `Citation`
carries `source_system` / `source_ref` today (`types.py:64-70`).

The frozen `RetrievalResult` (`types.py:73-77`) carried `chunks` + `citations`
only, with no way to attach the traversed path. `Citation` itself is not the
right carrier — it points at a single chunk, whereas a graph hop is a
`(subject)-[edge_kind]->(object)` triple with its own provenance.

The change must not break the Checkpoint-A freeze of `ne-contracts`: every
existing constructor, wire producer, and Protocol stays byte-for-byte
compatible.

## Decision

Add **three additive frozen value types** plus **one additive defaulted field**:

- `GraphEntityRef(entity_class, entity_key, display="")` — a pointer to a graph
  entity for a citation card.
- `GraphHop(subject, edge_kind, obj, source_system, source_ref)` — one traversed
  relation, carrying the ASSERTING record's provenance (same fields/semantics as
  `Citation`), so each hop is independently click-through-able.
- `GraphPath(hops)` — an ordered tuple of hops.
- `RetrievalResult.graph_paths: tuple[GraphPath, ...] = ()` — the multi-hop
  traversals for an answer. The default `()` means every existing constructor and
  every pre-1.3.0 wire producer stays valid, and a Tier-1-only deployment
  produces no graph paths.

**Trimmed by construction.** `GraphPath`s are only ever built from
`EntityGraph.paths` / trimmed-traversal output (brain-tier2-spec §1.3), so every
hop — including every intermediate node and edge — was visible to the requesting
principal. There is no API to attach an untrimmed path; the producer
(`ne.brain_graph.retrieval.GraphRetriever`) maps trimmed `GraphPathRecord`s into
`GraphPath`s and never fabricates a hop.

`Citation` is UNTOUCHED — its four required fields (`chunk_id`, `document_id`,
`source_system`, `source_ref`) stay required, and chunk citations keep flowing
through `citation_for` unchanged. No existing field or Protocol changed.

### Alternatives rejected

- **Overloading `Citation` with optional graph fields.** Rejected: a `Citation`
  points at one chunk; a hop is a typed triple with two entity endpoints. Bolting
  graph fields onto `Citation` would make its four-required-field contract
  ambiguous and every consumer would branch on "is this a chunk or a hop".
- **A separate top-level return type instead of a `RetrievalResult` field.**
  Rejected: `RetrievalResult` is the one `KnowledgeRead` return shape; a parallel
  type would fork the composition seam and break the `KnowledgeRead`-shaped
  `GraphRetriever` drop-in.

## Consequences

- **Purely additive MINOR bump 1.2.0 → 1.3.0.** No existing field is removed,
  renamed, or reordered; no existing API signature or observable behavior
  changes. All new types are `frozen=True` (Checkpoint-A immutability), and all
  `config_hash` invariants are untouched (no `config.py` change). This honors the
  Checkpoint-A freeze discipline (ADR-0001 freeze scope, SPEC §6): additive-only
  + ADR + version bump.
- **Consumer note for `ne-api`/console:** the SSE/wire schema gains an optional
  `graph_paths` array; absent = a Tier-1-only (config-gated) deployment.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the unit's
contract diff surface is `packages/ne-contracts/**`. It is indexed from the
repo-wide `docs/adr/README.md` for discovery.
