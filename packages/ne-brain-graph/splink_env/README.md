# Splink ER sidecar (out-of-process, hash-pinned)

Splink entity resolution runs in an **isolated, hash-pinned sidecar venv** because
Splink drags **igraph (GPL-2.0+)** unconditionally (C4, SPEC §2). The whole closure
(`splink==4.0.16` + `igraph` + `duckdb` + pandas/numpy/sqlglot/altair/jinja2) lives
ONLY in this venv and **never** enters the platform `uv.lock`. This is proven by:

- `packages/ne-brain-graph/tests/test_no_gpl_sidecar_leak.py` (grep-provable: none
  of splink/igraph/duckdb/lightrag/pipmaster appear in `uv.lock`);
- the `t2-sidecars-out-of-process` import-linter contract (no `ne.*` module imports
  splink/duckdb/igraph);
- `ne check licenses` (scans `uv.lock` only — stays green).

The platform side (`ne.brain_graph.er._splink.SplinkSidecar`) never imports splink;
it invokes `resolve.py` as a **subprocess** over temp JSON files.

## Build

The venv is built on demand by `ne.envbuild.ensure_sidecar_venv` (filelock-serialised,
atomic marker, AIR_GAP `pre_lock_check` hook). To build it manually / verify:

```
uv venv packages/ne-brain-graph/.splink-venv --python 3.12
uv pip install --python packages/ne-brain-graph/.splink-venv/bin/python \
    --require-hashes -r packages/ne-brain-graph/splink_env/requirements.txt
```

## Writable venv root for hardened deploys (`NE_SIDECAR_VENV_ROOT`)

The ne-app production image installs into a **root-owned, read-only** site-packages
tree and runs as a non-root `USER 10001`, so the in-package default venv location
(`packages/ne-brain-graph/.splink-venv`) is **unwritable at runtime** — a runtime
(re)build would fail. Set **`NE_SIDECAR_VENV_ROOT`** to a writable mount and the
sidecar resolves its venv to `$NE_SIDECAR_VENV_ROOT/.splink-venv` instead.

Resolution rule (NE-SVENV): when `NE_SIDECAR_VENV_ROOT` is **set-and-nonempty** the
venv is `$NE_SIDECAR_VENV_ROOT/.splink-venv`; otherwise it is the in-package sibling
above (default byte-identical). An explicit `venv_dir=` constructor kwarg always
wins over both.

Pre-build at the resolved path (mirror the recipe above, at the writable root):

```
uv venv "$NE_SIDECAR_VENV_ROOT/.splink-venv" --python 3.12
uv pip install --python "$NE_SIDECAR_VENV_ROOT/.splink-venv/bin/python" \
    --require-hashes -r packages/ne-brain-graph/splink_env/requirements.txt
```

**MUST MATCH:** the runtime's `NE_SIDECAR_VENV_ROOT` must equal the root the venv was
pre-built under. If they differ, an AIR_GAP deploy **fail-closes** on the "missing"
venv (C2) and a non-air-gap deploy attempts an unwanted `uv pip install`
rebuild/egress.

## Compile the lock (NEVER hand-edit `requirements.txt`)

```
uv pip compile packages/ne-brain-graph/splink_env/requirements.in --universal \
    --python-version 3.12 --generate-hashes \
    -o packages/ne-brain-graph/splink_env/requirements.txt
```

`AIR_GAP=1` makes `ensure_sidecar_venv` fail closed (no runtime egress/install) via
the `pre_lock_check` hook; the sidecar performs zero egress at resolve time (DuckDB
is in-process over the exported attribute frames).
