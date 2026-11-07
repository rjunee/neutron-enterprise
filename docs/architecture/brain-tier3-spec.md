# Brain Tier-3 — Formal Ontology Producer Spec (NE-085)

**Status:** build-ready producer spec · **Date:** 2026-07-09 · **Unit:** NE-085 (SPEC §5 Phase-7) — sized **XL, split at spike** (split defined in §2 below)
**Package:** `packages/ne-brain-ontology/` (stub exists: `packages/ne-brain-ontology/README.md`, `src/ne/brain_ontology/__init__.py` — "implemented by NE-085")
**Depends:** NE-081/NE-082 (Tier-2, `packages/ne-brain-graph/` — currently a stub per its README) for the property-graph→RDF lift ONLY; everything else in this unit is dependency-ready now. A sibling spec for Tier-2 is planned at `docs/architecture/brain-tier2-spec.md` (not yet present at time of writing); where this doc touches the Tier-2 seam it binds to the **frozen contract** (`ne.contracts.knowledge.GraphStore`, `packages/ne-contracts/src/ne/contracts/knowledge.py:152-161`), not to that spec, so the two units cannot drift against each other.

**Terminology disambiguation (do this everywhere in NE-085 code and docs):** "Tier-3" is overloaded in this repo. **Brain Tier-3** = the ontology knowledge tier (SPEC §3.1 ladder). **Approval Tier-3** = the financial/legal/irreversible HITL tier (SPEC §4.2). This spec writes "T3" for the brain tier and "approval tier N" for the HITL ladder, never bare "Tier-3".

---

## 0. What this tier is, in one paragraph

T3 adds standards-based typing (BFO-2020 upper ontology + Common Core Ontologies 2.1), machine-checkable SHACL validation on **every** triple write (fail-closed: an invalid triple is quarantined, never written), and a **verified-answers mode**: structured questions are answered by pre-approved, versioned SPARQL query templates executed over the validated triple store. The AI never generates the fact — it can only select a template and supply arguments that are schema-validated, guardrail-checked, PDP-authorized, and audited (C6). That answer class therefore cannot be fabricated, and every answer is replayable from its audit record. This is the regulated-environment tier (GxP, BCBS 239, HIPAA-clinical, defense) per SPEC.

Per the SPEC row and the ABI teardown (hereafter "teardown"), this unit is the platform's **primary vendoring target**: we LIFT the proven MIT triple-store layer from jupyter-naas/abi rather than rebuilding it (the owner's build-vs-adopt principle — default ADOPT; the one scoped native rebuild in this unit, the facade, is logged as an exception in §4.4 below).

---

## 1. Normative inputs (read these verbatim before building)

| Input | Where | What it binds |
|---|---|---|
| NE-085 row | SPEC | Scope: Fuseki + pySHACL gateway + BFO/CCO 2.1 pinned + LinkML→SHACL + property-graph→RDF lift + CCO-3.0 migration procedure + the ABI lift list + the re-teardown mandate |
| T3 ladder row | SPEC | "Fuseki + pySHACL shape-gateway (validate-before-write) + BFO-2020/CCO v2.1 (pinned; CCO 3.0 migration procedure required)"; LinkML is the tier-upgrade mechanism (one source → JSON Schema/Pydantic → entity contracts → SHACL+OWL); all tiers behind ONE KnowledgeRead/Write pair |
| Ontology-stack slot | SPEC | Fuseki + pySHACL + LinkML default; Oxigraph (embedded) alternate; ABI subpackage = LIFT-code seed; GraphDB rejected (license); ABI-as-substrate rejected (fails C1/C4/C6) |
| ABI posture | SPEC | LIFT-ONLY; per-package NOTICE "MIT License, Copyright (C) 2025 Naas.ai"; re-teardown at NE-085 spike start; 8 revisit triggers (teardown §7) |
| Teardown §3.2 | teardown lines 60-63 | What is real in ABI (facade, 5 adapters, hash-diff sync, templatablesparqlquery, BFO lint, 203 TTL) and what is absent (runtime SHACL, LinkML, PG→RDF lift, any write validation) |
| Teardown §4 lift table | teardown lines 103-106 | Exact paths: `ApacheJenaTDB2.py` (570 LOC), `TripleStoreService.py:436-604` (hash-diff sync), `oxigraph_server.py`, `validate_bfo_ontology.py` (1,785 LOC), `templatablesparqlquery/` (loader 209 LOC), BFO/CCO corpus = **pattern/seed only, re-pin upstream** |
| Contracts (FROZEN) | `packages/ne-contracts/src/ne/contracts/knowledge.py:164-175` (`TripleStore`, `ShapeValidator`), `:47-51` (`ShapeReport`) | The frozen minimal Protocol surface this package must satisfy; contract changes = ADR + additive version bump only (freeze rule) |
| C6 chokepoint | `packages/ne-runtime/src/ne/runtime/middleware.py:3-47`, `tools.py:3-17` | GovernedToolExecutor is the ONLY name→handler path; ToolBinding holds handler + trusted extractors; specs-only registry |
| Tier classification | `packages/ne-runtime/src/ne/runtime/actions.py:27-52` | `read.` prefix ⇒ approval tier 0; deny-by-default for unclassified names; Rego authoritative at decision time |
| C7 trimming pattern | `packages/ne-brain/src/ne/brain/trimming.py:3-38` | Fail-closed label algebra (`acl_match_labels` always non-empty), package-local Protocol pattern, composition-root injection |
| LinkML codegen isolation | `packages/ne-schema/src/ne/schema/codegen.py:3-15,52-66` | linkml confined to hash-pinned `.codegen-venv`; committed artifacts + freshness check; `SCHEMAS` registry is the extension point |
| Offline-vendoring precedent | `packages/ne-brain/NOTICE` (Qdrant/bm25 assets), `packages/ne-brain/tests/test_vendored_bm25_assets.py:1-38` | HF-cache-layout vendored assets + permanent guardian tests + `HF_HUB_OFFLINE` no-network proof |
| Hard constraints | SPEC | C1 self-host, C2 no-egress/AIR_GAP, C3 gateway-only model calls, C4 license gate, C5 contracts, C6 PDP+audit, C7 read+write knowledge plane, C11 pinned+hash-verified supply chain, C12 deterministic guardrails first |

---

## 2. Decomposition — the XL split (recommended, with gates)

The SPEC row says "XL (split at spike)". Recommended split into **one spike + three L units**, each one pipeline run (§6 protocol):

### NE-085-spike — ABI re-teardown + pin + backend skeleton (size M)
*Dependency-ready now. Everything else gates on its outputs.*

1. **Re-teardown ABI at its then-HEAD** (teardown §7 trigger 8, line 168 — mandatory): clone fresh, diff the five lift targets against the reviewed state at `4b0d12f3` (teardown line 3), check the 8 revisit triggers (esp. trigger 2: pyshacl appearing in ABI's uv.lock — would change how much we lift vs. build), re-verify MIT license at HEAD. **Pin the lift to one reviewed commit recorded in the vendor manifest — never track upstream** (teardown line 168).
2. **Vendor manifest**: `packages/ne-brain-ontology/vendor/MANIFEST.toml` recording ABI repo URL, pinned commit SHA, per-file source path → NE path mapping, sha256 of each lifted file at the pin, and the license line. (C11 discipline applied to vendored source.)
3. **Re-pin BFO/CCO from upstream** (spec-critical, bolded in the SPEC row): download BFO-2020 and the CCO **2.1** module set from their canonical upstream repos — NOT ABI's vendored 2.0 snapshot (teardown line 106: "Do not inherit ABI's stale CCO snapshot; upstream BFO/CCO are CC-BY/BSD"). Record upstream URLs + release tags + sha256 per artifact in `assets/ontologies/MANIFEST.toml`. Verify licenses at the pinned versions (C4 intake): BFO-2020 = CC-BY-4.0 (data, attribution in NOTICE), CCO 2.1 = BSD-3-Clause — both "free to embed" classes.
4. **Oxigraph dev tier walking skeleton**: pyoxigraph-embedded `TripleStore` implementation passing a first-cut conformance kit against the frozen Protocol (`knowledge.py:164-169`) — proves the package builds, imports cleanly under `.importlinter` (ne.brain_ontology is already in `root_packages`, `.importlinter:8`), and gives every later unit a no-docker test substrate.
5. **Design ADR-out**: written decision on whether any additive `ne-contracts` extension is needed (this spec's answer: **no** — see §4.1; the spike must confirm or file the ADR).

**Spike gate:** vendor manifest + ontology pin manifest committed; teardown addendum committed; skeleton green. If the re-teardown finds the lift targets materially degraded at HEAD, fall back to the reviewed `4b0d12f3` state (it is already adversarially verified) and note it.

### NE-085a — Triple store + SHACL gateway + LinkML→SHACL (size L)
*Depends: spike. Lane: `packages/ne-brain-ontology` + `packages/ne-schema` (additive) + `deploy/compose`.*

- De-naased Fuseki/TDB2 adapter + oxigraph adapter behind package-local backend protocols (§4.2).
- Hash-diff idempotent ontology hot-reload (§4.3).
- The `ValidatingTripleStore` write gateway: pySHACL validate-before-write, fail-closed, quarantine + `ShapeViolation` events (§5).
- LinkML→SHACL codegen in ne-schema (§6) — the additive capability this unit needs from a shipped library.
- Dual-backend conformance suite (oxigraph in-process always; Fuseki LIVE-PARKED/CI-service, §9).
- Fuseki compose service under a `brain-t3` profile (§8.3).

### NE-085b — Ontology corpus + lint + property-graph→RDF lift (size L)
*Depends: NE-085a; the lift portion additionally depends on NE-081/082 merged (SPEC sequencing "081→082→085"). If Tier-2 slips, ship 085b without the lift module and split it out.*

- BFO/CCO 2.1 vendored offline + loader with C2 no-egress guards (§7).
- `validate_bfo_ontology.py` lift → `ne ontology lint` + CI job (§7.3).
- Ontology version registry + the **CCO 3.0 migration procedure** (required by the SPEC row and its CCO 3.0 risk row) (§7.4).
- Property-graph(AGE)→RDF lift via the injected `GraphStore` Protocol (§10).

### NE-085c — Verified-answers mode (size L)
*Depends: NE-085a (store+gateway); benefits from 085b (typed corpus) but can build against the seed schema.*

- Template model + git-based template registry, config_hash-stamped (§11.2).
- `templatablesparqlquery` port: LangChain `StructuredTool` → Pydantic-arg-validated NE `ToolBinding`s through `GovernedToolExecutor` (§11.3).
- C7 principal-scoped dataset construction (§11.4).
- `VerifiedAnswer` artifact + replay (§11.5).
- Red-team suite: template-injection attempts, no-ungoverned-path proof, aggregate-leak tests (§12).

**Why this split:** the spike de-risks the two externally-pinned inputs (ABI commit, ontology versions) that everything else consumes; 085a is the load-bearing infrastructure; 085b is corpus + tooling with the only cross-unit dependency (Tier-2); 085c is the product feature and the highest-security surface, and it should build last so it lands on a hardened store, not alongside it.

---

## 3. Package layout

```
packages/ne-brain-ontology/
├── pyproject.toml                  # deps §8.1; entry points §8.2
├── NOTICE                          # §8.4 — Naas.ai MIT line + BFO CC-BY-4.0 + CCO BSD-3 + dep table
├── README.md
├── vendor/
│   └── MANIFEST.toml               # ABI pin: commit SHA, file map, per-file sha256 (spike output)
├── assets/
│   └── ontologies/
│       ├── MANIFEST.toml           # upstream URL + release tag + sha256 per artifact (spike output)
│       ├── bfo/bfo-2020.ttl        # vendored, pinned (CC-BY-4.0)
│       └── cco/<module>.ttl        # CCO 2.1 module set, vendored, pinned (BSD-3-Clause)
├── src/ne/brain_ontology/
│   ├── __init__.py
│   ├── config.py                   # OntologySettings (NeSettings subclass; backend=fuseki|oxigraph)
│   ├── store/
│   │   ├── protocols.py            # package-local rich backend protocols (§4.2)
│   │   ├── fuseki.py               # LIFT (MIT attribution header): de-naased ApacheJenaTDB2 adapter
│   │   ├── oxigraph.py             # LIFT: pyoxigraph embedded dev tier (from oxigraph_server.py)
│   │   └── sync.py                 # LIFT: sha256 hash-diff idempotent ontology hot-reload
│   ├── gateway.py                  # ValidatingTripleStore — the ONLY writer path (§5)
│   ├── shapes.py                   # ShapeValidator impl (pySHACL) + shape loading (ne-schema artifacts + curated)
│   ├── ontology.py                 # vendored-corpus loader, version registry, C2 no-egress guards
│   ├── quarantine.py               # quarantined-triple store + ShapeViolation emission
│   ├── lint/
│   │   └── validate_bfo.py         # LIFT: static BFO conformance lint (1,785 LOC seed)
│   ├── lift/
│   │   └── graph_to_rdf.py         # Tier-2 AGE → RDF lift (GraphStore injected; §10)
│   └── verified/
│       ├── templates.py            # QueryTemplate model + git-registry loader (§11.2)
│       ├── tools.py                # template → ToolBinding factory (§11.3)
│       ├── scope.py                # C7 principal→dataset-clause construction (§11.4)
│       └── answers.py              # VerifiedAnswer artifact + deterministic renderer (§11.5)
└── tests/                          # §9, §12
```

Import discipline (C3/C5): `ne.brain_ontology` imports `ne.contracts`, `ne.schema` (generated artifacts only), `ne.events` (event constructors) — and **nothing else platform-side**. It does NOT import `ne.brain`, `ne.brain_graph`, `ne.runtime`, or `ne.permissions`: the GraphStore (Tier-2 lift source), the GroupExpander (C7), and the AuditSink are **injected at the composition root** as package-local structural Protocols, exactly the established pattern documented in `packages/ne-brain/src/ne/brain/trimming.py:21-38`. Add the corresponding `.importlinter` forbidden edges in this unit.

---

## 4. The store: Protocol surfaces + the ABI lift

### 4.1 The frozen contract surface (what composition sees)

`ne.contracts.knowledge` already ships the T3 seams, frozen at contracts v1.2.0 (`packages/ne-contracts/pyproject.toml:3`):

```python
# packages/ne-contracts/src/ne/contracts/knowledge.py:164-175
class TripleStore(Protocol):
    async def sparql(self, query: str) -> Sequence[Mapping[str, Any]]: ...
    async def insert_triples(self, triples: Sequence[tuple[str, str, str]]) -> None: ...

class ShapeValidator(Protocol):
    async def validate(self, graph: Mapping[str, Any]) -> ShapeReport: ...
# ShapeReport (knowledge.py:47-51): conforms: bool, violations: tuple[str, ...]
```

**Decision: no contract change.** The frozen surface is deliberately minimal ("shipped now so tiered deployments do not require a contract version bump later", `knowledge.py:6-7`). The richer operational surface (named graphs, formats, transactions, dataset-scoped queries) lives in **package-local protocols** (§4.2) — the same pattern ne-brain used when the frozen contracts were too narrow (`trimming.py:21-31`: "ne-contracts is FROZEN … the fix is the established package-local structural-Protocol pattern"). The object registered under the contract slots is always the **governed** implementation:

- `ne.plugins.triple_store` → `ValidatingTripleStore` (the gateway, §5). Its `insert_triples` is SHACL-gated; its `sparql` is the **steward/audit surface**, not the agent surface (agents get templates only, §11.1).
- `ne.plugins.shape_validator` → `PyShaclValidator`.

`ShapeValidator.validate(graph: Mapping)` takes a mapping per the frozen signature; define the mapping shape concretely in this package: `{"triples": Sequence[tuple[str, str, str]], "format": "nt" | "turtle", "data": str | None}` — accept either a triple list or serialized RDF, documented in the conformance kit. (If the spike finds this too lossy in practice, the fallback is an **additive** `ShapeValidatorV2` via ADR + contracts 1.3.0 — but start without it.)

### 4.2 Package-local backend protocols (what the package uses internally)

`src/ne/brain_ontology/store/protocols.py`:

```python
class TripleStoreBackend(Protocol):
    """Raw backend. PRIVATE to ne.brain_ontology — never registered as a plugin,
    never exported from the package __init__ (mirrors the handler-privacy rule of
    ne-runtime tools.py:12-17: the raw writer exists only inside the gateway)."""

    async def query(self, sparql: str, *, dataset: DatasetSpec) -> QueryResult: ...
    async def update(self, sparql_update: str, *, graph: str) -> None: ...
    async def insert(self, data: bytes, *, fmt: RdfFormat, graph: str) -> None: ...
    async def delete_graph(self, graph: str) -> None: ...
    async def graphs(self) -> Sequence[str]: ...
    async def health(self) -> BackendHealth: ...

class DatasetSpec(BaseModel, frozen=True):
    """The FROM/FROM NAMED graph set a query may see. Constructed ONLY by trusted
    code (scope.py for principal reads; ontology.py for schema graphs)."""
    default_graphs: tuple[str, ...]
    named_graphs: tuple[str, ...] = ()
```

Two adapters:

| Adapter | Source | Role |
|---|---|---|
| `store/fuseki.py` | **LIFT** from ABI `libs/naas-abi-core/naas_abi_core/services/triple_store/adaptors/secondary/ApacheJenaTDB2.py` (570 LOC; teardown line 103) | Production. Keep: the MRSW-retry operational knowledge (TDB2 is multiple-reader/single-writer — retry-with-backoff on write-lock contention), result paging, the jena-text deployment note. De-naas: strip the ABI service-container/bus/event coupling (teardown line 103 "De-naasing" risk); swap ABI config objects for `OntologySettings`; make every method async (httpx, matching repo convention `knowledge.py:5-6` "Every I/O-crossing method is async"). |
| `store/oxigraph.py` | **LIFT** from ABI `oxigraph_server.py` (teardown line 103) | The no-docker dev tier: pyoxigraph embedded (in-process, file-backed or in-memory). This is what makes the conformance suite runnable on hosts without a docker daemon (a real constraint here — "no docker daemon on build host"). |

### 4.3 Hash-diff idempotent ontology hot-reload

**LIFT** `TripleStoreService.py:436-604` (teardown lines 61, 103): each schema/ontology graph is loaded into a named graph keyed by content; a SHA-256 over the canonicalized serialization is stored alongside; reload compares hashes and rewrites only changed graphs. This gives idempotent, restart-safe ontology deployment — re-running `ne ontology sync` against an already-loaded store is a no-op, and a changed CCO pin swaps exactly the changed module graphs. Lives in `store/sync.py`, driven by `assets/ontologies/MANIFEST.toml`.

### 4.4 The one native rebuild (logged exception to default-adopt)

ABI's 714-LOC `TripleStoreService` facade is **not** lifted: its pub/sub and bus coupling violates ADR-001's no-broker topology (teardown line 129 — ABI "bakes a broker into its core"), and NE's facade must be the SHACL gateway, which ABI verifiably lacks (teardown line 62: zero validation between agent output and graph write). Rebuilding a thin async facade against the backend protocols is smaller than de-coupling ABI's. The **adapters + sync + lint + template pattern + conformance cases** are the adopt payload; the facade is the scoped exception. (Build-vs-adopt principle.)

---

## 5. The SHACL write gateway (fail-closed, every write)

`gateway.py::ValidatingTripleStore` — the T3 half of C7's "shapes-as-contract" discipline ("JSON Schema + Great Expectations at T1/T2, real SHACL at T3") and the thing ABI's docs claimed but never built (teardown line 40: "pyshacl … 0 occurrences in uv.lock").

**Invariants (each is a test):**

1. **No unvalidated write path exists.** `ValidatingTripleStore` is the only object holding a `TripleStoreBackend` with write methods; backends are package-private (never in `__init__.__all__`, never entry-point-registered). Structural no-bypass, same construction as `ne-runtime/tools.py:12-17` (handlers never enter the registry).
2. **Validate-before-write, fail-closed.** `insert_triples(triples)`: (a) build the incoming delta graph; (b) assemble the validation dataset = delta + the CBD (concise bounded description) closure of every subject the delta touches, pulled from the target graph — so shape constraints that span existing data (cardinality, datatype on existing nodes) see their context; (c) run pySHACL against the compiled shapes graph (§6) with the vendored BFO/CCO as the ont-graph, **`do_owl_imports=False` always** (C2 — pySHACL's owl:imports resolution reaches the network; §7.2); (d) `conforms=False` ⇒ the ENTIRE batch is quarantined — no partial writes — and a `ShapeViolation` event is emitted (event exists since NE-005; carrier fields incl. `config_hash` per `packages/ne-schema/src/ne/schema/schemas/events.yaml`); (e) only on conforms does the backend `insert` run, inside one transaction where the backend supports it (TDB2 yes; oxigraph bulk-load).
3. **Validator errors are denials.** A pySHACL exception (malformed shapes, OOM, timeout) propagates as `ShapeValidationError` — never degrade to write-anyway. Mirrors deny-by-erroring in `trimming.py:18-19`.
4. **Quarantine is inspectable, replayable, and audited.** `quarantine.py` persists the rejected serialized delta + the full validation report + provenance + `config_hash` (platform Postgres, same spine as T1 quarantine). Steward release path: fix → re-validate → write (a curation action, approval tier per action class — releasing quarantined data is `write.internal.ontology.release`, tier 1).
5. **Performance bound is explicit, not implicit.** Delta+CBD validation is O(delta), not O(graph). Shapes whose semantics are non-local (`sh:sparql` constraints over the whole graph, global uniqueness) are marked `scope: global` in the shape metadata and run in the **nightly full-graph validation job** + at every ontology reload, not per-write. This split is declared per-shape in ne-schema annotations (§6) — a per-write shape that cannot be evaluated on delta+CBD fails CI, not production. (Honest note: this is the standard incremental-SHACL compromise; see risk R2.)

`ShapeReport` maps: `conforms` ⇒ frozen contract field; `violations` ⇒ one string per SHACL result, format `"<focusNode> <sourceShape> <message>"` (stable, greppable, redaction-safe — node IRIs and shape names, never literal payload values, matching the redaction discipline of `middleware.py:44-46`).

---

## 6. LinkML→SHACL codegen (one schema source)

ne-schema is the tier-upgrade mechanism. Today it generates Pydantic + JSON Schema from two LinkML sources (`packages/ne-schema/src/ne/schema/codegen.py:52-55`: `operational`, `events`) inside the isolated hash-pinned `.codegen-venv` (`codegen.py:3-9` — linkml's GPL/CC0 transitive closure never enters the platform lock; that isolation is exactly why pySHACL generation must also run there).

**Additive changes to ne-schema (the "capability added to a shipped library" this unit needs — flag in the unit plan):**

1. `codegen.py`: extend the artifact set per `SchemaSpec` with an optional `shacl` flag producing `<name>.shapes.ttl` via `linkml gen-shacl` (plus `gen-owl` for the OWL projection), wired into `ARTIFACTS`/`check_freshness` exactly like the existing artifacts (`codegen.py:58-66`).
2. `codegen/generate.py` (in-venv script): add the gen-shacl/gen-owl invocations.
3. Schema source curation: the operational schema's classes gain `class_uri`/`slot_uri` mappings into CCO/BFO terms (e.g. `Person` → `cco:Person`, which subclasses the BFO material-entity chain) plus `exact_mappings` for provenance. This is **modeling work, not plumbing** — budget it in NE-085a (see risk R5). New T3-only classes go in a new `schemas/ontology.yaml` source registered in `SCHEMAS` rather than bloating `operational.yaml`.

`ne.brain_ontology.shapes` then loads: (a) the committed generated `*.shapes.ttl` artifacts from `ne.schema.generated`, (b) curated hand-written shapes under `assets/shapes/` (for constraints LinkML cannot express), compiles them into one shapes graph, hashes it, and **feeds that hash into the package's config surface so it lands in `config_hash`** (schema version is already a named config_hash input). One schema source, three tiers of enforcement — and a shape change is a visible config change on every event.

Freshness guard: same contract as today — CI fails if committed SHACL artifacts are stale vs. the LinkML source (`codegen.py:14`, `check_freshness`).

---

## 7. BFO/CCO: offline vendoring, lint, migration

### 7.1 Vendoring (C2/C11)

The ontologies are **data assets vendored in-repo**, following the Qdrant/bm25 precedent exactly (`packages/ne-brain/NOTICE` "Vendored model assets (committed in-repo, not resolved into the platform lock/SBOM)"; guardian tests `test_vendored_bm25_assets.py:1-38`):

- `assets/ontologies/bfo/` + `assets/ontologies/cco/` — the pinned TTL files, ~a few MB total (ABI's whole ontology asset base is 2.1M, teardown line 32; BFO+CCO 2.1 alone is smaller).
- `assets/ontologies/MANIFEST.toml` — per-file: upstream repo URL, release tag/commit, sha256, license, role (upper | mid | domain).
- Guardian tests (permanent): manifest hashes match file contents; no symlinks; the loader loads the full corpus with **no network** (socket-disabled fixture, same shape as the `HF_HUB_OFFLINE=1` proof in `test_vendored_bm25_assets.py:30-38`); triple counts within expected bounds per module (catches truncated vendoring).

### 7.2 C2 no-egress guards at runtime

Three concrete leak paths exist in this stack and each gets a code guard + test:

1. **pySHACL owl:imports resolution** — `do_owl_imports=False` hardcoded at the single pySHACL call site; a test monkeypatches socket and validates a shape set whose ontology declares a remote `owl:imports`.
2. **rdflib remote fetches** (JSON-LD contexts, `Graph.parse(location=...)`) — the loader only ever parses from `assets/` paths and in-memory bytes; `location=`/URL parsing is lint-banned in this package (ruff custom rule or a grep CI check).
3. **Fuseki-side fetches** (`LOAD <http://...>` in SPARQL Update) — the gateway never issues `LOAD`; the steward SPARQL surface (§11.1) refuses `LOAD`/`SERVICE` keywords (deterministic pre-check, C12-style, before anything reaches the backend). `SERVICE` (SPARQL federation) is likewise refused in template validation (§11.2).

`AIR_GAP` engagement (mirror of `packages/ne-brain/src/ne/brain/sovereignty.py:50-74`) changes nothing here — the tier is offline by construction; the guards above hold unconditionally.

### 7.3 Static BFO lint

**LIFT** `validate_bfo_ontology.py` (1,785 LOC; teardown line 104: "~1 week of codified BFO rules — bucket mapping, restriction inheritance, disjointness — rdflib-only dependency") into `lint/validate_bfo.py`. Exposed as `ne ontology lint` (tools/ne-cli addition) and a CI job that runs on: the vendored corpus (at pin bump), the ne-schema OWL projection (every codegen change), and curated domain ontologies. Expect rule updates against CCO 2.1 — the lift was written against ABI's 2.0 snapshot (risk R4).

### 7.4 Version registry + the CCO 3.0 migration procedure (required deliverable)

`ontology.py` maintains an **ontology version registry**: the loaded corpus pin set (from the manifest) + shapes-graph hash + loaded-graph hashes (from §4.3 sync) — surfaced in health checks and stamped into `config_hash` inputs.

The migration procedure (a committed runbook, satisfying the SPEC row's "CCO-3.0 migration procedure" and the risk row "CCO 3.0 breaking (~Dec 2026)"):

1. Vendor the new corpus beside the old (`assets/ontologies/cco@3.0/`), dual-manifest.
2. Run `ne ontology lint` + pySHACL over the new corpus; diff term inventories (removed/renamed IRIs) mechanically → a term-migration table.
3. Update ne-schema `class_uri` mappings + regenerate SHACL/OWL; contract freshness CI proves nothing else drifted.
4. Rewrite instance data: SPARQL Update migration scripts generated from the term table, executed graph-by-graph via the gateway (validated against the NEW shapes) into a staging dataset.
5. **Replay the golden verified-answer set** (§11.5) against the staged store — byte-identical structured results (or reviewed diffs) gate the cutover.
6. Hash-diff sync swaps the schema graphs atomically; old corpus retained one release for rollback.

This procedure is *tested in miniature* in CI with a synthetic 2-term rename fixture, so the runbook is executable, not aspirational.

---

## 8. Dependencies, deployment, licensing

### 8.1 Runtime dependencies (all C4 "free to embed" — verify at build time)

| Dep | Pinned (verify at build) | License | Role | Notes |
|---|---|---|---|---|
| `pyshacl` | latest stable | Apache-2.0 | SHACL engine | in-process; owl-imports disabled (§7.2) |
| `rdflib` | (pyshacl's floor) | BSD-3-Clause | graph model, TTL/NT parse, CBD | |
| `pyoxigraph` | latest stable | MIT OR Apache-2.0 | embedded dev/test backend | Rust binary wheel — hash-pin per C11; check wheel availability for CI arch |
| `httpx` | workspace pin | BSD-3-Clause | Fuseki HTTP | already a workspace dep (`ne-brain/NOTICE`) |
| Apache Jena Fuseki | 5.x, **out-of-process container** | Apache-2.0 | production backend | never imported; a service like Qdrant/Keycloak |
| `testcontainers` (dev) | dev-only | Apache-2.0 | Fuseki conformance in CI | LIVE-PARKED locally (§9) |

Explicitly NOT dependencies: owlready2 (no runtime OWL reasoning in v1 — the reasoner-gate question is deferred, §13 OQ3), linkml (stays in the ne-schema codegen venv), any LangChain remnant from the template port.

### 8.2 Plugin registration (`plugins.py:21`)

```toml
[project.entry-points."ne.plugins.triple_store"]
fuseki   = "ne.brain_ontology.gateway:build_fuseki_store"      # ValidatingTripleStore(FusekiBackend)
oxigraph = "ne.brain_ontology.gateway:build_oxigraph_store"    # ValidatingTripleStore(OxigraphBackend)

[project.entry-points."ne.plugins.shape_validator"]
pyshacl  = "ne.brain_ontology.shapes:PyShaclValidator"
```

Factories return the **gateway-wrapped** store only — there is no entry point that yields a raw backend (invariant §5.1). Config: `brain.triple_store = "fuseki"` (prod) / `"oxigraph"` (dev), per the C5 selection idiom.

### 8.3 Fuseki deployment

- `deploy/compose/dev` + `pilot`: new `fuseki` service under profile `brain-t3` (opt-in — T3 is v1.2, must not fatten the default pilot). TDB2 dataset on a named volume; jena-text index **off by default** (it is a Fuseki-only capability that would silently fork the backend contract — see §9 conformance profile).
- **Image**: build NE's own image from the Apache binary distribution, hash-verified (C11 pinned digests; the community Docker images don't meet the intake bar). Open question OQ2 tracks whether the upstream `docker.io/apache/jena-fuseki` image's provenance is acceptable instead.
- **Auth + exposure**: Fuseki is reachable only from platform processes (compose network, no published port in pilot); admin/API credentials injected via SecretStore, configured through Fuseki's built-in auth. The SPARQL endpoint is NEVER exposed to users — contrast ABI's auth-less nginx SPARQL proxy with wildcard CORS (teardown line 86), which is the anti-pattern.
- **Backups**: TDB2 requires its backup protocol (or offline copy) — wire into NE-100's backup framework as a T3-profile item; note in the runbook.
- Air-gap: the Fuseki image + Jena dist ride the NE-086 offline bundle; nothing in this package downloads at runtime (§7).

### 8.4 NOTICE obligations

Per-package NOTICE (pattern: `packages/ne-brain/NOTICE`), containing:

1. `Portions derived from jupyter-naas/abi (pinned commit <SHA>) / MIT License, Copyright (C) 2025 Naas.ai / https://github.com/jupyter-naas/abi` — the exact line mandated by teardown line 97; each lifted file also carries a header comment naming its ABI source path + the pin.
2. Vendored ontology assets section: BFO-2020 / CC-BY-4.0 / upstream URL + attribution statement; CCO 2.1 / BSD-3-Clause / upstream URL (attribution per BSD terms).
3. The dependency table (§8.1) in the standard name/version/license/homepage format.
4. Fuseki + Jena as external services accessed over HTTP ("not distributed with this package" phrasing, as ne-brain's NOTICE does for TEI/Qdrant).

---

## 9. Conformance strategy (dual-backend, docker-constrained)

The teardown's "dual-backend testcontainers conformance suite" (teardown line 113) is a **pattern lift** (its code is entangled with naas_abi_core service classes — "crib cases, not code"). Adapt it to this repo's proven two-lane convention (`packages/ne-brain/tests/` local/server split; LIVE-PARKED skip pattern in `conftest.py:140-182`, necessitated by "no docker daemon on build host"):

- **Local lane (always runs, no docker):** the full conformance kit against `OxigraphBackend` in-process. Because pyoxigraph is embedded, T3 — unlike T1 — gets a *real* backend in every unit-test run, not a fake.
- **Server lane (LIVE-PARKED locally, service container in GitHub CI):** the SAME parametrized kit against a live Fuseki (env-var URL, skip-if-unreachable locally; a pinned Fuseki service in the CI workflow — same approach as the Qdrant/TEI server suites).
- **Kit contents** (crib ABI's 1,476-line oxigraph test file for cases, teardown line 103): named-graph CRUD; SPARQL SELECT/ASK/CONSTRUCT result parity; unicode/datatype round-trips; hash-diff sync idempotency (double-sync no-op; changed-module partial reload); MRSW contention (concurrent writers on Fuseki — the retry path); gateway invariants (§5) on both backends; large-batch paging.
- **Pinned common SPARQL profile:** the conformance kit is the definition of the backend contract — SPARQL 1.1 Query/Update ONLY, no jena-text `text:query`, no `SERVICE`, no SPARQL-star, no vendor functions. Anything Fuseki-only is a declared optional capability behind a backend feature flag, never used by templates (risk R6).

Per the DoD, the kit ships as the contract-conformance suite for the `triple_store` slot; a future QLever/GraphDB-replacement adapter merges only by passing it.

---

## 10. Property-graph → RDF lift (the Tier-2 seam)

Purpose: T3 does not re-ingest the world — it lifts the Tier-2 property graph (AGE, structured-first, Splink-resolved; NE-081/082) into typed RDF, so the validated triple store inherits T2's entity resolution and provenance instead of competing with it.

Design (`lift/graph_to_rdf.py`):

- **Input seam:** the frozen `GraphStore` Protocol (`knowledge.py:152-161`) — `cypher()` reads only — injected at the composition root. No `ne.brain_graph` import (§3). The lift consumes whatever NE-081 registers under `ne.plugins.graph_store`; coordinate the node/edge property conventions (entity ids, ACL labels, provenance fields) via the Tier-2 spec (`docs/architecture/brain-tier2-spec.md`) when it lands — the ONLY coupling is those property names, so record them in one shared constants module... in `ne-schema` (both tiers already depend on it), not in either brain package.
- **Mapping is schema-driven, not ad hoc:** LinkML classes/slots (with their §6 `class_uri`/`slot_uri` CCO mappings) define how a T2 node label + properties become typed triples. A T2 node with no LinkML class mapping is NOT lifted (logged + counted) — deny-by-default typing; untyped data has no business in the regulated tier.
- **IRIs are deterministic:** minted from the T2 stable entity id (`ne:entity/<id>`), so re-lifts are idempotent and citations can join back to T2/T1 provenance chains.
- **Everything flows through the gateway** (§5): the lift is just another writer; a lift batch that violates shapes quarantines like any other write. The lift is where SHACL earns its keep — it will catch T2 modeling drift mechanically.
- **Incremental:** watermark on T2's updated-at bookkeeping; state-in-store idempotency markers (the one good ABI workflow idea, teardown line 75, translated to Postgres per its verdict).
- **ACL labels ride along:** each lifted entity's source ACL labels (T2 carries them from T1/connectors) are asserted as provenance triples on the entity's named graph assignment (§11.4) — this is what makes C7 possible at T3.

---

## 11. Verified-answers mode (the product feature)

### 11.1 The read-surface doctrine (security core)

Three read surfaces, strictly separated:

| Surface | Who | Path | Governance |
|---|---|---|---|
| **Verified-answer templates** | agents (and console structured-question UI) | `GovernedToolExecutor.execute` ONLY | C6 full stack: guardrails → PDP → (tier gate) → audit; C7 dataset scoping |
| **Steward SPARQL console** | named steward/admin humans | `ValidatingTripleStore.sparql` via ne-api, PDP-gated by role | audited; deterministic `LOAD`/`SERVICE` refusal (§7.2); C7 dataset scoping applies to non-admin stewards |
| **Audit/replay reads** | the replay machinery (NE-030-style "why did the system say X") | internal | reconstructs template executions from audit records (§11.5) |

**There is NO agent-facing raw SPARQL. Ever.** An agent cannot compose a query — it can only invoke a registered template tool. This is the property that makes the "cannot be fabricated" claim true: the fact path is template → validated store, and the model's entire influence is (template choice, argument values), both of which are validated, PDP-checked, and audited. A test proves no ToolBinding exposing free-form SPARQL exists in the package (§12).

### 11.2 Templates: governed, versioned, in git — NOT in the graph

**Deliberate divergence from ABI:** ABI stores SPARQL templates *inside the triple store* and hydrates them into agent tools (`templatablesparqlquery`, teardown line 61 — "genuinely novel"). Elegant, but it makes the governed tool surface **mutable by graph writes**: anyone who can insert triples can mint or alter an agent-callable query. In a regulated tier that is an escalation path, so NE inverts it:

- Templates live in the **per-customer config bundle** (git; NE-070 config-bundle machinery): `templates/verified/<template_id>.yaml`.
- The template registry hash is a **`config_hash` input** — adding/changing a template visibly changes the platform config fingerprint on every subsequent event.
- Template changes therefore arrive by reviewed git change (the propose-then-approve lane NE-066 already established for config/policy diffs), never at runtime.

Template model (`verified/templates.py`):

```yaml
template_id: open-deviations-by-site         # tool name becomes read.kg.open_deviations_by_site
version: 3                                   # monotonic; bumps on ANY change
description: "Open deviation records for a manufacturing site, newest first."
args:                                        # → generated Pydantic model, strict
  site_code: {type: str, pattern: "^[A-Z]{3}[0-9]{2}$"}
  since:     {type: date, required: false}
sparql: |                                    # parameterized ONLY via typed bindings
  SELECT ?deviation ?opened ?severity ?title WHERE { ... FILTER(?site = ?site_code) ... }
scope:                                       # C7 declaration (§11.4)
  graph_classes: [quality-records]           # which ACL-labeled graph families it may see
  result_shape: enumerable                   # enumerable | aggregate
answer:                                      # deterministic rendering (§11.5)
  columns: [deviation, opened, severity, title]
  citation_var: deviation                    # per-row provenance IRI
```

**Injection-proofing (C12, deterministic, pre-model):** argument values are NEVER string-interpolated into the query text. They bind via SPARQL parameterization (initBindings/`VALUES` injection built by trusted code) with type enforcement from the generated Pydantic model; the template's static text is validated at registry-load time (parseable SPARQL 1.1; SELECT/ASK only — no UPDATE/LOAD/SERVICE/DELETE; only declared variables). A malicious arg value can therefore only ever be an RDF literal, never syntax. Registry-load validation failures fail boot, not the request.

### 11.3 Templates through the C6 chokepoint

`verified/tools.py` turns each registry entry into a standard `ToolBinding` (`ne-runtime/tools.py:42-62`) — Tier-3 is **additive**: zero changes to `GovernedToolExecutor`, the guardrail engine, the PDP, or the audit chain (they are Checkpoint-D-verified composition; touching them is out of scope and forbidden to this unit).

- `spec.name = "read.kg.<template_id>"` → classifies approval tier 0 under the deny-by-default action-class mirror (`actions.py:44-46`: `read.` prefix) and the authoritative `tiers.rego`. A future write-capable template class (none in v1) would classify `write.internal.kg.*` and inherit tier ≥1 automatically.
- `spec.input_schema` = the JSON Schema of the generated Pydantic args model (guardrail toolargs validation runs against it, C12).
- `handler` executes: resolve principal scope (§11.4) → parameterized query via the gateway → build `VerifiedAnswer` (§11.5). The handler is constructed by the factory and — per the structural rule — exists ONLY inside the binding handed to the executor (`tools.py:12-17`).
- `extract_audit_detail` (binding-owned, trusted — `tools.py:55-62`) records `{template_id, template_version, args_shape, result_count, ontology_version, shapes_hash}` — names/ids/shapes only, never result values, per the redaction discipline.
- PDP surface: the Rego bundle can express per-template policy (`allow read.kg.open_deviations_by_site for group quality-team`) with zero new mechanism — the tool name IS the policy key. Every execution emits the standard `pdp.decide` + `tool.execute` audit events stamped with `config_hash` (which now covers the template registry, §11.2).

The LangChain→Pydantic-AI port called out in the SPEC row lands here: ABI's loader built LangChain `StructuredTool`s; NE keeps ABI's RDF-declared-argument *validation design* (teardown line 105 "keep the RDF-declared arg-validation design" — realized as the YAML→Pydantic args model) and discards LangChain entirely. The runtime's Pydantic-AI toolset integration (NE-033's `toolset.py`) consumes ToolBindings as-is.

### 11.4 C7: permission-aware triple reads

The frozen `TripleStore.sparql(query)` has no principal parameter — scoping is enforced BELOW the query text, in trusted code, exactly like Qdrant payload filters sit below the query in T1:

- **Named-graph ACL partitioning.** Instance data is written into named graphs partitioned by ACL label domain (the lift asserts each entity's graph per its source ACL labels, §10). The schema graphs (BFO/CCO/shapes) are world-readable within the deployment.
- **`scope.py` builds the `DatasetSpec`** (§4.2) for a request: `acl_match_labels(principal, expander)` — the SAME fail-closed label algebra as T1, injected `GroupExpander` protocol, composition-root wiring per `trimming.py:21-38` — maps to the set of named graphs the principal may see, intersected with the template's declared `graph_classes` (§11.2). The dataset clause (FROM/FROM NAMED) is constructed from that set by trusted code; the template text contains no FROM. Fail-closed inheritance: the label set always contains `principal.id` and is never empty (`trimming.py:11-19`), so the constructed dataset is never "all graphs"; expander errors propagate (deny-by-erroring).
- **Provenance double-check (defense in depth):** enumerable templates declare `citation_var`; the handler verifies each result row's provenance graph is inside the principal's graph set before rendering — a belt against graph-assignment drift.
- **Aggregates are the honest hard case.** A COUNT over a partial view is not "the count" — it is the principal's-view count, and worse, aggregate deltas can leak existence of invisible data. v1 policy: `result_shape: aggregate` templates must declare `scope.acl_complete: true` (asserting the graph classes involved are visible in full to every principal the PDP will allow on this template — enforced by pairing the template with a PDP rule restricted to groups holding those labels). Templates that can't declare that ship as enumerable or don't ship. (Risk R3; the provable-trimming suite covers this, §12.)
- Steward free SPARQL (§11.1) gets the same DatasetSpec construction unless the principal holds the named T3-admin role.

### 11.5 The `VerifiedAnswer` artifact + replay

`verified/answers.py`:

```python
class VerifiedAnswer(BaseModel, frozen=True):
    template_id: str
    template_version: int
    args: Mapping[str, Any]              # the validated, typed args (not raw model text)
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]    # the structured result — THE answer
    citations: tuple[str, ...]           # per-row provenance IRIs (join to T1/T2 citations)
    config_hash: str                     # covers template registry + shapes + ontology pins
    ontology_version: str                # from the §7.4 registry
    executed_at: datetime
```

- **The answer IS the structured payload.** The deterministic renderer produces the user-facing table/text from `columns`+`rows` with zero model involvement. If a conversational surface wants NL phrasing, the phrasing pass is downstream, explicitly labeled a paraphrase, and the `VerifiedAnswer` payload travels with it — the verified claim is never the paraphrase.
- **Replayable:** the audit record (tool.execute detail + this artifact persisted by the handler) contains everything needed to re-execute: template@version (immutable in git history), typed args, config_hash, ontology_version. `ne verify-answer <audit_id>` re-runs and diffs.
- **Replay honesty (state the guarantee precisely):** TDB2/oxigraph have no point-in-time query. Replay guarantees **procedural identity** (same template, same args, same config, same ontology pins) against the *current* store; **bit-identical results** are guaranteed only if the touched graphs are unchanged since execution — determinable from the write-audit trail (every write is an audited gateway event) + the §4.3 graph hashes. The replay tool reports which of the two levels it achieved. Do not oversell temporal replay; bi-temporal history is NE-083's lane, not this unit's.
- **Golden verified-answer set:** a fixture suite of template executions with expected structured results — the regression gate for ontology migrations (§7.4 step 5) and template changes.

---

## 12. Governance invariants — the test list (each is CI-enforced)

1. **SHACL fail-closed:** no code path writes to a backend except through `ValidatingTripleStore`; a planted invalid triple is quarantined + `ShapeViolation` emitted + absent from the store (both backends). Validator crash ⇒ denial.
2. **No ungoverned query path:** grep/AST test asserting no `ToolBinding` in the package wraps free-form SPARQL; the only handler factory takes a registry `QueryTemplate`; backends are unexported (import test); templates execute ONLY via `GovernedToolExecutor` (integration test wiring a real executor with recording PDP/sink — asserts `pdp.decide` + `tool.execute` events with config_hash on every call).
3. **C7 provable trimming:** the T3 twin of NE-043's suite: principal A executes a template and receives rows whose provenance ⊆ A's graph set; a principal with a **known entity IRI** cannot obtain its data via any template when the ACL hides it (fetch-by-id parity — the `retrieve()` lesson); empty/broken expander ⇒ minimal set, never all; aggregate template without `acl_complete` pairing fails registry load.
4. **C2 fully offline:** socket-disabled test loads corpus + shapes + validates + answers a template end-to-end (oxigraph backend); the three leak-path guards of §7.2 individually tested.
5. **C12 before model judgment:** template arg violating the Pydantic schema is rejected by guardrail toolargs BEFORE any execution; `LOAD`/`SERVICE`/UPDATE keywords in a registry file fail load; an argument value containing SPARQL syntax executes as a literal (parameterization proof).
6. **Additivity:** zero diffs under `packages/ne-runtime/`, `ne-govern/`, `ne-audit/`, `ne-contracts/` (unless the spike files the §4.1 ADR); `.importlinter` edges for `ne.brain_ontology` enforced in CI.
7. **C4/C11:** license gate green over the new closure; vendored asset + vendor-manifest hash guardians; NOTICE entries present (DoD).

---

## 13. Open questions

- **OQ1 — CCO 2.1 availability at spike time:** confirm the exact upstream artifact set for "CCO 2.1" (the CCO repo's release layout has shifted between releases; SPEC pins 2.1). If upstream has moved to a 3.0-line by spike start, the spike escalates to the owner: pin 2.1 as specced + schedule §7.4, or pin newer and amend SPEC.
- **OQ2 — Fuseki image provenance:** own image built from the Apache dist (recommended, §8.3) vs. the upstream Docker image — decide at spike against the C11 intake bar; owns the NE-086 air-gap bundle size either way.
- **OQ3 — OWL reasoning:** v1 ships NO runtime reasoner (SHACL + static lint only — matching SPEC, which names neither HermiT nor ELK). The teardown notes ABI's reasoner claims were docs-only (teardown line 40). If a regulated engagement demands DL classification, that is a new unit (isolated service, license-check owlready2/HermiT first), not scope creep here.
- **OQ4 — Tier-2 property conventions:** the shared node/edge property names (§10) must be agreed with the NE-081/082 builder and recorded in ne-schema — coordinate when `brain-tier2-spec.md` lands.
- **OQ5 — verified-answers on T1/T2 surfaces:** does the console structured-question UI (Phase-5 console) get a v1.2 surface for template invocation, or is chat the only consumer initially? Owner/product call; the ToolBinding surface serves both without change.

## 14. Risks — honest ranking (this is the XL unit; these are why)

- **R1 — De-naasing underestimation (schedule).** The Fuseki adapter + sync lift assumes extraction is cheaper than rewrite. ABI's code is coupled to its service container/bus (teardown line 103 names "de-naasing" as the risk); if the spike finds the 570-LOC adapter is >50% ABI plumbing, the honest fallback is "crib the ops knowledge (MRSW retries, paging, TDB2 config) + the test cases, write the adapter fresh" — which converts weeks-saved back into days-spent. The spike's job is to price this before 085a commits. *Mitigation: spike deliverable includes a line-count triage of each lifted file: keep / rewrite / drop.*
- **R2 — Incremental SHACL soundness (correctness).** Delta+CBD validation (§5.2) is an approximation: constraints whose truth depends on triples outside the CBD closure (global cardinality, cross-graph uniqueness, `sh:sparql` walks) can pass per-write and fail globally. The per-write/global shape-scope split (§5.5) makes the gap explicit and bounded, but a shape mis-scoped as per-write is a silent validation hole. *Mitigation: CI meta-check that per-write shapes use only the delta-safe constraint vocabulary; nightly full validation alerts on any divergence — a divergence is a P1.*
- **R3 — C7 over SPARQL is intrinsically harder than over Qdrant (security).** Named-graph partitioning + provenance double-check + template-only agent access is a defensible design, but SPARQL's expressiveness means the assurance rests on *the templates* being reviewed — a clever enumerable template can still encode an existence probe against data outside its rows (e.g., OPTIONAL branches). *Mitigation: template review checklist (no OPTIONAL/EXISTS across graph classes outside scope; static analyzer over the parsed algebra enforcing that graph-class discipline mechanically); the provable-trimming suite (§12.3) includes adversarial templates; treat template addition like policy change (NE-066 review lane, §11.2).* This is the single hardest problem in the unit — budget review time accordingly.
- **R4 — BFO/CCO 2.0→2.1 skew breaks the lifted lint (quality).** `validate_bfo_ontology.py` (1,785 LOC) encodes bucket/term expectations from ABI's 2.0-era snapshot; CCO 2.1 renames and restructures modules. Expect a real porting pass, not a drop-in — the teardown's "~1 week of codified rules" (line 104) is the value floor, not the cost ceiling. *Mitigation: lint runs against the freshly pinned corpus in the spike; failures triaged rule-by-rule (rule wrong vs. corpus fact).*
- **R5 — LinkML→CCO mapping is modeling work with no automation (schedule/quality).** §6.3's `class_uri` curation for the operational schema (Account, Person, Document, Ticket, Order) requires ontology judgment (which CCO term, which BFO chain), and bad mappings poison both SHACL and the lift. *Mitigation: crib ABI's `ABIOntology.ttl` human-readable BFO aliasing as seed material (teardown line 106); run `ne ontology lint` over the generated OWL projection; keep the v1 mapped class set small (the 5 seed classes) and grow per engagement.*
- **R6 — Backend conformance divergence (portability).** Fuseki and oxigraph differ on text search (jena-text), federation, function libraries, and (historically) corner-case SPARQL semantics. If templates or the gateway quietly use a Fuseki-ism, the dev tier stops being a truth-preserving mirror. *Mitigation: the pinned common profile (§9) + dual-backend execution of the entire golden verified-answer set in CI; jena-text off by default.*
- **R7 — Fuseki operational burden (ops).** A JVM MRSW store is a different ops animal from the rest of the stack: heap sizing, single-writer contention under the lift's batch writes, TDB2 backup discipline, air-gap image size (NE-086). *Mitigation: the lifted MRSW-retry knowledge is precisely this; pilot-profile defaults documented in the runbook; lift batches serialized through one writer queue.*
- **R8 — Replay expectation management (commercial/compliance).** "Every answer is replayable" must be sold with the §11.5 precision (procedural identity always; bit-identity when touched graphs are unchanged, provable from the audit trail) — regulated buyers will probe this exact seam. *Mitigation: the replay tool reports its achieved level; the security dossier (NE-101 lineage) states the guarantee verbatim.*
- **R9 — Upstream drift between teardown and spike (supply chain).** ABI ships daily (teardown line 131); its HEAD at spike time may have materially changed the lift targets (better — e.g. real pySHACL, trigger 2; or worse). *Mitigation: that is exactly what the mandated re-teardown is for; the reviewed `4b0d12f3` state is the always-available fallback; never track upstream after the pin (teardown line 168).*

---

*Producer spec ends. Build order: NE-085-spike → NE-085a → {NE-085b, NE-085c} (b∥c after a, with 085b's lift module gated on NE-081/082). The orchestrator owns sequencing per SPEC §6.*
