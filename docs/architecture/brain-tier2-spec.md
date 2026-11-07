# Brain Tier-2 (Entity Graph) — Producer Specification (build-ready)

**Status:** SPEC for the unbuilt Tier-2 knowledge tier — NE-081 ("AGE store
adapter + structured-first graph construction + LinkML entity contracts") and NE-082
("Splink entity resolution + LightRAG integration + graph-aware citations").
The target package `packages/ne-brain-graph` exists as a stub ("Status: stub — implemented
by NE-081", `packages/ne-brain-graph/README.md:5`; module `ne.brain_graph`,
`packages/ne-brain-graph/pyproject.toml:14`) and is already registered in
`.importlinter:7` and both C3 chokepoint contracts (`.importlinter:38`, `.importlinter:80`).

**Date:** 2026-07-09 · **Author:** Fable specification architect pass ·
**Component licenses/versions re-verified 2026-07-09** (§8; every unit
re-verifies at build time regardless).

**What Tier-2 is:** a property graph on Apache AGE (the customer's
Postgres), built **structured-first** from source systems (accounts → contacts → orders
→ tickets), Splink entity resolution, LightRAG-style graph-aware retrieval, with the
LLM allowed only to *link* mentions to existing entities — new entities and merges are
review-gated. It exists so multi-hop questions work ("which customers are affected by
the change to product X via contract clause Y") without abandoning any Tier-1 guarantee.

**Security framing (the non-negotiable core):** Tier-2 adds a second principal-scoped
read plane. It MUST inherit the exact C7 discipline Tier-1 proved out
(`packages/ne-brain/src/ne/brain/trimming.py`): every graph read takes a `principal`,
is trimmed through the SAME label algebra (`acl_match_labels`), fails closed
(never-None, never-empty, deny-by-erroring), and **no untrimmed traversal path exists**
— a user can never traverse to, through, or via an entity or edge they could not see
at the source. Entity resolution NEVER auto-merges: statistical candidates become
steward proposals through the existing NE-066 apply-gate
(`packages/ne-flywheel/src/ne/flywheel/review.py:246`). Tier-1 is frozen: Tier-2 is
purely additive ("tier upgrades are additive deployments, not migrations").

---

## 0. Ground truth this spec builds on (all verified in-tree)

| Seam | Where | Fact Tier-2 must respect |
|---|---|---|
| C7 label algebra | `packages/ne-brain/src/ne/brain/trimming.py:67-89` (`acl_match_labels`), `:50-64` (`GroupExpander` Protocol), `:92-107` (`PrincipalAclFilter`) | Label set ALWAYS contains `principal.id`, is never empty, is deterministic (sorted); expander exception PROPAGATES (deny-by-erroring, trimming.py:16-18); production hook never returns `None`/`{}` |
| Group expansion | `packages/ne-permissions/src/ne/permissions/expand.py:54` (`PrincipalExpander.expand(principal) -> frozenset[str]`), `:97-100` (`TENANT_ALL`/`PUBLIC_LINK` only via `is_known` gate) | SYNC, pure indexed reads; unknown principal expands to exactly itself + verified token groups; revocation lag = sync interval (`ne-permissions/config.py:34-36`, 900 s default) |
| Reserved labels | `packages/ne-permissions/src/ne/permissions/models.py:33` (`TENANT_ALL = "acl:tenant-all"`), `:40` (`PUBLIC_LINK = "acl:public-link"`) | Same vocabulary is stamped by every connector (e.g. `ne-connect/src/ne/connect/m365/acl.py:41-42`); source-data forgery of `acl:*` labels quarantines (NE-CKD2 anti-forgery, m365/acl.py:73) |
| Trimmed store filter | `packages/ne-brain/src/ne/brain/qdrant.py:293-316` (`permission_filter`), `:54` (`_ACL_FIELD = "acl"`), `:124-128` (keyword index) | Chunk with EMPTY or MISSING `acl` matches NOTHING — the never-match-all direction. Graph nodes/edges get the identical semantic |
| Trimmed read paths | `packages/ne-brain/src/ne/brain/retriever.py:171` (`acl_filters` hook), `:192-206` (search), `:212-243` (retrieve, fail-closed `NotImplementedError` on unfilterable fetch) | Both `search` and `retrieve` route the principal through ONE hook; `{}` = match-nothing short-circuit before any store call |
| C7 guardian pattern | `packages/ne-brain/tests/test_c7_trimming_guardian.py:49` (`REGISTERED_PRINCIPAL_READ_PATHS`, AST sweep + behavioral no-bypass + production-hook invariant); D2 suite `tests/test_c7_provable_trimming.py` | Tier-2 ships the same three-layer guardian for `ne.brain_graph` (§6.4) |
| Frozen graph contract | `packages/ne-contracts/src/ne/contracts/knowledge.py:152-161` — **`GraphStore` already exists**: `upsert_nodes(nodes: Sequence[Mapping])`, `upsert_edges(edges: Sequence[Mapping])`, `cypher(query, params) -> Sequence[Mapping]` | Shipped at the v1.0.0 freeze precisely so T2 needs no contract bump for the store slot (knowledge.py:5-8). It is **unscoped-by-design** — treated exactly like `ChunkStore.get` (knowledge.py:94-100): composition-root-confined, never caller-facing |
| Citation shape | `packages/ne-contracts/src/ne/contracts/types.py:64-70` (`Citation`: `chunk_id`, `document_id`, `source_system`, `source_ref` — all required, frozen), `:73-77` (`RetrievalResult`: `chunks`, `citations`) | Graph-aware citations are an ADDITIVE contracts change (ADR + minor bump, §5.3), following the ADR-0003/ADR-0004 precedent (`packages/ne-contracts/adr/`) |
| Contract freeze | `packages/ne-contracts/src/ne/contracts/__init__.py:5-20` (additive-only + ADR + version bump; currently 1.2.0 at `:24`) | The ONLY ne-contracts edit in this whole tier is NE-082's citation types (§5.3) |
| Connector record | `packages/ne-contracts/src/ne/contracts/integration.py:34-39` (`Record`: `record_id`, `payload: AnyMap`, `source_id`), `:57-66` (`Connector` Protocol: `discover`/`backfill`/`incremental`/`permissions`), `:42-46` (`AclEntry`) | Structured-first construction consumes THIS protocol — the raw `payload` row, not chunk text |
| ACL resolution | `packages/ne-connect/src/ne/connect/acl.py:22-38` (`build_acl_index`), `:47-70` (`AclMapper.resolve` — fail-closed, never returns empty, raises `AclResolutionError`) | Graph construction reuses `AclMapper` verbatim: node/edge ACLs come from the SAME resolver that stamps chunk ACLs, so graph and chunk visibility can never diverge at the source |
| T1 ingest seam | `packages/ne-connect/src/ne/connect/bridge.py:121-238` (`SyncRunner`), `:240-243` (`_flush` → `KnowledgeWrite.upsert`), `:81-83` (deterministic `item_id = sha256(source\nsource_id\nrecord_id)`) | Tier-2 does NOT tap or wrap this path (§4.2 explains why); it is byte-untouched |
| Ingest durability precedent | `packages/ne-brain/src/ne/brain/ingest/spine.py:38-47` (upsert-new → delete-stale → ledger ordering), `:66-89` (ACL-staleness 3-way short-circuit) | Graph re-sync copies this ordering and the stale-ACL healing discipline |
| Review gate | `packages/ne-flywheel/src/ne/flywheel/review.py:246` (`ProposalReviewService`), `:277` (`approve`), `:302-325` (`record_apply` — RECORDS a human apply, never performs it), `:135-142` (transition table; `proposed→applied` illegal), `:17-22` (v1 = proposals + tracked manual apply) | Splink candidates ride THIS gate as a new lane/kind (§5.2); the gate is kind-agnostic (`review.py:365`) |
| Settings/config-hash | `packages/ne-contracts/src/ne/contracts/config.py:50` (`NeSettings`), `:77-82` (`env_prefix="NE_"`, `__` nesting, TOML, `extra="forbid"`), `:163` (`compute_platform_config_hash`), `:238` (`freeze_platform_config_hash`) | `BrainGraphSettings` follows the `PermissionsSettings` house pattern (`ne-permissions/config.py:21`) and joins the platform hash under module key `"brain_graph"` |
| LinkML source | `packages/ne-schema/src/ne/schema/schemas/operational.yaml` — Account:66, Person:91, Document:127, Ticket:164, Order:208; relationship slots are `inlined: false` by-id refs (`works_at`:117, `author`:148, `owned_by`:152, `about`:156, `belongs_to`:184, `requester`:189, `assignee`:193, `account`:219, `placed_by`:224) — modeled explicitly so they "become a typed relation at Tier 2" (operational.yaml:10-13) | NE-081's entity contracts generate from THIS one source ("one schema source generates JSON Schema + Pydantic models (T1 metadata/contracts), typed entity contracts (T2), and SHACL (T3)") |
| Codegen extension point | `packages/ne-schema/src/ne/schema/codegen.py:35-66` (`SchemaSpec`/`SCHEMAS`/`_artifacts` — currently 3 artifacts per schema), in-venv generator `packages/ne-schema/codegen/generate.py:67-100`, freshness gate `codegen.py:153` | Adding the graph target = a 4th artifact type flowing through the existing byte-compare CI gate |

Also load-bearing context: the platform composition root (`ne-api`) is not yet built —
`PrincipalAclFilter` is instantiated nowhere in non-test src (see
`docs/architecture/ne-api-service-spec.md`).
Tier-2 therefore ships **library + conformance surface + CLI**, and documents its
composition recipe for `ne-api`/`ne-ingest` exactly as Tier-1 modules do.

---

## 1. The Tier-2 Protocol surface

### 1.1 Two-layer design (mirrors ChunkStore / HybridRetriever exactly)

Tier-1's proven shape: a **frozen, unscoped store protocol** (`ChunkStore` —
knowledge.py:89-100; `get` is "UNSCOPED, NOT principal-trimmed" by design) beneath a
**principal-scoped read layer** (`HybridRetriever`, retriever.py:163) that is the only
caller-facing surface and routes every read through the ACL hook. Tier-2 replicates it:

```
frozen ne.contracts.knowledge.GraphStore        (exists, knowledge.py:152-161)
        ▲ implemented by
ne.brain_graph.age.AgeGraphStore                (NE-081; Neo4j alternate = same contract)
        ▲ consumed ONLY by
ne.brain_graph.read.EntityGraph                 (NE-081; the ONLY principal-scoped surface)
        ▲ consumed by
ne.brain_graph.retrieval.GraphRetriever         (NE-082; KnowledgeRead-shaped, graph-aware)
```

`GraphStore.cypher(query, params)` is raw and unscoped — the graph twin of
`ChunkStore.get`. It is **composition-root/adapter-confined**: no module outside
`ne.brain_graph.age` (and the construction/ER internals in §4/§5) may call it, enforced
by the Tier-2 guardian (§6.4 layer d) plus an import-linter contract (§7.3). Callers —
`ne-api`, agents via tools, NE-085's RDF lift — get `EntityGraph` and nothing rawer.

### 1.2 `EntityGraph` — the principal-scoped query surface (package-local, NOT in frozen ne-contracts)

Following the established package-local-Protocol pattern (`GroupExpander` in
trimming.py:50-64; `FilteredChunkFetch` in retriever.py:77-87; `QuerySparseEncoder`
retriever.py:59), the read surface is a concrete class in `ne.brain_graph.read` with a
package-local Protocol for swap tests. It does NOT go into frozen ne-contracts in v1.1
(no ADR needed; promotion to a contract is a v1.2 option once a second graph read impl
exists — C5 requires the conformance kit either way).

```python
# ne/brain_graph/read.py  (signatures normative)

class EntityGraph:
    """Principal-scoped, fail-closed entity-graph reads (C7).

    EVERY method takes `principal` and trims through the SAME label algebra as
    Tier-1 chunk reads (ne.brain.trimming.acl_match_labels). There is no
    untrimmed variant, no `principal=None` escape, no raw-cypher passthrough.
    """

    def __init__(
        self,
        store: GraphStore,                     # the frozen contract, knowledge.py:152
        *,
        group_expander: GroupExpander | None,  # ne.brain.trimming.GroupExpander, trimming.py:50
    ) -> None: ...

    async def lookup(
        self, keys: Sequence[EntityKey] | None = None, *,
        principal: Principal,
        entity_class: str | None = None,       # LinkML class name, e.g. "Account"
        name_query: str | None = None,         # exact/prefix property match (NOT semantic — that is GraphRetriever's job)
        limit: int = 50,
    ) -> Sequence[EntityNode]: ...

    async def neighbors(
        self, keys: Sequence[EntityKey], *,
        principal: Principal,
        hops: int = 1,                         # 1 <= hops <= settings.max_hops (default cap 3)
        edge_kinds: Sequence[str] | None = None,
        direction: Literal["out", "in", "both"] = "both",
        limit: int = 200,
    ) -> Subgraph: ...

    async def subgraph(
        self, keys: Sequence[EntityKey], *,
        principal: Principal,
        hops: int = 2,
        edge_kinds: Sequence[str] | None = None,
        limit_nodes: int = 500, limit_edges: int = 2000,
    ) -> Subgraph: ...

    async def paths(
        self, from_key: EntityKey, to_key: EntityKey, *,
        principal: Principal,
        max_hops: int = 4,
        limit: int = 20,
    ) -> Sequence[GraphPathRecord]: ...        # feeds NE-082 graph-aware citations
```

`EntityKey`, `EntityNode`, `EntityEdge`, `Subgraph`, `GraphPathRecord` are frozen
pydantic models in `ne.brain_graph.model` (generated + hand napkin types, §4.1).
`EntityKey = (entity_class: str, key: str)` where `key` is the deterministic identity
of §4.3.

### 1.3 Trimming semantics (normative — the C7 core)

1. **Label computation is Tier-1's, verbatim.** Every `EntityGraph` method computes
   `labels = acl_match_labels(principal, self._group_expander)`
   (trimming.py:67-89). ne-brain-graph **imports** `ne.brain.trimming` — it does NOT
   re-implement the algebra (one algebra, zero drift; the dependency direction
   ne-brain-graph → ne-brain is clean: ne-brain never imports ne.brain_graph, and
   ne-connect → ne-brain already establishes the direction, trimming.py:25-28).
   All fail-closed properties are inherited: non-empty always (contains
   `principal.id`), deterministic order, expander exceptions propagate
   (deny-by-erroring), unknown principals collapse to self + verified token groups.

2. **Every node AND every edge on every traversal is filtered.** Visibility of a hop
   `(a)-[r]->(b)` requires the caller's label set to intersect `a.acl` AND `r.acl`
   AND `b.acl`. This is stricter than endpoint filtering and is deliberate: a path
   THROUGH an invisible node/edge must not be traversable even when both endpoints
   are visible — otherwise `paths()` leaks the existence of the restricted link.
   The trimming predicate is injected into the generated cypher for every node and
   relationship variable (§3.4); it is never applied as a client-side afterthought
   on `EntityGraph` paths.

3. **Empty/missing `acl` matches NOTHING** — identical to
   `qdrant.py:304-306` ("MatchAny over an empty/absent array field never matches").
   A node or edge written without an ACL is invisible to every principal, including
   admins. Construction makes this unrepresentable anyway (`AclMapper.resolve` never
   returns empty — acl.py:64-69 — and the AGE adapter rejects un-ACL'd upserts,
   §3.3), so the store-level semantic is defense in depth, exactly like the
   retriever's unreachable `{}` branch (retriever.py:23-28).

4. **No untrimmed read path exists in the package.** `GraphStore.cypher` is the only
   untrimmed primitive and it is confined (§1.1). The Tier-2 guardian test (§6.4)
   makes adding an untrimmed principal-scoped read a CI failure, byte-for-byte the
   `REGISTERED_PRINCIPAL_READ_PATHS` mechanism of test_c7_trimming_guardian.py:49.

5. **Trimming is evaluated BEFORE store work** where possible (the retriever
   precedent, retriever.py:186-194): compute labels first; a raised expander error
   means no query is issued.

### 1.4 Query grammar and the ACL predicate on AGE

The trimmed-cypher builder (`ne.brain_graph.age.query.TrimmedCypherBuilder`) is the
single place traversal cypher is generated. Normative properties:

- **Parameterized, never interpolated.** All values — including the ACL label list —
  travel via the `cypher()` params argument (AGE's third `cypher(graph, $$...$$, $params)`
  agtype argument through a psycopg prepared statement). String interpolation of any
  caller-influenced value into the cypher text is forbidden (injection surface) and
  the builder's unit tests assert generated text contains only whitelisted
  identifiers (labels/edge kinds validated against the generated LinkML enum, §4.1 —
  identifiers cannot be parameterized in cypher, so they are allowlist-validated
  instead).
- **The predicate** (primary form, per node/edge variable `x`):
  `any(l IN x.acl WHERE l IN $acl_labels)`.
  **Build-time verification required** (§9-R1): AGE's openCypher subset must be
  probed for `any()`/list-comprehension support at the pinned version; the fallback
  encoding is documented there. This is a named acceptance test, not an assumption.
- **Bounded traversal.** `hops` is clamped by `BrainGraphSettings.max_hops` (default
  cap 3, hard ceiling 6) and every query carries `LIMIT`; unbounded variable-length
  expansion (`[*]` with no upper bound) is forbidden in the builder.

### 1.5 Neo4j alternate (C5)

Neo4j CE is the named alternate and is **GPLv3 → out-of-process only**
(C4): a separate service reached over the Apache-2.0 `neo4j` Python driver.
The alternate implements the same frozen `GraphStore` + registers under the same plugin
slot (§7.2); the conformance kit (§6.1) plus the C7 guardian are the swap gate. v1.1
ships AGE only (C5 allows one production adapter per slot; the kit is what guarantees
swappability). The one dialect hazard — AGE's cypher-in-SQL wrapping vs
Bolt — is fully contained inside the adapters because callers never write cypher (§1.1).

---

## 2. What NE-081 lands (store + construction + LinkML entity contracts)

Summary: `ne-schema` grows the T2 codegen target; `ne-brain-graph` gains the AGE
adapter, the trimmed read layer (§1), the structured-first `GraphSyncRunner`, settings,
CLI verbs, and the full test battery. No ne-contracts change. No ne-brain / ne-connect
source change.

## 3. NE-081 part A — the Apache AGE store adapter

### 3.1 Component + driver decision (licenses verified 2026-07-09, §8)

- **Apache AGE** (Apache-2.0). AGE ships a release line **per Postgres major**:
  PG16 is frozen at AGE 1.6.0; PG17 has 1.7.0 (2026-02-11); PG18 has 1.8.0
  (2026-07-09). **Pin: PostgreSQL 17 + AGE 1.7.0** for the pilot-tier image (PG18/1.8.0
  is 0 days old at spec time — revisit at build). The per-PG-branch lag is a standing
  risk (§9-R2).
- **Driver: psycopg 3 (`psycopg>=3.3,<4`), LGPL-3.0 — C4 case-by-case class with
  in-repo precedent:** psycopg was already owner-approved for NE-033
  ("psycopg(LGPL,owner-approved)"). The dedicated
  `apache-age-python` driver is REJECTED: last release 0.0.7 (2023-09-22), ~3 years
  stale, psycopg2-based, hard antlr4 pin — fails the maintainer-viability intake
  criterion. The adapter executes openCypher through AGE's `cypher()`
  SQL function with `LOAD 'age'` / `search_path` session setup, and ships its own
  small agtype codec (parse agtype scalars/maps/paths from result text; ~200 LOC,
  test-pinned against AGE fixtures).
- psycopg is **confined to `ne.brain_graph._pg`** (one chokepoint module, the
  `ne.gateway._litellm` / `ne.mcp._sdk` pattern — .importlinter:69-70, :103-104) so a
  future asyncpg or driver swap is one file, and the LGPL surface is auditable.

### 3.2 Graph schema on AGE

- **One graph per deployment**: `ne_graph` (C10 single-tenant invariant as it then stood; that C10 was REMOVED 2026-07-17 —
  no tenant column anywhere, same rule as the principal store,
  `ne-permissions/store.py:18-21`).
- **Node label = LinkML class name** (`Account`, `Person`, `Document`, `Ticket`,
  `Order` — operational.yaml:66,91,127,164,208). **Edge label = upper-snake of the
  LinkML relationship slot** (`WORKS_AT`, `AUTHOR`, `OWNED_BY`, `ABOUT`, `BELONGS_TO`,
  `REQUESTER`, `ASSIGNEE`, `ACCOUNT`, `PLACED_BY` — derived mechanically from the
  `inlined: false` slots listed in §0; the derivation lives in the generated
  contracts, §4.1, so graph and schema cannot drift).
- **Required properties on EVERY node** (adapter-enforced, upsert rejected otherwise):
  `key` (deterministic identity, §4.3), `entity_class`, `source_system`, `source_ref`
  (the source-of-record pointer, same semantics as `Citation.source_ref`),
  `acl` (non-empty list of labels — chunk-identical vocabulary), `content_hash`
  (canonical-JSON sha256 of the typed attribute set), `schema_version`
  (`ne.schema` SCHEMA_VERSION, `generated/_manifest.py:6`), `config_hash`,
  `synced_at` (ISO-8601 UTC). Typed attributes from the LinkML contract ride as
  plain properties (`name`, `domain`, `status`, …).
- **Required properties on EVERY edge:** `key` (deterministic, §4.3), `edge_kind`,
  `source_system`, `source_ref`, `acl` (non-empty), `config_hash`, `synced_at`.
  An edge's `acl` is the ACL of the **asserting record** (the row that stated the
  relationship — e.g. the Ticket row asserts `BELONGS_TO`); traversal additionally
  requires both endpoints visible (§1.3.2), so an edge can never widen access to a
  node.
- **Indexes:** AGE stores label tables in Postgres — the adapter's
  `ensure_graph()` (the `ensure_collection` twin, qdrant.py:104-128) creates the
  graph, the label tables, and btree/GIN expression indexes on `key` and `acl` per
  label (plain SQL on the underlying `ne_graph."Label"` tables; agtype property GIN
  is the documented approach). Index DDL is versioned in the adapter and re-applied
  idempotently.

### 3.3 `AgeGraphStore` — the frozen-contract adapter

Implements `GraphStore` (knowledge.py:152-161) exactly:

- `upsert_nodes(nodes)` / `upsert_edges(edges)`: batched `MERGE` on `key`, property
  overwrite on match (upsert), in `_UPSERT_BATCH_SIZE` batches (the qdrant.py:53
  pattern). **Rejects** (raises `GraphAclError`) any node/edge whose `acl` is
  missing/empty — fail-closed at the write boundary, mirroring `AclMapper.resolve`'s
  never-empty guarantee (acl.py:64-69).
- `cypher(query, params)`: parameterized execution, agtype decode, returns
  `Sequence[Mapping]`. Unscoped by design; confinement per §1.1.
- Plus package-local capabilities NOT on the frozen contract (the
  `FilteredChunkFetch`/`PayloadUpdatable` precedent — retriever.py:77-87,
  spine.py:154-166): `update_acl(keys, acl)` (cheap ACL-only correction for the
  stale-ACL heal path, twin of `QdrantChunkStore.update_payload` qdrant.py:151-167)
  and `delete_where_stale(source_system, run_marker)` for pruning (§4.4).

### 3.4 Trimmed reads

`EntityGraph` (§1.2) composes `AgeGraphStore` + `GroupExpander`. All four methods
compile through `TrimmedCypherBuilder` (§1.4), which structurally requires the label
set — there is no builder entry point that omits it. Illustrative generated shape
(2-hop neighbors):

```
MATCH (a)-[r1]-(m)-[r2]-(b)
WHERE a.key IN $keys
  AND any(l IN a.acl  WHERE l IN $acl_labels)
  AND any(l IN r1.acl WHERE l IN $acl_labels)
  AND any(l IN m.acl  WHERE l IN $acl_labels)
  AND any(l IN r2.acl WHERE l IN $acl_labels)
  AND any(l IN b.acl  WHERE l IN $acl_labels)
RETURN ... LIMIT $limit
```

Every variable gets the predicate — generated, not hand-maintained, so a new query
shape cannot forget one (and the guardian's behavioral layer proves it, §6.4).

## 4. NE-081 part B — structured-first graph construction

### 4.1 LinkML entity contracts (the ne-schema codegen target)

**One schema source generates all tiers**. NE-081 adds a 4th artifact
type to the existing codegen exactly along the extension path the module was built
for:

- New generator function in `packages/ne-schema/codegen/generate.py` (beside
  `_generate_pydantic` :67 / `_generate_json_schema` :78): `_generate_entity_graph`,
  a deterministic walker over the LinkML classes that emits
  `generated/operational_graph.py` containing:
  - `class EntityContract` frozen models per concrete class (`AccountEntity`,
    `PersonEntity`, …) — the typed node property sets (identifier slot `id`
    operational.yaml:30-32 + attributes, MINUS the relationship slots);
  - `EDGE_DEFS: tuple[EdgeDef, ...]` — one entry per `inlined: false` relationship
    slot: `(edge_kind, subject_class, slot_name, object_class, multivalued)`,
    e.g. `("BELONGS_TO", "Ticket", "belongs_to", "Account", False)` from
    operational.yaml:184-188;
  - `ENTITY_CLASSES`/`EDGE_KINDS` frozensets — the §1.4 identifier allowlists.
- Extend `SchemaSpec`/`_artifacts()` (`codegen.py:35-66`) with the 4th artifact +
  the generator CLI arg; the artifact then flows automatically through
  `check_freshness` (`codegen.py:153`) and the `test_generated_freshness.py` CI gate.
- **No YAML semantic change** → schema `version:` stays 1.0.0 (operational.yaml:14);
  the CHANGELOG gains a tooling note only. (If a future engagement needs edge
  properties or renamed edge kinds, that's a LinkML `annotations:` addition WITH a
  version bump — out of v1.1 scope.)
- Re-export via the `_LAZY` map in `ne/schema/__init__.py:22-48`.

`ne-brain-graph` imports ONLY the generated contracts (never linkml — the linkml
toolchain stays confined to the git-ignored codegen venv per
`ne-schema/README.md:127-151`). T3 continuity: NE-085's LinkML→SHACL target and the
property-graph→RDF lift consume the SAME classes and `EDGE_DEFS`, which
is what makes the lift mechanical (§9-D3).

### 4.2 Construction is a SECOND governed consumer of the Connector Protocol — not a rider on the T1 chunk path

**Decision (with rationale, because the obvious alternative is wrong):** graph
construction runs as its own runner (`GraphSyncRunner`) that consumes
`Connector.backfill/incremental/permissions` (integration.py:57-66) directly, with its
own cursor + dedup namespace. It does NOT wrap the T1 `SyncRunner`'s writer or mapper
(bridge.py:240-243), for three verified reasons:

1. **T1 dedup starves a tap.** `SyncRunner` content-hash-skips unchanged records
   (bridge.py:177-184). A mapper/writer tap only ever sees *changed* records — so
   enabling Tier-2 on an installed, already-synced deployment (the entire
   "upgrade-in-place" selling point) would build an empty graph. The
   graph MUST be able to backfill independently.
2. **The chunk item has already flattened the structure.** By `_flush`, Salesforce/SQL
   records are joined `content_fields` text + partial metadata
   (salesforce/sync.py:65-125, sql/sync.py:66-129); the full structured row exists
   only on `Record.payload` (bridge.py:113-118 keeps it in `_Buffered`, but that is a
   private internal). Structured-first means consuming `Record.payload`, per the T2
   definition.
3. **Zero T1 modification.** SPEC's additive-tier promise and this spec's mandate:
   `ne-connect` and `ne-brain` are byte-untouched (§6.2 pins this with a test-level
   guard in the unit's DoD).

Cost acknowledged: steady-state incremental double-pull against source APIs (M365
delta tokens are per-consumer by design; SQL/Salesforce cursor queries are cheap;
Slack is not a v1.1 graph source, §4.3). A shared-stream optimization is a flagged
v1.2 option (§9-R7), never a correctness requirement.

`GraphSyncRunner` (in `ne.brain_graph.construct`) reuses ne-connect's public pieces
verbatim: `build_acl_index` + `AclMapper` (acl.py:22-70) for fail-closed ACL
resolution keyed on `record.source_id` (acl.py:61), and `ConnectorStateStore`
(state.py:58-124) with cursor keys namespaced `graph:{...}` and the dedup triple
`(source, source_id, record_id)` (state.py:35-51) hashed over the *typed entity
projection* (not the raw payload) so attribute-irrelevant source churn doesn't
re-upsert. Dependency direction: ne-brain-graph → ne-connect → ne-brain — acyclic;
imports go through `ne.connect`'s root exports only (`ne/connect/__init__.py:12-16`
deliberately excludes the dlt-dragging SQL module; the SQL *mapping config types* are
imported from `ne.connect.sql.config` by the composition root, not by ne-brain-graph
core — see §7.1 note).

### 4.3 Record → typed entities + edges (the mappers)

Per-source `GraphRecordMapper`s (pure, deterministic functions) in
`ne.brain_graph.construct.mappers`, driven by declarative mapping config (§7.4):

| Source | Structured input (verified) | Entities | Edges |
|---|---|---|---|
| Salesforce | Bulk API 2.0 CSV rows; `Record(record_id=Id, source_id=<ObjectName>, payload=row)` (salesforce/connector.py:211) | `Account` ← Account rows; `Person` ← Contact rows; `Ticket` ← Case rows; `Order` ← Order/Opportunity rows (mapping config decides) | FK columns → edges: Contact.AccountId → `WORKS_AT`; Case.AccountId → `BELONGS_TO`; Case.ContactId → `REQUESTER`; Order.AccountId → `ACCOUNT` … |
| SQL | dlt `sql_table` rows; `Record(record_id=str(id), source_id="{table_ref}:{record_id}", payload=row)` (sql/connector.py:249-264) | table → LinkML class per mapping config (`orders` → `Order`, `tickets` → `Ticket`, …) | FK columns → edge kinds per mapping config |
| M365 | driveItem metadata payload (`id,name,web_url,…` — m365/connector.py:197-217) | `Document` | `OWNED_BY`/`AUTHOR` when the payload carries resolvable principal refs; else node-only (author enrichment is NOT an LLM job in NE-081) |
| Slack | free-text messages (slack/connector.py:307-331) | **none in v1.1** — Slack is not a structured entity source; mention-linking of message chunks is NE-082's LLM-link pass (§5.4) | — |

Normative mapper rules:

- **Deterministic identity:** `key = uuid5(NAMESPACE_URL, f"ne-entity:{source_system}:{entity_class}:{source_id}:{record_id}")`
  (the `point_id_for` discipline, qdrant.py:57-59). Same input ⇒ same node forever;
  re-sync is a property overwrite, never a duplicate. Edge key =
  `uuid5(..., f"ne-edge:{edge_kind}:{from_key}:{to_key}:{source_ref}")`.
- **Typed validation:** payload → the generated `EntityContract` model
  (extra="forbid" — generate.py:67-74 codegen already forbids extras). Validation
  failure → that record is **quarantined** (recorded + counted, batch continues) —
  the spine's document-caused taxonomy (spine.py:49-55), never a silent drop, never a
  partial node.
- **Dangling edge targets fail soft-closed:** an FK pointing at an entity not (yet)
  synced produces a **pending edge** row in the sync ledger, retried at the end of the
  run and on the next run — never a placeholder node (placeholder nodes would be
  un-ACL'd inventions). Persistent danglers surface in the sync report.
- **ACL:** node ACL = `AclMapper.resolve(record)` output, verbatim — the same labels
  the T1 chunk for that record carries (bridge.py:95 sets the chunk's; we set the
  node's from the same resolver). Edge ACL = asserting record's ACL (§3.2). All the
  connector label vocabularies flow through unchanged (Entra GUIDs, `sf-profile:`/
  `sf-permset:` — salesforce/acl.py:61-62, operator-declared SQL principals —
  sql/config.py:70-116, and the reserved `acl:tenant-all`/`acl:public-link`).
- **Provenance:** `source_system` = contract source, `source_ref` = the same value
  the T1 citation path uses (`metadata.source_ref` falling back to document id —
  retriever.py:98-103); for connector records that is `f"{source_id}:{record_id}"`
  normalized per source. This equality is what makes graph hops citable (§5.3) and
  lets the console click through to the source of record.

### 4.4 Idempotent re-sync + the sync ledger

`ne_graph_sync_ledger` (plain Postgres table beside the graph, SQLAlchemy — the
`IngestLedger` discipline, spine.py:115): rows keyed
`(source_system, entity_class, key)` with `content_hash`, `acl_hash`, `status`,
`last_run_id`, `reason`. Normative behaviors (all lifted from the spine's DECIDED
semantics):

- **Unchanged short-circuit is 3-way** (spine.py:66-89): same content_hash + same
  acl_hash + clean row ⇒ no-op; same content but changed ACL ⇒ cheap
  `update_acl` (no property re-write); anything else ⇒ full upsert. The stale-ACL
  case matters for exactly the reason documented at spine.py:77-82 — a narrowed
  source ACL must reach the graph even when attributes didn't change, or trimmed
  traversal serves a chunk-invisible entity.
- **Ordering: upsert-new → delete-stale → ledger commit** (spine.py:38-47). Prune =
  after a **full** (backfill) run, delete nodes/edges for this source whose ledger
  row wasn't touched by the run (`delete_where_stale`, §3.3); incremental runs never
  prune (source deletions arrive via full re-sync in v1.1 — CDC is NE-084's lane).
- **Determinism as a tested property:** two full runs over identical fixtures produce
  byte-identical graph exports (§6.3 T-CON-3, the NE-068 "byte-identical double-run"
  precedent).
- **Audit:** one `graph.sync` AuditEvent per run (config_hash-stamped, counts:
  upserted/unchanged/acl-refreshed/quarantined/pruned/pending-edges) through the same
  dual-append discipline every governed writer uses (via `ne.audit` +
  `ne-events` outbox; use `ne.audit.ensure_transactional_sqlite` on any dual-append
  engine — the NE-CLEAN2 shared recipe).

---

## 5. What NE-082 lands (Splink ER + LightRAG-style retrieval + graph-aware citations)

## 5.1 Splink entity resolution — statistical candidates, review-gated, never auto-merge

**Component:** Splink `>=4.0.16,<5` (MIT; v5 is in dev — pin 4.x, §9-R5) with the
**DuckDB backend** (MIT, in-process, the well-trodden path; Splink's Postgres backend
is documented second-tier). DuckDB operates on attribute frames exported from the
graph — zero egress, AIR_GAP-indifferent (C2).

**The job** (`ne.brain_graph.er.SplinkResolutionJob`, scheduled via the NE-034
cron-as-isolated-agent pattern in `ne-worker`):

1. Export per-class attribute frames (e.g. all `Person` nodes: name/email/phone/
   account-key) from AGE via confined `cypher()` — an internal, non-principal read
   (it is a batch job over the whole graph, running as the platform, not as a user;
   its OUTPUT is gated, which is where the control belongs).
2. Blocking rules + comparisons per class from declarative ER config (§7.4);
   deterministic seed; model training artifacts stored with `config_hash`.
3. Pairs scoring above `settings.er_proposal_threshold` become **proposals** — and
   that is the ONLY output. **The job's sole write surface is the proposal store**
   (test-pinned, §6.3 T-ER-1). It cannot reach `upsert_nodes/edges` (enforced by
   construction: the job is composed with a proposal emitter, not a GraphStore
   writer; the guardian's import sweep backs it, §6.4d).

**Riding the EXISTING NE-066 gate** (the flywheel agent-verified touchpoints):

- New enum members (additive, ne-flywheel 0.3.0 → 0.4.0):
  `FixLane.ENTITY_RESOLUTION = "entity_resolution"` in
  `packages/ne-flywheel/src/ne/flywheel/lanes.py:41-47` (the diff renderer's
  `_KNOWN_LANES` allowlist derives from `FixLane` — diffs.py:69 — so the lane is
  automatically trusted) and
  `ProposalKind.ENTITY_MERGE_CANDIDATE = "entity_merge_candidate"` in
  `proposals.py:46-50`. The proposal table stores kind/lane as plain str columns
  (proposals.py:143-145) — **no migration**.
- The job constructs `FixProposal(kind=ENTITY_MERGE_CANDIDATE, lane=ENTITY_RESOLUTION,
  status="proposed", …)` with a **deterministic content-addressed `proposal_id`**
  over the unordered pair of entity keys (the `_proposal_id` discipline,
  curation.py:181-183) — re-runs are idempotent (`SqlAlchemyProposalStore.append` is
  SAVEPOINT-idempotent on duplicate ids, proposals.py:234). `payload` carries:
  both `EntityKey`s, match weight/probability, the comparison vector, blocking rule
  id, model `config_hash`, and display attributes for the steward card.
- Steward reviews in the NE-067 console queue (which renders any lane's proposals via
  the server-sanitized `render_proposal_diff`, diffs.py:372 — add an ER branch that
  renders a before/after entity-pair preview, the `_golden_lines` precedent
  diffs.py:267).
- **Apply is a separate, human-triggered, gate-checked step** (review.py:17-22 — v1 is
  proposals + tracked manual apply): the operator runs
  `ne brain-graph apply-merge <proposal_id>`, which (a) reads
  `ProposalReviewService.status_of(proposal_id)` (review.py:329) and **fails closed
  unless status == APPROVED** (the `proposed→applied` transition is structurally
  illegal in the gate itself — review.py:135-142); (b) performs the merge (below);
  (c) calls `record_apply(proposal_id, provenance, applied_ref=<merge op id>)`
  (review.py:302-325). Rejection feeds the signal loop automatically
  (review.py:33-46) — no extra work.
- **Merge semantics — non-destructive and reversible:** the merge writes a
  `SAME_AS` edge between the two nodes + sets `canonical_key` on the non-surviving
  node; it never deletes either node and never unions ACLs — each node keeps its own
  `acl`, and the `SAME_AS` edge's `acl` is the **intersection** of the two nodes'
  label sets (a principal who cannot see both sides cannot see that they are the
  same entity — the resolution itself is information). `EntityGraph` reads collapse
  `SAME_AS` pairs to the canonical node **only when the edge is visible** to the
  caller. Un-merge = delete the `SAME_AS` edge + clear `canonical_key` (a second
  gated op). Every merge/unmerge emits a `graph.entity_merge` AuditEvent.

## 5.2 LightRAG integration — graph-aware retrieval as a DERIVED, rebuildable index

**Component findings (verified 2026-07-09, §8):** `lightrag-hku` 1.5.4, MIT, very
active. Critical facts that shape the design: (a) its retrieval IS usable over an
externally-constructed graph via `ainsert_custom_kg` (populates graph + entity/relation
vector sidecars + chunk KV — the whole read path), so we are NOT forced through its
LLM extraction pipeline; (b) it ships `PGGraphStorage` (Apache AGE over asyncpg) among
pluggable backends; (c) its core deps include `pipmaster` (a RUNTIME pip installer —
supply-chain/air-gap violation if left live) and `google-genai`; (d) its bundled API
server has a real CVE history (CVE-2026-30762 auth bypass, CVE-2026-39413 JWT
confusion, CVE-2025-6773 path traversal) — **library-only adoption; the server is
banned from any deployment**; (e) its query modes have **no principal concept**.

**Adoption shape (owner build-vs-adopt principle: default adopt, isolation is the
adoption path for awkward deps — the Presidio/Docling precedent):**

1. **Isolated:** LightRAG runs in a hash-pinned sidecar venv
   (`ne.envbuild.ensure_sidecar_venv` — the `presidio_env`/`ragas_env`/ne-schema
   codegen pattern) with a fully compiled, hashed requirement set; `pipmaster` is
   neutralized by pre-installing every needed extra so its install paths never
   execute, plus an AIR_GAP/no-egress test proving no runtime install or download
   occurs (§6.3 T-LR-3). `google-genai` never gets credentials/config; model calls
   cannot reach it (next point).
2. **C3 clean:** LightRAG's `llm_model_func` and `embedding_func` are injected
   callables backed by `ne-gateway` (frontier lane) and TEI respectively — no
   provider SDK is importable outside the gateway anyway (.importlinter:30-70
   enforces repo-wide, and `ne.brain_graph` is in its source list, .importlinter:38).
3. **Derived index, never source of truth:** a `LightRagProjector` exports the
   governed AGE graph (entities with description strings composed from typed
   attributes; relations; `source_id` links to T1 chunk ids — the chunk_id equality
   from §4.3/bridge.py:81-83 makes this join exact) into LightRAG via
   `ainsert_custom_kg`. The projection is rebuildable from scratch at any time
   (`ne brain-graph rebuild-lightrag`); its storage lives in the same Postgres
   (its own schema `ne_lightrag`), and NOTHING reads LightRAG storage except the
   retrieval step below. LLM mention-linking (§5.4) enriches the projection —
   *links* only, per the T2 row.
4. **C7 — trim-before-return (the one place Tier-2 must post-filter, stated
   honestly):** LightRAG queries run with `only_need_context=True` — it returns
   candidate entities/relations/chunks and composes NO answer. `GraphRetriever`
   then re-validates EVERY candidate against the trimmed surfaces before anything
   leaves the retrieval layer: entities/relations via `EntityGraph.lookup/neighbors`
   (server-side trimmed), chunks via the T1 `HybridRetriever.retrieve(chunk_ids,
   principal=...)` (retriever.py:212-243 — already fail-closed on fetch-by-id).
   Anything not provably visible is DROPPED before prompt assembly, citation
   assembly, or any model call. This is fail-closed (unverifiable ⇒ dropped;
   verification errors propagate = deny-by-erroring), but it is post-filtering of an
   untrimmed candidate generator — flagged as a design risk with its mitigation and
   fallback in §9-R4. The candidates never leave process memory untrimmed, are never
   logged, and never reach the gateway.
5. **`GraphRetriever`** (`ne.brain_graph.retrieval`) implements the
   `KnowledgeRead`-shaped surface (search/retrieve with principal — knowledge.py:54-70)
   so it slots into the existing composition exactly like `RetrievalPipeline`
   (pipeline.py:37): T1 hybrid search anchors query → entity linking → LightRAG
   context expansion → trim-before-return → merged `RetrievalResult` with graph
   paths (§5.3). It registers in the Tier-2 C7 guardian's
   `REGISTERED_PRINCIPAL_READ_PATHS` (§6.4).

## 5.3 Graph-aware citations (the ONLY ne-contracts change in this tier)

A multi-hop answer must cite the traversal, not just the endpoint chunks. Additive
contracts change, ADR-0005 + version 1.2.0 → 1.3.0, following the ADR-0004 pattern
verbatim (single additive optional field + new frozen types; ADR co-located in
`packages/ne-contracts/adr/`):

```python
# ne/contracts/types.py — NEW frozen types (additive)
class GraphEntityRef(_Frozen):
    entity_class: str          # LinkML class name
    entity_key: str            # deterministic key (§4.3)
    display: str = ""          # steward/console label, e.g. account name

class GraphHop(_Frozen):
    subject: GraphEntityRef
    edge_kind: str             # e.g. "BELONGS_TO"
    obj: GraphEntityRef
    source_system: str         # provenance of the ASSERTING record —
    source_ref: str            # same fields/semantics as Citation (types.py:69-70)

class GraphPath(_Frozen):
    hops: tuple[GraphHop, ...]

# RetrievalResult (types.py:73-77) — ONE additive defaulted field:
class RetrievalResult(_Frozen):
    chunks: tuple[ScoredChunk, ...]
    citations: tuple[Citation, ...]
    graph_paths: tuple[GraphPath, ...] = ()   # NEW, default () — additive
```

- `Citation` itself is untouched (all four fields stay required — types.py:64-70);
  chunk citations keep flowing through `citation_for` (retriever.py:90-103)
  unchanged. Existing constructors/consumers are unaffected (defaulted field).
- **Trimmed by construction:** `GraphPath`s are only ever built from `EntityGraph.paths`
  / trimmed traversal output (§1.3), so every hop in a citation was visible to the
  requesting principal — including every intermediate node and edge. There is no API
  to attach an untrimmed path (T-CIT-2 proves a path through an invisible node never
  appears, §6.3).
- Each hop is independently **click-through-able** to its source of record via
  `source_system`/`source_ref` — the same provenance the T1 citation carries, because
  construction stamped the asserting record's ref on the edge (§4.3).
- Consumer note for `ne-api`/console: additive — SSE/wire schema gains an optional
  `graph_paths` array; absent = Tier-1-only deployment (the tier is config-gated,
  §7.4).

## 5.4 LLM mention-linking (bounded, review-gated — completing the SPEC T2 row)

SPEC: "LLM pass only *links* mentions to existing entities (new entities gated
by review)." Scope in NE-082 *(original sketch for a `MENTIONS`-edge job that was
never built — see the correction after these bullets)*:

- A batch enrichment job proposes `MENTIONS` edges: T1 chunk → existing entity, via
  gateway-routed extraction (C3) constrained to the known-entity vocabulary
  (candidate set from `EntityGraph.lookup` by name/alias; the model output is
  validated against `ENTITY_CLASSES` + existing keys — a hallucinated entity cannot
  be created, C12).
- `MENTIONS` edge `acl` = the CHUNK's acl ∩ entity visibility applies at read time as
  always (§1.3.2).
- Confidence below threshold, or any would-be-NEW entity ⇒ a `FixLane.ENTITY_RESOLUTION`
  proposal (same gate as merges). *Superseded sketch, kept for the trail:* ~~its write
  surface is proposals + high-confidence link-edges only, and the link-edge writer
  validates target existence. High-confidence auto-links are Tier-1-style writes
  (autonomous, logged — approval tier 0/1 semantics) and are
  config-disableable.~~

> **Correction (2026-07-29, NE-BRAIN-TIER2-AUTOLINK — the auto-link WRITE promise in this
> section is SUPERSEDED by the merged code; scoped to LINK assertions, which is what this
> bullet is actually about):** no auto-link write path exists for LINK assertions, in any
> mode. Two facts, both checkable in the merged tree:
>
> 1. **The §5.4 `MENTIONS`-edge job was never built.** NE-082 shipped DECISION-3 instead:
>    mention-linking as chunk→entity `MENTIONS` edges is DEFERRED pending a chunk-node
>    modeling decision (`packages/ne-brain-graph/README.md`, the "DECISION-3 — LLM
>    mention-linking is DEFERRED" heading) — no chunk nodes, no
>    `MENTIONS` edges, no link-edge writer anywhere in `ne-brain-graph`.
> 2. **The shipped LLM pathway (NE-T2X extraction) counts and DROPS auto-band LINK
>    assertions, in EVERY mode.** `packages/ne-brain-graph/src/ne/brain_graph/extraction/job.py`
>    `_handle_link` (:599-629; the auto-band branch is :615-621): a `link` assertion at/above
>    `extraction_auto_link_threshold` is counted into `ExtractionRunReport.links_auto` and
>    DROPPED — the function returns (:621) without ever appending to a proposal or writing to
>    the graph. The module docstring records both decisions:
>    "``links_auto`` is counted, never queued, never written" (mode 1, NE-T2X-2 — the
>    never-WRITTEN half is pinned structurally by
>    `packages/ne-brain-graph/tests/test_extraction_review_gate.py`, which proves the job
>    composes over a scan-only store; the never-QUEUED half is the auto-band `return` in
>    `_handle_link` (:621), a code-path fact not separately test-pinned) and "Modes 2/3 decision
>    (NE-T2X-4): ``links_auto`` stays counted-and-dropped in EVERY mode" — under DECISION-3 a
>    link has no graph representation, so there is nothing for any mode to write. A
>    below-auto-band link at or above `extraction_min_confidence` IS queued, as a
>    review-gated `LINK` assertion — which, if approved, is itself a counted no-op at write
>    time: `_write_validated_batch`
>    (`packages/ne-brain-graph/src/ne/brain_graph/extraction/apply.py`, currently :319-362)
>    branches on `assertion.kind is GraphExtractionAssertionKind.LINK` and increments
>    `links_noop` instead of writing (currently :339-340), for the same DECISION-3 reason as
>    fact 1 above. The counter makes the queue-shrink operator-visible (strictness design
>    §2.4).
>
> **What modes 2/3 DO write — a different assertion kind, deliberately excluded from the
> claim above:** `new_entity`/`new_edge` proposals move their VOUCHING point, not their write
> surface, and in modes 2/3 that vouching point is AFTER the write. NE-082's
> `write_provisional_extraction`
> (`packages/ne-brain-graph/src/ne/brain_graph/extraction/apply.py`, currently :447; its
> docstring reads "Write a PROPOSED extraction batch IMMEDIATELY, at ``assurance="extracted"``
> ... the write happens BEFORE any human vouching") is called from `ExtractionRuntime`'s
> extraction-task route (`packages/ne-api/src/ne/api/extraction.py`, the
> `write_provisional_extraction(` call in `ExtractionRuntime.run`'s per-batch loop,
> currently :136; the attesting `job.run(source=source, immediate_write_composed=True)`
> call is currently :126) once that run returns
> — a host must explicitly attest to owning that write (gated in `ExtractionJob.run`'s mode
> check, `packages/ne-brain-graph/src/ne/brain_graph/extraction/job.py`, currently :360-373;
> the job alone cannot perform it and refuses to run in modes 2/3 without the attestation). So
> in `read_flagged`/`autonomous` mode, an entity/edge proposal DOES land in the graph, at
> `assurance="extracted"`, before any steward reviews it (strictness design §2.3's
> confirmation-ledger model). This is orthogonal to links, which — per the two facts above —
> stay unwritten in every mode.
>
> The claim this section actually supports, checked against the code above: no autonomous
> tier-0/1 LINK write exists, in any mode — stronger than §5.4's original sketch for links
> specifically. It is NOT a claim that the LLM can never mutate the graph at all; the
> modes-2/3 paragraph above is the path where it does, pre-review, for `new_entity`/`new_edge`.
> If mention-linking ever lands (strictness design §3.6), auto-link writes would be that
> future unit's decision, not a latent promise of this spec.

---

## 6. Tests (the battery that makes the invariants real)

### 6.1 Conformance kit (C5)

`GraphStoreContractKit` in `ne.contracts.testing.kits.graph_store` — additive-exempt
territory (`ne.contracts.testing.*` lands with a patch bump, no ADR), following the kit pattern exactly (`testing/kits/__init__.py:5-19`: a
`<Protocol>ContractKit` class, abstract `create_graph_store()`, `asyncio.run`-driven
`test_*` methods): upsert idempotency (double-upsert ⇒ one node), property overwrite
on merge, empty-ACL upsert rejection, parameterized cypher echo, batch behavior.
Reference in-memory adapter in `testing/memory.py` (networkx-free — plain dicts) so
the kit runs without Postgres. `AgeGraphStore` passes it against a real AGE service
container (server-marked tests, the `test_chunk_store_conformance_server.py` split);
the Neo4j alternate must pass the same kit to merge (C5).

### 6.2 NE-081 tests

- **T-CON-1 construction correctness:** Acme-style structured fixtures (Salesforce
  CSV + SQL rows) → typed nodes/edges with exact expected keys, properties, ACLs,
  edge kinds; dangling-FK → pending-edge (never placeholder); invalid payload →
  quarantine (batch continues).
- **T-CON-2 idempotent re-sync:** run twice ⇒ store byte-stable (node/edge counts +
  content hashes identical); changed row ⇒ single property overwrite; narrowed ACL on
  unchanged attributes ⇒ `update_acl` path taken (spine.py:66-89 semantics); full
  re-sync prunes source-deleted entities via upsert→delete→ledger ordering.
- **T-CON-3 determinism:** byte-identical double-run of the full construction over
  fixtures (canonical export compare — the NE-068 precedent).
- **T-KHOP k-hop correctness:** fixture graph with known topology; `neighbors`/
  `subgraph`/`paths` return exactly the expected sets at hops 1..3, direction and
  edge-kind filters honored, limits enforced, hop cap enforced.
- **T-C7-G the traversal-trimming no-bypass suite (the headline test)** — §6.4.
- **T-SCHEMA:** generated entity contracts freshness (byte-compare gate); `EDGE_DEFS`
  covers every `inlined: false` slot in operational.yaml (walker-vs-YAML cross-check);
  contract models reject extra fields.
- **Tier-1 untouched guard:** the NE-081/NE-082 units' DoD asserts `git diff` over
  `packages/ne-brain/src` and `packages/ne-connect/src` is empty (orchestrator-checked;
  ne-flywheel's two enum lines in NE-082 are the sole cross-package edit, called out
  in the PR body).

### 6.3 NE-082 tests

- **T-ER-1 ER-is-review-gated (structural):** the Splink job composed as in
  production exposes NO graph write object; a full job run against fixtures mutates
  ONLY `ne_fix_proposals` (assert graph export byte-identical before/after — the
  NE-065 "proposals-only, test-proven" discipline).
- **T-ER-2 gate enforcement:** `apply-merge` on a `proposed` proposal raises and
  writes nothing (red-control); on `approved` it merges + `record_apply` lands with
  `applied_ref`; a second apply is blocked by the gate's unique `(proposal_id,
  action)` index (review.py:175-180). Rejected proposal ⇒ merge impossible forever
  (terminal state, review.py:135-142).
- **T-ER-3 merge semantics:** `SAME_AS` edge acl = label-set intersection; a
  principal seeing only one side gets no collapse and no SAME_AS in any read;
  un-merge restores; determinism of candidate generation (fixed seed ⇒ identical
  proposal ids on re-run ⇒ idempotent append).
- **T-LR-1 trim-before-return:** fixture where LightRAG context contains entities/
  chunks invisible to the querying principal ⇒ none appear in `RetrievalResult`
  (chunks, citations, or graph_paths); verification-error ⇒ the query FAILS (no
  silent degradation to untrimmed).
- **T-LR-2 C3:** LightRAG's model/embedding calls hit the injected gateway/TEI fakes
  only; import-linter green with the new contracts (§7.3).
- **T-LR-3 sovereignty:** projector + query under AIR_GAP + socket-disabled: no
  egress, no runtime pip install (pipmaster dead), rebuild-from-graph works offline.
- **T-CIT-1 graph-citation shape:** multi-hop answer over fixtures yields
  `graph_paths` whose hops carry exact expected `edge_kind` + `source_system`/
  `source_ref`; every hop endpoint visible to the principal; wire-serialization
  round-trips.
- **T-CIT-2 trimmed-path property:** a path whose ONLY route crosses an invisible
  node/edge is absent even when both endpoints are visible (the §1.3.2 property).
- **Contracts:** ADR-0005 additive-compat tests (old constructors still valid;
  default `()`); frozen/hashable discipline for the three new types (the
  `ne-contracts` frozen-lie regression class from Checkpoint A).

### 6.4 The Tier-2 C7 guardian (permanent, unskippable)

`packages/ne-brain-graph/tests/test_c7_graph_trimming_guardian.py`, cloning the
three-layer mechanism of `test_c7_trimming_guardian.py` (ne-brain) verbatim, plus one
graph-specific layer:

- **(a) structural sweep:** AST-walk `src/ne/brain_graph` for every class with an
  `async def` read taking a `principal` arg; pin to
  `REGISTERED_PRINCIPAL_READ_PATHS = {"EntityGraph", "GraphRetriever"}` — a new
  principal-scoped surface fails CI until registered AND behaviorally covered.
- **(b) behavioral no-bypass:** for each registered class, a match-nothing label set
  yields empty results with the store provably untouched; a real label set reaches
  the store as a cypher parameter on EVERY node/edge variable (assert on the
  generated query text + params of a recording fake store).
- **(c) production-hook invariant:** labels always non-empty, contain
  `principal.id`, expander exception propagates (re-assert over the imported
  `acl_match_labels` in the graph composition).
- **(d) raw-cypher confinement:** AST sweep asserting `GraphStore.cypher` /
  `AgeGraphStore.cypher` call sites exist ONLY in the allowlisted internal modules
  (`read.py` via the builder, `construct/*`, `er/export.py`, `lightrag/projector.py`)
  — an unregistered call site fails CI. (This is the graph twin of layer (a)'s
  "unscoped store fetch can never masquerade as a scoped path".)

Plus the **D2-style provable suite** (`test_c7_graph_provable_trimming.py`, mirroring
`test_c7_provable_trimming.py`): real AGE container + REAL ne-permissions
`PrincipalStore`/`PrincipalExpander` over sqlite, composed exactly as production —
proving across transitive groups, unknown principals, disjoint ACLs, empty-ACL
nodes, `TENANT_ALL`, and query-time revocation that **a caller holding a known
`entity_key` cannot `lookup()`/`neighbors()`/`paths()` anything that a trimmed
traversal hides**, on every registered read path.

---

## 7. Package layout, dependencies, settings

### 7.1 Layout — extend the existing `packages/ne-brain-graph` stub (decided)

The repo already decided this (repo layout; the stub package;
`.importlinter` registration). Do NOT fold Tier-2 into `ne-brain`: (a) the C7 guardian
in ne-brain pins its read-path set — additive tiers should extend by NEW package with
its OWN guardian, not by growing the frozen tier's sweep surface; (b) dependency
weight (psycopg/splink/duckdb) stays out of every T1-only deployment; (c) the C3/C5
seam story stays legible: `ne.brain_graph → {ne.contracts, ne.schema, ne.brain,
ne.connect, ne.events, ne.audit, ne.flywheel†}` († NE-082 only, for the proposal
store/gate types). All acyclic against the existing edge set (ne-connect → ne-brain
already exists, trimming.py:25-28; nothing imports ne.brain_graph).

```
packages/ne-brain-graph/
├── pyproject.toml                # module ne.brain_graph (exists); deps below; entry points §7.2
├── src/ne/brain_graph/
│   ├── __init__.py               # lazy re-exports
│   ├── model.py                  # EntityKey/EntityNode/EntityEdge/Subgraph/GraphPathRecord (frozen)
│   ├── config.py                 # BrainGraphSettings (NeSettings)
│   ├── _pg.py                    # psycopg chokepoint (driver-confined)
│   ├── age/
│   │   ├── store.py              # AgeGraphStore (GraphStore impl) + ensure_graph
│   │   ├── agtype.py             # agtype codec
│   │   └── query.py              # TrimmedCypherBuilder (§1.4)
│   ├── read.py                   # EntityGraph (§1.2) — principal-scoped surface #1
│   ├── construct/
│   │   ├── runner.py             # GraphSyncRunner (§4.2)
│   │   ├── mappers.py            # per-source GraphRecordMapper (§4.3)
│   │   └── ledger.py             # ne_graph_sync_ledger (§4.4)
│   ├── er/                       # NE-082
│   │   ├── _splink.py            # splink chokepoint
│   │   ├── job.py                # SplinkResolutionJob → proposals only
│   │   ├── export.py             # graph → attribute frames
│   │   └── merge.py              # gate-checked apply-merge / un-merge
│   ├── lightrag/                 # NE-082
│   │   ├── _lightrag.py          # sidecar-venv chokepoint (envbuild)
│   │   ├── projector.py          # ainsert_custom_kg projection (§5.2.3)
│   │   └── retrieval.py          # GraphRetriever — principal-scoped surface #2
│   └── cli.py                    # `ne brain-graph …` verbs: sync, backfill, apply-merge,
│                                 #   unmerge, rebuild-lightrag, verify
├── tests/                        # §6 battery incl. both guardians
└── README.md / NOTICE            # NOTICE: splink/lightrag/duckdb/psycopg entries (DoD)
```

Note on the SQL mapping types: `ne.connect.sql` deliberately drags dlt
(`ne/connect/__init__.py:12-16`), so `construct/mappers.py` defines its own thin
table→class mapping config and does NOT import `ne.connect.sql`; only the composition
root (which already runs connectors) touches both.

### 7.2 Dependencies + plugin registration

```toml
dependencies = [
  "ne-contracts", "ne-schema", "ne-brain", "ne-connect",
  "ne-events", "ne-audit", "ne-envbuild",
  "psycopg[binary]>=3.3,<4",        # LGPL-3.0 — case-by-case, owner-approved precedent
  "splink>=4.0.16,<5",              # MIT  (NE-082; drags duckdb MIT, sqlglot MIT, pandas/numpy BSD)
]
# lightrag-hku>=1.5.4,<1.6 (MIT) lives in the hash-pinned sidecar venv requirements,
# NOT the platform lock (the presidio_env/ragas_env isolation pattern) — its closure
# (google-genai, pipmaster, nano-vectordb, asyncpg, pgvector) stays out of uv.lock.

[project.entry-points."ne.plugins.graph_store"]
age = "ne.brain_graph.age.store:AgeGraphStore"
```

(The `ne.plugins.<slot>` mechanism per plugins.py:20-33 / contracts README; the
Neo4j alternate later registers `neo4j = ...` under the same slot.)
Every dependency goes through the C4 gate + intake criteria at build time as normal;
§8 is the spec-time verification.

### 7.3 Import-linter additions (CI)

Mirroring `c3-mcp-sdk-chokepoint` (.importlinter:72-104), three new contracts:
`psycopg` only via `ne.brain_graph._pg`; `splink` (+`duckdb`) only via
`ne.brain_graph.er._splink`; `lightrag`* only via `ne.brain_graph.lightrag._lightrag`
(defense in depth — the sidecar venv means it shouldn't be platform-importable at
all). `ne.brain_graph` is already present in every existing contract's source list.

### 7.4 Settings (`BrainGraphSettings(NeSettings)`) + config_hash

House style per `PermissionsSettings` (ne-permissions/config.py:21): an `NeSettings`
subclass — values layer init > `NE_` env (`NE_` prefix + `__` nesting,
config.py:77-82) > TOML > defaults; secrets as `SecretStr` (masked in the hash
preimage, config.py:149-161).

```python
class BrainGraphSettings(NeSettings):
    enabled: bool = False                      # the tier is opt-in (additive deployment)
    database_url: str = "postgresql://..."     # customer Postgres w/ AGE
    graph_name: str = "ne_graph"
    max_hops: int = Field(default=3, ge=1, le=6)
    default_read_limit: int = Field(default=200, ge=1)
    sources: dict[str, GraphSourceMapping] = {}    # per-source entity/edge mapping (§4.3)
    # -- NE-082 --
    er_enabled: bool = False
    er_proposal_threshold: float = Field(default=0.95, ge=0.5, le=1.0)
    er_blocking: dict[str, ErClassConfig] = {}     # per-class blocking/comparison config
    lightrag_enabled: bool = False
    lightrag_env_dir: Path | None = None           # sidecar venv root
    mention_link_min_confidence: float = Field(default=0.9, ge=0.0, le=1.0)  # NEVER SHIPPED — correction below
    mention_autolink_enabled: bool = True  # NEVER SHIPPED — correction below
```

> **Correction (2026-07-29, NE-BRAIN-TIER2-AUTOLINK — the two `mention_*` fields above are
> PHANTOM knobs):** neither `mention_link_min_confidence` nor `mention_autolink_enabled`
> exists on the shipped `BrainGraphSettings`
> (`packages/ne-brain-graph/src/ne/brain_graph/config.py:133-254`) — they were part of the
> §5.4 sketch that DECISION-3 deferred (see the §5.4 correction). Setting
> `NE_BRAIN_GRAPH_MENTION_LINK_MIN_CONFIDENCE` / `NE_BRAIN_GRAPH_MENTION_AUTOLINK_ENABLED`
> is a SILENT no-op (verified 2026-07-29 against the merged tree: `BrainGraphSettings()`
> constructs cleanly with both env vars set — pydantic-settings reads env values only for
> declared fields, and `extra="forbid"` does not catch unknown prefixed env vars). The
> shipped confidence knobs are the NE-T2X pair `extraction_min_confidence` (default 0.7) and
> `extraction_auto_link_threshold` (default 0.9, validated `>= extraction_min_confidence` by
> a model validator) — and clearing the auto-link threshold triggers NO write in any mode:
> the link is counted into `links_auto` and dropped (§5.4 correction). The rest of this code
> block is a pre-build sketch too (e.g. the shipped `sources` is a tuple of
> `GraphSourceMapping`, ER blocking config is `er_classes`, and the whole `extraction_*`
> family is absent here) — `config.py` is authoritative; this correction fixes only the two
> fields a reader could try to SET.

The composition root includes it in `compute_platform_config_hash({...,
"brain_graph": settings})` / `freeze_platform_config_hash` (config.py:163, :238 —
ADR-0003 mechanism), so every `graph.sync`, ER-run, merge, and graph-cited answer is
replayable by config_hash (the "why did the system say X" property now
covers traversals: audit replay = config_hash + retrieved chunk ids + `graph_paths`).

---

## 8. Component verification snapshot (2026-07-09 — re-verify at each build)

| Component | Version (date) | License | C4 class | Notes |
|---|---|---|---|---|
| Apache AGE | 1.7.0/PG17 (2026-02-11); 1.8.0/PG18 (2026-07-09); PG16 frozen at 1.6.0 | Apache-2.0 | free | per-PG-major release lines; pin PG17+1.7.0 (§3.1, §9-R2) |
| apache-age-python | 0.0.7 (2023-09-22) | Apache-2.0 | free | **REJECTED — unmaintained ~3 yrs, psycopg2-based** |
| psycopg | 3.3.4 | LGPL-3.0-only | case-by-case | owner-approved precedent; chokepoint-confined |
| Splink | 4.0.16 (2026-03-11) | MIT | free | v5 in dev (5.0.0.dev4, 2026-07-01) — pin `<5` |
| duckdb (Splink core dep) | 1.5.4 | MIT | free | in-process ER backend |
| lightrag-hku | 1.5.4 (2026-06-24) | MIT | free | **library-only; API server banned (CVE-2026-30762/-39413, CVE-2025-6773); pipmaster neutralized; sidecar-venv isolated** |
| networkx (lightrag dep) | 3.6.1 | BSD-3-Clause | free | sidecar venv only |
| neo4j driver (alternate, later) | — | Apache-2.0 | free | Neo4j CE itself GPLv3 ⇒ out-of-process only |

No AGPL/SSPL/BUSL/FSL/NC anywhere in the verified closure.

---

## 9. Decomposition, T3 dependency, open questions & risks

### 9.1 Decomposition — confirmed as two serial units in the BRAIN lane

**NE-081 — graph store + construction + entity contracts (L).** Lands: ne-schema 4th
codegen artifact (`operational_graph.py` + freshness gate); `AgeGraphStore` + agtype
codec + `ensure_graph` + `GraphStoreContractKit` (+ in-memory reference adapter);
`TrimmedCypherBuilder` + `EntityGraph`; `GraphSyncRunner` + mappers + sync ledger +
`graph.sync` audit; `BrainGraphSettings`; CLI `sync/backfill/verify`; both C7 guardian
suites + T-CON/T-KHOP/T-SCHEMA batteries; deploy: AGE extension in the pilot Postgres
image (compose profile `brain-graph`). **No ne-contracts change. No ne-brain/ne-connect
change.** Dep: all merged (Phase 4/5 complete) — dispatchable now in the Phase-7 lane.

**NE-082 — ER + LightRAG + graph-aware citations (L).** Lands: `SplinkResolutionJob`
+ frames export + ER config; ne-flywheel additive enums (`FixLane.ENTITY_RESOLUTION`,
`ProposalKind.ENTITY_MERGE_CANDIDATE`, 0.3.0→0.4.0) + ER diff-renderer branch;
gate-checked `apply-merge`/`unmerge` + `SAME_AS` semantics; LightRAG sidecar env +
projector + `GraphRetriever` + trim-before-return; mention-linking job; **ne-contracts
ADR-0005 (`GraphEntityRef`/`GraphHop`/`GraphPath` + `RetrievalResult.graph_paths`,
1.2.0→1.3.0)** — contracts-lane coordination required (additive-only freeze
discipline, ne-contracts `__init__.py:5-20`); T-ER/T-LR/T-CIT batteries; CLI verbs.
Depends on NE-081 (same lane ⇒ serial). Suggested internal build order: citations ADR
first (small, unblocks console/ne-api wire work), then ER, then LightRAG (its spike
is the schedule risk).

**Seams other units consume:** `ne-api` adds `/v1/brain/graph/*` endpoints over
`EntityGraph`/`GraphRetriever` (extend `docs/architecture/ne-api-service-spec.md` §1.3
brain surface — Tier-2 read endpoints are C7 surfaces there, same as brain search);
NE-085 (below); NE-083 may share the AGE adapter learnings for the temporal memory
graph spike.

### 9.2 T3 dependency flag (NE-085)

NE-085's property-graph→RDF lift builds directly on NE-081's artifacts:
the lift walks `EDGE_DEFS` + `EntityContract` classes (one LinkML source ⇒ the RDF
mapping is generated, not hand-written) and reads the graph through the confined
`GraphStore.cypher` under a platform (non-principal) batch identity — but SHACL-side
reads/writes surfaced to users MUST route back through trimmed surfaces. Two NE-081
design points exist *for* NE-085: deterministic `key`s (stable IRIs) and
`source_system`/`source_ref` on every node/edge (RDF provenance). Any temptation to
shortcut entity identity (e.g. keying on display names) breaks the lift — don't.

### 9.3 Open questions / risks (build must resolve; none may be hand-waved)

- **R1 — AGE openCypher predicate support (build-blocker-class verification).** The
  ACL predicate `any(l IN x.acl WHERE l IN $acl_labels)` must be probed on pinned
  AGE 1.7.0 (subset coverage of `any()`/list membership over agtype lists is not
  guaranteed by docs). Fallback ladder, in order: (a) list-comprehension form
  `size([l IN x.acl WHERE l IN $acl_labels]) > 0`; (b) SQL-side filtering — wrap the
  cypher in the outer SQL query and apply an agtype `@>`-style containment on the
  returned rows *per path element* (AGE results are SQL rows; this keeps trimming
  server-side); (c) adapter-internal post-filter of complete paths before crossing
  the adapter boundary (still fail-closed, weakest perf). Whichever lands, T-C7-G/
  T-KHOP pin it. Also verify psycopg parameter passing into `cypher()`'s third
  agtype argument (prepared-statement form) — never string-build.
- **R2 — AGE per-PG-branch lag.** PG16 is capped at AGE 1.6.0; historical ~12-month
  lag on new PG majors. Pin PG17+AGE 1.7.0; record in the deploy profile that the
  platform Postgres major is now coupled to AGE availability (a pilot on customer
  PG16 gets AGE 1.6.0 — the adapter must be tested against 1.6.0 AND 1.7.0, or PG17
  declared a Tier-2 prerequisite. Decide at NE-081 build; spec default: **PG17
  required for Tier-2**, documented in the install guide).
- **R3 — GraphStore contract minimalism.** The frozen `GraphStore` has no
  delete/health/ensure surface; package-local capabilities (§3.3) fill the gap (the
  established `FilteredChunkFetch`/`PayloadUpdatable` precedent). If a second
  engagement needs them cross-implementation, promote via ADR then — not now.
- **R4 — LightRAG has no principal concept (the honest architectural tension).**
  Primary design post-filters (trim-before-return, §5.2.4) — fail-closed but with
  two real costs: per-principal recall degradation (candidates trimmed away are not
  re-fetched from the trimmed graph in v1.1) and wasted compute on invisible
  candidates. Mitigation: `GraphRetriever` blends trimmed `EntityGraph` expansion
  around the T1 anchors so a heavily-trimmed principal still gets governed graph
  context. **Fallback (pre-authorized here):** if the NE-082 spike shows the
  projection/trim sandwich is not worth the library, implement the LightRAG
  *methodology* natively (dual-level local/global retrieval over `EntityGraph` +
  T1) — the NE-024 native-RAGAS-methodology precedent makes this a
  sanctioned, logged adopt-exception path; the NE-082 row's "LightRAG integration"
  wording is satisfied per the NE-047 interpretation rule (the row
  denotes intent, not a literal library mandate, when the library fights a
  constraint). Either way the `GraphRetriever` surface and tests are identical.
- **R5 — Splink v5 transition** (5.0.0.dev4 live). Pin `<5`; the ER config module is
  the only splink-API-touching layer (chokepoint), so the v5 port is contained.
  Watch item for the cadence process.
- **R6 — Graph ACL ∩ chunk ACL expander interop.** Same expander, same labels, same
  revocation lag (900 s sync — ne-permissions/config.py:34-36) — but Tier-2 adds a
  SECOND stamped copy of source ACLs (nodes/edges beside chunk payloads). The two
  copies are healed by independent syncs (T1 spine's stale-ACL path vs §4.4's), so a
  window exists where chunk and node ACLs for the same record diverge (bounded by
  the two sync cadences). This is an availability skew, not a leak — each read path
  fail-closes on ITS copy — but the demo/eval permission-trap suite (NE-075) must
  not assert cross-tier simultaneity. Document the bound in the module README;
  align the two sync schedules in the composition root.
- **R7 — double-pull cost** (§4.2): acceptable v1.1; a shared-Record-stream tee is a
  v1.2 optimization candidate — only if a real engagement's rate limits demand it.
- **R8 — ER as an untrimmed batch reader.** The Splink job reads the whole graph as
  the platform (like the T1 indexer embedding all chunks). Its inputs never leave
  process/DuckDB-temp, artifacts store no raw attribute frames beyond run scope, and
  its only output is the gated proposal store; the steward console shows proposal
  payloads — so proposal display attributes are minimized (names/keys, not full
  records) since steward-queue visibility is role-based, not per-record-ACL-based
  (same class of exposure as existing content-lane proposals; note it in the ER
  README).
- **R9 — Composition root still missing** (ne-api unbuilt — RESUME-STATE.md:67).
  Tier-2, like Tier-1's trimming, is fully built + provably composed in tests but
  wired to HTTP only when NE-103/NE-104 land. The `ne-api` spec's brain section must
  gain the graph endpoints at that time; until then the CLI (`ne brain-graph …`) and
  the D2-style suites are the operational proof surface.
