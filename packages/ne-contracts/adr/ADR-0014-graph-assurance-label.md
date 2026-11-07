# 0014 — Graph element `assurance` provenance label

## Status

Accepted (2026-07-24; NE-T2X-0 — register A-P1-4/D-P2-2).

## Context

The NE-T2X epic makes
LLM entity/relation extraction an optional, configurable Tier-2 pathway
alongside today's structured, connector-derived graph backbone. The epic's
safety floor (design §2.1, F2 — Truthful provenance) requires that every
extracted graph element is **always** truthfully labeled, at every read
surface, in every deployment mode — including the least-supervised mode
("YOLO"/`autonomous`). Concretely: a consumer (console, agent, retrieval
provenance) must always be able to tell a fabricated/LLM-produced graph
element apart from one derived from a structured connector.

`GraphEntityRef` / `GraphHop` (ADR-0005, 1.3.0) are the frozen, `_Frozen`
(`ConfigDict(frozen=True)`) value objects that carry graph-aware citations
through `RetrievalResult.graph_paths`. Per SPEC §6 freeze discipline, any
field addition to a shipped `ne-contracts` model is a contract change
requiring an ADR and a version bump — the same discipline every prior
`ne-contracts` growth since Checkpoint-A has followed (ADR-0003…0013).

## Decision

Add one field to each model, additive-with-default, and bump `ne-contracts`
1.10.0 → 1.11.0 (MINOR).

1. **`assurance: str = "source"`, appended as the LAST field on both
   `GraphEntityRef` and `GraphHop`.** The default `"source"` IS the pre-1.11
   semantics: today's only producer of these types
   (`ne-brain-graph`'s `lightrag/retrieval.py:62,83`) builds exclusively
   backbone-derived refs and hops, so the default is not a placeholder —
   it is a true statement about every element built before this field
   existed. Every existing constructor call stays valid unchanged, and
   every previously-serialized payload (which never carried an `assurance`
   key) validates the new field to its default — therefore every legacy
   payload lands on `"source"`, and an upgrade never mislabels prior data
   as extracted.

2. **Exactly three values, and why the set is complete.**
   - `"source"` — derived from a structured connector (the backbone).
   - `"extracted"` — produced by the optional LLM-extraction pathway,
     not yet vouched for by a human.
   - `"confirmed"` — extracted, and subsequently human-approved.

   No fourth value, for two reasons. First, deployment MODE (which of the
   three extraction postures — `ingest_review` / `read_flagged` /
   `autonomous` — a deployment runs) is a config-seam concern, not a
   provenance concern (design D-P2-2): the mode governs *when* a human
   vouches, `assurance` records *whether* one has. Encoding mode into this
   field (e.g. a `"extracted-yolo"` value) would conflate immutable
   provenance with a deployment posture that can change over the element's
   lifetime, and would force a graph-wide relabel on every posture flip;
   deployment posture instead rides `config_hash` and `source_system` stays
   permanent history (`source_system="ne:llm"` never changes regardless of
   mode). Second, there is deliberately no decay/auto-promotion state
   (design §6.2): popularity or age is not verification, so an unconfirmed
   element stays `"extracted"` until a human acts — there is no fourth
   value for "extracted a while ago" or "read many times".

3. **Plain `str`, not `Literal`/`Enum`.** Adapting ADR-0013 point 2
   verbatim: fixing the type to a closed enumeration here would force every
   future value through a contracts version bump. The closed value set is
   instead owned and validated at the writer/config seam — `ne-brain-graph`'s
   `ExtractionWriter` and the review/apply state machine (later NE-T2X
   units) — which can evolve its own validation without destabilizing this
   frozen carrier type.

4. **Never caller/model-forgeable.** Same discipline as
   `GroundedProvenance.strictness` (ADR-0013 point 3) and the design's
   A-P1-3 writer-side defense: `assurance` is stamped ONLY by trusted write
   paths (`ExtractionWriter`, a later unit), never sourced from LLM model
   output or a caller's request. This unit ships only the contract field;
   the writer-side stamping discipline and its guardian tests are NE-T2X-2/3
   — this ADR states that as a forward design reference, not a claim this
   unit's tests exercise.

## Consequences

- **Purely additive MINOR bump 1.10.0 → 1.11.0.** Proven by the entire
  pre-existing `ne-contracts` suite passing unmodified except the two
  version-pin literals (`tests/test_import.py`, `tests/test_graph_citation_types.py`),
  plus a new `tests/test_graph_assurance.py` covering the default, legacy
  no-assurance-key payload validation, the wire-gains-the-field-automatically
  `model_dump` claim, `"extracted"`/`"confirmed"` round-trips, and the
  distinguishability guarantee (two otherwise-identical refs differing only
  in `assurance` compare unequal).
- **`ne-api`'s wire is UNCHANGED in this unit.** `wire.py` builds
  `GraphEntityRefView`/`GraphHopView` field-by-field (wire.py:345-449), not
  via `model_dump()` passthrough, so the exact-key-set wire pin
  (`packages/ne-api/tests/test_graph_wiring.py::test_brain_search_surfaces_trimmed_graph_paths_on_wire`,
  lines 486-499) stays green untouched. `assurance` reaching the wire is a
  deliberate later unit (NE-T2X-4), not a side effect of this one.
- **`_Frozen` is `ConfigDict(frozen=True)` only — stated honestly.** It does
  NOT set `extra="forbid"`; that remains unchanged by this unit (out of
  scope, and changing it would be breaking). Frozen-ness (assignment raises
  `ValidationError`) and hashability are what this ADR relies on for the new
  field, not extra-key rejection.
- **Hash-chain verification, WORM export, and replay are untouched** —
  extracted elements are ordinary audited writes with an extra label; this
  field carries no audit-chain, WORM, or payload-minimization implication.
- **What would be BREAKING later (forbidden in this and every later unit):**
  renaming/retyping `assurance`; removing the `"source"` default; making the
  field caller-settable at any layer; encoding deployment mode into it.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the
unit's contract diff surface is `packages/ne-contracts/**`. It is indexed
from the repo-wide `docs/adr/README.md` for discovery.
