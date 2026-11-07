# ne-brain-graph

Tier-2 knowledge tier: a principal-scoped **entity graph** on Apache AGE, built
**structured-first** from source-system records. NE-081 lands the foundation
(store + trimmed read layer + construction + LinkML entity contracts); Splink
entity resolution, LightRAG-style retrieval, and graph-aware citations are NE-082.

## Two-layer privacy (the ChunkStore / HybridRetriever mirror)

```
frozen ne.contracts.knowledge.GraphStore     (unscoped, composition-root-confined)
        ▲ implemented by
ne.brain_graph.age.store.AgeGraphStore        (Apache AGE; psycopg confined to _pg)
        ▲ consumed ONLY by
ne.brain_graph.read.EntityGraph               (the ONLY caller-facing surface)
```

`GraphStore.cypher` is raw and unscoped — the graph twin of `ChunkStore.get`. It
is never exported to callers. Every `EntityGraph` read (`lookup` / `neighbors` /
`subgraph` / `paths`) takes a `principal` and trims through the SAME label algebra
as Tier-1 chunk reads — `ne.brain.trimming.acl_match_labels` is **imported, never
re-implemented** (one algebra, zero drift). The ACL predicate is injected on
**every node AND edge variable** of the generated cypher, so a path through an
invisible node or edge is untraversable even between visible endpoints. Returned
payloads strip `acl` and the governance hashes UNCONDITIONALLY. The extraction
`evidence` chunk-ref list is also stripped (NE-T2X-3 — closing the
U2-documented over-return through the metadata channel), but ONLY from
`ne:llm` elements — a backbone connector's contract-declared `evidence`
business field is not a floor field and still surfaces, since extraction
evidence refs exist only on elements this pathway wrote. An empty/missing
`acl` matches NOTHING (fail-closed, defense in depth). `EntityNode`/`EntityEdge` surface `assurance` as a typed field
(NE-T2X-3): every construction site sets it EXPLICITLY from the stored element
via a fail-closed truth-table — an `ne:llm` element with a missing/forged
assurance prop reads `"extracted"`, never `"source"`, and a non-`ne:llm` element
can never claim `"confirmed"`/`"extracted"` via a smuggled stored prop.

The C7 guardians (`test_c7_graph_trimming_guardian.py` layers a–e +
`test_c7_graph_provable_trimming.py` D2 over real ne-permissions) make a new
un-trimmed principal-scoped read path, or a raw-cypher call outside the allowlist,
a CI failure. Layer (e), added in NE-T2X-3, is a repo-wide AST sweep pinning
`ExtractionWriter` as the ONLY module that may compose a `source_system="ne:llm"`
node/edge — the same guardian discipline extended to F2 (truthful provenance),
proven non-vacuous with planted-offender RED cases.

## R1 — the ACL predicate rung ladder (runtime-probed)

AGE's openCypher subset support for list predicates over `agtype` lists is not
guaranteed by docs, so the rung is **runtime-probed** in
`AgeGraphStore.ensure_graph()` and exposed as `store.predicate_style`, consumed by
`TrimmedCypherBuilder`:

1. `ANY` — `any(l IN x.acl WHERE l IN $acl_labels)` (primary);
2. `SIZE_COMPREHENSION` — `size([l IN x.acl WHERE l IN $acl_labels]) > 0`;
3. `POST_FILTER` — the predicate is omitted from the cypher text and `EntityGraph`
   filters complete paths adapter-side on the label-intersection rule (rung c,
   always-correct fail-closed; may under-return, never over-return).

The label list only ever travels via the parameterized `$acl_labels` argument
(never string-interpolated); `entity_class`/`edge_kind` are cypher identifiers,
allowlist-validated against the generated `ENTITY_CLASSES`/`EDGE_KINDS`. Traversal
is a UNION of fixed-length named-variable patterns — never an unbounded `[*]`.

**Which rung a live run takes** is recorded by `test_graph_store_conformance_server.py`
against a real AGE container (see below); on the pinned image the probe selects the
first rung the server accepts and the server test pins it. **Live-verified
(NE-AGE-CYPHER-LITERALS):** on `apache/age:release_PG17_1.6.0`, `any(...)` (rung 1)
is rejected outright as a syntax error, so the probe falls through and lands on
`SIZE_COMPREHENSION` — `test_probe_selects_a_rung` pins this exact rung rather
than a vacuous not-None assertion, and the `GraphStoreContractKit` builds its own
lookup queries at `store.predicate_style`, never a hardcoded rung, so the kit
exercises whichever rung the adapter under test actually selected.

### AGE `cypher()` invocation form (live-verified)

AGE's parser hook for `ag_catalog.cypher()` requires its first two arguments —
the graph name and the cypher query text — as **literals resolved at parse
time**, not bind parameters: a server-side bind on either raises `a name
constant is expected`; client-side single-quote interpolation of the query text
raises `a dollar-quoted string constant is expected` (AGE wants a
**dollar-quoted** string constant, not a single-quoted one). Neither is
parameterizable. `_pg.py` therefore:

* validates the graph name against a strict identifier charset and REJECTS
  (never sanitizes) anything else, fail-closed with `GraphStoreError`, before
  inlining it as a name literal;
* wraps the query text in a dollar-quote block whose tag is chosen to be
  collision-safe against the text's own content (extends `$ne$` → `$nee$` → ...
  until absent — scanned over the text plus a trailing `$`, since text ending in
  `$ne` would otherwise form `$ne$` against the closing delimiter's own first
  character), and escapes every literal `%` to `%%` (psycopg treats a bare `%`
  as placeholder syntax whenever any bind parameters are passed);
* keeps the params map as the **sole** bind parameter (the third,
  `%s::agtype`, argument) — every value and label list still travels via the
  parameterized blob, canonical JSON, never interpolated into the cypher text.

There is **no ClientCursor fallback**: AGE rejects a client-interpolated third
argument outright (`third argument of cypher function must be a parameter`), so
a driver-side-quoting path can never execute against a real server. Before this
fix, `AgeGraphStore` had never executed cypher against a real AGE server on
either path — every green AGE result came from `InMemoryGraphStore`.

## R2 — Postgres/AGE version pin (and the Docker-image drift)

Tier-2 requires **PostgreSQL 17**; the production pin is **AGE 1.7.0** (the newest
release on the PG17 line). Drift note: Docker Hub's `apache/age` PG17 images
currently stop at `release_PG17_1.6.0` — so live conformance tests run against
1.6.0 and the adapter targets the openCypher subset common to 1.6.0/1.7.0. A
from-source 1.7.0 pilot image is deploy-unit scope, out of this unit.

## R6 — chunk-vs-node ACL sync skew

Tier-2 stamps a SECOND copy of source ACLs (on nodes/edges, beside the Tier-1
chunk payloads). The two copies are healed by independent syncs, so a bounded
window exists where chunk and node ACLs for the same record differ. This is an
availability skew, **not a leak** — each read path fail-closes on ITS own copy.
Align the two sync cadences at the composition root; do not assert cross-tier
simultaneity in permission-trap suites.

## Construction (`GraphSyncRunner`)

A **separate** governed consumer of the Connector Protocol — NOT a tap on the T1
`SyncRunner` (whose content-hash dedup would starve a tap into an empty graph on an
already-synced deployment; proven by `test_runner_separation.py`). It reuses the
SAME `AclMapper` that stamps chunks, copies the spine's 3-way
unchanged/ACL-heal/full-upsert semantics and the upsert → delete → ledger-commit
ordering, parks dangling-FK edges as pending (never placeholder nodes), stamps
`synced_at` only on actual writes (idempotent re-run = zero writes), and emits
exactly one `config_hash`-stamped `graph.sync` AuditEvent per run.

Entity contracts + `EDGE_DEFS` are generated from the ONE LinkML source
(`ne.schema.operational_graph`, the 4th codegen artifact) so graph and schema
cannot drift. Node/edge keys are deterministic UUIDv5 (stable IRIs for the NE-085
RDF lift); the content hash is over the typed projection.

## Composition recipe (ne-api / ne-ingest)

```python
store = AgeGraphStore(settings.database_url, settings.graph_name)
await store.ensure_graph()                      # creates graph + indexes + probes rung
graph = EntityGraph(store, group_expander=PrincipalExpander(principal_store),
                    settings=settings, audit_sink=audit_sink)  # the only caller-facing surface
# reads: await graph.neighbors([EntityKey(...)], principal=principal, hops=2)

# construction (ne-ingest):
runner = GraphSyncRunner(connector, contract, store, ledger, state_store, mapping,
                         actor="ingest", audit_sink=audit, config_hash=cfg)
await runner.run(mode="backfill")
```

`BrainGraphSettings` joins `compute_platform_config_hash({..., "brain_graph": s})`.

## CLI

```
ne-brain-graph verify                                  # connect, ensure graph, print rung + counts
ne-brain-graph sync --mode incremental --source <name>
ne-brain-graph backfill --source <name>                # alias for sync --mode backfill
ne-brain-graph er-run [--class Person]                 # Splink ER -> ENTITY_MERGE_CANDIDATE proposals
ne-brain-graph apply-merge <proposal_id> [--actor X]   # apply an APPROVED merge (fails closed otherwise)
ne-brain-graph unmerge <edge_key> --node <key> --confirm
ne-brain-graph rebuild-lightrag                        # rebuild the LightRAG projection from the graph
```

The CLI refuses to run when `settings.enabled` is `False`; `er-run` additionally
refuses unless `er_enabled`, and `rebuild-lightrag` unless `lightrag_enabled`.
`apply-merge` exits non-zero when the NE-066 gate has not APPROVED the proposal.

## NE-082 — Splink ER, graph-aware retrieval, graph-aware citations

### Entity resolution is review-gated (never auto-merge)

Splink runs OUT-OF-PROCESS in a hash-pinned sidecar venv (`splink_env/`, C4 — GPL
igraph never enters `uv.lock`). `SplinkResolutionJob` exports per-class attribute
frames (a NON-principal PLATFORM batch read over `scan_nodes`, running as the
platform, not as a user — **R8**: its OUTPUT is what the gate controls, and only
the configured compare/blocking fields leave the graph, minimizing proposal-payload
exposure), scores pairs in the sidecar, and its SOLE write surface is the NE-065
proposal store (`ENTITY_MERGE_CANDIDATE` on the additive `FixLane.ENTITY_RESOLUTION`
lane). Nothing auto-mutates the graph. A steward reviews the proposal in the NE-067
console; `apply-merge` fails closed unless `ProposalReviewService.status_of` is
`APPROVED`. An approved merge is NON-destructive and reversible: a `SAME_AS` edge
under the INTERSECTION of the two nodes' ACLs (a principal who cannot see both
sides cannot learn they are the same entity), `source_system='ne:er'` (so a
connector re-sync's `delete_edges_from_source` never removes it), plus a
`canonical_key` on the non-canonical node. Every merge/unmerge emits a
`graph.entity_merge` audit event. v1.1 does NOT auto-collapse `SAME_AS` on read —
it is an ordinary trimmed edge; `canonical_key` rides in `EntityNode.properties`.

### Graph-aware retrieval — native default, LightRAG as a config flip (R4)

`GraphRetriever` (a `KnowledgeRead`-shaped surface) built the NATIVE
EntityGraph-expansion path FIRST and it is the DEFAULT
(`graph_expansion_backend="native"`): T1 search → anchor via
`EntityGraph.lookup(source_refs=...)` → expand via `neighbors` → cite via `paths`.
Both backends are FAIL-CLOSED trim-before-return (C7): every entity/chunk is
re-validated against the principal-scoped `EntityGraph` / T1 retriever; a
verification error PROPAGATES (deny-by-erroring — no silent degradation to
untrimmed). The LightRAG backend is a hash-pinned **library-only** sidecar
(`lightrag_env/`): the bundled **API server is BANNED** (CVE-2026-30762 /
CVE-2026-39413 / CVE-2025-6773), `pipmaster`'s runtime installer is neutralized by
a fully pre-installed pinned closure, and in-sidecar LLM calls are banned.
Selecting `graph_expansion_backend="lightrag"` without a wired sidecar fails
closed — the pre-authorized native methodology (NE-024 precedent) is the fallback
and loses no functionality (**R4**). The sidecar seam is the typed
`GraphExpansionSidecar` Protocol (both methods BLOCKING); a composition root calls
`GraphRetriever.startup()` ONCE at startup to front-load the (potential venv-build)
`ensure()` off-loop, and every per-request `query()` is dispatched via
`asyncio.to_thread` so the event loop is never blocked.

### Graph-aware citations (ADR-0005)

`RetrievalResult.graph_paths` (additive, `ne-contracts` 1.2.0→1.3.0) carries the
traversed `GraphPath`/`GraphHop`/`GraphEntityRef` triples. Each hop is built ONLY
from trimmed `EntityGraph.paths` output, so a path never cites an untraversable
node/edge (trimmed by construction — a path whose only route crosses an invisible
node is absent even when both endpoints are visible).

### R6 — chunk/node ACL skew bound

Graph node/edge ACLs come from the SAME `AclMapper.resolve` that stamps chunk ACLs,
so graph and chunk visibility cannot diverge AT THE SOURCE. Any residual skew is
bounded by the connector's ACL-refresh interval (the stale-ACL heal path) — the
same revocation-lag bound Tier-1 carries.

### DECISION-3 — LLM mention-linking is DEFERRED

tier2-spec §5.4 LLM mention-linking (chunk→entity `MENTIONS` edges) is DEFERRED to a
future unit: it needs a chunk-node modeling decision no spec supplies. It is NOT
built here.

### Sidecar build

The Splink/LightRAG venvs are built on demand by `ne.envbuild.ensure_sidecar_venv`
(filelock-serialised, atomic marker, `AIR_GAP` `pre_lock_check` fail-closed).
Both `SplinkSidecar.ensure()` and `LightRagSidecar.ensure()` are AIR_GAP
fail-closed: a CURRENT venv is a silent no-op, but an ABSENT/STALE venv raises a
typed `ne.brain.sovereignty.AirGapViolationError` BEFORE any `uv pip install`
egress (pypi.org/files.pythonhosted.org) — pre-build the venv at
image-build/pre-staging time. The
ledger's `ne_graph_pending_edges` table gained a `from_key` column (DECISION-2).
It is a DERIVED, rebuildable retry buffer, so the `GraphSyncLedger` constructor
self-heals a pre-DECISION-2 table: it detects the missing `from_key` column and
drops+recreates the pending table with the current shape (the dangling FKs it held
are re-parked from source on the next sync). No manual DB recreate is required.
See `splink_env/README.md` for the hash-pinned compile + build steps.

### Read-path auditing (NE-164b)

`EntityGraph`'s four public reads (`lookup`/`neighbors`/`subgraph`/`paths`)
optionally emit ONE config_hash-stamped `AuditEvent` per completed call,
extending the T1 `HybridRetriever` pattern (NE-164) to the T2 graph tier over
the same frozen `AuditSink` seam. Auditing is OFF by default when the caller
explicitly passes `None` — a REQUIRED keyword-only `audit_sink: AuditSink |
None` constructor arg (NE-VG-2: required since it has no legitimate default;
`None` means no event machinery runs at all, and every pre-NE-164b composition
stays byte-identical, but the choice must now be stated explicitly at every
call site — an omitted `audit_sink` is a construction-time type error, not a
silent default). Each method also gains a keyword-only `trace_id: str | None =
None` param.

**Event shape** — `action="knowledge.graph_read"`,
`resource="knowledge://graph"`, `outcome=success`, `actor=principal.id`, and a
deterministic `detail` grammar:

```
op=<lookup|neighbors|subgraph|paths> nodes=<n> edges=<e> hops=<h> acl_trim_delta=<d>
```

**C7 counts-only rule** — the event carries the query SHAPE only: node/edge/
hop COUNTS and the adapter-side ACL-trim DELTA. It NEVER carries an ACL label
value, a principal group label, an entity key, a display name, or raw cypher
text — the leak-assertion tests in `test_graph_read_audit.py` prove this over
`model_dump_json()`.

**Delta = adapter-side lower bound** — `acl_trim_delta` counts ONLY rows
dropped by `EntityGraph`'s own post-filter (`_node_visible`/
`_path_all_visible`). The in-cypher `$acl_labels` predicate trims fully
server-side on every shipped store (`InMemoryGraphStore`, the Apache AGE
adapter), so the delta is `0` there; it only becomes positive when a store's
probed rung degrades to `POST_FILTER` and this layer's own filter does the
work — a LOWER bound on total trimming, not an upper one (unlike the T1 event).

**Fail-closed sink-append** — when an `audit_sink` IS injected, a sink
`append` failure (or `ConfigHashNotFrozenError`) PROPAGATES: the read is never
served unaudited (NE-CKD6 discipline, mirroring NE-164's write/T1-read path).
Exactly ONE event per public read call, never per-node/per-edge (the NE-164
write-rate rule; sampling explicitly deferred, not built).

**trace_id-as-data threading** — `ne-brain-graph` gains NO `ne.runtime`
import anywhere; `trace_id` is threaded purely as an optional string parameter.
`ne-api`'s `/v1/graph/*` routes resolve `current_run_trace_id()` (the NE-166
run-scoped correlation id, `None` outside any run scope) and pass it down
explicitly on every call; the composition root (`ne-api/platform.py`) wires
the ONE platform audit sink into the single composed `EntityGraph` at
construction, alongside its existing `group_expander=`.

## Entity-merge apply gate (NE-BRAIN-T2-WIRING F5)

`er.merge.apply_merge` is fail-closed on the proposal KIND, not just its review
status. Before parsing any payload it loads the proposal's `kind` alongside its
`payload` and refuses — with a clean `MergeGateError` — any APPROVED proposal whose
kind is not `entity_merge_candidate`, and any `entity_merge_candidate` whose payload
is missing a well-formed `left`/`right` with an `entity_key`. An approved non-ER
proposal (or a malformed ER payload) therefore raises a graceful, message-bearing
`MergeGateError` with zero store writes and zero `graph.entity_merge` audit emissions
— never a bare `KeyError`.

## LLM extraction (NE-T2X, opt-in)

An optional, review-gated LLM entity-extraction pathway alongside the
structured-first backbone — a real, priced choice,
not a replacement for structured sync. Three modes share ONE mechanism (the
proposal spine below). This package ships the mechanism for all three:
`ExtractionJob`/`ExtractionWriter`/`apply_extraction`/`revert_extraction`
(mode 1, `ingest_review`) plus `write_provisional_extraction` (modes 2/3's
immediate write, `assurance="extracted"`, sharing `apply_extraction`'s
validate-then-write core). **Modes 2/3 are composable via the ne-api host**
(NE-T2X-4's `ExtractionRuntime`) — `ExtractionJob.run(...,
immediate_write_composed=True)` is a HOST attestation, not a capability this
job gains on its own; this job structurally still holds no write-capable
store (the T-ER-1 mirror, unchanged). `read_flagged` (write-then-flag-at-read)
and `autonomous` (write-then-review-retroactively, "YOLO") differ only in
WHEN a human vouches — see the design doc §2.2/§2.3.

**The floor, every mode, no exceptions** (pinned by `test_extraction_red_team.py`):
every extracted element is ALWAYS schema-valid (contract-validated through the
SAME generated `ENTITY_CONTRACTS`/`EDGE_DEFS` the connector sync path uses),
ALWAYS truthfully provenance-labelled (`source_system="ne:llm"`,
`assurance ∈ {"extracted","confirmed"}`, an `evidence` chunk-ref list — every
one of these is a writer-stamped constant, NEVER read from model output or a
deserialized payload — see `ExtractionWriter`'s module docstring for the exact
hazard this closes), ALWAYS ACL-scoped to its single evidencing chunk (no
union/intersection across chunks — assertion-scoped inheritance, a re-evidence
appends a ref but never widens the ACL), ALWAYS audited (`graph.extraction` /
`graph.extraction_apply` / `graph.extraction_revert` / `graph.extraction_purge`
on the C10 chain, counts-and-ids only), and ALWAYS reversible
(`revert_extraction`'s evidence-refcounted delete, or a wholesale
`extraction purge`). `evidence` refs are STORE-side only (needed for
`revert_extraction`'s refcounting) — NE-T2X-3's read layer strips them from
every read payload, so they never surface to a principal via `EntityGraph`/
`GraphRetriever` (the sole-writer sweep also pins `ExtractionWriter` as the
only module that may compose `source_system="ne:llm"`, repo-wide).

**Mode-1 structural invariant (the T-ER-1 mirror):** `ExtractionJob` is
composed with a SCAN-ONLY store — no upsert/delete is reachable from the job
object; its sole write is the proposal store, exactly like `SplinkResolutionJob`.
The graph write happens later, gated: `apply_extraction` refuses unless the
NE-066 proposal is APPROVED, then writes via `ExtractionWriter` with
`assurance="confirmed"`.

**Identity (no fuzzy matching, ever):** `entity_key_for("ne:llm", entity_class,
"extracted", canonical_form(display))` — deterministic UUIDv5, permanently
namespace-separated from every backbone key. Surface-form variance ("Acme" vs
"Acme Corp.") is DELIBERATELY not healed here (nondeterministic identity is
worse than fragmentation); the existing Splink ER lane heals it as an ordinary
review-gated `ENTITY_MERGE_CANDIDATE`.

**Settings** (`NE_BRAIN_GRAPH_*`, all opt-in, `extraction_enabled=False` the
byte-identical default):

| Setting | Default | Meaning |
|---|---|---|
| `extraction_enabled` | `False` | Master gate. |
| `extraction_mode` | `"ingest_review"` | The human-vouching point; enabling never lands in YOLO by omission. |
| `extraction_risk_accepted` | `False` | Must be `True` for `extraction_mode="autonomous"` (boot-fails otherwise). |
| `extraction_sources` | `()` | Eligible connector `source_system`s; empty = nothing eligible. |
| `extraction_min_confidence` | `0.7` | Below this, discarded and counted. |
| `extraction_auto_link_threshold` | `0.9` | `link` assertions at/above this resolve without a proposal row. |

**CLI:**

```
ne-brain-graph extraction run [--source <name>]      # refuses unless extraction_enabled
ne-brain-graph extraction purge [--actor X] --confirm # delete every ne:llm element (+ any edge on them, incl. ER SAME_AS)
```

**DECISION-3 stays deferred:** evidence rides as `evidence` chunk-ref props (the
T1 `source_ref` grammar), NOT as `MENTIONS` edges — no chunk nodes are created
by this pathway (see the DECISION-3 note above, unchanged).

**Determinism, stated honestly (design E-P1-1):** proposal ids are
content-addressed over the TYPED assertion (kind/class/keys/edge-triple/
evidence-ref), so re-running the SAME chunks is store-idempotent DESPITE model
phrasing/confidence variance — but this is NOT byte-determinism of model
output the way `test_construction_determinism.py`'s connector-sync lane is
(there is no model in that loop at all). A display re-wording that changes
its canonical form mints a genuinely new proposal — pinned by
`test_extraction_determinism.py`.

**Extraction prompt contract (NE-T2X-PROMPT-SCHEMA).** The extraction system
prompt (`extraction/prompt.py`'s `render_extraction_system_prompt`) is a
DERIVED schema block, not hand-written prose: `kind` values come from
`GraphExtractionAssertionKind`, entity classes/properties come from
`ENTITY_CONTRACTS` (minus the writer's own `_PLATFORM_OWNED_KEYS` strip-list),
and edge triples come from `EDGE_DEFS` — the SAME authorities the validation
wall (`job.py`'s `_validate_wall`/`_handle_*`) itself reads. This closes a
real defect: the first live rig run showed the shipped prompt claiming "the
schema you were given" while never stating it, so a real routed model
invented a plausible-but-wrong shape and every assertion was quarantined
(zero valid batches). `confidence` is demanded from the model and NEVER
defaulted — `extraction_min_confidence`/`extraction_auto_link_threshold` both
route on that value, so a synthesized default would make both thresholds
meaningless. The prompt<->wall correspondence — the one thing not
mechanically derivable, the per-kind field lists themselves — is pinned by
`test_extraction_prompt_wall_contract.py`'s closed-loop round trip: a fixture
parsed straight out of the rendered prompt's own example block is fed through
the real `ExtractionJob` end to end, so a drift on either side turns red.

The per-kind field vocabulary (which fields belong to `link` vs `new_entity`
vs `new_edge`) lives in ONE set of module constants in `extraction/prompt.py`,
and both the hand-written prose sentences and the worked example entries are
generated from those same constants, so a prose/example rename can no longer
diverge -- the second, independently-typed copy no longer exists
(`test_prompt_states_every_wall_vocabulary_item` pins the prose independently
by running its containment asserts against the prompt with the example JSON
block stripped out). Example property values are likewise DERIVED, not
hand-typed: each field's own pydantic constraint metadata (pattern/bounds)
drives a deterministic shortest-match search for a value that actually
satisfies it, failing closed (`ValueError`) on an unsatisfiable-in-bounds
constraint rather than shipping a self-contradicting example. This is pinned
by `test_every_entity_class_example_properties_pass_the_wall`, which
validates EVERY entity class's example properties through the real writer
wall, not just `Document`, the class today's `EDGE_DEFS` sort order happens
to pick for the rendered prompt.

## Live tests (LIVE-PARKED)

```
docker run --name ne-age -e POSTGRES_PASSWORD=agens -p 5455:5432 \
    apache/age:release_PG17_1.6.0
export NE_AGE_DSN=postgresql://postgres:agens@localhost:5455/postgres
uv run pytest packages/ne-brain-graph/tests/test_graph_store_conformance_server.py
```

As of NE-AGE-CYPHER-LITERALS this suite is 18/18 green against a real
`apache/age:release_PG17_1.6.0` container (14 `GraphStoreContractKit` tests + the
probe-rung pin + 3 new tests proving the collision-safe dollar tag/`%%`
round-trip, the graph-name injection guard, and that the params blob is still
the sole parameterized bind). Before this unit it was 3/15, failing with `a name
constant is expected` — `AgeGraphStore` had never actually executed cypher
against a real AGE server.

Without a reachable `NE_AGE_DSN`, the server-marked module skips with a LIVE-PARKED
message; the always-on suite runs entirely over the in-memory reference store.

## Live proof lane (NE-T2X-6) — `test_extraction_live.py`

`packages/ne-brain-graph/tests/test_extraction_live.py` is the T2X extraction
epic's real merge gate: an ADDITIONAL layer over the in-process suites above
that composes the REAL production `ne-api` platform (real `LiteLLMGateway`
routed at a rig's vLLM, real `AgeGraphStore`, real Cedar PDP, real
`DurableApprovalGate`) and proves all three extraction modes, the
extracted-vs-backbone escalation discrimination end-to-end, the AGE purge
Cypher against a real server, and the `/v1/brain` decided boundary (Argus
P3-4, PR #343).

The `ne.brain_graph._pg.execute_cypher` defect that once blocked every write
to a real Apache AGE server (AGE's `cypher()` function requires its
graph-name and `$$`-quoted query-text arguments to be literal SQL text,
never a bind parameter) was RESOLVED by NE-AGE-CYPHER-LITERALS (PR #344):
live conformance on `test_graph_store_conformance_server.py` went 3/15 to
15/15 against `apache/age:release_PG17_1.6.0`. NE-T2X-6 independently
confirmed this live: the AGE-only purge leg
(`test_live_mode3_age_purge_empties_ne_llm_and_spares_backbone`) has been
observed green locally (2026-07-27, `2 passed, 7 skipped` for the module,
against the builder's own AGE container). The 7 `_live_rig` legs (vLLM +
AGE together) remain unproven until the orchestrator's rig run — see the
runbook and the ledger row's status.
