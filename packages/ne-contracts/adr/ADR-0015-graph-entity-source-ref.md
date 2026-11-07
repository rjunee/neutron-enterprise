# 0015 — Graph entity `source_ref` evidence ref

## Status

Accepted (2026-07-27; NE-T2X-5-ENDPOINT-EVIDENCE-REF — Codex cross-review of
NE-T2X-5, filed as a P3 finding).

## Context

Design §6.1 promises the evidencing chunk behind an unconfirmed graph fact is
**one click away**. NE-T2X-5 (ADR-0014, `assurance` label) delivered the
correct FLAG for every valid shape, including the one where a hop's asserting
EDGE is vouched for (`assurance="source"`/`"confirmed"`) but one of its
ENDPOINTS — the subject or object `GraphEntityRef` — is `"extracted"`. The
console correctly renders the "LLM-extracted — unconfirmed" chip for that
shape (`ne.console`'s `provisionalAssuranceForHop` correctly picks the
endpoint's label). But the chip was then handed the EDGE's `source_ref` —
the only ref `GraphEntityRefView` (`ne-api` `wire.py:347`) had to offer —
which is a DIFFERENT element's provenance. The user is shown unrelated
evidence, and because that ref does not usually match the writer-stamped
`llm:<sha16>:<ref>` grammar, the confirm deep-link silently fails to render
either. `SearchPage.tsx:139-154` carried an explicit `KNOWN LIMITATION`
comment acknowledging exactly this gap.

The evidencing ref for an extracted ENTITY already exists one layer down:
`ne.brain_graph.model.EntityNode.source_ref` (the writer stamps
`llm:<sha16>:<ref>` there — design §3.2, every extracted element carries
exactly one evidencing chunk under the assertion-scoped ACL rule). But it is
DROPPED at the wire boundary: `ne.brain_graph.lightrag.retrieval._ref()`
builds a `GraphEntityRef` from an `EntityNode` field-by-field and never
copies `source_ref`, because `GraphEntityRef` (`ne-contracts` `types.py`,
frozen since ADR-0005/1.3.0) has no such field to receive it. The datum is
therefore NOT reachable at or below the `ne-api` wire boundary by any
derivation over data the layer already has — it was never carried that far
in the first place. A console-only fix, or a fix confined to `ne-api`'s
`wire.py`, would be theatre: there is nothing for either layer to derive
from.

Per SPEC §6 freeze discipline, any field addition to a shipped `ne-contracts`
model is a contract change requiring an ADR and a version bump — the same
discipline ADR-0003…0014 have all followed, most recently ADR-0014 for the
sibling `assurance` field this ADR extends.

## Decision

Add one field to `GraphEntityRef`, additive-with-default, and bump
`ne-contracts` 1.11.0 → 1.12.0 (MINOR).

1. **`source_ref: str = ""`, appended as the LAST field on `GraphEntityRef`
   (after `assurance`).** The default `""` is NOT a claim of "no
   provenance" — it means "not threaded through this layer yet", true of
   every constructor call and every payload serialized before this field
   existed. Every existing call site stays valid unchanged, and every
   previously-serialized `GraphEntityRef` payload (which never carried a
   `source_ref` key) validates to `""` — an upgrade never fabricates
   provenance for prior data.

2. **Same semantics as `GraphHop.source_ref`, one layer down.** The ref
   identifies the record that evidences THIS entity, exactly the way
   `GraphHop.source_ref` identifies the record that asserts the edge. For a
   `ne:llm`-extracted entity it is the writer-stamped `llm:<sha16>:<ref>`
   grammar (`ne-brain-graph`'s `ExtractionWriter`); for a backbone entity it
   is the connector's own ref (e.g. `Account:A1`). Opaque identifier only —
   never fact text or a display string (C12), matching every other
   `source_ref` field in this package.

3. **Threaded, not derived, at every layer it crosses.** The datum already
   exists at `EntityNode.source_ref` (`ne-brain-graph` `model.py`); this ADR
   makes it reachable at the contract boundary so `_ref()`
   (`ne-brain-graph` `lightrag/retrieval.py:61`) can copy it field-by-field,
   exactly the NE-T2X-4 `assurance`-threading precedent. This is
   EXPLICITLY NOT the NE-T2X-4 `provisional_source_count`
   derive-at-the-wire-boundary precedent: that count was derivable because
   every input it needed (`assurance`) was already ON the existing
   contract. Here the input (`EntityNode.source_ref`) is NOT on the
   existing contract at all — there is nothing to derive from without this
   field.

4. **`ne-api`'s `GraphEntityRefView.source_ref` is REQUIRED, no default —
   the anti-silent-drop stance ADR-0014 established for `assurance`.**
   Unlike the contracts-side field (defaulted for backward compatibility),
   the wire view has no legacy payloads to stay compatible with — it is
   rebuilt field-by-field on every response. A construction site that
   forgets to pass it fails LOUD (`pydantic.ValidationError`, caught by
   CI), never silently reproducing this exact defect by falling back to an
   empty/wrong ref.

## Rejected alternatives

- **Derive `source_ref` at the `ne-api` wire boundary from data already on
  the contract.** Rejected: no input reachable at that boundary carries it.
  `GraphEntityRef` (pre-1.12) has no such field, and `GraphHop.source_ref`
  is the EDGE's ref, not the entity's — using it would reproduce the exact
  defect this ADR fixes. This is the discriminator against the NE-T2X-4
  `provisional_source_count` precedent (point 3 above): derivation is only
  the right move when the datum is *reachable*, and here it is not.
- **Console-only patch that swaps which ref is displayed.** Rejected: the
  console renders exactly what the wire hands it (C7); the wire never sent
  the entity's own ref, so there is nothing correct for the console to
  select. This would be exactly the "theatre" the unit brief forbids.

## Consequences

- **Purely additive MINOR bump 1.11.0 → 1.12.0.** Every pre-1.12
  `GraphEntityRef` constructor call and every previously-serialized payload
  stays valid, landing on `source_ref=""`. Proven by the pre-existing
  `ne-contracts` suite passing unmodified except the two version-pin
  literals (`tests/test_import.py`, `tests/test_graph_citation_types.py`),
  plus new coverage in `tests/test_graph_assurance.py` for the default and
  the round-trip.
- **No `config_hash` impact.** `compute_config_hash` hashes `NeSettings`
  models only; `ne.contracts.__version__` and the shape of `GraphEntityRef`
  are not inputs to it.
- **`uv.lock` regenerates the `ne-contracts` package entry only** — via
  plain `uv lock`, never hand-edited (repo shared-file policy §1.4).
- **`ne-brain-graph`'s `_ref()` now threads `EntityNode.source_ref` onto the
  `GraphEntityRef` it builds** — the entity's own evidencing ref survives
  past the point that used to drop it.
- **`ne-api`'s `GraphEntityRefView` gains a REQUIRED `source_ref` field**,
  threaded field-by-field in `graph_path_views()` from each hop's
  `subject`/`obj`. `ProvisionalSummary.provisional_source_refs`
  DELIBERATELY stays hop-only in this unit — folding entity refs into that
  list would change the shape of the §6.3 PDP/approval payload
  (`ne.runtime.provisional`'s consumer), which is outside this unit's
  lanes; a possible follow-up unit, not a gap papered over.
- **The console's `GraphEntityRefWire`/`provisionalAssuranceForHop` now
  carry/select the correct per-element ref** — the chip shown for an
  extracted ENDPOINT carries that endpoint's own evidencing ref, and its
  confirm deep-link resolves, closing the design §6.1 gap for this shape.
- **What would be BREAKING later (forbidden in this and every later unit):**
  renaming/retyping `source_ref`; removing the `""` default on the
  contracts-side field; making the field caller-settable at any layer;
  making `ne-api`'s wire-view field optional/defaulted (that would
  reintroduce the silent-drop failure mode this ADR closes).

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the
unit's contract diff surface is `packages/ne-contracts/**`. It is indexed
from the repo-wide `docs/adr/README.md` for discovery.
