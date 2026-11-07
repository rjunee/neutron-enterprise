# NE-083 — Apache AGE Driver Feasibility Spike: Graphiti Query Inventory vs. AGE 1.7.0

**Status:** feasibility memo (spike exit — resolves the one gating unknown of
`docs/architecture/ne083-temporal-memory-spike.md` §3.2 item 2 / §3.4 decision rule)
**Verdict (up front): FEASIBLE — GO on the AGE driver (Option B), with three documented
shims** (fulltext→`tsvector`, vector→`pgvector`, datetime→ISO-8601-string codec) **and two
dialect variants** (validated-label interpolation; fixed-length-UNION traversal fallback).
No Graphiti query family is unexpressible on AGE. The Neo4j-CE fallback (Option A) is
retained as the *dev vehicle only* and priced in §3.
**Evidence base:** the merged NE-081 `AgeGraphStore` (this repo's live proof of what AGE
executes: `packages/ne-brain-graph/src/ne/brain_graph/age/`), NE-037's memory modules
(`packages/ne-runtime/src/ne/runtime/memory_store.py`, `memory_promotion.py`), and
Graphiti upstream `main` (getzep/graphiti, Apache-2.0) inspected 2026-07-09:
`graphiti_core/driver/driver.py`, `graphiti_core/graph_queries.py`,
`graphiti_core/models/nodes/node_db_queries.py`, `graphiti_core/models/edges/edge_db_queries.py`,
`graphiti_core/search/search_utils.py`.

---

## 0. What the spike had to prove

The decision memo settled Option B contingent on one gate (spike memo §3.2: the
Cypher-dialect gap "is the **primary feasibility gate**"): *does Apache AGE's openCypher
subset support Graphiti's actual query inventory, directly or via the driver-variant
mechanism plus same-Postgres shims?* This memo enumerates that inventory feature-by-feature
against AGE 1.7.0, citing NE-081's merged adapter as in-repo proof for every construct AGE
must execute, and upstream Graphiti source for every construct Graphiti emits.

Two structural facts make the mapping tractable:

1. **Graphiti already ships a per-provider query-variant mechanism.** Every
   dialect-sensitive fragment is generated per `GraphProvider` (enum: `NEO4J`, `FALKORDB`,
   `KUZU`, `NEPTUNE`) via `graphiti_core/graph_queries.py`
   (`get_range_indices` / `get_fulltext_indices` / `get_nodes_query` /
   `get_relationships_query` / `get_vector_cosine_func_query`) and per-provider branches in
   the node/edge save-query modules. A new driver implements the `GraphDriver` ABC
   (`execute_query`, `session`, `close`, `delete_all_indexes`,
   `build_indices_and_constraints`, plus the `fulltext_syntax`/`build_fulltext_query`
   hooks) — the AGE work is *writing a variant set*, not patching Graphiti core. Exactly
   what the spike memo predicted (§3.2 item 2).
2. **Upstream already blesses out-of-graph delegation.** The Neptune driver delegates
   fulltext to OpenSearch (an external index) and computes vector cosine **client-side**
   over fetched embeddings. An AGE driver delegating to `tsvector`/`pgvector` **in the same
   Postgres, same transaction domain** is strictly cleaner than the accepted Neptune
   posture.

---

## 1. The AGE-vs-Graphiti query-inventory matrix (primary deliverable)

Legend — **DIRECT**: AGE 1.7.0 executes the construct as-is (or with a trivially
interpolated identifier); **VARIANT**: expressible through Graphiti's per-provider variant
mechanism with a rewritten but semantically identical Cypher shape; **SHIM**: delegated to
plain Postgres (tsvector/pgvector/SQL DDL) in the same database; **NOT**: unsupported and
un-shimmable (none found).

| # | Graphiti feature | Query shape Graphiti emits (upstream cite) | AGE 1.7.0 support | NE-081 evidence |
|---|---|---|---|---|
| 1 | **Node upsert** (Episodic / Entity / Community save, single + bulk) | `MERGE (n:Entity {uuid: $u}) SET n = $props`; bulk `UNWIND $nodes AS node MERGE … SET` (`node_db_queries.py`) | **DIRECT** — identical shape runs live in this repo | `store.py:171`: `UNWIND $rows AS row MERGE (n:{label} {key: row.key}) SET n = row`, exercised against a real AGE container by `GraphStoreContractKit` (`tests/test_graph_store_conformance_server.py:57-60`) |
| 2 | **Dynamic label attachment** (Neo4j-only syntax `SET n:$(node.labels)`) | `node_db_queries.py` Neo4j branch | **VARIANT** — AGE has no dynamic-label function; the AGE branch interpolates allowlist-validated label identifiers, exactly like Graphiti's own FalkorDB branch (`SET n:{labels}`, string-built) | NE-081 does precisely this: labels are Cypher identifiers, injection-guarded by allowlist (`store.py:61-65` `_validate_label`; `query.py:12-15`) |
| 3 | **Edge upsert** (`MENTIONS`, `RELATES_TO`, `HAS_MEMBER`) | `MERGE (source)-[e:RELATES_TO {uuid: $u}]->(target) SET e = $edge_data`; bulk `UNWIND` (`edge_db_queries.py`) | **DIRECT** | `store.py:186-191`: `UNWIND $rows AS row MATCH (a {key: row.from_key}), (b {key: row.to_key}) MERGE (a)-[e:{kind} {key: row.key}]->(b) SET e = row` — live-conformance-proven |
| 4 | **`ON CREATE SET` / `ON MATCH SET`** | **Not used** by any Graphiti save query (verified: all saves are plain `MERGE … SET`) | AGE lacks it (apache/age#1619) — **non-issue today**; conformance canary pins it (§6 R3) | NE-081 likewise never needed it (plain `MERGE`+`SET` property-overwrite semantics, `store.py:14-15`) |
| 5 | **Temporal validity filters** (bi-temporal `valid_at`/`invalid_at`/`created_at`/`expired_at` WHERE comparisons via `SearchFilters`) | property comparison fragments assembled by `edge_search_filter_query_constructor` (`search_utils.py`) | **SHIM (codec-level)** — agtype has no datetime scalar; the driver stores **ISO-8601 UTC strings**, for which lexicographic order == chronological order. Comparison operators on string properties are plain openCypher. Precedent upstream: the Neptune driver already coerces non-native values to strings | The exact normalization discipline NE-037 already enforces for SQLite parity (`memory_store.py:24-28`, `_as_utc`/`_require_aware` `memory_store.py:89-97`); and `_pg.execute_cypher` already stringifies datetimes at the chokepoint (`json.dumps(..., default=str)`, `_pg.py:88`) |
| 6 | **Edge invalidation / supersession** (contradiction handling: LLM decides in Python, then stamps `SET e.expired_at = …, e.invalid_at = …`) | property `SET` on a matched edge | **DIRECT** | property-SET-on-match proven by `update_acl` (`store.py:227-247`) |
| 7 | **k-hop / BFS traversal** | `MATCH path = (origin {uuid: $u})-[:RELATES_TO\|MENTIONS*1..{depth}]->(:Entity)` + `UNWIND relationships(path)` (`search_utils.py`) | **DIRECT / VARIANT** — rel-type alternation `:A\|B` is proven (row-right); AGE supports **bounded** VLE `*1..n`, but NE-081 deliberately avoids VLE and has a **proven fallback**: rewrite as a UNION of fixed-length named-variable arms with a hop ceiling — semantically identical for Graphiti's small `bfs_max_depth` (default ≤ 3) | alternation: `query.py:147-152` emits `:"+"\|".join(edge_kinds)`, kit-proven; UNION-arms pattern: `query.py:211-238` `_traverse_arms` with `_HARD_HOP_CEILING = 6` (`query.py:59`); "never an unbounded `[*]`" is a stated invariant (`query.py:16-17`, README:49) |
| 8 | **Node-distance reranker** | **1-hop** neighbor queries scored client-side (`MATCH (center:Entity {uuid: $c})-[:RELATES_TO]-(n:Entity {uuid: …}) RETURN 1 AS score`, inverted in Python) — *not* `shortestPath()` (`search_utils.py`) | **DIRECT** — plain 1-hop MATCH. (Bonus headroom: AGE master adds `age_shortest_path`/`age_all_shortest_paths` SRFs, apache/age#2430 — nice-to-have, not required; verify presence in the pinned 1.7.0 build) | 1-hop match shape = `build_neighbors` hop-1 arm (`query.py:240-253`), kit-proven |
| 9 | **Fulltext / BM25 search** (nodes + relationships) | `CALL db.index.fulltext.queryNodes/queryRelationships` (Neo4j) / `db.idx.fulltext.*` (FalkorDB) / `QUERY_FTS_INDEX` (Kuzu) / **OpenSearch delegation** (Neptune) — all behind `get_nodes_query`/`get_relationships_query` + the driver's `fulltext_syntax`/`build_fulltext_query` hooks | **SHIM** — AGE has no fulltext index or CALL procedures. AGE branch delegates to a **`tsvector` side table + GIN index in the same Postgres**, keyed by element uuid + `group_id`, maintained by the driver on every save; the fulltext "query" is SQL, joined back to graph elements by uuid. Honest note: `ts_rank` is not BM25 — acceptable because Graphiti fuses rankings via RRF (rank order, not absolute score, is what matters); the recall-quality eval (§6 R1) guards it | The pattern is the spike memo's named design (§3.2 item 3) and mirrors NE-081's SQL-DDL-beside-cypher precedent (`_ensure_indexes`, `store.py:114-131` — GIN/btree indexes on AGE's label tables via `_pg.execute_sql`) |
| 10 | **Vector similarity search** | `vector.similarity.cosine()` (Neo4j) / `vec.cosineDistance` (FalkorDB) / `array_cosine_similarity` (Kuzu) / **client-side cosine** (Neptune) — behind `get_vector_cosine_func_query` | **SHIM** — AGE agtype has no vector type/functions. AGE branch delegates to **`pgvector` side tables in the same Postgres** (`embedding vector(N)`, `ORDER BY embedding <=> $v`, filtered by `group_id`), uuid-joined back to graph elements. Degraded rung (if pgvector absent): Neptune-style client-side cosine over group-filtered candidates — an upstream-accepted posture | pgvector is already a SPEC slot (NE-091) on the same platform Postgres; embeddings never enter agtype (avoids agtype float-precision/size concerns entirely) |
| 11 | **Community/clustering** | community **detection is client-side Python** (label propagation over fetched edges — no GDS/graph-algorithm procedures); storage = `MERGE (c:Community …)` + `MATCH (c:Community)-[:HAS_MEMBER]->(m:Entity)` | **DIRECT** — plain MERGE/MATCH; nothing server-side to port | same MERGE/MATCH shapes as rows 1/3 |
| 12 | **`group_id` partition filter** (per-agent isolation) | `WHERE n.group_id IN $group_ids` on every search path | **DIRECT** | `IN $param` proven: `n0.key IN $keys` (`query.py:189`), params travel as the parameterized `::agtype` third argument, never interpolated (`_pg.py:66-71,88`) |
| 13 | **Aggregates / plumbing** (`count`, `collect`, `coalesce`, `WITH`, `ORDER BY`, `LIMIT`, `UNWIND`) | throughout `search_utils.py` | **DIRECT** — all in AGE's subset | `count()` live-proven (`store.py:376-392`); `UNWIND` rows 1/3; `LIMIT` in every NE-081 template |
| 14 | **List predicates** (`any(…)`) in some filter paths | filter constructors | **DIRECT on AGE 1.7.0** — with a probed fallback ladder if a deployment lands on 1.6.0 | this is NE-081's R1 ladder verbatim: runtime probe selects ANY → SIZE_COMPREHENSION → POST_FILTER (`store.py:134-156`, `query.py:20-31`, README:33-53); SPEC records that 1.6.0 lacks `any()` and 1.7.0 has it (Phase-7 decisions of record) |
| 15 | **Index/constraint DDL** (`build_indices_and_constraints`, `get_range_indices`, `get_fulltext_indices`) | `CREATE INDEX … IF NOT EXISTS` / fulltext-index DDL per provider | **SHIM-by-SQL (trivial)** — AGE indexes are Postgres indexes on the label tables; the AGE branch returns SQL DDL executed via the psycopg chokepoint, plus tsvector/pgvector side-table DDL | `_ensure_indexes` (`store.py:114-131`) does exactly this today |
| 16 | **Driver session/transaction surface** (`GraphDriver.execute_query`, `session`, `transaction()`) | ABC in `driver/driver.py`; base class supplies an immediate-execution `_SessionTransaction` fallback | **DIRECT** — psycopg async connection with `LOAD 'age'` + `ag_catalog` search path; autocommit or explicit tx both available | `_pg.connect` (`_pg.py:35-41`); ClientCursor fallback for servers rejecting prepared agtype params (`_pg.py:92-99`); agtype result decoding is a solved, test-pinned codec (`agtype.py:44-54`, pinned to AGE 1.6/1.7 output shapes per `agtype.py:13-15`) |

**Findings summary:** 10 DIRECT, 2 VARIANT (both with merged NE-081 precedents), 4 SHIM
(all four are same-Postgres delegations with an upstream-blessed precedent in the Neptune
driver), **0 NOT**. No Graphiti query is unexpressible even at the worst rung — and the
worst rungs (POST_FILTER, client-side cosine, fixed-arm UNION) are all *fail-closed
correct, possibly slower*, never *wrong*.

---

## 2. Go/no-go verdict

**GO — build the AGE driver (Option B).** The spike memo's decision rule (§3.4: "if lane B
shows no *hard* AGE blocker … build B") is satisfied: every feature lands DIRECT, VARIANT,
or same-Postgres SHIM, and this repo has already merged and live-conformance-tested the
hard 80% of the driver's mechanics — parameterized `cypher()` execution over psycopg
(`_pg.py`), the agtype codec (`agtype.py`), batched `UNWIND`+`MERGE` upserts
(`store.py:160-194`), validated-identifier interpolation, the runtime-probed predicate
ladder, and SQL-DDL-beside-cypher index management. The genuinely new work is the Graphiti
`GraphDriver` packaging, the tsvector/pgvector side tables, and conformance hardening.

**Effort sizing (spike memo asked for this at exit):** driver core ≈ 1–1.5 wk; fulltext +
vector shims ≈ 1–1.5 wk; conformance vs. a Neo4j reference via testcontainers ≈ 1–2 wk.
Total **low-weeks (M-L)**, matching the memo's §3.2 estimate. Nothing here reopens the
backend decision.

**The priced fallback (Option A, only if 083c fails in practice):** Graphiti + Neo4j CE
out-of-process — C4-*permitted* verbatim (C4 names "Neo4j CE as a separate service
over Apache-2.0 drivers") and it works with zero driver effort, but it permanently costs
every single-tenant deployment (C10) a second, backup-less (no online backup in CE),
GPLv3-in-the-bundle JVM database with its own ops runbook, NOTICE/source-offer machinery,
and air-gap source vendoring (NE-086), against the one-graph-engine Postgres-gravity
rationale that put T2 on AGE. Schedule saved: roughly the 3–5 driver weeks above, spent
once; operational + legal-review cost: every engagement, forever (spike memo §3.1). The
fallback trigger is narrow and empirical: a Graphiti integration-suite failure on AGE that
survives the variant mechanism AND both shim rungs — nothing in the matrix predicts one.

---

## 3. The out-of-process Graphiti facade (the LLM seam, air-gap-compatible)

Graphiti runs out-of-process regardless of backend (spike memo §2: its openai-SDK closure
would trip C3's import-linter rule and bloat the sovereign lock). The adopted
isolation shape is the established sidecar pattern (Splink `splink_env/`, LightRAG
`lightrag_env/` — `packages/ne-brain-graph/README.md:180-189`,
`ne.envbuild.ensure_sidecar_venv`):

```
ne-runtime (platform lock, in-process)
 ├── TemporalAgentMemory (implements frozen MemoryProvider, runtime.py:155-167)
 │     └── thin httpx client → loopback ne-memory-graph service
 └── SqlAlchemyEpisodicStore + MemoryPromoter — UNTOUCHED (source of truth + gate)

ne-memory-graph sidecar (hash-pinned venv/container; graphiti-core + openai SDK +
                          psycopg + the VENDORED AGE driver live ONLY here)
 ├── HTTP surface (loopback-bound, service-token): POST /episodes · POST /search ·
 │    POST /search/as_of · GET /healthz
 ├── Graphiti(graph_driver=AgeDriver(dsn=platform_postgres)) — same Postgres as NE-081 T2,
 │    its own graph name (e.g. ne_memory) + its own tsvector/pgvector side tables
 ├── LLM  → OpenAI-compat base_url (two lanes, below)
 └── Embedder → TEI /v1/embeddings (OpenAI-compat; loopback 8085 in the pilot,
      deploy/compose/pilot/README.md:46-55 — in-VPC, C3-exempt, registry-tagged)
```

**The LLM base_url — resolving the memo's open question Q2.** Correction of record first:
`ne-serve` is **not** an HTTP server package to hang a route on — it is the vLLM compose
serving profile + a thin `httpx` client (`packages/ne-serve/README.md`: "vLLM is an
external container service, never a platform Python dependency"). So the memo's
"ne-serve-style facade" resolves as:

- **AIR_GAP lane: no facade needed.** Point Graphiti's OpenAI-compat client **directly at
  the ne-serve vLLM server** (it natively speaks `/v1/chat/completions`, with
  xgrammar-backed structured output — `ne-serve/README.md`). This is in-VPC inference:
  exempt from gateway *routing* but registry-tagged and AIR_GAP-accounted per C3
  — the exact posture ne-serve's C3-accounting section documents. C2's
  open-weight-only mode keeps working with zero new machinery.
- **Frontier lane: a small NEW loopback gateway facade**, `ne.memory_graph.llm_facade` —
  an ASGI app exposing `/v1/chat/completions`, backed by the in-process `ModelGateway`
  Protocol (`ne/contracts/models.py:69-77`: `complete()` / `stream()`), living in the
  **host-side** `packages/ne-memory-graph` package (platform lock; imports `ne-gateway`,
  never a provider SDK — C3-clean since litellm stays confined to `ne.gateway._litellm`).
  Loopback-bound like qdrant/TEI (pilot README caddy-only rule), bearer = service token
  from SecretStore. Every extraction call thus remains gateway-governed: C2 egress
  allowlist, C12 deterministic guardrail caps, audit — no bypass is constructible because
  the sidecar has no other route out (loopback-only bind + NE-012 egress guard).
  Non-streaming `complete()` suffices for Graphiti extraction (structured/JSON outputs);
  `response_format` passes through to the gateway request. Streaming is optional later.

Both lanes give the sidecar the *same* OpenAI-compat contract — the lane choice is one
`base_url` config value, so AIR_GAP is a config flip, not a code path.

**Degradation mode (unchanged from the memo §2):** service down ⇒ `append()` still
succeeds (the episodic INSERT is the transaction; ingestion is queued from the log) and
`recall()` falls back to the retained v1 keyword scan (`memory_store.py:347-371`). Memory
writes are never lost; agent runs never fail on a memory-index outage.

---

## 4. MemoryProvider-preserving migration (replay-based, rollback-free)

Unchanged in structure from the decision memo §4; restated with the AGE-driver concretes:

1. **The log is the truth; the graph is a derived index.** `ne_agent_memory_events`
   (`memory_store.py:67-81`) is kept byte-identical — append-only
   (`memory_store.py:12-16`), per-agent isolated (`memory_store.py:18-23`). The AGE graph
   (+ its tsvector/pgvector side tables) is a rebuildable projection: drop it and v1 recall
   still works. Rollback is therefore free by construction.
2. **Backfill = replay**: per agent, in `occurred_at` order, each event becomes a Graphiti
   episode (`group_id = agent_id`, episode name = `event_id`,
   `reference_time = occurred_at` — the row's valid-time instant, `memory_store.py:75`;
   `recorded_at`, :76, is the transaction-time watermark). Idempotent by episode name ⇒
   resumable; throttled (every event is an LLM extraction call — §6 R1).
3. **Dual-write**: `TemporalAgentMemory.append()` = the unchanged episodic INSERT
   (`memory_store.py:172-215`) + enqueue ingestion keyed by `event_id`. Ingestion failure
   never fails `append()`; a catch-up worker replays the gap from the log (watermark =
   last-ingested `recorded_at` per agent). The log *is* the queue — no new infrastructure.
4. **Shadow-read burn-in**: v1 keyword recall and graph recall side-by-side; assert the
   graph path never *loses* an event the keyword path surfaces (facts may add results);
   divergence metrics emitted.
5. **Cutover = factory flag**: `create_agent_memory`
   (`packages/ne-runtime/src/ne/runtime/factory.py:496-506`) returns the
   `TemporalAgentMemory` facade instead of `AgentMemory` under a `RuntimeSettings` flag.
   Flip back = instant rollback. The frozen Protocol (`runtime.py:155-167`) is untouched;
   the conformance test (`packages/ne-runtime/tests/test_memory_provider_contract.py:34-43`)
   runs unmodified against the new facade, plus the C5 two-implementation swap suite now
   that the slot has ≥2 implementations. Fix — do not port — the v1 `recall(limit=0)`
   off-by-one (`memory_store.py:361-371`).
6. **Promotion / curation gate: structurally unchanged — no governance bypass is
   constructible.** `MemoryPromoter` (`memory_promotion.py:76-332`) reads learned-fact
   events *by event_id from `SqlAlchemyEpisodicStore`* and is the only path to the frozen
   `KnowledgeWrite` seam (audit-first `propose()`, distinct-curator `promote()`,
   unwind-on-audit-failure — `memory_promotion.py:39-51`). The sidecar holds **no
   `KnowledgeWrite` reference** — the same structural property as `propose()` never
   touching `self._writer` (`memory_promotion.py:16-18`). Graph-*synthesized* facts an
   agent wants promoted are first appended as a new `learned-fact` episodic event (content
   = fact text, provenance = source-episode event_ids), then flow through the existing
   `propose()` → `promote()` gate. Promotion never reads from the graph.
7. **Isolation**: the facade binds one `agent_id` exactly as `AgentMemory` does
   (`memory_store.py:323-334`); every sidecar search call carries
   `group_ids=[agent_id]` (matrix row 12) **plus** a facade-side defense-in-depth
   post-filter on returned provenance (NE-037's bar: existence never leaks cross-agent,
   `memory_store.py:18-23`; adversarial cross-group test mandatory, §6 R5).

---

## 5. Decomposition (buildable units)

| Unit | Content | Deps | Size |
|---|---|---|---|
| **NE-083b — facade / service / migration** | New `packages/ne-memory-graph`: host-side package (thin `TemporalAgentMemory` facade + httpx client + `llm_facade` ASGI app + backfill/catch-up CLI) + hash-pinned sidecar env (`graphiti_env/`, `ensure_sidecar_venv` pattern) + factory flag in `create_agent_memory` + degradation fallback + backfill / dual-write / shadow-read + MemoryProvider conformance + C5 swap test + adversarial isolation test + recall-quality eval fixtures (ne-eval). **Dev backend = Neo4j CE testcontainer** (Option A as the throwaway dev vehicle, per the spike memo §3.4 — it never enters a customer bundle), so 083b does not serialize behind 083c. | NE-083 spike (this memo) | L |
| **NE-083c — AGE driver + upstream PR** | The vendored `graphiti_age` driver **inside the sidecar** (written against the pinned `graphiti-core` `GraphDriver` ABC): psycopg execution chokepoint + agtype codec (adapt the NE-081 `_pg.py`/`agtype.py` patterns — the sidecar venv cannot import `ne-brain-graph`, so the ~150 lines are replicated with attribution, extraction-to-shared deferred), ISO-8601-UTC datetime codec, validated-label interpolation variant, bounded-VLE traversal with the fixed-arm UNION fallback (probe-selected, NE-081 R1 style), `tsvector` + `pgvector` side tables + `build_indices_and_constraints` SQL DDL, then: run **Graphiti's own integration suite** dual-backend (Neo4j reference vs. AGE) via testcontainers (the NE-085 dual-backend pattern), flip 083b's backend config to AGE, and open the **upstream PR to getzep/graphiti**. **Vendored-until-merged**: the driver lives in the sidecar at a pinned `graphiti-core` version; the pin is re-verified every Trident loop; upstream merge is upside (community maintenance), never a dependency. AGE cutover happens **before any customer ship** so Neo4j CE never enters a deployment. | NE-083b (facade seam), NE-081 merged (shared AGE infra — done, PR #85) | M-L |

Closes the "Graphiti-AGE-driver contribution decision (NE-083 spike)" open item. It no
longer lives in the spec at all: §12 is pointer-only (open items live one per file, never as
numbered items there), and this item was extracted out of
§12's preamble backlog in PR #518 and RESOLVED in
`docs/architecture/ne083-graphiti-age-decision-record.md`, which is where the decision and
its reasoning now live. **Decision = contribute the AGE driver; vendor until merged.**

---

## 6. Open questions / risks (honest register)

**R1 — LLM-extraction cost + AIR_GAP quality (reasoning-critical stage; model routing
stated plainly).** Every `append()` and every backfilled event incurs an LLM extraction
call. Routing: **frontier lane = a gateway-routed frontier model** (the extraction-quality
reference); **AIR_GAP lane = an open-weight model on in-VPC vLLM from the NE-011 registry
menu** — a *different, weaker model class on a reasoning-critical stage*, and its
entity/contradiction-extraction quality is **unmeasured**. This must never be silently
downgraded: NE-083b ships a per-lane extraction-quality eval fixture + per-event cost
numbers, and the build unit's acceptance criteria name the routing explicitly. Backfill of
a large corpus is real token spend under C12 guardrail caps — throttle + budget it.

**R2 — migration correctness definition.** LLM extraction is stochastic ⇒ replay is not
deterministic and the graph is an *index, not a ledger*. Correctness is defined as:
(a) episodic log intact (trivially true — untouched); (b) shadow-read no-loss property
holds through burn-in; (c) every fact's provenance resolves to audited `event_id`s. This
definition goes verbatim into NE-083b's acceptance criteria to keep audit expectations
honest.

**R3 — residual AGE-openCypher gaps.** (i) `ON CREATE SET`/`ON MATCH SET` absent
(apache/age#1619) — unused by Graphiti today, but an upstream refactor could adopt it: the
pinned version + a conformance canary that greps the pinned query modules turns that into
a loud failure, not silent breakage. (ii) Bounded-VLE semantics/perf at depth on large
memories is unproven — the fixed-arm UNION fallback (NE-081-proven) is the probe-selected
safety net; perf is measured in 083c conformance. (iii) Datetime-as-string ordering
depends on strict UTC ISO normalization at ONE codec chokepoint — property-name allowlist
(`valid_at`/`invalid_at`/`created_at`/`expired_at`) + codec round-trip tests pin it.
(iv) `ts_rank` ≠ BM25 — RRF fusion mitigates; the R1 eval guards recall quality.
(v) Image drift: Docker Hub's `apache/age` PG17 line stops at 1.6.0
(`ne-brain-graph/README.md:55-61`) — the `any()` rung (and the optional shortest-path
SRFs, apache/age#2430) want the from-source **AGE 1.7.0 pilot image**, which is already a
deploy-unit item for T2; until then the probe ladder keeps 1.6.0 correct-but-slower.

**R4 — Graphiti upstream churn.** The driver interface has already deprecated a backend
(Kuzu ships with a `DeprecationWarning`; upstream now recommends Neo4j/FalkorDB) — an AGE
driver PR may sit unmerged or the ABC may move. Mitigation is structural: vendored driver
at a pinned `graphiti-core`, re-verified per loop; the facade + episodic log mean even a
forced Graphiti major-version migration is a re-derivation of an index, never a data
migration.

**R5 — `group_id` isolation hardness.** Graphiti's partition filter must be verified as a
hard filter on *every* search/traversal path (adversarial cross-group test in 083b), with
the facade post-filter as defense in depth — NE-037's existence-never-leaks bar.

**R6 — sidecar closure hygiene.** psycopg (LGPL) and the openai SDK live only in the
sidecar venv (C3/C4 posture identical to splink_env/lightrag_env); the platform lock gains
nothing. The C3 import-linter contract extends to `ne.memory_graph` host modules (no
provider SDK; `ne-gateway` import allowed for the facade only).
