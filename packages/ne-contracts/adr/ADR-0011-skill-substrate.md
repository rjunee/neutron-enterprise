# 0011 — Skill Forge contract surface (`ne.contracts.skills` + `SkillSubstrate`)

## Status

Accepted (2026-07-20; NE-SKILL-0 — docs/architecture/skill-forge-neutron-enterprise-design.md §3, docs/architecture/skill-forge-implementation-plan.md §1).

## Context

The Skill Forge (a governed lifecycle that observes completed multi-step work,
authors reusable "skill" procedures, gates them through the existing HITL/eval
spine, and makes the approved set discoverable to agents) needs a frozen seam
before any of its implementation units can start. Per the SPEC §6 freeze
discipline, the cross-agent types and the substrate Protocol must land through
the contracts lane first, with an ADR and a version bump — exactly how
`ne-contracts` has grown since Checkpoint-A (ADR-0003…0010).

This unit delivers ONLY the contract shape: the frozen types, the two canonical
hashes, the `SkillSubstrate` Protocol, the entry-point slot name, and a
conformance kit. Everything that populates or consumes these types (the registry,
the discovery tools, the forge, the steward apply seam, the Claude Code binding)
is a later unit's job.

Two design inputs shaped the frozen surface:

1. **The ONE-ACL decision (design §7.7, owner-directed).** An earlier draft gave
   skills a bespoke `scope: {departments, roles}` field with its own visibility
   logic. The owner flagged it as a duplicate authorization path (a maintenance
   hazard) and directed consolidation onto the platform's single authorization
   substrate. Skill visibility must use the SAME `ne-permissions` label
   vocabulary that `PrincipalExpander` matches knowledge chunks against — one ACL
   across the whole brain.

2. **A Codex adversarial pass over the initial contracts shape (plan §1.7).** It
   surfaced a set of corrections that cannot be made cheaply after 1.8.0 freezes
   (they would invalidate stored hashes or break every binding), so they are
   decided here rather than deferred.

## Decision

Add a new frozen module `ne/contracts/skills.py` and re-export its public names
from `ne.contracts`. All models are `ConfigDict(frozen=True, extra="forbid")`.

### The contract surface

- `SkillSource(StrEnum)` — the provenance channel (`bundled`, `engagement`,
  `forged`, `learned`, `manual`), one member per line.
- `SkillPack` — the portable skill artifact (a lossless superset of the
  cross-agent SKILL.md standard): `name` (slug), `version` (≥1), `description`
  (≤500), `triggers` (non-empty, ORDER-significant — the eval targets), `body`,
  `source`, and defaulted `category`, `tags`, `requires_tools`, `platforms`,
  `acl_labels`, `provenance`.
- `WorkflowStep` / `WorkflowSnapshot` — a substrate-neutral completed-work
  record; datetimes validated timezone-aware; `finished_at` may not precede
  `started_at`.
- `SkillInvocation` — one firing, for usage telemetry.
- `AuthoringStandard` — the typed house authoring standard passed to `author()`.
- `skill_content_hash()` / `skill_routing_hash()` — the two canonical hash
  functions, also exposed as `SkillPack.content_hash` / `.routing_hash` computed
  fields.
- `SnapshotPage` / `InvocationPage` — the cursor-pull return `NamedTuple`s.
- `SkillSubstrate(Protocol)` — the per-backend seam.

### Decisions, with rationale

1. **Procedural-ONLY in v1 — no executable-artifact field.** `SkillPack` carries
   no `scripts`/`code`/`executables` field. v1 skills are procedure text only,
   which keeps skill firing inside the C6 governed-tool chokepoint: an agent
   following a procedure still calls every real tool through the one governed
   executor. Adding an executable-artifact field later would be reviewed as a
   redaction/governance regression, not a routine additive field. This is
   enforced structurally by an exact-field-set test (there is no "for later"
   executables field). It is a deliberate, named narrowing below the full
   SKILL.md standard, wideable only when a governed-execution story exists.

2. **ONE ACL — `acl_labels`, not a scope map.** `acl_labels: tuple[str, ...]` is
   the shared `ne-permissions` label vocabulary (design §7.7), the SAME set
   `PrincipalExpander` matches chunks against — NOT a bespoke
   `{departments, roles}` scope model. `()` means GLOBAL (visible to all). The
   one honest difference from a knowledge chunk is label ORIGIN, not a second
   engine: a chunk *derives* labels from a source system at ingestion; a skill
   has no source system, so its labels are *assigned by the approver at the
   gate*. Same vocabulary, same expander, same PDP, same audit — assigned vs
   derived is the only delta, and it lives entirely at creation time.

3. **D-12 — TWO hashes, both frozen now.** `content_hash` includes `acl_labels`
   (version identity + hash-pinned serve — re-labelling is a content change that
   versions through the gate); `routing_hash` EXCLUDES `acl_labels` (the eval
   gate binds to it, so a relabel-at-apply does not invalidate every stored
   eval). Both are computed fields over canonical JSON — not stored fields with
   an equality validator, so an inconsistent pack is unconstructable and no
   call site can pass a stale hash. Retrofitting either hash after 1.8.0 would
   break every stored hash and the eval-gate contract, so both are frozen here.
   The field classification is fixed and tripwired: ORDERED (`name`, `version`,
   `description`, `triggers`, `body`, `source`, `category`); SORTED-SET (`tags`,
   `requires_tools`, `platforms`, plus `acl_labels` in `content_hash` only);
   EXCLUDED (`provenance` and the hashes themselves). Set-semantic fields are
   normalized to `sorted(set(...))` before hashing so a visibility-identical
   permutation yields the same hash and dedups correctly; `triggers` is
   deliberately NOT sorted (its order is meaningful). `requires_tools` joins the
   sorted-set class by its subset semantics — an extension of the plan's named
   three, recorded here so the classification is complete. A tripwire test fails
   the moment `SkillPack` gains a field not explicitly classified hashed-or-
   excluded, so a future "safe to defer new optional fields" allowance can never
   silently un-hash an equivalence-bearing field.

4. **D-13 — `author()` returns `SkillPack`, sealed by re-validation.** Because
   the hashes are computed fields, there is no unsealed-draft type to add: an
   `author()`-returned `SkillPack` is already internally consistent. The draft
   carries empty `provenance` and `acl_labels=()`; the core seals it by
   RE-VALIDATION (`model_validate` over `model_dump(exclude=<hashes>)` with
   `provenance`/labels supplied), never `model_copy(update=...)` (which bypasses
   validators on a frozen model). The hashes serialize OUT (populating the DB
   columns) but are never accepted IN — `extra="forbid"` rejects the dumped hash
   keys, which is exactly why the normative reconstruction excludes them. This
   pinned pydantic 2.13.4 behavior is asserted in
   `tests/test_skills_contracts.py::test_roundtrip_via_dump_excluding_hashes`.

5. **D-14 — the dead `author(edits=)` parameter is DROPPED.** No described call
   path consumes it: the near-dup path uses `existing=`, and steward edits are
   applied server-side at the ne-api apply seam, never round-tripping through
   `substrate.author`. A frozen parameter with no consumer is the most expensive
   YAGNI, and the accepted cost is stated: adding even a defaulted parameter
   later is a Protocol-breaking change for every binding.

6. **Typed `AuthoringStandard`, not `AnyMap`.** The authoring standard is a
   known-shape interface input, so bindings code against a typed contract rather
   than `Mapping[str, Any]`. An `extra: AnyMap` bag preserves additive evolution
   without a signature change. (`provenance` stays `AnyMap` — see (11).)

7. **`SnapshotPage` / `InvocationPage` `NamedTuple`s with `exhausted`.** The
   cursor-pull return is a self-documenting `NamedTuple` (unpacks positionally)
   carrying an explicit `exhausted` end-of-stream flag; the `cursor` is an
   OPAQUE substrate-defined STRING resume token, stable on an empty/exhausted
   page. This is explicitly NOT the flywheel integer `after_id` pattern
   (curation.py) — it is described as what it is, an opaque string cursor,
   because the CC transcript binding has no monotonic DB id.

8. **The `ne.plugins.skill_substrate` slot is normative HERE — no registration
   code.** A plugin slot is just an entry-point GROUP NAME (`plugins.py:20`); a
   slot "exists" the moment a provider declares an entry point. This unit names
   the slot normatively and declares NO entry point and NO registration file —
   NE-SKILL-1a/5 declare the first entry points. Stated here so nobody hunts for
   a missing file.

9. **`withdraw()` is KEPT, not folded into set-replace `materialize`.** A
   dedicated `withdraw` lets a targeted retirement or security-pull remove a
   skill from a surface without recomputing every surface's active set. Folding
   it into a full set-replace `materialize` would force a recompute for an
   immediate cross-surface removal; the plan §9 freeze checklist retains it.

10. **`materialize` keeps `Sequence[SkillPack]` with a no-authorization
    contract.** An index-projection narrowing (name+description rows) was
    considered and rejected for v1; instead the docstring freezes the contract
    that packs arrive ALREADY ACL-filtered for the surface's principal by the
    core and the substrate makes NO authorization decision. The substrate half
    of the ONE-ACL guarantee is enforced by the kit's over-materialize tripwire
    (`test_materialize_does_not_refilter`): materialize four ACL-diverse packs
    and assert all four appear — a substrate that drops or re-filters any fails.

11. **`provenance` stays `AnyMap` with a documented key contract.** A typed
    `SkillProvenance` was rejected for v1 (freeze cost of typing a field later);
    the well-known keys (`snapshot_id`, `session_id`, `principal_id`,
    `config_hash`, `review_id`) are documented on the field, and signing refs
    (R-9) can ride inside additively.

12. **Signed registration is a downstream obligation this contract enables.**
    The two hashes make labels tamper-EVIDENT but do not by themselves BIND a
    label assignment to an approval. approve→register must later record a signed
    C10 hash-chain entry binding `{content_hash, acl_labels, approver,
    review_id}`; that is a NE-SKILL-1b apply-seam obligation, enabled here by
    exposing both hashes.

### Conformance kit (C5)

`SkillSubstrateContractKit` ships in the same unit
(`ne/contracts/testing/kits/skill_substrate.py`; `ne.contracts.testing.*` is
freeze-exempt, so the kit can evolve after 1.8.0). It is CAPABILITIES-AWARE via a
`capabilities` class attribute: NE-SKILL-1a lands the read side and overrides
`capabilities` to drop `author`, so the two author checks SKIP rather than fail
(honest subset). The read subset is every check except the two author checks; the
FULL kit is the NE-SKILL-1b/5 bar, and the C5 dual-binding swap test becomes
CI-blocking when the second binding (NE-SKILL-5) lands, per the C5 "≥2
implementations" rule.

### Portability invariant (owner-directed)

`SkillPack` is a lossless superset of the cross-agent SKILL.md standard: its
fields map 1:1 to SKILL.md YAML frontmatter (`name`/`description`/`version`) plus
the fixed body sections (When to Use / Procedure / Pitfalls / Verification), so a
neutron skill is portable to another harness by construction. Two deliberate,
named deviations, both wideable later: (a) `description ≤500` (our discovery
routes on the description with no native loader); (b) procedural-only (see (1)).

## Consequences

- **Purely additive MINOR bump 1.7.0 → 1.8.0.** `skills.py` is a NEW module; no
  existing Protocol, type, model, or signature is touched. Proven by the entire
  pre-existing `ne-contracts` suite passing unmodified except the two version-pin
  literals (`tests/test_import.py`, `tests/test_graph_citation_types.py`).
  `test_plugins.py` and `test_protocol_inventory.py` pass byte-unmodified
  (`SkillSubstrate` is a Phase-7 addition outside the SPEC §4 32-Protocol
  inventory; this unit declares no entry point).
- **What would be BREAKING later (and is therefore forbidden in this and every
  later unit):** renaming or retyping any of these surfaces; adding a REQUIRED
  field to these models after 1.8.0; changing any `SkillSubstrate` method
  signature after 1a/5 implement it; re-classifying or un-hashing any hashed
  field. New OPTIONAL defaulted fields remain additive — but the hash-
  classification tripwire forces each one to be explicitly classified.
- **This is a one-way door.** Because the hashes are frozen now, any late field
  addition/rename after merge invalidates stored hashes; owner-review-before-
  merge is the backstop, and this decision list should surface in the PR body.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the unit's
contract diff surface is `packages/ne-contracts/**`. It is indexed from the
repo-wide `docs/adr/README.md` for discovery.
