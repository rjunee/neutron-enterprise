# ne-memory-graph — temporal-graph agent memory (NE-083b)

Bi-temporal knowledge-graph agent memory **behind the frozen `MemoryProvider`
Protocol** — callers are byte-unaffected. The NE-037 episodic log stays the
source of truth; the temporal graph is a **derived, rebuildable index**.

```
ne-runtime (in-process, unchanged callers)
 └── TemporalAgentMemory  ── implements frozen MemoryProvider (runtime.py:155-167)
       │   append = episodic INSERT (the TRUTH) + best-effort queued episode
       │   recall = graph fact hits  UNION  retained v1 keyword floor
       └── httpx ──► loopback ne-memory-graph sidecar  (service_token, 127.0.0.1)
                          │
              graphiti_env/ sidecar venv (hash-pinned, OUT-OF-PROCESS)
                Graphiti(graph_driver = Neo4jDriver [dev] | AgeDriver [ship, NE-083c])
                  ── NE_MEMORY_GRAPH_BACKEND flips: 'neo4j' (dev) | 'age' (ship)
                 ├── LLM  ─► ONE base_url env switches the lane:
                 │     frontier ⇒ ne.memory_graph.llm_facade (loopback, gateway-governed)
                 │     air_gap  ⇒ ne-serve vLLM /v1 (in-VPC, OpenAI-compat, no egress)
                 └── Embedder ─► TEI /v1/embeddings (in-VPC, loopback)
```

## Model routing — stated PLAINLY (R1)

Extraction (entity + contradiction detection) is a **reasoning-critical** stage.
Routing per lane, named openly and never buried:

- **frontier lane** — extraction routes through the in-process gateway
  (`task="memory-extraction"`, a gateway-governed **frontier model**) via the
  loopback `llm_facade`. This is the extraction-quality reference.
- **air_gap lane** — extraction routes to an **open-weight in-VPC vLLM model**
  from the NE-011 registry menu. This is a **weaker model class on a
  reasoning-critical stage**, and its extraction quality is **UNMEASURED** until
  the live per-lane eval runs. This unit ships the eval harness (`evals/`) + per-
  event cost capture (the `llm_facade` structured cost log); **live numbers are
  LIVE-PARKABLE** (no vLLM endpoint in CI). The AIR_GAP extraction-quality delta
  is an **open risk at merge** — recorded here, in `evals/README.md`, and in the
  PR body / AS-BUILT, per the "never silently downgrade a critical stage" rule.

## Migration correctness (R2) — verbatim definition

LLM extraction is stochastic, so replay is **not** deterministic and the graph is
an **index, not a ledger**. Migration correctness is defined as:

1. **the episodic log is intact** (trivially true — NE-037's store, promoter, and
   contract test are byte-unchanged);
2. **shadow-read no-loss** holds through burn-in (`shadow.shadow_read().lost == ()`
   — the graph path never loses an event the keyword path surfaces);
3. **every fact's provenance resolves** to audited `event_id`s (the facade drops
   any graph hit whose provenance does not resolve to the bound agent's events).

It is **NOT** bit-identity.

## Degradation mode

If the sidecar is down: `append` still writes the episodic row (the INSERT is the
transaction; ingestion is best-effort and swallowed), and `recall` returns exactly
the v1 keyword floor. Agent runs never fail on a memory-index outage.

## Governance (no bypass)

This package holds **no `KnowledgeWriter` / `KnowledgeWrite` reference** (a
structural test proves it). Promotion of a graph-synthesized fact into the
knowledge plane still routes **append → propose → promote** through the UNCHANGED
`MemoryPromoter`, which reads the LOG, never the graph. Reads are bound to one
`agent_id`; every search carries `group_ids=[agent_id]` plus a defense-in-depth
provenance post-filter, so existence never leaks cross-agent (the NE-037 bar).
`recall_shared` stays the v1 explicit-allowlist path.

## Run

| verb | what it does |
|------|--------------|
| `ne-memory-graph serve` | ensure the sidecar venv, run the Graphiti service (loopback) |
| `ne-memory-graph llm-facade` | run the frontier-lane OpenAI-compat facade over a real gateway (frontier egress is redacted fail-closed via `ne.govern.create_redactor`, NE-162b) |
| `ne-memory-graph backfill --all` / `--agent <id>` | replay the full episodic log into the graph |
| `ne-memory-graph catch-up --all` | replay only the per-agent watermark gap |

### Env

| var | meaning |
|-----|---------|
| `NE_MEMORY_GRAPH_SERVICE_URL` | loopback sidecar base URL (default `http://127.0.0.1:8093`) |
| `NE_MEMORY_GRAPH_SERVICE_TOKEN` | THE shared secret: enforced by the sidecar, sent by the client, required by the llm_facade, and used as the sidecar's LLM api_key unless overridden. **REQUIRED** — the sidecar now REFUSES to start (fail-closed, mirroring the llm_facade) when this is empty; every route also 401s. Deploys MUST set it. |
| `NE_MEMORY_GRAPH_ALLOW_INSECURE_NO_TOKEN` | dev-only escape hatch: set to exactly `1` to run WITHOUT a service token (auth disabled). Any other value keeps the fail-closed default. Never set in production. |
| `NE_MEMORY_GRAPH_LANE` | `frontier` (default) or `air_gap` |
| `NE_MEMORY_GRAPH_LLM_BASE_URL` | sidecar LLM base_url — the lane switch (loopback llm_facade vs vLLM /v1) |
| `NE_MEMORY_GRAPH_LLM_API_KEY` | optional override for the sidecar's LLM api_key (defaults to the service token; vLLM ignores it) |
| `NE_MEMORY_GRAPH_EMBED_BASE_URL` | sidecar embedder base_url (TEI /v1/embeddings, loopback) |
| `NE_MEMORY_GRAPH_NEO4J_URI` | dev/test Neo4j-CE bolt URI |
| `NE_RUNTIME_SESSION_DATABASE_URL` | the episodic log DB URL (shared with sessions) |

### Dev Neo4j-CE container (DEV/TEST ONLY — never bundled, never in uv.lock)

```
docker run -d --rm -e NEO4J_AUTH=neo4j/testpassword -p 7687:7687 neo4j:5.26-community
```

## AGE backend (NE-083c — the ship path)

**Apache AGE on the platform Postgres is the CUSTOMER/ship backend; Neo4j-CE remains a
dev/test container only and never enters a deployment.** The backend is one env flip;
Graphiti runs the vendored `graphiti_age.AgeDriver` (see
`graphiti_env/graphiti_age/`) against the same Postgres as the Tier-2 graph store — so
temporal memory ships on ONE Apache-2.0 datastore with no second GPLv3 database.

| var | meaning |
|-----|---------|
| `NE_MEMORY_GRAPH_BACKEND` | `neo4j` (dev default) or `age` (ship). Anything else → loud `RuntimeError` |
| `NE_MEMORY_GRAPH_AGE_DSN` | psycopg DSN for the platform Postgres with the `age` extension (required when backend=`age`) |
| `NE_MEMORY_GRAPH_AGE_GRAPH` | AGE graph name (default `ne_memory`) |

**Conformance harness.** `graphiti_env/conformance.py --backend {age,neo4j}` drives a
fixed, offline (no-LLM) episodic→facts→recall scenario and emits canonical JSON;
AGE == Neo4j-CE iff the JSON matches (`tests/test_dual_backend_conformance.py`). The
compared core now also covers the three review-fix paths: `fresh_graph` (a search on a
freshly-built, unseeded graph returns empty — the `{graph}_vec` table is created up
front, no `UndefinedTable`), `token_node_summary`/`token_edge_fact` (a property VALUE
containing literal `::vertex`/`::edge`/`::path`/`::numeric` tokens round-trips
byte-identical), and `edge_similarity` (source/target-filtered edge similarity applied
IN SQL + `min_score` parity, so AGE returns Neo4j's filtered set, not a superset).

```
docker run -d -p 5455:5432 -e POSTGRES_PASSWORD=age apache/age:release_PG17_1.6.0
export NE_AGE_DSN='postgresql://postgres:age@127.0.0.1:5455/postgres'
```

**Probe rungs (AGE 1.6.0 vs 1.7.0, R3.v).** The PG17 pilot image is
`apache/age:release_PG17_1.6.0` (1.7.0 targets PG18). Three fail-closed ladders exist —
predicate `ANY→SIZE_COMPREHENSION→POST_FILTER`, traversal DIRECT-VLE→UNION-arms,
vector pgvector→client-cosine — and all three are runtime-probed in
`build_indices_and_constraints` AND applied in the query path (`apply_predicate_style` /
`apply_traversal_style` in `execute_query`, plus the vector rung driving the side-table
search). On the shipping AGE 1.6.0 image all three probes select the DIRECT rungs, so
behavior and the dual-backend AGE==Neo4j conformance are unchanged; on a future AGE build
that rejects `any()` / `*1..n` the probed rung auto-degrades (SIZE_COMPREHENSION /
POST_FILTER; UNION-arms for the two Graphiti BFS shapes). The degraded rungs cover the
canonical Graphiti shapes and fail LOUD on anything else — worst case is a loud
`AgeDialectError`/`AgeQueryError`, never a silently-wrong result. See
`docs/architecture/ne083c-graphiti-age-upstream.md` (Fail-closed ladders).

## Cutover / rollback

`RuntimeSettings.memory_backend='temporal-graph'` makes `create_agent_memory`
return this facade via the `ne.plugins.memory_provider` entry point; the default
(`'episodic'`) is byte-unaffected `AgentMemory`. Flip back = instant rollback (the
episodic path never stopped working; the graph is a droppable index).

## C4 posture

`graphiti-core` / `neo4j` (driver) / `posthog` NEVER enter `uv.lock` — the closure
lives only in `graphiti_env/`'s hash-pinned sidecar venv (grep-proven by
`tests/test_no_graph_closure_in_lock.py` + the `memory-graph-sidecar-out-of-process`
import-linter contract). Neo4j CE (GPLv3) is a dev/test container only; the
AGE-driver cutover (NE-083c) removes it before any customer ship.

## AIR_GAP posture (C2)

The Graphiti sidecar venv builder (`GraphitiSidecar.ensure()`) is fail-closed
under `AIR_GAP`: a CURRENT venv is a silent no-op, but an ABSENT or STALE one
raises a typed `ne.brain.sovereignty.AirGapViolationError` BEFORE any
lock/rmtree/subprocess — a runtime `uv pip install` egress
(pypi.org/files.pythonhosted.org) from inside the customer VPC is forbidden. The
sanctioned path is a PRE-BUILT venv: build it at image-build/pre-staging time.
The loopback health poll (`wait_healthy`) uses a dedicated urllib opener that
hard-disables `HTTP(S)_PROXY`/`ALL_PROXY` and refuses cross-host redirects, so
the sidecar service token is never diverted to an unsanctioned proxy or host.
