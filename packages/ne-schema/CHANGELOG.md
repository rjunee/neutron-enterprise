# Changelog — ne-schema

All notable changes to the operational schema are recorded here. The schema
version lives in `src/ne/schema/schemas/operational.yaml` (`version:`) and is
propagated into the generated manifest and JSON Schema; `test_versioning.py`
enforces that this file carries a matching `## <version>` entry. Any schema change
MUST bump `version:` and add an entry here.

## 1.1.0 — 2026-08-23 — Meridian governed multi-hop operational graph vocabulary

- Add the bounded `Customer`, `AutoLoan`, `PaymentSchedule`, and `ServicingNote`
  operational entities required for the PRE-016C governed Meridian graph path.
- Add exact directed relationships for `Customer → AutoLoan`,
  `AutoLoan → PaymentSchedule`, and both `Customer`/`AutoLoan → ServicingNote`.
- Preserve the original generic operational entities and edges unchanged. Stable
  Meridian identifiers, fixture-derived enums, integer-cent lower bounds, and
  closed-world generated models fail malformed or foreign projections closed.

## Tooling — 2026-07-09 — Tier-2 entity-graph contracts (NE-081)

- Additive 4th codegen artifact `generated/operational_graph.py`: frozen
  `<Class>Entity` node contracts + `EDGE_DEFS`/`ENTITY_CLASSES`/`EDGE_KINDS`/
  `ENTITY_CONTRACTS`, derived mechanically from the SAME `operational.yaml`
  (identity + provenance + scalar attributes, MINUS the `inlined: false`
  relationship slots, which become the typed `EDGE_DEFS`). Enrolled via
  `SchemaSpec.graph_filename` so it flows through the byte-compare freshness
  gate. **No schema semantic change** — `operational.yaml` is byte-untouched and
  `version:` stays `1.0.0`; the NE-004 pydantic/JSON-Schema/manifest artifacts
  are byte-identical. Consumed by `ne-brain-graph` (Tier-2).

## events 1.1.0 — 2026-08-03 — per-call latency and TTFT on CostEvent (B12)

- `CostEvent` gains two OPTIONAL slots, additive (no field removed or
  retyped): `latency_ms` (integer ≥ 0 — one monotonic call-start→termination
  delta, rounded to whole ms) and `ttft_ms` (integer ≥ 0 — streaming calls
  only, call-start→first-chunk delta). Both are omitted (`None`) when not
  measured: absent means unknown, never zero. Threaded through
  `ne.events.new_cost_event`; the same values ride the per-call gateway
  `AuditEvent` provenance stringified (NE-163a conventions).

## events 1.0.0 — 2026-07-03 — event spine schema (AuditEvent, CorrectionSignal, ApprovalEvent, CostEvent, ShapeViolation + PlatformEvent base and EventRecordSet container)

- Second LinkML source `events.yaml`, enrolled in the codegen registry alongside
  `operational.yaml` (its own version line, manifest `_events_manifest.py`, and
  JSON Schema root `EventRecordSet`). The codegen driver now iterates a
  `SchemaSpec` registry; the operational artifacts are unchanged (byte-identical).
- `PlatformEvent` abstract base carries the SPEC §4 mandate on EVERY event:
  required `config_hash` (SHA-256 hex, `pattern` enforced), `actor`, `provenance`
  (required string map), `occurred_at`, plus optional `kpi_tag` (C8) and
  `trace_id`, and an `event_id` identifier / `event_type` discriminator.
- Concrete events: `AuditEvent`, `CorrectionSignal`, `ApprovalEvent` (tier 0–3),
  `CostEvent` (tokens/USD/lane), `ShapeViolation`.
- Enums: `AuditOutcome`, `CorrectionKind`, `ApprovalDecision`, `ViolationSeverity`.
- The generated Pydantic events structurally satisfy the frozen
  `ne.contracts.EventLike` surfaces without inheritance (contract-freeze option
  (a); ne-contracts untouched — see ADR-0002).

## 1.0.0 — 2026-07-03 — initial seed operational schema (Account, Person, Document, Ticket, Order, OperationalRecordSet + enums)

- Seed operational domain model: `Account`, `Person`, `Document`, `Ticket`,
  `Order`, and the `OperationalRecordSet` container.
- Typed relations by identifier reference (Tier 1): `Person.works_at → Account`,
  `Document.author/owned_by → Person` and `Document.about → Account*`,
  `Ticket.belongs_to → Account` with `requester/assignee → Person`,
  `Order.account → Account` and `Order.placed_by → Person`.
- Enums: `AccountStatus`, `DocumentType`, `TicketStatus`, `TicketPriority`,
  `OrderStatus`.
- Constraints: identifiers on every entity, required fields, `pattern`
  validation (domain, email, currency, order number), `minimum_value` bounds,
  and `extra="forbid"` closed-world models.
- Codegen: one LinkML source generates Pydantic models + JSON Schema via an
  isolated hash-pinned codegen venv; `ne schema generate` / `ne schema check`.
