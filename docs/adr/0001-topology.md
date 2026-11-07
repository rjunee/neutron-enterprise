# 0001 — Topology: modular monolith, three deployable processes

## Status

Accepted (2026-07-03; decided at spec review, finding P0-1; committed by NE-003).

## Context

The platform must satisfy C5 (loose coupling) and C10 (single-tenant) without
paying the operational weight of a distributed system or a message broker. We
need one topology every unit builds against, so module seams and deployment
boundaries are decided once rather than re-litigated per unit. The question:
how many processes, where do the module boundaries fall, and how do modules
communicate?

## Decision

**A modular monolith deployed as three platform processes plus infrastructure
services.** Module boundaries are **Python package boundaries** (the
`ne-contracts` Protocols), NOT network boundaries — most inter-module calls are
in-process Protocol calls.

Three deployable platform processes:

| Process | Contains | Notes |
|---|---|---|
| `ne-api` | console backend, `KnowledgeRead`/`KnowledgeWrite`, model gateway, identity middleware | stateless, horizontally scalable |
| `ne-worker` | agent runtime (on DBOS), flywheel, schedulers, signal/outbox exporter | DBOS owns its Postgres schema |
| `ne-ingest` | connector runs, ingestion pipeline | batch/queue driven |

Infrastructure services (separate containers): Postgres, Qdrant, Keycloak, OPA
(sidecar), TEI (embedding + rerank), vLLM (GPU profile), OTel collector, and
Langfuse (optional).

**Cross-process communication is limited to:** PDP calls (OPA over HTTP),
inference (TEI/vLLM over HTTP), OTLP export, and the database. **Typed events
travel via a Postgres outbox table** consumed by the exporter in `ne-worker` —
**there is no message broker in v1.** Everything else is in-process Protocol
calls.

## Consequences

- Any deviation from this topology requires a new ADR.
- Introducing a message broker would be an additive, ADR-gated change; v1
  deliberately ships without one (the Postgres outbox is the event transport).
- Only `ne-api` scales horizontally in v1; `ne-worker` and `ne-ingest` are sized
  to workload but are not designed for horizontal fan-out in this version.
- DBOS Transact (in `ne-worker`) is the durability engine. A move to Temporal is
  an engagement-time port, not a config swap — there is no durability plugin
  slot in `ne-contracts` (SPEC §4).
- Because module boundaries are package boundaries, a module can later be split
  into its own process without changing its callers, as long as its seam stays a
  Protocol.

### Freeze scope (ne-contracts v1.0.0)

The `ne-contracts` v1.0.0 **frozen surface** is every module under
`ne/contracts/` EXCEPT `ne.contracts.testing.*` — i.e. the contract modules
`types`, `events`, `config`, `plugins`, `errors`, `lifecycle` and the domain
modules `knowledge`, `models`, `governance`, `runtime`, `integration`,
`identity`, `secrets`, `storage`, `evalobs`. Post-freeze changes to the frozen
surface are **additive-only** and require an ADR plus a contract version bump.

`ne.contracts.testing.*` (the conformance kits and reference adapters) is
**additive-exempt**: new kits and adapters land with a patch bump and no ADR,
PROVIDED they add no new requirements to existing adapters. SPEC §11 records this
decision.
