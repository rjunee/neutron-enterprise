# ne-brain-ontology

Brain Tier-3 — the ontology knowledge tier (BFO-2020 + CCO v2.1, SHACL
validation, verified answers). Producer spec: `docs/architecture/brain-tier3-spec.md`.

## Status: NE-085a — triple store + SHACL gateway + LinkML→SHACL + conformance

Builds on the merged NE-085-spike (offline-vendored BFO/CCO corpus, the oxigraph
walking skeleton, `PyShaclValidator`, the ABI vendor manifest). NE-085a adds the
load-bearing T3 infrastructure. Verified-answers (C6 tools) + the property-graph
→RDF lift are NE-085b/c.

### The store: two backends, one gateway

Everything writes THROUGH the SHACL gateway — the raw backends are package-private
(never exported, never a bare entry point).

- **`OxigraphBackend`** (`store/oxigraph.py`) — embedded `pyoxigraph.Store`, the
  no-docker dev/test tier. T3 gets a *real* backend in every unit-test run.
- **`FusekiBackend`** (`store/fuseki.py`) — an async httpx client for a Fuseki/TDB2
  DATASET endpoint over the SPARQL 1.1 Query + Graph Store Protocol. De-naased from
  ABI's `ApacheJenaTDB2.py`: the MRSW single-writer retry knowledge (transient
  500/503 → exponential backoff + jitter; 4xx non-retryable) is cribbed, the
  adapter written fresh. **Fuseki itself is not shipped** — it is an out-of-process
  service (current upstream 6.1.0, Java 21).
- Both satisfy the package-local `TripleStoreBackend` Protocol (`store/protocols.py`).

Backend selection is config-driven: `OntologySettings.backend = "oxigraph" | "fuseki"`.

```python
from ne.brain_ontology import build_oxigraph_store, build_fuseki_store, OntologySettings
store = build_oxigraph_store(OntologySettings())            # dev
store = build_fuseki_store(OntologySettings(fuseki_url=...))  # prod
```

### The `ValidatingTripleStore` write gateway (fail-closed)

`gateway.py` — every `insert_triples` is SHACL-validated (delta + one-hop CBD
context) BEFORE it reaches the backend:

- **Conforms** → the whole batch is written into `settings.data_graph_iri`.
- **Does not conform** → the whole batch is QUARANTINED (no partial writes), a
  `ShapeViolation` event is emitted (`quarantined=True`, `config_hash` +
  `shapes_hash`-as-`schema_version`), and `TripleQuarantinedError` is raised — the
  data is NEVER written.
- **Validator error** → `ShapeValidationError` (a denial; nothing written, nothing
  quarantined).

`sparql()` is the steward/audit read surface (SELECT/ASK only) with a deterministic
`LOAD`/`SERVICE`/`INSERT`/`DELETE`/… refusal (C12/C2). Agents get verified-answer
templates (085c), never raw SPARQL.

**Typed-literal writes.** The write path carries each slot's datatype into the RDF
literal it validates and stores: a predicate→`sh:datatype` map is derived from the
SAME compiled per-write shapes that validation runs (`operational.shapes.ttl` +
curated per-write shapes), so a typed LinkML slot (`xsd:integer` `employee_count`,
`xsd:dateTime` `created_at`, `xsd:decimal`, `xsd:boolean`, …) is emitted as
`Literal(value, datatype=…)` and PASSES `sh:datatype` instead of being quarantined.
The map can never drift from what validation enforces. `xsd:string` slots are left
as plain literals (in RDF 1.1 a plain literal IS an `xsd:string`, so this is
byte-identical to storing them untyped) — and the IRI-vs-literal rule keeps its
precedence, so an IRI-shaped value handed to a datatyped slot becomes a `URIRef`
that correctly fails `sh:datatype` and is quarantined. If one predicate ever
carried conflicting datatypes across shapes it is excluded from the map (untyped
fallback → still fail-closed at validation); no such conflict exists today.

**Steward-surface IRIREF masking.** The deterministic keyword ban applies to SPARQL
SYNTAX only. Before scanning for the forbidden keywords, `<IRIREF>` content is masked
to the empty `<>` (angle brackets kept), so a legitimate `SELECT`/`ASK` whose IRI
contains a segment like `/service/` or `/load/`
(e.g. `<https://example.com/service/123>`) is ACCEPTED — its path is data, not a
SPARQL `SERVICE`/`LOAD` keyword. A real `SERVICE`/`LOAD`/`INSERT`/`DROP` keyword
sits OUTSIDE any IRIREF, so it is still seen and refused; the masking only removes
false positives and never weakens the ban.

**Quarantine / release.** Rejected batches land in a `TripleQuarantine` lane
(`SqliteTripleQuarantine`; Postgres swaps in at the Protocol seam). A steward can
`await store.release(quarantine_id)`: the stored delta is re-validated and, if it
now conforms, written and the row removed; still-invalid → re-raise with fresh
violations. `await store.validate_full_graph()` is the nightly hook that runs the
per-write **and** global (`sh:sparql`) shapes over the whole data graph.

### LinkML → SHACL codegen (one schema source)

`ne-schema` now emits a committed `operational.shapes.ttl` from the same LinkML
source that produces the Pydantic/JSON-Schema/entity-graph artifacts (additive —
the pre-existing artifacts are byte-unchanged). Generation is byte-deterministic:
linkml's `ShaclGenerator` output is canonicalized (sorted `sh:ignoredProperties`
lists, rdflib canonical blank-node labels, sorted N-Triples) so the freshness gate
holds. `shapes.load_compiled_shapes()` compiles the generated shapes + curated
shapes (`assets/shapes/`, `scope = per-write | global`) into one hashed graph.

### Hash-diff idempotent hot-reload

`store/sync.py::sync_ontologies` loads the vendored corpus, rewriting only the
graphs whose pinned sha256 changed (state lives in `urn:ne:ontology:sync-state`).
A double sync is a no-op; a changed pin swaps exactly the changed graphs. The
writer refuses any graph IRI outside `urn:ne:ontology:*`.

### Conformance kit + the Fuseki LIVE-PARKED lane

`tests/test_conformance_kit.py` is the contract-conformance suite for the
`triple_store` slot. The oxigraph lane runs in-process ALWAYS; the Fuseki lane is
**LIVE-PARKED** — it runs only when `NE_FUSEKI_URL` answers `ASK {}` within 2s
(a local server or a future CI service), otherwise it SKIPs as `LIVE-PARKED`. Run
the Fuseki lane locally with:

```
NE_FUSEKI_URL=http://localhost:3030/ne uv run pytest packages/ne-brain-ontology
```

The MRSW retry + GSP wiring is unit-tested with `httpx.MockTransport` (always,
no server).

### Package-data asset resolution (tracked Codex P2, closed)

The vendored corpus lives INSIDE the package (`ne/brain_ontology/assets/`) and
resolves via `importlib.resources` — never `__file__`-parents path math that
breaks under a wheel install (the cedar/acme/sidecar precedent). Proven by a
subprocess site-packages-simulation test (real `uv build --wheel` variant
LIVE-PARKABLE).

## Status: NE-085b — corpus+alignment wiring, BFO gate, property-graph→RDF lift

### Corpus + alignment startup wiring

The vendored BFO-2020 + CCO-2.1 corpus loads into the `ValidatingTripleStore` at
startup via the public `await store.sync_ontology_corpus()` composition-root hook
(hash-diff idempotent; a re-sync is a zero-write no-op; offline C2). Alongside the
11 CCO modules and BFO, NE-085b ships a **self-authored operational-alignment
ontology** (`assets/ontologies/ne/operational-alignment.ttl`, a new `[ne]` manifest
source): `neop:Account/Person/Document/Ticket/Order` are declared `owl:Class` and
subclassed under CCO (`Organization`, `Person`, and `Information Content Entity` as
the documented fallback where CCO-2.1 has no exact label), the nine `EDGE_DEFS`
slot predicates get `rdfs:domain`/`rdfs:range`, and `nea:IdentityAssertion` reifies
SAME_AS. This compensates for the `class_uri` curation `ne-schema` never received
(the sanctioned in-package alignment given the mayTouch fence) — the `neop:*` IRIs
**byte-match** the generated `operational.shapes.ttl` `sh:targetClass` IRIs. CCO
parent IRIs were EXTRACTED from the vendored TTLs by `rdfs:label`, never invented.
`store.ontology_version()` surfaces the §7.4 registry (source pins + per-file
sha256 + `shapes_hash`) for health checks / `config_hash` inputs.

### Governed BFO-conformance gate

`lint/gate.py::run_corpus_conformance()` runs the lifted `validate_bfo` static lint
over the FULL loaded corpus (BFO + CCO + alignment) + any `extra` graph, and
returns only the UNTRIAGED errors. The lifted rules encode ABI's CCO-2.0-era
expectations; against CCO-2.1 they surface three ERROR categories
(`BUCKET_MAPPING`, `INHERES_IN`, `WRONG_INVERSE`) that are **rule-vs-corpus skew**,
not operational defects — each is triaged in `TRIAGED_ERROR_ALLOWLIST` with a
written justification (the corpus is authoritative; the rule is stale). Any
untriaged ERROR fails the gate. `python -m ne.brain_ontology.lint` is the
bare-gate CI surface (exit 0 green / 1 untriaged); `ne ontology lint`
(NE-ONTOLOGY-LINT-CLI) is the operator verb — a documented alias of `ne
ontology conformance`. Non-vacuity: a planted
`owl:inverseOf`-itself violation (not naturally present in the corpus) turns the
gate red.

### The property-graph→RDF lift

`lift/graph_to_rdf.py::PropertyGraphLift` reads the Tier-2 AGE graph through an
**injected** `GraphScanSource` Protocol (`scan_nodes`/`scan_edges` — the
package-local twin of the T2 batch-read capability; `AgeGraphStore` and
`InMemoryGraphStore` both satisfy it structurally; NO `ne.brain_graph` import under
`src/`, enforced by the import lane) and mints RDF:

- **Deterministic IRIs** from the T2 UUIDv5 keys (`urn:ne:entity:<key>`,
  `urn:ne:sameas:<edge_key>`) — an **unchanged** re-lift is idempotent by RDF set
  semantics and citations join back to T2. A re-lift over **changed** T2 data must
  go through `reconcile()` (NE-085d, below), not the append `run()`.
- **Deny-by-default typing**: an `entity_class` with no LinkML mapping is not
  lifted (counted); a node property not on the entity's contract is skipped; an
  unknown edge kind is skipped. Untyped data has no business in the regulated tier.
- **SAME_AS reification**: emitted as a `nea:IdentityAssertion` (typed +
  `same_as_left`/`same_as_right`/`source_ref`), validated by the new curated
  per-write `identity_assertion.ttl` shape. `owl:sameAs` is deliberately NOT
  asserted — the closed generated shapes would quarantine it and no runtime
  reasoner consumes it.
- **Fail-closed gateway mediation**: every batch goes through
  `insert_triples_into` (the same validate-before-write path as `insert_triples`,
  refusing `urn:ne:ontology:*` graphs); a non-conforming batch is quarantined
  whole (its id in `LiftReport.quarantined`, valid siblings still land), a
  validator error is a denial. The CBD context for the lift is the FULL union (a
  lifted edge's endpoint typing lives in a different ACL graph and `sh:class` must
  see it) — validation is a platform concern, trimming applies to reads only.

#### ACL partitioning (C7 trim parity) and the under-return note

Each lifted triple is placed into per-ACL-label named graphs
(`urn:ne:graph:acl:<quoted-label>`): node triples into a graph per node ACL label;
edge / IdentityAssertion triples into the graphs for the **conjunction**
`edge.acl ∩ from.acl ∩ to.acl` (T2's hop rule). A principal read via
`store.sparql_scoped(query, dataset=graphs_for_labels(labels))` unions only the
graphs for the labels the principal holds, so a lifted triple is trimmed
identically to its T2 source — proven by the known-IRI probe test (a hidden
entity's exact IRI returns zero rows). `graphs_for_labels(())` raises (fail-closed:
never all-graphs). An element with no/empty ACL, or an edge with an empty
intersection, is not lifted (counted).

**Under-return note (for 085c's template layer).** Trim parity is EXACT for
entities but deliberately conservative for edges: the conjunction rule can only
under-return vs T2, never over-return (the sanctioned direction per T2's
`POST_FILTER`). A principal whose visibility is split across multiple labels on the
three edge elements may see fewer edges via the lift than via T2's per-element hop
check. `apply_merge` already writes SAME_AS acl as the endpoint intersection, so
SAME_AS is unaffected.

#### Codex P2 fixes: the `DeclaredLiteral` lift seam + full-graph coverage

**FIX-1 — the lift is slot-datatype-authoritative.** A slot typed as an
`xsd:string` **literal** whose value is IRI-shaped (`Document.uri =
"https://example.com/doc/1"`, `nea:source_ref`) used to be coerced into a `URIRef`
by the gateway's `_rdf_object` string-shape heuristic, failing `sh:datatype` and
quarantining the whole node — valid data silently dropped. The lift now emits a
frozen `DeclaredLiteral(lexical, datatype)` at the `insert_triples_into` seam for
every literal slot, derived (via `_slot_literal_datatypes()`) from the SAME compiled
per-write shapes the validator enforces, so lift typing can never drift from what
quarantines. A `DeclaredLiteral` is never subject to the IRI heuristic; the
heuristic is **retained unchanged** for plain-`str` objects (entity IRIs, and any
schema-signal-less writer via `insert_triples`, where an IRI-shaped value for a
datatyped slot still correctly quarantines).

**FIX-2 — full-graph validation sees the lifted graph.** `validate_full_graph()`
previously validated ONLY `settings.data_graph_iri`, so the nightly/global
conformance gate (promoted BFO-conformance gate + global curated shapes) was blind
to every triple the lift writes into per-ACL named graphs — a fail-open governance
hole. It now builds its dataset from `backend.graphs()` — the data graph **plus**
every non-`urn:ne:ontology:*` named graph (the schema graphs are excluded: they are
validated by the lint gate, and the global `sh:sparql` shapes must not fire on
corpus `rdfs:label`s). This is a **global governance read under platform identity**
by design (fail-closed gate coverage); it does not touch the C7 principal-trimmed
read path (`sparql_scoped`), which stays per-ACL.

#### Mutation-aware reconciliation (NE-085d)

The append `run()` path is **initial-load only**: it appends minted triples and
never removes an element's prior triples, so a re-lift over **mutated** T2 data was
unsafe in two empirically-reproduced ways — (1) a changed single-valued slot (an
`Account` name) left the stale value, so `sh:maxCount` quarantined the whole node
and the update never landed; (2) an ACL change (`alpha`→`beta`) left the old triples
in the vacated `alpha` graph, so a revoked principal could still read stale data (a
C7 over-return). `reconcile()` closes both:

- **Validate-then-atomic-swap, per per-ACL graph.** `reconcile()` recomputes the
  COMPLETE desired triple partition from a full T2 scan, then for every desired graph
  calls `ValidatingTripleStore.replace_acl_graph()`, which validates the graph's full
  recomputed content through the SAME fail-closed pySHACL validator and, only on
  conformance, atomically replaces the graph via the backend's whole-named-graph
  `replace_graph_turtle`. A changed slot → the rebuilt graph holds ONLY the new value
  (no `sh:maxCount` conflict); an ACL move → the entity lands in the new label's graph
  while the vacated `alpha` graph is rebuilt/**purged** without it (over-return
  closed). Fully-removed elements are purged; a graph whose last element left T2 is
  `delete_graph`-ed (`report.graphs_deleted`).
- **Per-graph quarantine granularity; prior content intact.** The swap is
  all-or-nothing per graph: a non-conforming recomputed graph is quarantined (its row
  carries the `graph_iri`) and its **PRIOR content is left readable** — validation
  runs BEFORE any destructive op, so there is no delete-then-validate hole (the 085a
  fail-closed invariant). This is coarser than `run()`'s per-element granularity by
  design (SPEC §11 constraint (a)); `run()` is retained for initial load so valid
  siblings still land there.
- **`run()` is now correct for a FRESH initial load (NE-085e), including
  SHACL-required-edge classes** (`Order.account`/`placed_by`, `Ticket.belongs_to`):
  each element validates together with a desired-union two-hop CBD context (the
  same cross-ACL-graph mechanism `reconcile()` uses), so a required edge that
  hasn't landed yet no longer quarantines its own node whole. `run()` is still
  append-only and NOT mutation-aware — use `reconcile()` for any re-run over
  **mutating** T2 data.
- **Unchanged-skip no-op.** A graph whose recomputed content equals its stored content
  (rdflib triple-set equality; the lift emits no blank nodes, so the comparison is
  exact) is skipped — a byte-identical re-lift performs **zero** `replace_graph_turtle`
  / `delete_graph` calls (`report.graphs_unchanged`).
- **Desired-union CBD context.** The per-graph validation payload draws its two-hop CBD
  context from the DESIRED union (never the stale store — pulling stale context is
  exactly what caused failure mode 1), so a cross-graph `sh:class` (an edge target's
  `rdf:type` in another per-ACL graph) is satisfied without any stale triple.
- **Release semantics for a reconcile quarantine.** `release()` re-inserts (append) and
  re-validates against the still-stale store, so a reconcile-quarantine row fail-closed
  re-quarantines — NOT a corruption path. Remediation is **fix-T2-and-re-run-reconcile**.
- **Cross-graph non-atomicity.** The swap is atomic PER graph (oxigraph `clear`+bulk
  load in-process; Fuseki HTTP PUT single transaction) but not across graphs; a crash
  mid-reconcile leaves each graph individually valid and a re-run converges. Nightly
  `validate_full_graph()` (FIX-2) is the global backstop.
- **Usage rule.** Schedule `reconcile()`, not `run()`, for any recurring/incremental
  lift. The recurring `python -m` entry is **deliberately not wired** here: composing a
  real `GraphScanSource` needs `ne.brain_graph`/platform wiring that the brain-ontology
  import lane (C3) forbids under `src/`. The capability (`reconcile()`) is the
  deliverable; scheduling it against live-mutating T2 data is the composition root's
  later wiring, and requires owner sign-off (SPEC §5 row).

### Deferred to later units / the deploy lane

- **Fuseki compose service** (a `brain-t3` profile) and the deploy/backup runbook —
  OUT of this unit's scope; the orchestrator riders it with the deploy lane.
- **`config_hash` composition-root wiring.** The gateway REQUIRES `config_hash` at
  construction and rides `shapes_hash` on `schema_version`; folding `shapes_hash`
  into the platform freeze-input set is an app-root/085c concern.
- **Verified-answers mode** (C6 tools, shipped in NE-085c) and the **CCO-3.0
  migration runbook** — NE-085c / orchestrator riders. The **`ne ontology lint`
  CLI verb** shipped in NE-ONTOLOGY-LINT-CLI as an alias of `ne ontology
  conformance`; the **nightly full-graph + lint CI workflow** was evaluated
  there and DECLINED — the gate already runs on every commit via the
  full-suite CI pass (`test_bfo_conformance_gate.py` + the CLI tests), and its
  only inputs (vendored corpus, triage allowlist) have no non-commit drift
  channel (C2 offline).

## NE-085c — Verified answers (curated SPARQL templates as governed C6 tools)

Verified answers expose CURATED, PARAMETERIZED SPARQL queries over the
SHACL-validated T3 graph as governed C6 tools. A "verified answer" is grounded in
the validated RDF graph, permission-trimmed (C7), citation-carrying, and executed
through the SAME `GovernedToolExecutor` every other tool uses (audited,
approval-tiered, guardrailed). The AI never composes a query — it can only select a
registered template and supply typed arguments.

### The three read surfaces (§11.1 — security core)

| Surface | Who | Path | Controls |
|---|---|---|---|
| `insert_triples*` (write) | the lift / curation | the SHACL gateway | fail-closed validation, quarantine |
| `sparql` (steward/audit) | stewards / replay | `ValidatingTripleStore.sparql` | SELECT/ASK only, keyword refusal |
| **verified-answer templates** | **agents** | **`GovernedToolExecutor.execute` ONLY** | **C6 full stack + C7 dataset scoping** |

**There is NO agent-facing raw SPARQL. Ever.** A test proves no tool exposing
free-form SPARQL exists, and that `ne.brain_ontology.verified` imports neither
`ne.runtime` nor `ne.govern` (the binding conversion lives in the ne-api seam,
`ne.api.verified_tools`, because the brain-ontology import lane forbids those
imports, C3).

### Template TOML format (`verified/<template_id>.toml`)

Templates are **per-customer config-bundle content (git)** — the package ships
none. The registry hash is a `config_hash` input, so adding/changing a template
visibly moves the platform fingerprint on every subsequent event.

```toml
template_id = "accounts-by-name"        # tool name → read.kg.accounts_by_name (tier 0)
version = 2                             # monotonic; bumps on ANY change
description = "Accounts by exact name."
sparql = """
SELECT ?account ?name WHERE {
  #BINDINGS#                            # trusted code replaces this with a VALUES block
  ?account a <https://neutron.enterprise/schema/operational/Account> .
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
  FILTER(?name = ?q_name)
}
"""

[args.q_name]                          # → a generated strict (extra='forbid') Pydantic model
type = "str"                           # str | int | bool | date | datetime | iri
pattern = "^[A-Za-z0-9_ ]{1,64}$"      # str/iri only
max_length = 64
# enum = ["open", "closed"]            # str only, mutually exclusive with pattern:
                                       # a CLOSED vocabulary, advertised as "enum" in
                                       # the generated tool schema the agent sees and
                                       # REFUSED fail-loud pre-gate when violated —
                                       # never executed into silent zero rows. The
                                       # refusal names the field + the permitted
                                       # values (already-published schema) and never
                                       # echoes the rejected value (C12). A vocabulary
                                       # that is itself sensitive must NOT be an enum:
                                       # declaring one IS publishing it to the agent.

[scope]                                # C7 declaration
graph_classes = ["alpha", "beta"]      # ACL-label graph families the template may read
result_shape = "enumerable"            # aggregate is REJECTED at load in v1 (see below)

[answer]
columns = ["account", "name"]
citation_var = "account"               # per-row provenance IRI — must be a projected column
```

Every validation is done at **registry LOAD** (boot fails, never per-request): the
SPARQL parses, is SELECT-only, names no banned keyword
(`LOAD`/`SERVICE`/`INSERT`/`DELETE`/… plus `FROM`/`GRAPH`/`LIMIT`/`OFFSET` — trusted
code owns dataset scoping and the row cap — plus `UNION`/`MINUS`/`EXISTS`, and no
nested subquery), carries exactly one `#BINDINGS#` marker, references every declared
arg as `?var`, projects every declared column, binds every answer column via a real
**triple pattern** (a column reachable only through `VALUES`/`BIND`/a SELECT
expression fails load), and never projects a declared arg as an answer column unless
it is a graph-joined `iri` (arg-echo corpus poisoning, grounded-agent design
§2.7-F1 — `entity-by-iri` is the sanctioned shape). The `UNION`/`MINUS`/`EXISTS`/
subquery ban keeps the triple-pattern check sound: without it, a multi-branch body
could graph-join a column on one path while a `BIND` fabricates it on another,
making a single global "is this variable ever a triple term" check insufficient.

**Injection-proofing (C12):** argument values are NEVER string-interpolated. They
bind via `VALUES` + rdflib `.n3()` serialization, so a malicious value can only ever
be an RDF literal, never syntax; the final rendered query is re-parsed before
execution (fail closed).

**C7 permission trimming:** the query text carries no `FROM`; trusted code builds
the `DatasetSpec` from `graphs_for_labels(principal labels ∩ template graph_classes)`
— fail-closed (empty ⇒ error, never the full union; expander errors propagate). A
provenance double-check confirms every cited IRI is inside the principal's graph set.

### Documented divergences from brain-tier3-spec §11.2

- **TOML, not YAML** — honors "C4 no new deps" (`tomllib` is stdlib; `pyyaml` is
  deliberately not added even though it is in the lock).
- **All args required in v1** — optional filtering is a second template.
- **SELECT-only** — `ASK`/`CONSTRUCT`/update forms are rejected at load.
- **`result_shape: aggregate` is REJECTED at load** (stricter than §11.4's
  `acl_complete` pairing) — an aggregate over a permission-trimmed view leaks
  existence of invisible data (R3); it ships as enumerable or not at all. Re-enabling
  is an owner/product call, not silent scope creep.
- **The `LIMIT` is injected by trusted code**, never by the template author.

### Replay guarantee (§11.5)

`VerifiedAnswer` + the audit detail carry `template@version` / args / `config_hash` /
`ontology_version`. A replay is **procedurally identical**; the structured result is
**bit-identical only when the touched graphs are unchanged** (an append to a read
graph legitimately changes the rows). No temporal-query claim is made (NE-083's
lane); the `ne verify-answer` CLI is out of this unit — the replay primitive ships as
library functions (`answers_equal`, `render_answer_text`) + tests.

### Governed HITL template store (NE-ONTOLOGY-FLYWHEEL)

The boot template directory (`NE_ONTOLOGY_VERIFIED_TEMPLATES_DIR`) is **read-only** config
loaded and fingerprinted at deploy. NE-ONTOLOGY-FLYWHEEL adds a *governed*
continuous-improvement path so a steward can add/modify a verified-answer template
**without a redeploy**:

1. **Propose** — a steward POSTs a template TOML; it is validated at propose-time by
   the SAME `QueryTemplate` load gate (SELECT-only, banned-keyword, typed args,
   citation projection — fail-closed, so an invalid template never becomes a
   proposal) and rides the existing NE-066 review gate as a
   `verified_answer_candidate` proposal on the `ontology` lane.
2. **Approve / reject** — through the untouched NE-066 `ProposalReviewService` state
   machine (row-locked transitions).
3. **Apply** — on steward apply, the template is persisted to the append-only,
   `config_hash`-tracked `ne_verified_templates` store (`verified/store.py`) in ONE
   transaction with the audit dual-append (chain failure rolls the persist back).
   The effective registry re-fingerprints via
   `load_template_registry_with_overlay(boot_dir, stored)` (highest version wins
   across boot ∪ store; a stored `(id,version)` diverging from a boot pair fails
   loud) and the **projected** platform `config_hash` delta is chain-audited.

**Restart-to-activate (design honesty statement).** Activation is
restart-to-activate, **not** in-process hot-reload. `config_hash` is process-frozen
by architectural decision (NE-CKC5: a re-freeze to a different hash raises), and the
`GovernedToolExecutor` binding set + the guardrail `ToolArgPolicy` map are frozen at
boot inside the C6/C12 chokepoints. So apply = **governed persist + audited
`config_hash`-delta projection**; the next process restart — an operator
`compose restart`, **not** a redeploy (no config bundle, no image, no deploy
pipeline) — re-freezes to EXACTLY the audited projected hash and the approved
template becomes a live `read.kg.*` governed tool. The re-fingerprint provability
test proves this equality (audited `new_config_hash` == a fresh boot's frozen hash).

**Store persistence caveat.** The overlay is read from the flywheel signal database.
Production is Postgres and tests use file-backed SQLite, both of which persist across
the short-lived boot read; an in-memory `sqlite://` URL would NOT retain governed
templates across a restart.

The apply **orchestration** (the flywheel gate + the audit chain) lives in
`ne-api` (`verified_curation.py`), not here, because the `.importlinter`
brain-ontology-lane contract forbids `ne.brain_ontology` importing
`ne.flywheel`/`ne.audit` — `store.py` imports only sqlalchemy/pydantic/stdlib.

### Read-path auditing (NE-164b)

`execute_template` (the T3 verified-answer read core) optionally emits ONE
config_hash-stamped `knowledge.verified_read` `AuditEvent` per COMPLETED
execution, extending the T1/T2 `knowledge.retrieve`/`knowledge.graph_read`
pattern (NE-164/NE-164b) to the ontology tier over the same frozen `AuditSink`
seam. Auditing is OFF by default — additive `audit_sink: AuditSink | None =
None` and `trace_id: str | None = None` keyword-only params (`None` sink means
no event machinery runs at all; every pre-NE-164b composition is
byte-identical).

**Event shape** — `action="knowledge.verified_read"`,
`resource="knowledge://verified"`, `outcome=success`, `actor=principal.id`,
and a deterministic `detail` grammar:

```
rows=<n> row_limit=<l> truncated=<true|false>
```

**No `acl_trim_delta` field (by design)** — unlike the T1/T2 read-path events,
this one carries no trim delta. The C7 trim happens A-PRIORI here: the
`DatasetSpec` graph scoping (`scoped_dataset`) means the rendered SPARQL query
can never see a forbidden graph in the first place, so there is no countable
trimmed-row delta to report. Fabricating one would misrepresent how the
trimming actually works — the orchestrator-sanctioned no-delta case.

**C7/C12 counts-only rule** — the detail carries row/limit/truncation COUNTS
only — never template args, row VALUES, citation IRIs, or `template_id` (the
template identity already rides the `GovernedToolExecutor`'s `tool.execute`
event via `extract_audit_detail` at the ne-api seam; duplicating it here would
be a redundant C7/C12 surface). The leak-assertion test in
`test_verified_read_audit.py` proves this over the full event payload.

**Fail-closed sink-append** — when an `audit_sink` IS injected, a sink
`append` failure (or `ConfigHashNotFrozenError`) PROPAGATES — surfacing at the
ne-api route as a `ToolExecutionError`/502, never a silently-unaudited read
(NE-CKD6 discipline). A FAILED execution (args validation, scope denial, or
the provenance double-check) emits NOTHING: the refusal/exception precedes the
audit decision, exactly like NE-164's fail-closed branches.

**trace_id-as-data threading** — this package gains NO `ne.runtime` import
anywhere (the brain-ontology import lane, C3; guarded by
`test_verified_no_ungoverned_path.py`). `build_verified_answer_tools` takes an
additive `trace_id_provider: Callable[[], str | None] | None = None` — the
TURN_PRINCIPAL `principal_provider` pattern applied to the NE-166 run-trace
ContextVar. `ne-api`'s `verified_tools.py` binds it to `current_run_trace_id`
(evaluated inside the `/v1/verified/invoke` request context) because
`GovernedToolExecutor.execute` has no `trace_id` parameter — the provider is
the sanctioned data-only equivalent.

### NE-CTXB-7 — Layer-3 COUNT aggregates (count-only v1)

A `[answer] kind = "count"` template is a **server-side verified COUNT**. The TOML
contract adds `group_by = [<triple-bound vars>]`, `count_over = <triple-bound var>`,
`as = <col name>` (and the citation var **must** be a `group_by` key). An explicit
`columns` key is **refused** — the columns are DERIVED as `[*group_by, <as>]`:

```toml
[answer]
kind = "count"
group_by = ["account", "name"]
count_over = "order"
as = "n_orders"
citation_var = "account"     # must be a group_by key (the per-group citation IRI)
count_total_rows = true      # opt-in total_rows (below)
```

The template **body stays a plain single-path conjunctive SELECT** — every existing
ban (UNION / MINUS / EXISTS / subquery / LIMIT / OFFSET / FROM / GRAPH / ORDER-BY-in-body)
still fires, so `_triple_pattern_vars`' soundness is untouched. TRUSTED code renders the
projection: `SELECT ?account ?name (COUNT(DISTINCT ?order) AS ?n_orders) … GROUP BY
?account ?name` + the trusted ORDER BY (citation first) + trusted LIMIT (which bounds
GROUP rows for a count template). `COUNT(DISTINCT …)` is **mandatory**: the C7 multi-graph
union has **bag semantics** on oxigraph — a 2-order account visible via 2 ACL graphs was
spike-verified to count as 8 with a plain `COUNT(?order)`; `DISTINCT` recovers the true
per-group cardinality.

The GUARD-2 projection-hygiene proof is **relaxed for the single declared count column
only** (a trusted scalar the renderer computes), and that relaxation is **hard-coupled in
code** to the `UNION`/`MINUS`/`EXISTS` bans: if those are ever weakened the count path
refuses to load (guardian test). Per-group citation = the group-key entity IRI (a real
graph subject), so `check_citations_in_dataset` works unchanged and the C7 double-check
stays on the aggregate path.

**`total_rows` (opt-in via `count_total_rows`, either kind):** under truncation ONLY, a
trusted `COUNT(*)` rewrite over the IDENTICAL body + the SAME C7 dataset fills
`total_rows` — the degenerate case of the same machinery (rows kind → bag-solution total;
count kind → number of DISTINCT groups). Semantics are **AS-OF-COUNT-TIME**: a second,
unsnapshotted read, so on a live store the rows and the total may reflect different
snapshots (exact on fixtures; no test/arc asserts rows-vs-total equality live). It runs
under a **dedicated `count_timeout_s` read timeout** (≤2s p95 budget, §5.3, measured on
the rig in NE-CTXB-8 — not here); on timeout `total_rows` stays `None` (an honest unknown,
never fabricated), and a malformed probe result raises `VerifiedAnswerCountError`. The
opt-in is **per-template only** — a deployment-setting override is deferred to NE-CTXB-8.

**DECLARED trust downgrade (R4-P0-2):** aggregate cells and `total_rows` are
trusted-COMPUTED scalars. The gate grounds them by CONTAINMENT and
`check_citations_in_dataset` cannot ARITHMETICALLY validate them — a downgrade from
per-cell graph provenance to replay-determinism + test-anchored correctness. The
**recount-equivalence + mutation tests + C7 traps + replay pin ARE the correctness
anchor** (`test_verified_count_execution.py`).

**`SUM` / `AVG` / `MIN` / `MAX` are explicitly deferred** (a follow-up unit) — count-only v1.

## Deterministic verified routing (verified-answer-first Layer 1, NE-VAF-U1)

Layer 1 of verified-answer-first
routes a raw user prompt to a zero-arg verified-answer template with **no model
call** — a load-validated `[routing]` TOML block plus a PURE, deterministic
matcher (`ne.brain_ontology.verified.routing`, stdlib-only: no I/O, no clock, no
randomness). The STATEFUL router that calls into this matcher, applies the
clarify-turn bound, and falls back to the model lives in `ne.runtime` (U3) —
this package only supplies the data + the pure functions.

### The `[routing]` TOML block

```toml
[routing]
domain = "accounts-directory"
triggers = ["list every account i am cleared to see", "list the accounts i can see"]
guard_terms = ["accounts directory", "cleared to see"]
clarify_max = 2                        # default 2; how many CLARIFY turns before U3 REFUSEs
```

- **Zero-arg gate** — a template declaring `[routing]` with non-empty `args` FAILS
  TO LOAD: v1 deterministic routing never extracts an argument from prose (no
  principal-context arg source exists in `ArgDef` today), so only zero-arg
  templates may route.
- **`triggers`/`guard_terms` must be non-empty**, and every `guard_term` must be
  **multi-word** (normalizes to 2+ tokens) — a single-token guard like `"account"`
  is a false-positive machine and is refused at load.
- **Fail-loud on an old binary** — `RoutingSpec`/`QueryTemplate` keep
  `extra="forbid"`, so a `[routing]` block with an unrecognized key, or a
  `[routing]` block fed to code built before this field existed, fails at LOAD —
  it is never silently ignored.
- **`registry_hash` semantics unchanged** — the hash covers raw template file
  bytes (`templates.py`'s `load_template_registry`, untouched by this unit).
  Adding `[routing]` to a template file legitimately MOVES `registry_hash`
  (an audited config change); a template with no `[routing]` block hashes
  byte-identically to before this unit shipped.

### The pinned matcher — precision over recall

A **mis-route is worse than a refusal**. `match_template(prompt, rows)` returns
one of `ROUTE(template_id)` / `CLARIFY(domain[, domains])` / `PASS` (`REFUSE` is
enum-only in this package — the stateful clarify-bound-exhausted transition is
owned by U3's router):

1. **Triggers** are matched as an ORDER-SENSITIVE token subsequence (gaps
   allowed, order preserved) against a pinned normalization (`normalize`: NFKC
   → casefold → delete apostrophes → punctuation-to-space → whitespace split,
   keeping stopwords) — a single unambiguous trigger hit ROUTEs.
2. **Negation veto** — a negation token (`don't`/`dont`/`not`/`no`/`never`/
   `without`, post-normalization) preceding a matched span — trigger OR guard —
   vetoes the match to `PASS`.
3. **Compound veto** — a co-joined second imperative (`" and <verb>"` or
   `" then "`) downgrades a would-be ROUTE to `CLARIFY` so the dropped clause
   surfaces, never silently executing only the first half.
4. **Guard terms** are matched as a CONTIGUOUS phrase (stricter than the
   trigger subsequence); a guard hit with no trigger hit is a `CLARIFY`, never
   a guess.
5. **Multi-domain tie-break** — 2+ domains hitting guards (or triggers) is a
   deterministic, sorted-by-domain `CLARIFY` listing every domain — never a
   silent pick.

`match_selection(reply, domain_options)` resolves the clarify-turn follow-up
(`"1"`/`"(1)"`/`"option 1"`/`"#1"`/`"a"`/`"(a)"`, or an unambiguous echo of an
option's canonical trigger) to a `template_id`, because a natural reply like
`"1"` carries no trigger tokens and would otherwise fall through to the model.
An ambiguous or unrecognized reply returns `None` — the same precision-over-
recall bias.

### Consuming the routing table with no cross-package import

`build_routing_table(registry)` projects every routed template into a frozen,
`template_id`-sorted tuple of `RoutingRow` — **strings/`tuple[str, ...]`/`int`
only, no ontology objects**. `ne.runtime` (U3) declares a structural `Protocol`
over just the fields it needs and consumes this table with **no import of
`ne.brain_ontology` at all** — the same inversion precedent as the
`GroupExpander`/`principal_provider` injection seams used elsewhere in this
package.

## NE-GA-G5 — grounded-agent settings (`OntologySettings`, §2.6)

Three opt-in flags turn the grounded agent ON end-to-end (composed at the
`ne-api`/`ne-worker` root, NE-GA-G5 — this
package only carries the settings, not the wiring):

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `grounded_agent_enabled` | `NE_ONTOLOGY_GROUNDED_AGENT_ENABLED` | `False` | Turns the grounded agent on. Requires verified answers enabled — a grounded-on/verified-off boot is a fail-loud `ValueError`, never a silently-ungrounded agent. |
| `grounded_agent_name` | `NE_ONTOLOGY_GROUNDED_AGENT_NAME` | `"grounded-analyst"` | The ONE chat `agent_name` that gets the grounding post-step + verified-reads-only toolset restriction. Every other agent is untouched. |
| `grounded_assist_enabled` | `NE_ONTOLOGY_GROUNDED_ASSIST_ENABLED` | `True` | The G4 bounded one-pass model assist (a bare, no-tool-catalog gateway completion). `False` leaves a pure hard-gate. |

These three fields live in `OntologySettings` — never `RuntimeSettings`/
`GovernSettings` — because `OntologySettings` is stamped into `config_hash`
**only when the T3 read surface is on**. That keeps a default-off deployment's
`config_hash` byte-identical, and a grounded-on deployment's hash moves exactly
when its behavior does (a T3-on, grounded-off deployment does take a one-time
hash move on upgrade, since the stamped model gained fields — a real, audited
config-surface change, not a regression).
