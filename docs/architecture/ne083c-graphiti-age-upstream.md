# NE-083c — `graphiti_age` upstream-PR-readiness memo

**Decision (NE-083 Option B):** contribute the Apache-AGE driver upstream to
getzep/graphiti under Apache-2.0. This memo makes the driver *separable and
documented*; it explicitly does **NOT** block NE-083c on the actual upstream PR.

**Pin:** graphiti-core **0.29.2** (Apache-2.0). Re-verified against the *installed*
source at build time (the STEP-2 hard prerequisite), not web summaries. Per-loop
re-verify cadence: the venv-gated canary
(`tests/test_graphiti_pin_canaries.py`) fails loud if the pin, the seam names, or the
`ON CREATE SET` R3.i invariant drift.

## Live validation (build-time, docker present) — and two corrections it forced

The driver was run **end-to-end against live services** at build time (not live-parked
on this host): `apache/age:release_PG17_1.6.0` **and** `neo4j:5.26-community`. Both
binding gates passed live:

- `tests/test_age_driver_server.py` — the full SPEC-row round-trip (Graphiti
  `EpisodicNode/EntityNode/EntityEdge.save` → AGE graph facts → temporal-as-of /
  traversal / group-isolation / tsvector-fulltext / vector recall) on a fresh AGE graph.
- `tests/test_dual_backend_conformance.py` — **AGE canonical JSON == Neo4j-CE canonical
  JSON**, byte-identical on the graph-recall core (the binding AGE==Neo4j-CE proof).

Running it live surfaced two defects that the previously live-parked build had never
exercised (both fixed + pinned by always-on tests):

1. **AGE `cypher()` invocation form (was fundamentally wrong).** The NE-081-derived
   `_pg` composed `ag_catalog.cypher(%s, %s, %s::agtype)` with the graph name AND the
   query text as **bind parameters**. AGE rejects that — it requires arg 1 (graph) and
   arg 2 (cypher text, `$$…$$`) to be **inlined literals**, and only arg 3 (params) may
   be bound (`a name constant is expected` / `a dollar-quoted string constant is
   expected`). `_pg` now inlines the validated graph name as a quoted literal and the
   cypher text inside a fixed `$ne083c$…$ne083c$` dollar-quote (tag-collision → raise),
   with the canonical-JSON params as the sole bind. The AS-clause labels are generated
   `col{i}` names, so a dynamic Graphiti RETURN alias (`ok`/`name`/`count`) can never be
   rejected (this also subsumes the reviewer's finding-2 allowlist defect).
2. **Graphiti's Neo4j write queries need three residual rewrites, not just a `vecf32`
   strip.** Live, AGE raised on the real save queries: `SET n = $entity_data` →
   `SET clause expects a map` (fixed: rewrite to `SET n = properties($entity_data)`);
   `SET n:Entity` label-add → unsupported (AGE binds the label at MERGE; stripped);
   `WITH n CALL db.create.setNodeVectorProperty(...)` on the write path → stripped
   (embeddings route to the vec side table). These are mechanical dialect rewrites in
   `rewrite_cypher`, NOT openCypher-expressiveness gaps — **no STOP condition was hit**
   (AGE 1.6.0 expressed every query family with a rewrite or a probed fail-closed rung;
   `labels(n)`/`properties(n)`/`count`/`collect`/bounded-VLE all execute natively).

The always-on suite now pins each rewrite and the exact inlined SQL form; the live
gates are the standing behavioral proof and re-run whenever `NE_AGE_DSN` (+ optionally
`NE_MEMORY_NEO4J_URI`) and the sidecar venv are present.

## Review-fix round 2 (Codex 3× P2 + Argus P2 + one bundled parity fix)

A post-approval review found four real defects **on the core read/search path** (the
whole deliverable is "AGE behaves identically to Neo4j-CE"), plus a fifth parity gap
the new harness surfaces. Dispositions:

- **FIX-1 (Codex P2 — silent data corruption).** The agtype annotation strip ran a
  regex over the ENTIRE JSON payload, so a stored property VALUE containing literal
  `::vertex`/`::edge`/`::path`/`::numeric` had those substrings removed on read.
  `_codec._strip_annotations` is now a **string-aware scanner** (tracks double-quote +
  backslash-escape state; strips the `::type` suffix only OUTSIDE JSON string literals).
  Pinned by `test_age_codec.py` (`..._inside_string_is_preserved`,
  `..._with_escaped_quotes`, `..._outside_strings_still_stripped_everywhere`) and proven
  live-parked by the harness `token_node_summary`/`token_edge_fact` byte-identical
  round-trip vs Neo4j ground truth.
- **FIX-2 (Codex P2 — crash on valid state).** The pgvector `{graph}_vec` table was
  created lazily on the first embedding upsert, so a `/search` against a fresh /
  episode-only graph raised `UndefinedTable`. The table is now created **up front in
  `build_indices_and_constraints`** on BOTH rungs, using an **undimensioned** `vector`
  column (pgvector accepts an untyped vector; the only index is a `group_id` btree,
  which needs no dimension). The `_vec_dim` lazy-DDL path is deleted; the `to_regclass`
  probe in `_vec_search` is demoted to documented defense-in-depth. Fresh-graph search
  now returns empty like Neo4j — pinned by the harness `fresh_graph` section (all `[]`)
  and the driver-source structural test.
- **FIX-3 (Codex P2 — wrong results vs Neo4j).** Edge-similarity search ignored the
  `source_node_uuid`/`target_node_uuid` filters Graphiti passes. The `{graph}_vec`
  table now carries `source_uuid`/`target_uuid` **endpoint columns** (populated from
  graphiti-core's `edge_data.source_uuid`/`target_uuid`, edges.py:343-344), and the
  filters are applied **IN the vec-search SQL** on both rungs (`(%s::text IS NULL OR
  source_uuid = %s)` …). This replaces the prior bounded post-filter (whose widened
  fetch window could under-return vs Neo4j); the widened-fetch helper is deleted and
  `filter_edge_candidates` is kept only as cheap fail-closed defense-in-depth. Proven
  by the harness `edge_similarity` section (`filtered_alice_acme == ['works']`,
  `wrong_pair_acme_lead == []`, `unfiltered == ['role','works']`).
- **NULL-cell reconstruction (surfaced LIVE by the FIX-3 harness, fixed same run).**
  The new edge-similarity harness path reconstructs `EntityEdge` objects via
  `get_by_uuid`, which the prior harness never exercised. Running it live exposed a
  read-path bug: `execute_query` fed a **SQL NULL result cell** (a cypher projection of
  an absent property — e.g. the `works` edge's unset `expired_at`/`invalid_at`) into the
  strict codec, which raised `AgtypeDecodeError: agtype cell was None` and silently
  dropped the whole edge, so AGE returned a strict *subset* of Neo4j. The fix maps a
  `None` result cell straight to Python `None` (a legitimate null, exactly what Neo4j
  returns) at the driver's result-assembly layer, keeping the codec strict for real text
  cells. This is what makes the live `edge_similarity` equality pass — part of delivering
  FIX-3's identical-results guarantee, not new scope.
- **FIX-4 (Argus P2 — fail-closed rung honesty).** At NE-083c the **vector** ladder was
  probed AND applied while the predicate and traversal ladders were probed/recorded (or
  pinned as builders) without engaging the query path — stated plainly then (no
  overclaim) and tracked as an explicit Followup. **That followup landed in
  NE-083c-FOLLOWUP:** the predicate and traversal ladders are now runtime-probed AND
  applied in the query path too (see the wired "Fail-closed ladders" section below); all
  three rungs are probed and applied.
- **min_score parity (bundled fix, stated plainly — a 5th issue, same class as FIX-3).**
  graphiti-core's Neo4j node/edge similarity paths filter `WHERE score > $min_score`
  (`search_utils.py`, strict `>`, `DEFAULT_MIN_SCORE = 0.6`), but `AgeSearchInterface`
  ignored `min_score`, so at the default threshold AGE returned a superset. Both
  `node_similarity_search` and `edge_similarity_search` now drop hits with
  `score <= min_score` (strict `>`, upstream semantics) before reconstruction; ordering
  is by score on both rungs so filter-then-limit == limit-then-filter. It is a pure
  parity change (no behavior beyond matching Neo4j), included because the harness's
  `edge_similarity.default_min_score_unfiltered` case would otherwise disagree.

### Caveats recorded plainly

- **PGVECTOR-rung live coverage.** The live harness image
  `apache/age:release_PG17_1.6.0` ships **without** pgvector, so live runs exercise the
  **CLIENT** (jsonb + client-cosine) rung. The PGVECTOR rung's fresh-graph behavior and
  undimensioned-column shape are held by the **always-on** DDL/search-SQL pins and the
  driver-source structural test (`vec_pgvector_ddl` called in `_probe_vector_backend`,
  `_vec_dim` gone) — NOT by a live pgvector server in this unit.
- **AGE 1.7.0-on-PG17 drift (informational, image unchanged).** AGE 1.7.0 (2026-01-21)
  has now been advanced on the PG17 branch too (not only PG18), and master is at
  1.8.0-dev. Per the brief the live harness stays pinned to the **1.6.0 PG17** image and
  every "direct on 1.7.0" matrix row remains runtime-probed with the 1.6.0-selected
  fallback; the image is deliberately NOT changed here.
- **Upstream source/target-filter quirk.** graphiti-core applies the source/target
  edge filters **only inside** the `group_ids is not None` branch
  (`search_utils.py` edge_similarity_search); the AGE driver applies them whenever
  provided (a strictly fail-closed superset). The harness therefore always passes
  `group_ids=[GROUP_A]` on the edge-similarity scenarios so it compares equality on the
  actual call shapes Graphiti uses.

## Review-fix round 3 (Codex 2× P1 — the BULK `/episodes` ingestion path)

Rounds 1–2 and the conformance harness covered only the **single-save** path
(`EntityNode`/`EntityEdge`/`EpisodicNode.save`). But the sidecar `/episodes` service —
the actual production ingestion surface — drives `Graphiti.add_episode`, which routes
through `add_nodes_and_edges_bulk_tx` and issues Graphiti's **bulk** UNWIND-based save
queries. That whole query family was unhandled and untested. Two P1 findings on it,
both fixed and now **live-verified against `apache/age:release_PG17_1.6.0`**:

- **FIX-BULK-1 (Codex P1 — bulk ingestion blocked).** The bulk node/edge saves write
  embeddings via `WITH n, node CALL db.create.setNodeVectorProperty(…)` /
  `WITH e, edge CALL db.create.setRelationshipVectorProperty(…)` — the UNWIND row rides
  alongside the node/edge var. The single-var strip regex missed this multi-var form, so
  the residual `CALL db.*` tripped the fail-loud guard and blocked ALL `/episodes`
  ingestion. `_VECTOR_PROP_CALL_RE` now matches a **comma-separated WITH variable list**.
  Coupled fix (required to make the bulk node query translate at all): the bulk node save
  also uses the Neo4j-5 **dynamic-label** form `SET n:$(node.labels)`, which AGE rejects
  with a hard `syntax error at ":"` (live-verified). A new `_SET_DYNLABEL_RE` strips it —
  same posture as the existing static `SET n:Label` strip (the `:Entity` label is bound
  at MERGE). The bulk `SET n = node` / `SET e = edge` map-from-UNWIND-row form executes on
  AGE **directly** (live-verified) and is left untouched — unlike the single-save
  `SET n = $param`, which still needs `properties(...)`. Pinned by `test_age_dialect.py`
  (multi-var CALL strip, dynamic-label strip, bulk node/edge query survival, `SET = node`
  untouched).
- **FIX-BULK-2 (Codex P1 — `/search` blind to episode-ingested data).** `plan_side_tables`
  recognised only the single-save shapes, so bulk-ingested nodes/edges/episodes never
  created `{graph}_ft` / `{graph}_vec` rows and `/search` missed everything ingested
  through `/episodes`. It now also plans the bulk shapes: `nodes` (→ node ft + name_embed
  vec per element), `entity_edges` (→ edge ft + fact_embed vec, endpoints read from
  `source_node_uuid`/`target_node_uuid` — **distinct** from single-save `edge_data`'s
  `source_uuid`/`target_uuid`), and `episodes` (→ node ft from content). Embeddings are
  stripped out of each bulk element (never enter agtype, row 10). A bulk payload is
  detected as a **list containing map elements** (`_has_map_element`) so the single
  `EpisodicNode.save`'s `entity_edges` param — a list of edge-uuid *strings* — is NOT
  mistaken for the bulk edge shape (regression guard `..._entity_edges_list_not_bulk`).
- **Harness extension.** `conformance.py` gained a `bulk_recall` section (group
  `agent-d`) that ingests via the **exact pinned bulk query builders**
  (`get_entity_node_save_bulk_query` etc.) and recalls backend-agnostic graph facts —
  compared byte-identically AGE-vs-Neo4j (the parity proof the harness previously
  lacked). Bulk side-table population is asserted AGE-side via
  `age_side_table_recall.bulk_{ft,vec}_{node,edge}_populated`. Both live-parked tests
  (`test_dual_backend_conformance`, `test_age_driver_server`) pass on fresh
  `apache/age:release_PG17_1.6.0` + `neo4j:5.26-community` containers.

## Chosen seam + provider base (pinned-source evidence)

The pinned `GraphDriver` (`graphiti_core/driver/driver.py`) exposes **two legacy
delegation interfaces** ("kept for backwards compatibility during Phase 1", driver.py
L96–99) plus the abstract execution surface:

| seam | where consulted (pinned source) | `graphiti_age` choice |
| --- | --- | --- |
| `graph_operations_interface` | `nodes.py`/`edges.py` `save/get/delete` check it, then `except NotImplementedError` → generic `execute_query(get_<x>_query(provider), **args)` | **`None`** — all writes/reads flow through `AgeDriver.execute_query` (datetime codec + embedding extraction + `rewrite_cypher`) |
| `search_interface` | `search/search_utils.py` `*_fulltext_search`/`*_similarity_search` check `if driver.search_interface:` FIRST (e.g. L586) before the provider-hardcoded fulltext/vector cypher | **`AgeSearchInterface`** — fulltext/vector run against the `{graph}_ft`/`{graph}_vec` side tables; `CALL db.index.fulltext.*` / `vector.similarity.cosine` NEVER reach AGE |
| `build_indices_and_constraints` | driver-implemented; `Neo4jDriver`/`FalkorDriver` call `get_range_indices`/`get_fulltext_indices` | **fully overridden** — AGE graph + per-label btree + ft/vec side tables + R1 probes; the Neo4j-only `CREATE FULLTEXT INDEX` DDL is never emitted |

**Provider base = `GraphProvider.NEO4J`** (NOT the memo's predicted FalkorDB). Evidence
from `graph_queries.py`: the FALKORDB branch injects RediSearch fulltext procedures,
`vecf32()` vector wrappers, and string-interpolated labels; the default (Neo4j) branch
emits plain openCypher closest to what AGE's `cypher()` accepts. Riding NEO4J minimises
residual rewrites; `rewrite_cypher` handles the residue (all live-verified, see the Live
validation section): `SET x = $map` → `SET x = properties($map)`, `SET n:Label` strip,
`WITH n CALL db.create.set*VectorProperty(...)` strip, `vecf32(...)` strip, and a
fail-loud guard on any *remaining* `CALL db.*`.

> **Correction to the feasibility memo:** it framed the search delegation as "the SAME
> seam Neptune overrides." The pinned source shows Neptune does **not** use a search
> seam — `search_utils.py` has hardcoded `if driver.provider == GraphProvider.NEPTUNE:`
> branches. The clean, vendored-driver-usable seam is `search_interface`, which we use.

## 16-row feasibility matrix → implementation mapping

| # | family | AGE realisation | pinned always-on test |
| --- | --- | --- | --- |
| 1 | scalar `RETURN` (healthz) | `execute_query` → `cypher()` 1 col | `test_age_pg_chokepoint` |
| 2 | node upsert (`MERGE ... SET`) | NEO4J-base cypher, translated | `test_age_dialect` (label guard) |
| 3 | edge upsert | NEO4J-base cypher, translated | `test_age_dialect` |
| 4 | uuid lookup (`{uuid: $u}`) | direct cypher | conformance (live) |
| 5 | temporal as-of (before invalidation) | ISO-8601-UTC string compare | `test_age_codec` (lexical==chrono) |
| 6 | temporal as-of (after invalidation) | same | `test_age_codec` |
| 7 | 1-hop traversal | bounded-VLE `*1..1` | `test_age_dialect` |
| 8 | 2-hop traversal | bounded-VLE `*1..2` / UNION arms | `test_age_dialect` |
| 9 | fulltext (SHIM) | `{graph}_ft` tsvector + `websearch_to_tsquery` + `ts_rank` | `test_age_dialect` |
| 10 | vector top-k (SHIM) | `{graph}_vec` (undimensioned pgvector `<=>` / client-cosine), created up front at build-indices time, source/target-filtered IN SQL | `test_age_dialect` |
| 11 | embedding load | extracted in `sanitize_params`, routed to vec table | `test_age_codec` |
| 12 | group_id filter | `group_id = ANY(%s)` + cypher `IN $group_ids` | `test_age_dialect` |
| 13 | count/collect | `cypher()` aggregate cols | conformance (live) |
| 14 | datetime codec (SHIM) | `encode_dt` timespec=µs UTC | `test_age_codec` |
| 15 | SQL-DDL indices (SHIM) | btree over `properties->>'uuid'/'group_id'` | driver `_ensure_label_indexes` |
| 16 | `WHERE uuid IN $uuids` | direct cypher | conformance (live) |

10 DIRECT + 2 VARIANT (traversal ladders) as translated cypher; 4 SHIM
(tsvector/pgvector/datetime/DDL) as same-Postgres delegations. **Zero rows resolved by
silent no-op** — an unrecognised construct raises `AgeDialectError`.

## Fail-closed ladders (AGE 1.6.0 pilot; 1.7.0 targets PG18 — R3.v)

`predicate` ANY→SIZE_COMPREHENSION→POST_FILTER; `traversal` DIRECT-VLE→UNION-arms;
`vector` pgvector→client-cosine.

**Wiring status (stated plainly — all three probed AND applied, NE-083c-FOLLOWUP):** the
build-time probe trio runs in `build_indices_and_constraints` (`_probe_vector_backend`,
`_probe_predicate_rung`, `_probe_traversal_rung` — a `MATCH (a)-[*1..2]->(b)` canary), and
every rung is applied in the query path:

- **vector** — the selected rung drives `_vec_upsert`/`_vec_search`.
- **predicate** — `apply_predicate_style(text, self._predicate_style)` in `execute_query`:
  **ANY** passthrough → **SIZE_COMPREHENSION** rewrites `any(l IN n.labels WHERE l IN $p)`
  to `size([l IN n.labels WHERE l IN $p]) > 0` → **POST_FILTER** strips the predicate to
  `true` and emits an `(alias, param)` spec, and the driver drops non-matching rows via
  `passes_label_post_filter` (parity with Neo4j: a node with null/absent labels is
  filtered out).
- **traversal** — `apply_traversal_style(text, self._traversal_style)`: **DIRECT_VLE**
  re-emits each VLE segment with bounds clamped to HOP_CEILING (a pure passthrough when
  ≤ 6); **UNION_ARMS** expands the ONE supported VLE segment into a `\nUNION\n` of
  fixed-length arms (one per hop count `lower..upper`), covering exactly the two
  graphiti-core 0.29.2 NEO4J BFS shapes that can reach `execute_query` — the plain
  node-BFS segment and the path-bound edge-BFS shape (`MATCH path = … relationships(path)`,
  whose chain rels are named `__vle_r1..__vle_rk` and `relationships(path)` becomes the
  literal rel list).

**Fail-loud residual posture:** a degraded rung that cannot express a shape raises rather
than return silently-wrong rows — a non-canonical label-`any()` on SIZE/POST_FILTER,
POST_FILTER without a whole-node projection, an unbounded `*`/`*a..` on either traversal
rung, and an unsupported UNION-arms shape (≠1 VLE segment, a bound rel var, or a residual
path token) all raise `AgeDialectError`. **Degraded-rung LIMIT (NE-083c-FU2, #123):** the
POST_FILTER and UNION_ARMS rungs pop the trailing `LIMIT` off the cypher
(`pop_trailing_limit`) and enforce it in-driver — POST_FILTER filters then limits (Neo4j
filter-then-limit parity: never an under-count from limiting the unfiltered candidate
set), and UNION_ARMS applies one global bound after the union (never `arms × limit`
over-return). A non-trailing `LIMIT`, any `SKIP`, a missing/non-int trailing-`LIMIT`
param, or an in-query `LIMIT`/`SKIP`/`ORDER BY` reaching UNION-arms expansion raises
`AgeDialectError` — the pre-FU2 accepted per-arm over-fetch no longer exists. Pinned by
`packages/ne-memory-graph/tests/test_age_dialect.py`
(`test_post_filter_with_limit_returns_requested_matching_count`,
`test_union_arms_with_limit_global_bound_pipeline`,
`test_union_arms_edge_bfs_with_limit_pipeline`, `test_pop_trailing_limit_fail_loud`,
`test_union_arms_rejects_in_query_limit_skip_and_order_by`,
`test_driver_pops_trailing_limit_and_slices_after_post_filter`).

**On the shipping AGE 1.6.0 image** all three probes select the DIRECT rungs (ANY
predicate, direct VLE, pgvector-or-client vector), so behavior and the dual-backend
AGE==Neo4j canonical equality are unchanged (the live conformance test now asserts
`predicate == 'any'` and `traversal == 'direct_vle'` before the equality). The degraded
predicate/traversal rungs are proven by forced-rung unit tests only — a live AGE build
that actually rejects `any()`/`*1..n` is not available on the pinned image, stated plainly.

## Separability / lift-out path

`graphiti_env/graphiti_age/` is a self-contained package with **zero `ne.*` imports**
(grep-verifiable; `_codec`/`_dialect` are pure stdlib, `_pg` imports only psycopg,
`driver` imports only `graphiti_core` + siblings). To lift upstream:

1. Copy `graphiti_age/` under `graphiti_core/driver/age/`.
2. Add `GraphProvider.AGE` to the enum (the one change a vendored driver cannot make —
   see the R4 note) and, optionally, an AGE branch in `graph_queries.py`/`search_utils.py`
   so the driver need not intercept search via `search_interface`.
3. No relicensing step: the four files already carry Apache-2.0 headers.
4. Run getzep's own driver integration suite dual-backend (their infra) — an
   **upstream-PR-time task, NOT an NE-083c merge blocker.** The binding conformance for
   THIS repo is `graphiti_env/conformance.py` + `tests/test_dual_backend_conformance.py`.

## R4 churn posture

graphiti-core is pre-1.0 and churns. Mitigations: (a) the exact-pin 0.29.2 + the
venv-gated canary; (b) the driver depends only on the stable `GraphDriver` /
`SearchInterface` ABCs + the provider-branched generic query path, not on internal
query text; (c) an upstream `GraphProvider.AGE` (post-merge) removes the
`search_interface` interception entirely, shrinking the surface exposed to churn.

## Relicensing note

The driver ships here under the first-party Ryan Junee copyright header. On
acceptance upstream it is relicensed Apache-2.0 (matching graphiti-core and Apache
AGE). Until then it is first-party code confined to the sidecar venv, never in
`uv.lock`, never platform-imported (`tests/test_no_age_driver_leak.py`).
