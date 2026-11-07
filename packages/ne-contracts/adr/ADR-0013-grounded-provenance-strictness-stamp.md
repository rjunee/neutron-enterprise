# 0013 — `GroundedProvenance.strictness` tier stamp

## Status

Accepted (2026-07-21; NE-HGATE-0 — register C-P2-7/B-P0-1/D-P2-2).

## Context

The grounding-strictness design (docs/architecture/anti-hallucination-grounding.md §7) lets a
deployment run the grounding gate at one of several posture tiers ("strict" |
"standard" in v1). Downstream consumers — audit surfaces, ne-console, support
tooling — need to know which tier the gate actually ran at for a given turn,
not just what the deployment is configured to today (configuration can change
between the turn and the read). That means the tier has to be STAMPED onto the
per-turn provenance record, not merely inferred from current config.

`GroundedProvenance` (ADR-0010, 1.7.0) is the existing per-turn grounded-answer
provenance carrier — but it is frozen (`ConfigDict(frozen=True,
extra="forbid")`), so per SPEC §6 freeze discipline, adding any field to it is
a contract change requiring an ADR and a version bump, exactly like every
prior `ne-contracts` growth since Checkpoint-A (ADR-0003…0012).

## Decision

Add one field, additive-with-default, and bump `ne-contracts` 1.9.0 → 1.10.0
(MINOR).

1. **`strictness: str = "strict"`, appended as the last field.** No other
   field on `GroundedProvenance`, and no other model in `runtime.py`, is
   touched. The default `"strict"` IS the pre-1.10 semantics: every
   pre-1.10.0 constructor call and every previously-serialized wire payload
   (validated under `extra="forbid"`) lands on `"strict"` — the tier the gate
   actually implemented before this unit existed. Upgrades therefore never
   silently loosen a deployment's effective posture; they only make the
   existing posture visible.

2. **Plain `str`, not a `Literal["strict", "standard"]`.** The enumerable set
   of valid tier values is a deployment-configuration concern that belongs on
   the config seam (`OntologySettings`), not baked into the frozen provenance
   contract. Fixing the type to a `Literal` here would force a contract bump
   every time a new tier is introduced at the config layer; a plain `str`
   keeps the provenance carrier stable while `OntologySettings` (a later,
   non-frozen unit) owns the closed set and its validation.

3. **Not caller/request-selectable (B-P0-1).** `strictness` is stamped ONLY
   by the G3 runtime post-step, sourced from the deployment's own
   `OntologySettings`, exactly the same forgeability discipline the class
   docstring already establishes for every other field on this model (the
   model's free text / a caller's request has no channel into it). A
   per-request tier would let a caller self-select a laxer grounding
   assurance level — an explicit non-goal.

4. **A separate field, not encoded into `gate` (D-P2-2).** `gate` is the
   rulebook IDENTITY — a version string for the deterministic grounding gate
   implementation itself (`GATE_VERSION`). `strictness` is the deployment
   POSTURE the gate ran under. Conflating the two would force every consumer
   that only cares about one axis to parse a composite string; keeping them
   as separate fields keeps both axes independently machine-readable and
   avoids a string-parsing contract nobody asked for.

## Consequences

- **Purely additive MINOR bump 1.9.0 → 1.10.0.** Proven by the entire
  pre-existing `ne-contracts` suite passing unmodified except the two
  version-pin literals (`tests/test_import.py`, `tests/test_graph_citation_types.py`).
  `test_grounded_provenance_docs.py`'s pinned docstring phrases ("ADOPTED",
  `assist_used=False`/`True`, "deterministic verified-table fallback") are
  untouched. `test_plugins.py`'s plugin-list assertion is untouched — this
  unit adds no entry points.
- **Automatically additive on the wire.** `ne-worker`'s `sse.py` / `turn_wire.py`
  emit `model_dump()` for the done-frame's `grounded` key, so the SSE payload
  gains `grounded.strictness` with zero code change in `ne-worker` — the one
  place that DOES need an edit is the exact-payload test pin
  (`packages/ne-worker/tests/test_turn_wire.py::test_done_sse_payload_includes_grounded_when_present`),
  which now expects the literal `"strictness": "strict"` key. The
  `grounded=None` path (`test_done_sse_payload_omits_grounded_when_none`) is
  unaffected — no `grounded` key is emitted at all when there is no
  provenance to carry.
- **Every pre-1.10 constructor stays valid.** `GroundedProvenance(...)` built
  with exactly the six pre-1.10 keyword arguments succeeds and reports
  `strictness == "strict"`; `model_validate` on a legacy six-key payload
  succeeds the same way under `extra="forbid"`.
- **NE-HGATE-2 stamps the real tier.** This unit ships only the contract
  field; the G3 runtime write path that reads `OntologySettings` and sets
  `strictness` to the deployment's actual configured tier is a later unit.
- **What would be BREAKING later (forbidden in this and every later unit):**
  renaming/retyping `strictness`; removing the `"strict"` default; making the
  field caller-settable at any layer; folding it into `gate`.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the
unit's contract diff surface is `packages/ne-contracts/**`. It is indexed from
the repo-wide `docs/adr/README.md` for discovery.
