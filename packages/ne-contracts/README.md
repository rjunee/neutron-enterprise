# ne-contracts

The seams that make loose coupling (C5) real: minimal, versioned Protocols; the
event *surfaces* platform events satisfy; the layered configuration framework and
`config_hash`; the entry-point plugin registry; module-lifecycle hooks; and the
contract-conformance test-kit pattern. Every other platform module builds against
this package and nothing else for its seams.

**Version 1.0.0 is the Checkpoint-A freeze baseline.** Version 1.1.0 adds the
composed platform `config_hash` (additive; ADR-0003). See
[Versioning & freeze policy](#versioning--freeze-policy).

Zero heavy dependencies: only `pydantic` and `pydantic-settings` (both MIT). No
provider SDK, no LiteLLM, no DBOS (C3 model independence).

## Protocol inventory (maps 1:1 to SPEC §4)

All Protocols are `typing.Protocol` (structural, not nominal) so adapters never
import-couple to this package to satisfy a seam.

| Domain | Protocols |
|---|---|
| Knowledge (9) | `KnowledgeRead`, `KnowledgeWrite`, `ChunkStore`, `Embedder`, `SparseEncoder`, `Reranker`, `GraphStore` (T2), `TripleStore` (T3), `ShapeValidator` (T3) |
| Models (2) | `ModelGateway`, `ModelRegistry` |
| Governance (5) | `PolicyDecisionPoint`, `Guardrail`, `Redactor`, `AuditSink`, `ApprovalGate` |
| Runtime (6) | `AgentSession`, `ToolRegistry`, `SubAgentDispatcher`, `Scheduler`, `MemoryProvider`, `ChatAdapter` |
| Integration (3) | `Connector`, `ActionTool`, `DataContract` |
| Identity (2) | `PrincipalResolver`, `TokenVerifier` |
| Secrets (1) | `SecretStore` (Vault KV v2 semantics) |
| Storage (1) | `ObjectStore` (S3-compatible: put/get/WORM-archive) |
| Eval/obs (3) | `EvalSuite`, `SignalEmitter`, `KpiRecorder` |

That is **32 Protocols**; `tests/test_protocol_inventory.py` is the
protocol-by-protocol Checkpoint-A receipt.

### Additive source-authority seams (1.19.0)

ADR-0022 adds a separate source-authority contract family outside the historical
SPEC §4 inventory: generation-bound `SourceIdentity`, immutable ACL/contract-bound
artifact identities and authority receipts, protected source reference/resolution
types, bounded authorized pages/continuations, and eight least-privilege Protocols.
The Reader and Writer are intentionally separate capabilities; request composition
must never receive the Writer. `KnowledgeRead`, `KnowledgeWrite`, and `ChunkStore`
retain their exact frozen method sets. Supported-source producers and downstream
consumers import this vocabulary rather than defining package-local siblings.

### Async convention

- Every I/O-crossing method is `async`.
- `Guardrail.check` and `Redactor.redact` are deliberately **sync** — they are
  deterministic, code-enforced checks that run BEFORE any model judgment (C12).
- Streaming/iterator seams (`ModelGateway.stream`, `Connector.backfill` /
  `incremental` / `permissions`) are **sync methods that return an
  `AsyncIterator`**: callers `async for` over the returned iterator; the method
  itself is not a coroutine.

### Immutable, hashable value objects

Every value object is a frozen pydantic model. Attribute reassignment is blocked
by pydantic, and **mapping fields are coerced at validation to an immutable,
hashable `FrozenDict`** (values are recursively frozen — nested maps become
`FrozenDict`, lists become tuples). So `hash(Principal(id="u"))` works, and
`principal.claims["x"] = "y"` raises `TypeError`. The declared field type stays a
plain `Mapping`, so plain-`dict` construction remains source-compatible at every
call site; `model_dump()` / `model_dump_json()` emit plain nested `dict`/`list`.
`FrozenDict`, `StrMap`, `AnyMap`, and `FloatMap` are exported from `ne.contracts`.

### Fetch-by-id (citation & audit replay)

Two additive fetch-by-id seams complement principal-scoped `search`:

- `KnowledgeRead.retrieve(chunk_ids, *, principal)` — **principal-scoped**,
  ACL-consistent with `search` (a principal can never `retrieve()` a chunk
  `search()` would trim); for citation/evidence replay (NE-062).
- `ChunkStore.get(chunk_ids)` — **unscoped**, for audit reconstruction (NE-030
  "why did the system say X"); NOT principal-trimmed — callers above the audit
  layer must use `KnowledgeRead.retrieve`.

## Configuration framework

`NeSettings` (a `pydantic-settings` `BaseSettings`) is the base for every
module's settings. Layering precedence, HIGH → LOW:

1. init kwargs
2. environment (`NE_` prefix, `__` nesting — e.g. `NE_BRAIN__VECTOR_STORE`)
3. TOML file (`ne.toml` by default; TOML is read with stdlib `tomllib`, no extra
   dependency)
4. field defaults

Selecting *which* file feeds the TOML layer (selection never changes value
precedence — init > env > TOML > defaults is fixed):

- **Static default** — a subclass overrides `toml_file` in its `model_config`
  (defaults to `ne.toml` in the cwd).
- **Per-instance / per-customer** — `Settings.from_config_file(path, **overrides)`
  selects `path` as the TOML source for that one construction (this is what the
  NE-070 per-customer config bundles build on).
- **Deployment convention** — set `NE_CONFIG_FILE`; the base class honors it at
  construction time. An explicit `from_config_file` call always wins over it.

A selected-but-missing file fails fast with `ConfigFileNotFoundError` instead of
silently falling back to defaults. `config_file` is a reserved, non-field name —
subclasses must not declare a field called `config_file`.

```python
from pydantic import BaseModel, Field
from ne.contracts import NeSettings

class Brain(BaseModel):
    vector_store: str = "qdrant"

class Settings(NeSettings):
    brain: Brain = Field(default_factory=Brain)

# Per-customer selection; init overrides still win over the file's values:
settings = Settings.from_config_file("customers/acme.toml")
```

### `config_hash`

`config_hash` is the **SHA-256 over the fully-resolved, canonicalized
configuration tree** (models, retrieval config, policy bundle version, schema
version, prompt versions) — computed at process start and stamped on every event
and eval run (SPEC §4). It is what makes "why did the system say X on March 3"
answerable.

- `compute_config_hash(settings)` → 64-char hex digest. The tree is dumped with
  `model_dump(mode="json")`, which renders `SecretStr`/`SecretBytes` as
  `"**********"`, so **secret material never enters the preimage** and **secret
  rotation does not change the hash**. In addition (ADR-0006), **URL userinfo
  (`user:pass@`) in ANY plain-`str` value is masked to the fixed sentinel
  `***:***`** at canonicalization time, so **DB passwords carried in plain-`str`
  DSN fields never enter the preimage** and **DSN credential rotation does not
  change the hash**. The JSON is canonicalized (sorted keys, tight separators,
  ASCII) so the digest is independent of field/dict ordering.

  > **One-time reshuffle (ADR-0006).** Upgrading to userinfo masking changes the
  > `config_hash` value ONCE for any deployment whose config carried userinfo in
  > a DSN (e.g. the pilot compose injects `postgresql://ne:PASSWORD@…`). Configs
  > WITHOUT URL userinfo hash byte-for-byte identically to before (proven by a
  > legacy-formula equivalence test), so the shift is scoped to userinfo-bearing
  > deployments. The release fingerprint is unaffected (it hashes default
  > settings, whose DSNs carry no userinfo). See ADR-0006.
- Process-start freeze API:
  - `freeze_config_hash(settings)` — compute, store, and return the hash;
    idempotent when re-called with an identically-hashing config; raises
    `ConfigHashMismatchError` if a *different* hash is already frozen.
  - `get_config_hash()` — return the frozen hash; raises
    `ConfigHashNotFrozenError` if freeze has not run.
  - `reset_config_hash_for_testing()` — test-only.

**Composed platform hash (1.1.0, ADR-0003).** `compute_config_hash` hashes a
*single* settings model, but the platform runs many module settings with no
aggregate. The composed variant covers **all** module configs (audit #7):

- `compute_platform_config_hash(modules: Mapping[str, BaseModel])` → 64-char hex
  digest over `{module_key: <canonical tree>}`, where each module's tree is
  canonicalized exactly as `compute_config_hash` does. `sort_keys` is recursive,
  so the digest is independent of the input mapping's insertion order **and**
  every nested key order. Changing ANY module's config changes the hash. Modules
  are **passed in** — `ne-contracts` imports no module settings (C5 decoupling).
  Raises `ValueError` on an empty mapping.
- `freeze_platform_config_hash(modules)` — freeze the composed hash into the
  **same** process-global slot as `freeze_config_hash` (ONE frozen hash per
  process; mixing single-module and platform freezes with different digests
  raises `ConfigHashMismatchError`), same idempotent/mismatch semantics.
  `get_config_hash()` returns it unchanged.

## Plugin registry

Adapters register via Python entry points under `ne.plugins.<slot>`; selection
is config-driven:

```python
from ne.contracts import PluginRegistry

registry = PluginRegistry()
store_cls = registry.resolve("vector_store", settings.brain.vector_store)  # e.g. "qdrant"
store = store_cls(...)
```

An adapter package declares its entry point in its own `pyproject.toml`:

```toml
[project.entry-points."ne.plugins.vector_store"]
qdrant = "ne_qdrant.adapter:QdrantChunkStore"
```

`PluginRegistry.register(slot, name, factory)` adds an **in-process**
registration (for tests/embedding) that OVERRIDES an entry point of the same
name. Resolution checks the programmatic table first, then entry points.
Helpers: `iter_plugins(slot)`, `load_plugin(slot, name)` (raises
`PluginNotFoundError` listing available names), `PLUGIN_GROUP_PREFIX`.

> Entry-point discovery requires the package to be installed. Bootstrap with
> `uv sync --all-packages` before running tests, or discovery fails with an
> empty plugin set.

## Contract-conformance test kits

A kit is a class named `<Protocol>ContractKit` — deliberately NOT prefixed
`Test`, so pytest never collects the abstract base. An adapter proves conformance
by subclassing the kit and overriding its `create_*()` factory:

```python
from ne.contracts.storage import ObjectStore
from ne.contracts.testing.kits.object_store import ObjectStoreContractKit

class TestMyObjectStore(ObjectStoreContractKit):
    def create_store(self) -> ObjectStore:
        return MyObjectStore(...)
```

pytest collects `TestMyObjectStore` (name matches `Test*`), inherits every
`test_*` method, and runs the full contract against the real adapter — no mocking
past the seam under test. Legacy kits under `ne.contracts.testing.kits` import
`pytest` (install the `testkit` extra). Source-authority kits exported from
`ne.contracts.testing` are stdlib-only, so importing the package does not pull
pytest into runtime plugin discovery. The reference adapters in
`ne.contracts.testing.memory` likewise load as plain entry-point plugins.

**Kits are contract law** (plan §2.4): changing a kit requires a PR-body note
plus synthesis sign-off, exactly like changing a Protocol signature.

Shipped kits: `ObjectStoreContractKit` (WORM semantics, injectable clock),
`SecretStoreContractKit` (Vault KV v2), and `SkillSubstrateContractKit`
(cursor-pull pagination, materialize/withdraw idempotence, the ONE-ACL
over-materialize tripwire, and author checks). The first two are proven
end-to-end here against the in-memory reference adapters, resolved through the
plugin registry — the exact pattern Checkpoint A instantiates.
`SkillSubstrateContractKit` is CAPABILITIES-AWARE: a read-only binding runs an
honest subset by overriding its `capabilities` attribute, so the author checks
skip until a full binding lands (NE-SKILL-1b/5).

NE-RTSWAP phase (a) adds five capabilities-aware kits for the runtime-facing
slots: `AgentSessionContractKit`, `ToolRegistryContractKit`,
`SubAgentDispatcherContractKit`, `SchedulerContractKit`, and
`MemoryProviderContractKit` — proving `run`/`close`, register/get/list,
`dispatch`, `schedule`/`cancel`/`list_jobs`, and `append`/`recall` are real,
testable seams. The ne-runtime reference bindings
(`DepartmentAgentSession`, `RuntimeToolRegistry`, `GovernedSubAgentDispatcher`,
`CronScheduler`, `AgentMemory`) are the first passing binding of each.

`ObjectStoreContractKit` enforces as contract law that **WORM retention can only
be extended, never shortened**: while a window is active, `archive()` with a
`retain_until` earlier than the current value (including any past timestamp)
raises `WormViolationError` (equal = idempotent, later = extend). Every current
and future `ObjectStore` adapter inherits this guarantee — matching S3 Object
Lock compliance-mode parity.

## Module lifecycle

`ModuleLifecycle` (runtime-checkable) exposes `module_name`, `async health() ->
HealthCheck`, `schema_version() -> str | None`, and `async migrate() -> str |
None`. Consumed by the NE-069 migration runner and deploy health probes;
implementations must be idempotent.

## Events

This package defines only the structural event **surfaces** (`EventLike` plus
five named placeholders: `AuditEventLike`, `CorrectionSignalLike`,
`ShapeViolationLike`, `ApprovalEventLike`, `CostEventLike`). Every event carries
`event_type`, `config_hash`, optional `kpi_tag`, `actor`, `provenance`, and
`occurred_at`. The field-complete **LinkML event schemas land in NE-005**;
because the surfaces are structural, NE-005's generated classes satisfy them
without importing anything from here.

## Durability

Runtime Protocols are **durability-engine-agnostic**. DBOS Transact is the
durable-execution engine, wrapped by the `@ne_workflow` / `@ne_step` decorators
that ship in `ne-runtime` (NE-033) — NOT here. A future Temporal move is an
**engagement-time port, not a config swap** (SPEC §4); there is deliberately no
durability slot in the plugin registry.

## Versioning & freeze policy

`ne-contracts` 1.0.0 is the Checkpoint-A freeze baseline. After Checkpoint A,
changes are **additive-only** and each requires an ADR in
[`docs/adr/`](../../docs/adr/) plus a contract version bump (SPEC §6, plan §0.5).
A Protocol another module depends on is never silently changed.

- **1.1.0** — additive: composed platform `config_hash`
  (`compute_platform_config_hash` / `freeze_platform_config_hash`), ADR-0003
  (`adr/` in-package). MINOR bump; the single-module API is unchanged and
  retained for backward compatibility.
- **1.2.0** — additive: `PolicyDecision.tier` (bundle-decided approval tier,
  ADR-0004 in-package). MINOR bump; default `None` keeps every 1.x producer/
  consumer valid.
- **1.4.0** — additive: `CompletionRequest.trace_id` (run-scoped correlation id
  as data, ADR-0007 in-package). MINOR bump; default `None` keeps every 1.x
  producer/consumer valid.
- **1.5.0** — additive: `SubAgentSpec.route_lane_hint` (narrow-only lane hint,
  ADR-0008 in-package). MINOR bump; default `None` keeps every 1.x producer/
  consumer valid.
- **1.6.0** — additive: `VerifiedProvenance` + `AgentRunResult.verified`
  (structural verified-answer provenance, ADR-0009 in-package). MINOR bump;
  default `None` keeps every 1.x producer/consumer valid.
- **1.7.0** — additive: `GroundedProvenance` + `AgentRunResult.grounded`
  (structural grounded-answer provenance for the grounding gate, ADR-0010
  in-package). MINOR bump; default `None` keeps every 1.x producer/consumer
  valid.
- **1.8.0** — additive: the Skill Forge contract surface (`ne.contracts.skills`
  — `SkillPack`/`WorkflowSnapshot`/`WorkflowStep`/`SkillInvocation`/
  `AuthoringStandard`, the two canonical hashes `content_hash` (labels-in) /
  `routing_hash` (labels-out), `SnapshotPage`/`InvocationPage`, and the
  `SkillSubstrate` Protocol), the normative `ne.plugins.skill_substrate`
  entry-point slot name (no registration code), and `SkillSubstrateContractKit`,
  ADR-0011 in-package. MINOR bump; a NEW module, so every 1.x producer/consumer
  is unchanged. `SkillSubstrate` is a Phase-7 addition OUTSIDE the SPEC §4
  32-Protocol inventory (which stays at 32) — `acl_labels` is the shared
  ne-permissions ACL, not a scope map (§7.7); v1 skills are procedural-only.
- **1.9.0** — additive: the context-budget primitive layer (ADR-0012 in-package).
  Six new exports — `ContextBudgetExceededError` (the operationally-raised,
  counts-only C12 governed refusal, one class distinguished by `stage`) and the
  NEW pure-stdlib `ne.contracts.tokens`: `TokenEstimator` (Protocol),
  `ByteTokenEstimator`, frozen `ContextBudget`, and the module defaults
  `DEFAULT_BYTES_PER_TOKEN` / `DEFAULT_PER_CALL_OVERHEAD`. MINOR bump; a NEW module
  plus a NEW error leaf, so every 1.x producer/consumer is unchanged. The default
  divisor is `1.5`, chosen by the vendored real-tokenizer oracle (it REFUTED the
  design's `3.0` on demo-shaped `urn:ne:entity:*` rows) — downstream units inherit
  `DEFAULT_BYTES_PER_TOKEN` from the module. The `errors.py` family docstring is
  rewritten and an AST guardian test pins that no production code broad-catches
  `NeContractsError`.
- **1.10.0** — additive: defaulted `GroundedProvenance.strictness` tier stamp
  (ADR-0013 in-package). MINOR bump; a defaulted field on a frozen model, so
  every 1.x producer/consumer is unchanged; old serialized payloads validate
  as `"strict"`.
- **1.11.0** — additive: defaulted `assurance` provenance label on
  `GraphEntityRef`/`GraphHop` (ADR-0014 in-package). MINOR bump; a defaulted
  field on frozen models, so every 1.x producer/consumer is unchanged; old
  serialized payloads validate as `"source"` (the backbone semantics they
  were produced under). `"extracted"`/`"confirmed"` are the NE-T2X
  LLM-extraction pathway labels.
- **1.12.0** — additive: defaulted `source_ref` evidence ref on
  `GraphEntityRef` (ADR-0015 in-package). MINOR bump; a defaulted field on a
  frozen model, so every 1.x producer/consumer is unchanged; old serialized
  payloads validate as `source_ref=""` ("not threaded through this layer",
  never a claim of no provenance). Closes the design §6.1
  one-click-evidence gap for the endpoint-extracted shape
  (NE-T2X-5-ENDPOINT-EVIDENCE-REF).
- **1.13.0** — additive: public `thaw` export, the sanctioned
  egress-boundary converter that recursively normalizes a `FrozenDict`
  (and any nested `FrozenDict`/tuple/frozenset it contains) back to plain
  `dict`/`list` builtins, for a caller handing a frozen contract value to a
  third-party library that type-checks for literal builtins rather than
  accepting the `Mapping`/`Sequence` ABCs (ADR-0016 in-package). MINOR
  bump; the function itself is not new — it is the private serializer
  already wired into every `FrozenDict` field's `model_dump`, promoted to a
  public name. The private `_thaw` alias (`_thaw is thaw`) keeps `ne-api`'s
  existing private import working unchanged, so every 1.x producer/consumer
  is unchanged. Reference call site: `ne.gateway.gateway.LiteLLMGateway
  ._call_params` (NE-GATEWAY-FROZENDICT).
- **1.14.0** — additive: defaulted `decision_event_id` on `ApprovalOutcome`
  (ADR-0017 in-package). MINOR bump; a defaulted field on a frozen model, so
  every 1.x producer/consumer is unchanged; old serialized payloads validate
  as `None`. Only the gate call that actually appends the decision
  `AuditEvent` (`approval.granted` / `approval.rejected`) populates it with
  that record's real `event_id`; an outcome rehydrated by polling
  (`await_outcome`) stays `None`.
- **1.15.0** — additive: defaulted `principal_id` on `CompletionRequest`
  (ADR-0018 in-package). MINOR bump; a defaulted field on a frozen model, so
  every 1.x producer/consumer is unchanged; old serialized payloads validate
  as `None`. Carries the calling principal's opaque identifier (the
  `Principal.id` shape, joinable with the audit chain) so the gateway can
  stamp `CostEvent.actor` with the principal that caused the call instead of
  the gateway's own service identity; `None` (or an empty string) means the
  gateway falls back to its service actor.
- **1.16.0** — additive: defaulted `approval_id` on `ApprovalOutcome`
  (ADR-0019 in-package). MINOR bump; a defaulted field on a frozen model, so
  every 1.x producer/consumer is unchanged; old serialized payloads validate
  as `None`. The durable request id of the approval the outcome decided —
  unlike `decision_event_id` it is stable row state, so every
  `DurableApprovalGate` path populates it (direct decision AND rehydration);
  the C6 executor reads it to stamp the execution→approval join onto
  `tool.execute` (B14). A producer with no durable request stamps nothing,
  and consumers must omit the key then — never invent one.
- **1.17.0** — additive: defaulted `requested_model` on `CompletionRequest`
  (ADR-0020 in-package). MINOR bump; old payloads validate with `None`, which
  preserves the governed task route. An explicit value is a boot-registry
  gateway alias for this call's desired primary, not a provider model string,
  endpoint, or untyped provider-bound parameter. The gateway still validates
  registry membership, lane and egress policy, and records the requested and
  actually served aliases separately.
- **1.18.0** — additive: the closed inference-target lifecycle surface
  (ADR-0021 in-package). `InferenceTargetOperator` exposes typed
  `describe`/`status`/`start`/`stop` methods over strict frozen values carrying
  only opaque safe identifiers, closed operation/state/outcome enums,
  correlation timestamps and the private-manifest SHA-256. Executable paths,
  executable digests, argv, environment, endpoints, credentials, raw child
  output and free-form error text are structurally absent. The accompanying
  conformance kit uses a binding-owned mutation counter to distinguish a real
  idempotent retry from a second provider/Compose side effect.
- **1.19.0** — additive: source-authority identities, immutable artifact and
  authority receipts, target-specific protected-source refs/resolutions,
  bounded authorized retrieval/continuation values, eight least-privilege
  Protocols, and conformance kits (ADR-0022 in-package). The M365 contract
  identity is produced by ne-connect from its exact credential-free ODCS,
  permission, PII, exact fixed-duration freshness, mapper, and config descriptor.
  Continuation decode exposes only a strictly parsed key-id/MAC locator; the selector
  authenticates it against private durable state. No authority
  store/evaluator, producer admission, runtime enforcement, or change to the
  frozen Knowledge Protocols ships in this contract-only unit.
- **1.20.0** — additive: verifier-authored request expiry and the minimal
  `AuthenticatedRequestContext` authorization snapshot (ADR-0023 in-package).
- **1.21.0** — additive: the content-free
  `SourceBoundEvidenceProjectionResolution` target and the extending
  `SourceBoundEvidenceProjectionResolver` Protocol for governed evidence projection
  without changing the existing five-target resolver seam (ADR-0024 in-package).
- **1.22.0** — additive: the independent
  `EvidenceAssurance=direct|inferred|extracted` vocabulary and
  `SourceBoundToolResultSetRef` for one protected Tool result backed by an
  ordered complete EvidenceRecord set (ADR-0025 in-package; ADR-0024/1.21.0 is
  reserved by PRE-015A1). The original single-ref type remains unchanged, and
  graph element assurance remains the separate
  `source|confirmed|extracted` vocabulary.
- **1.25.0** — additive: `Completion.finish_reason` and the derived
  `Completion.truncated` (the provider's stop reason, carried verbatim rather
  than discarded, so a completion cut off at the output budget can no longer be
  served as if it were whole; ADR-0028 in-package). MINOR bump; default `None`
  keeps every 1.x producer/consumer valid and reads as not-truncated.
- **1.26.0** — additive: the OPTIONAL `TurnContextBudget` capability Protocol —
  `async context_budget_for_turn(task, *, requested_model, lane)` returning a
  `TurnModelBudget` (the frozen `(model, budget)` pair: the `ContextBudget` of
  the model IN FORCE for one turn, plus the name of the model it belongs to),
  or `None` when it cannot be named; `lane` is the same lane pin the caller is
  about to put on its `CompletionRequest` (ADR-0029 in-package; the async
  `TurnModelBudget`-answering `lane`-taking form was amended in this same
  release cycle before the version shipped — no second bump). `ModelGateway` is unchanged and stays frozen;
  this is a separate capability a concrete gateway MAY also satisfy, tested for
  with `isinstance`, with the caller's composed budget as the documented
  fallback. MINOR bump; every existing implementation stays valid.
- **1.27.0** — additive: the OPTIONAL `PolicyDecision.approval_window_seconds`
  field (ADR-0030 in-package) — the policy-resolved per-action approval window
  in seconds, carried FIRST-CLASS on the decision. Deliberately NOT an
  obligation: `obligations` is the Rego/Cedar parity surface and Cedar
  obligations are static per-rule annotations, which cannot express a
  data-resolved per-action value (the CI-caught error that preceded this
  field). Default `None` keeps every pre-1.27.0 producer valid and means "the
  policy named no window; the deployment default applies" — never "expire
  immediately". MINOR bump; mirrors the `tier` precedent (1.2.0, ADR-0004).
- **1.28.0** — additive: the OPTIONAL `PolicyDecision.approval_window_declared`
  bit beside the window (bundle 1.4.0). `True` only when the window came from
  `data.ne.config.approval_windows.actions[<action>]` or `.tiers[<tier>]` — a
  number this deployment wrote down; `False` for `tiers.rego`'s built-in floor,
  for a pre-1.4.0 bundle, and whenever the window is `None`. It exists for one
  consumer: the runtime's park-time coherence check
  (`_require_honourable_approval_window`) refuses a window that outruns the
  caller's authorization horizon, and that refusal buys legibility rather than
  safety — so it fires on a DECLARATION and merely warns on a floor, because a
  floor is not a promise anyone made. Default `False` keeps every pre-1.28.0
  producer valid and lands on the warn-and-proceed side. MINOR bump.
- **1.29.0** — additive: the OPTIONAL `PolicyDecision.standing_approval_seconds`
  field (ADR-0030 amended in-package for the F1 standing-approval carryover) —
  how long an approver's DECISION stays usable once the attempt it was asked
  for has terminalized `authorization_expired`, in seconds, resolved by the
  policy bundle beside `approval_window_seconds` and carried the same
  first-class way. Two clocks, two questions: the window bounds how long a
  human has to ANSWER; the standing lifetime bounds how long that ANSWER
  outlives the attempt. Default `None` keeps every pre-1.29.0 producer valid
  and means "the policy named no standing lifetime", which the runtime reads
  as "mint NO standing approval" — the fail-safe direction, never an unbounded
  one. MINOR bump; shares the 1.27.0 field's bool/str validator, because a
  coerced `True` would mint a ONE-SECOND standing lifetime.
