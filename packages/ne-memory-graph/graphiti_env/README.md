# Graphiti temporal-memory sidecar (out-of-process, hash-pinned)

Graphiti runs in an **isolated, hash-pinned sidecar venv** because `graphiti-core`
drags the **openai SDK** (C3), the **neo4j driver**, and **posthog** telemetry
(default ON). The whole closure (`graphiti-core==0.29.2` + neo4j + openai + posthog
+ numpy/pydantic/tenacity) lives ONLY in this venv and **never** enters the
platform `uv.lock`. This is proven by:

- `packages/ne-memory-graph/tests/test_no_graph_closure_in_lock.py` (grep-provable:
  none of `graphiti-core`/`graphiti`/`neo4j`/`posthog` appear in `uv.lock`);
- the `memory-graph-sidecar-out-of-process` import-linter contract (no `ne.*` module
  imports `graphiti_core`/`neo4j`/`posthog`);
- `ne check licenses` (scans `uv.lock` only — stays green).

The platform side (`ne.memory_graph.sidecar.GraphitiSidecar`) never imports Graphiti;
it launches `service.py` as a **subprocess uvicorn server** and talks to it over
loopback HTTP.

## Telemetry (C1)

`service.py` forces `GRAPHITI_TELEMETRY_ENABLED=false` (an OVERRIDE, not a
`setdefault`) BEFORE any `graphiti` import, neutralizing the posthog phone-home.
The sidecar binds `127.0.0.1` only and validates its LLM/embed base URLs
loopback-or-allowlisted (fail-closed, C2) — it has no other egress route.

## Lane switch (ONE base_url)

- **frontier**: `NE_MEMORY_GRAPH_LLM_BASE_URL` = the `ne.memory_graph.llm_facade`
  loopback URL (gateway-governed frontier model, C3-clean);
- **air_gap**: `NE_MEMORY_GRAPH_LLM_BASE_URL` = ne-serve's in-VPC vLLM `/v1` URL
  (natively OpenAI-compat, C3-exempt, no egress). See the R1 routing note in the
  package README / `evals/README.md` — AIR_GAP routes a reasoning-critical stage to
  an open-weight model, quality UNMEASURED until the live per-lane eval runs.

## Build

The venv is built on demand by `ne.envbuild.ensure_sidecar_venv` (filelock-serialised,
atomic marker, AIR_GAP `pre_lock_check` hook). To build it manually / verify:

```
uv venv packages/ne-memory-graph/.graphiti-venv --python 3.12
uv pip install --python packages/ne-memory-graph/.graphiti-venv/bin/python \
    --require-hashes -r packages/ne-memory-graph/graphiti_env/requirements.txt
```

`AIR_GAP=1` makes `ensure_sidecar_venv` fail closed (no runtime egress/install) via
the `pre_lock_check` hook — the venv build needs network, so it fails closed under
air-gap (expected; vendor the wheels in the NE-086 air-gap bundle).

## Writable venv root for hardened deploys (`NE_SIDECAR_VENV_ROOT`)

The ne-app production image installs into a **root-owned, read-only** site-packages
tree and runs as a non-root `USER 10001`, so the in-package default venv location
(`packages/ne-memory-graph/.graphiti-venv`) is **unwritable at runtime**. Set
**`NE_SIDECAR_VENV_ROOT`** to a writable mount and the sidecar resolves its venv to
`$NE_SIDECAR_VENV_ROOT/.graphiti-venv` instead.

Resolution rule (NE-SVENV): when `NE_SIDECAR_VENV_ROOT` is **set-and-nonempty** the
venv is `$NE_SIDECAR_VENV_ROOT/.graphiti-venv`; otherwise it is the in-package
sibling above (default byte-identical). An explicit `venv_dir=` constructor kwarg
always wins over both.

Pre-build at the resolved path (mirror the recipe above, at the writable root):

```
uv venv "$NE_SIDECAR_VENV_ROOT/.graphiti-venv" --python 3.12
uv pip install --python "$NE_SIDECAR_VENV_ROOT/.graphiti-venv/bin/python" \
    --require-hashes -r packages/ne-memory-graph/graphiti_env/requirements.txt
```

**MUST MATCH:** the runtime's `NE_SIDECAR_VENV_ROOT` must equal the root the venv was
pre-built under. If they differ, an AIR_GAP deploy **fail-closes** on the "missing"
venv (C2) and a non-air-gap deploy attempts an unwanted `uv pip install`
rebuild/egress.

## Compile the lock (NEVER hand-edit `requirements.txt`)

```
uv pip compile packages/ne-memory-graph/graphiti_env/requirements.in --universal \
    --python-version 3.12 --generate-hashes \
    -o packages/ne-memory-graph/graphiti_env/requirements.txt
```

## Dev Neo4j-CE container (DEV/TEST ONLY — never bundled, never in uv.lock)

```
docker run -d --rm -e NEO4J_AUTH=neo4j/testpassword -p 7687:7687 neo4j:5.26-community
```

## Backend flip — Neo4j-CE (dev) vs Apache AGE (ship, NE-083c)

`service.py::_resolve_backend` reads `NE_MEMORY_GRAPH_BACKEND`:

- unset / `neo4j` → the Neo4j-CE driver (dev default);
- `age` → the vendored `graphiti_age.AgeDriver` (`graphiti_env/graphiti_age/`) against
  the platform Postgres via `NE_MEMORY_GRAPH_AGE_DSN` (+ `NE_MEMORY_GRAPH_AGE_GRAPH`,
  default `ne_memory`); a missing DSN is a loud `RuntimeError`;
- anything else → a loud `RuntimeError` naming the env var (never a silent fallback).

`psycopg[binary]>=3.3.4,<4` is added to `requirements.in` for the AGE driver
(LGPL-3.0-only, sidecar-venv-only here — but ALSO legitimately in the platform lock via
ne-brain-graph, so the lock-grep test does not assert its absence).

### AGE container + conformance harness

```
docker run -d -p 5455:5432 -e POSTGRES_PASSWORD=age apache/age:release_PG17_1.6.0
export NE_MEMORY_GRAPH_AGE_DSN='postgresql://postgres:age@127.0.0.1:5455/postgres'
python conformance.py --backend age    # inside the sidecar venv; prints canonical JSON
```

`conformance.py` is the standing dual-backend proof: a fixed, offline (no-LLM)
episodic→facts→recall scenario whose canonical JSON must match across the AGE and
Neo4j-CE drivers (`tests/test_dual_backend_conformance.py`). Beyond the recall core it
also compares the three review-fix paths — `fresh_graph` (empty search on a fresh
graph, no `UndefinedTable`), `token_node_summary`/`token_edge_fact` (agtype-token-
bearing property VALUES round-trip byte-identical), and `edge_similarity`
(source/target-filtered edge similarity + `min_score` parity).
