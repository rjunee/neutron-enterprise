# 0002 — Event outbox lives in a new `ne-events` package

## Status

Accepted (2026-07-03; decided and committed by NE-005). Non-contract decision —
no `ne-contracts` change, so no contract version bump.

## Context

ADR-001 §4.1 fixed the v1 event transport: **typed events travel via a Postgres
outbox table** drained by an exporter seam in `ne-worker` — no message broker.
NE-005 implements that outbox. Two questions had to be settled before writing
code:

1. **Which package owns the outbox runtime** (the SQLAlchemy table, the
   same-transaction `append`, the cursor drain seam)?
2. **Does implementing concrete events require changing the FROZEN
   `ne-contracts` v1.0.0 event surfaces** (`EventLike` and the five named
   subsurfaces)?

`ne-contracts` v1.0.0 is frozen: post-Checkpoint-A changes to a Protocol are
additive-only and require an ADR **plus** a contract version bump (SPEC §6). The
frozen package is also deliberately **zero-heavy-deps** (pydantic +
pydantic-settings only) so every module can depend on it cheaply.

## Decision

**1. The outbox runtime lives in a new `packages/ne-events` package**, depending
on `ne-contracts`, `ne-schema`, and `sqlalchemy`.

- **Not `ne-contracts`.** It is frozen, and its zero-heavy-deps rule forbids
  SQLAlchemy. A runtime data-access implementation is not a seam Protocol.
- **Not `ne-obs`.** Putting the outbox there would couple every event *producer*
  (which must call `append`) to the entire OpenTelemetry dependency stack that
  `ne-obs` pulls in. Producers should depend on the event transport without
  depending on tracing.
- `ne-events` is the natural home: it owns the event **schemas' transport** while
  `ne-schema` owns the schemas themselves.

**2. The outbox APIs are deliberately NOT `ne-contracts` Protocols in v1.**
There is a single implementation and the drain seam is package-internal
(`ne-worker` will call `drain_once`). A `TransportPort`-style Protocol can be
extracted **additively** later if a second transport (e.g. a broker) ever
appears — that would be an additive `ne-contracts` change at that time, with no
impact on today's freeze.

**3. The frozen event surfaces are SUFFICIENT — `ne-contracts` is untouched
(contract-freeze option (a)).** `EventLike` and its subsurfaces are
`runtime_checkable` **structural** Protocols (attribute-presence checks;
`ne-contracts`' own `test_events_surface.py` proves a non-inheriting object
conforms). The LinkML-generated events in `ne-schema` carry all six required
attributes with assignable types (`str`, `str | None`, `datetime`, and a
`dict` ⊆ `Mapping[str, Any]` for `provenance`), so they satisfy the surfaces
**without inheritance and without importing `ne-contracts`**. NE-005 therefore
changes zero bytes under `packages/ne-contracts/` and needs no contract ADR.

## Consequences

- A new workspace member `ne-events` enters the platform lock, adding SQLAlchemy
  (MIT) and its platform-conditional transitive `greenlet` (MIT). Both are C4
  free-class.
- Event producers depend on `ne-events` (and transitively `ne-schema`) but not on
  `ne-obs`/OpenTelemetry — the tracing and the transport stay decoupled.
- The outbox seam is mockable-free in tests: the logic runs real SQL against
  SQLite, with an env-gated live-Postgres suite as the parkable acceptance.
- If a second transport is ever needed, extracting a Protocol is an additive,
  ADR-gated change — not a v1 obligation.
- Because the generated events conform structurally, the `ne-schema` ↔
  `ne-contracts` relationship stays a *seam*, not an inheritance coupling: either
  can evolve within its contract without importing the other.
