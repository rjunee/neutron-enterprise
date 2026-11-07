# ne-schema

**One LinkML source, many tiers.** `src/ne/schema/schemas/operational.yaml` is the
single source of truth for the operational domain model. It is the tier-upgrade
mechanism from SPEC §3.1:

| Tier | Generated from the same source |
|------|--------------------------------|
| **T1 (this unit)** | Pydantic models + JSON Schema |
| T2 (later) | typed entity contracts |
| T3 (later) | SHACL / OWL |

Because every artifact derives from one schema, the models, the JSON Schema, and
(later) the typed contracts and shapes cannot drift from each other.

## Seed operational model

Nine entities plus a container, with typed relations expressed as identifier
references (Tier 1 → the generated Pydantic fields are `str` id links; the same
relations become typed entity references at Tier 2):

- **Account** — a customer/prospect/partner organization (the anchor entity).
- **Person** — a contact/employee/user; `works_at → Account`.
- **Document** — content; `author`/`owned_by → Person`, `about → Account*`.
- **Ticket** — a service request; `belongs_to → Account` (required),
  `requester`/`assignee → Person`.
- **Order** — a commercial order; `account → Account`, `placed_by → Person`.
- **Customer** — a Meridian banking customer; `has_auto_loan → AutoLoan*` and
  `has_servicing_note → ServicingNote*`.
- **AutoLoan** — a Meridian auto loan; `has_payment_schedule → PaymentSchedule*`
  and `has_servicing_note → ServicingNote*`.
- **PaymentSchedule** — a typed due-date, amount, and payment-state record.
- **ServicingNote** — a typed hardship/repossession/contact/resolution fact.
- **OperationalRecordSet** — tree-root container binding a set of the above;
  it is the JSON Schema root class.

Enums: `AccountStatus`, `DocumentType`, `TicketStatus`, `TicketPriority`,
`OrderStatus`, `CustomerStatus`, `AutoLoanStatus`, `PaymentScheduleStatus`, and
`ServicingNoteType`. Every class, slot, and enum carries a description.

Cross-field integrity rules (e.g. a Ticket's `resolved_at` only when it is
closed) are **deliberately out of scope at Tier 1** — they are SHACL (Tier 3)
territory and are not enforced by the generated Pydantic models.

### Conventions & ne-contracts alignment

The generated models align with ne-contracts value-object conventions where they
overlap: same pydantic pin (`>=2.13.4,<3`) and `extra="forbid"` strictness. These
entities are **records, not value objects** — they are intentionally *not* frozen
(operational data is updated in place). ne-schema imports nothing from other `ne`
packages; ne-contracts is consumed conceptually only (pin/strictness parity).

## Event schemas (SPEC §4)

A **second** LinkML source, `schemas/events.yaml`, defines the typed platform
event spine (NE-005). ne-schema is now a small **schema registry**: the codegen
driver (`codegen.py`) iterates a `SchemaSpec` tuple, so each source generates its
own `<name>.py` / `<name>.schema.json` / manifest and all six artifacts flow
through the one freshness gate. Adding a schema is one `SchemaSpec` entry.

Every event extends the abstract `PlatformEvent`, which mandates the fields SPEC
§4 requires on **every** event:

- **`config_hash`** — SHA-256 hex (`pattern`-enforced), the process-start
  configuration digest stamped on every event.
- **`actor`** — the acting principal.
- **`provenance`** — a required string map (may be empty) of where the event and
  its state came from.
- **`occurred_at`** — UTC occurrence timestamp.
- optional **`kpi_tag`** (C8, KPI-first) and **`trace_id`**, plus an `event_id`
  identifier and an `event_type` discriminator.

Concrete events: **`AuditEvent`**, **`CorrectionSignal`**, **`ApprovalEvent`**
(tier 0–3), **`CostEvent`** (tokens/USD/lane), **`ShapeViolation`**; the JSON
Schema root is **`EventRecordSet`** (a batch container). Enums: `AuditOutcome`,
`CorrectionKind`, `ApprovalDecision`, `ViolationSeverity`.

The generated Pydantic events **structurally** satisfy the frozen
`ne.contracts.EventLike` surface (and its five named subsurfaces) *without
inheritance* — ne-schema imports nothing from ne-contracts at runtime, and
ne-contracts is untouched (contract-freeze option (a); see `docs/adr/0002-*`).
The Postgres outbox that carries these events lives in the sibling `ne-events`
package (ADR-001 §4.1, ADR-0002).

## Layout

```
packages/ne-schema/
  src/ne/schema/
    schemas/operational.yaml     # source of truth #1 — operational domain
    schemas/events.yaml          # source of truth #2 — event spine (SPEC §4)
    generated/                   # committed codegen output — DO NOT EDIT
      operational.py             #   Pydantic models (operational)
      operational.schema.json    #   JSON Schema (root: OperationalRecordSet)
      _manifest.py               #   operational SCHEMA_VERSION / LINKML / SHA256
      events.py                  #   Pydantic models (events)
      events.schema.json         #   JSON Schema (root: EventRecordSet)
      _events_manifest.py        #   events SCHEMA_VERSION / LINKML / SHA256
      __init__.py                #   the one hand-written file here
    codegen.py                   # platform-side driver + SchemaSpec registry
    __init__.py                  # re-exports the generated models + versions
  codegen/
    requirements.in              # linkml==1.11.1
    overrides.txt                # jsonschema[format-nongpl] (drops GPL rfc3987)
    requirements.txt             # compiled, ==-pinned + --generate-hashes
    generate.py                  # runs INSIDE the isolated venv (imports linkml)
  .codegen-venv/                 # git-ignored isolated codegen env
```

## Regeneration workflow

```
uv run ne schema generate   # regenerate the committed artifacts from the YAML
uv run ne schema check      # CI gate: fail if committed artifacts are stale
```

`generate` builds (or reuses) the isolated codegen venv, runs the in-venv
generator, and writes `generated/`. `check` regenerates into a temp dir and
byte-compares — it is the same gate `test_generated_freshness.py` runs in CI.

## Schema-versioning discipline

The schema version lives in `operational.yaml` (`version:`) and is propagated
into `generated/_manifest.py` (`SCHEMA_VERSION`) and `operational.schema.json`
(`version`). Any schema change **must**:

1. bump `version:` in `operational.yaml`,
2. add a matching `## <version>` entry to `CHANGELOG.md`,
3. regenerate and commit the artifacts.

`test_versioning.py` machine-checks that all four agree and that the manifest
records the source `SOURCE_SHA256` and the `LINKML_VERSION` pin.

## Isolated codegen env (SPEC §2 C4 / C11)

linkml (Apache-2.0) is a superb schema toolchain, but its **transitive closure is
not license-clean for the platform lock**: a build-time audit of all 88 dists in
linkml 1.11.1's closure found the GPLv3+ `rfc3987` (pulled by `jsonschema[format]`
and imported in-process when present), plus `linkml-runtime` and 7 more CC0-1.0
dists, `sparqlwrapper` (W3C), and `docutils` (mixed GPL classifier) — none of which
the frozen license gate (`tools/supply-chain/license-policy.toml`) can classify
FREE. Putting linkml in the workspace lock would therefore embed GPL in the
platform venv and fail the gate closed.

So linkml is confined to an **isolated, hash-pinned codegen venv** that never
enters `uv.lock`, the SBOM, or the platform venv:

- `requirements.txt` is compiled with `uv pip compile --universal --generate-hashes`
  and the `overrides.txt` swaps `jsonschema[format]` → `jsonschema[format-nongpl]`,
  **eliminating the GPL `rfc3987`** outright (format validation uses the non-GPL
  `rfc3986-validator` / `rfc3987-syntax`).
- The venv is built with `uv pip install --require-hashes` (C11: exact-pinned +
  hash-verified), applying the same override.
- `test_codegen_env_policy.py` asserts `rfc3987` is absent, `linkml==1.11.1` is
  present, and every requirement is `==`-pinned and hashed.

The only distribution ne-schema adds to the platform lock is its runtime
dependency `pydantic` (already locked by ne-contracts). See `NOTICE`.
