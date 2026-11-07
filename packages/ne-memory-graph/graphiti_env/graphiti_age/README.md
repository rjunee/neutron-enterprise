# graphiti_age — a Graphiti GraphDriver over Apache AGE

`graphiti_age` implements Graphiti's `GraphDriver` seam (graphiti-core==0.29.2) on top
of **Apache AGE** (openCypher over PostgreSQL) via **psycopg**, so temporal-graph
memory can run on the same platform Postgres as the Tier-2 graph store — with **no
second GPLv3 database** (Neo4j-CE) in any customer deployment.

This module is **first-party Ryan Junee code** (Apache-2.0 headers),
authored to be **separable** and contributed upstream to getzep/graphiti under
Apache-2.0 per the NE-083 Option-B decision. It has **zero `ne.*` imports** and lives
entirely inside the sidecar venv.

## What it is

| File | Responsibility | Imports |
| --- | --- | --- |
| `_codec.py` | strict agtype decode + ISO-8601-UTC datetime codec + `sanitize_params` (datetime encode, embedding extraction) | stdlib only |
| `_pg.py` | the psycopg chokepoint — `LOAD 'age'`, parameterized `ag_catalog.cypher(%s,%s,%s::agtype)` | psycopg only |
| `_dialect.py` | label guard, R1 predicate ladder, bounded/UNION traversal, ft/vec DDL+SQL shims, residual cypher rewrites | stdlib only |
| `driver.py` | `AgeDriver(GraphDriver)` + `AgeSearchInterface(SearchInterface)` | graphiti_core + siblings |

## The seam (pinned graphiti-core==0.29.2)

Three delegation points, all read from the installed source:

1. **Writes/reads** — `graph_operations_interface = None`, so `nodes.py`/`edges.py`
   fall back to `execute_query(get_<x>_query(provider), **args)`. We ride
   `GraphProvider.NEO4J` (closest openCypher) and translate the residue in
   `execute_query` (datetime codec, embedding extraction, `rewrite_cypher`).
2. **Search** — `search_utils.py` checks `if driver.search_interface:` before its
   provider-hardcoded fulltext/vector cypher. `AgeSearchInterface` runs search against
   the `{graph}_ft` (tsvector) and `{graph}_vec` (pgvector or client-cosine) side
   tables, so `CALL db.index.fulltext.*` / `vector.similarity.cosine` never reach AGE.
3. **Indices** — `build_indices_and_constraints` is fully overridden: it creates the
   AGE graph, per-label btree indexes, the ft/vec side tables, and runs the vector +
   predicate probes.

## The four feasibility shims

* **tsvector fulltext** — `to_tsvector('simple', …)` GENERATED column + GIN;
  `websearch_to_tsquery` + `ts_rank` (rank order only, fused under RRF — not BM25).
* **vector** — an **undimensioned** pgvector `vector` column + `<=>` when the extension
  is present; else a `jsonb` column + client-side cosine (fail-closed correct, the
  Neptune-blessed rung). The `{graph}_vec` table is created **up front** in
  `build_indices_and_constraints` (both rungs), so a search against a fresh /
  episode-only graph returns empty like Neo4j instead of raising `UndefinedTable`. It
  carries `source_uuid`/`target_uuid` endpoint columns so edge-similarity searches
  filter on the node pair IN SQL (parity with the Neo4j filtered path). Node/edge
  similarity also applies `min_score` (strict `>`, upstream semantics) before returning.
* **datetime codec** — every temporal prop is an ISO-8601-UTC string with
  `timespec=microseconds` so lexicographic order == chronological order.
* **SQL-DDL indices** — btree over `properties->>'uuid'` / `->>'group_id'` per label.

## Fail-closed ladders (AGE 1.6.0)

`predicate` ANY → SIZE_COMPREHENSION → POST_FILTER; `traversal` DIRECT-VLE `*1..n`
(n ≤ 6) → fixed UNION arms; `vector` pgvector → client-cosine. **Wiring status:** all
three ladders are runtime-probed in `build_indices_and_constraints` AND applied in the
query path — vector drives `_vec_upsert`/`_vec_search`; `apply_predicate_style` applies
the predicate rung in `execute_query` (ANY passthrough / `size([...]) > 0` rewrite /
predicate stripped to `true` + an in-driver `passes_label_post_filter`); and
`apply_traversal_style` applies the traversal rung (clamped direct VLE / UNION-arms for
the two Graphiti BFS shapes). On the shipping AGE 1.6.0 image all three probes select the
DIRECT rungs, so behavior — and the dual-backend AGE==Neo4j conformance — is unchanged;
on a future AGE build that rejects `any()` / `*1..n` the probed rung auto-degrades. The
degraded predicate/traversal rungs cover the canonical Graphiti 0.29.2 shapes and fail
LOUD on anything else (a non-canonical label-`any()`, POST_FILTER without a whole-node
projection, an unbounded VLE, or an unsupported UNION-arms shape all raise
`AgeDialectError`) — never a silent wrong answer. On a degraded rung (POST_FILTER
predicate strip or UNION-arms expansion) a trailing `LIMIT` is popped from the cypher and
enforced in-driver AFTER the label filter / union, so the driver returns up to the
requested number of actually-matching rows (never an under-count from limiting the
unfiltered candidate set, never `arms × limit`); a non-trailing `LIMIT`/`SKIP`, or an
`ORDER BY` on a UNION-arms expansion, raises `AgeDialectError` (fail-loud). The degraded
rungs are proven by forced-rung unit tests (no live AGE build actually rejects the direct
forms here).

## Lifting it upstream

The package is a drop-in `graphiti_core.driver` sibling: copy `graphiti_age/`, register
`GraphProvider.AGE` (upstream enum change), and relicense the four files to Apache-2.0.
See `docs/architecture/ne083c-graphiti-age-upstream.md` for the full lift-out path and
the seam evidence.
