# LightRAG graph-retrieval sidecar (library-only, out-of-process, hash-pinned)

LightRAG (`lightrag-hku==1.5.4`, MIT) is used **LIBRARY-ONLY** in an isolated,
hash-pinned sidecar venv. It is NOT a platform dependency: the whole closure
(`lightrag-hku` + `nano-vectordb` + `networkx` + `google-genai` + `pipmaster` +
`tiktoken` + …) lives ONLY in this venv and **never** enters the platform
`uv.lock`. This is proven by:

- `packages/ne-brain-graph/tests/test_no_gpl_sidecar_leak.py` (grep-provable: none
  of lightrag/lightrag-hku/pipmaster appear in `uv.lock`);
- the `t2-sidecars-out-of-process` import-linter contract (no `ne.*` module imports
  `lightrag`/`pipmaster`);
- `ne check licenses` (scans `uv.lock` only — stays green).

## The bundled API SERVER is BANNED

Only the library is installed and driven in-process **inside this sidecar** via
`project.py` / `query.py`. The bundled LightRAG **API server is BANNED** — it
carries three unpatched-in-1.5.4 advisories:

- **CVE-2026-30762** — authentication bypass;
- **CVE-2026-39413** — JWT confusion;
- **CVE-2025-6773** — path traversal.

The platform side (`ne.brain_graph.lightrag._lightrag.LightRagSidecar`) never
imports lightrag; it invokes `project.py` / `query.py` as **subprocesses** over
JSON files.

## `pipmaster` neutralized + zero egress (C2 / AIR_GAP)

LightRAG imports `pipmaster`, a runtime pip-installer. It is **neutralized by a
fully pre-installed pinned closure**: every dependency is already present, so
pipmaster's install path is never taken. Combined with `AIR_GAP=1`
(`ensure_sidecar_venv`'s `pre_lock_check` fails closed), the sidecar performs
**zero** runtime egress or installs. `T-LR-3` (parked live) asserts this.

## LLM calls are BANNED in-sidecar (C3)

- `embedding_func` is a **DETERMINISTIC local hash-embedding** defined in-script —
  zero network.
- `llm_model_func` **RAISES**; queries run with `only_need_context=True` and
  pre-supplied keywords, so the LLM is **never** invoked. The ban is provable: any
  code path that reaches the LLM crashes.

The retrieval security property does NOT rest on the sidecar: every candidate the
sidecar returns is **re-validated platform-side, trim-before-return**, against the
principal-scoped `EntityGraph` + the T1 retriever (C7). Unverifiable candidates are
dropped; verification errors PROPAGATE (deny-by-erroring, no silent fallback).

## Build

The venv is built on demand by `ne.envbuild.ensure_sidecar_venv` (filelock-
serialised, atomic marker, AIR_GAP `pre_lock_check` hook). To build / verify:

```
uv venv packages/ne-brain-graph/.lightrag-venv --python 3.12
uv pip install --python packages/ne-brain-graph/.lightrag-venv/bin/python \
    --require-hashes -r packages/ne-brain-graph/lightrag_env/requirements.txt
```

## Writable venv root for hardened deploys (`NE_SIDECAR_VENV_ROOT`)

The ne-app production image installs into a **root-owned, read-only** site-packages
tree and runs as a non-root `USER 10001`, so the in-package default locations
(`packages/ne-brain-graph/.lightrag-venv` and `.lightrag-workdir`) are **unwritable
at runtime**. Set **`NE_SIDECAR_VENV_ROOT`** to a writable mount and the sidecar
resolves BOTH its venv (`$NE_SIDECAR_VENV_ROOT/.lightrag-venv`) AND its persistent
workdir (`$NE_SIDECAR_VENV_ROOT/.lightrag-workdir`, written by `project.py`/`insert`)
under that root.

Resolution rule (NE-SVENV): when `NE_SIDECAR_VENV_ROOT` is **set-and-nonempty** the
venv/workdir live under it; otherwise they are the in-package siblings above (default
byte-identical). Explicit `venv_dir=`/`workdir=` constructor kwargs always win.

Pre-build at the resolved path (mirror the recipe above, at the writable root):

```
uv venv "$NE_SIDECAR_VENV_ROOT/.lightrag-venv" --python 3.12
uv pip install --python "$NE_SIDECAR_VENV_ROOT/.lightrag-venv/bin/python" \
    --require-hashes -r packages/ne-brain-graph/lightrag_env/requirements.txt
```

**MUST MATCH:** the runtime's `NE_SIDECAR_VENV_ROOT` must equal the root the venv was
pre-built under. If they differ, an AIR_GAP deploy **fail-closes** on the "missing"
venv (C2) and a non-air-gap deploy attempts an unwanted `uv pip install`
rebuild/egress.

## Compile the lock (NEVER hand-edit `requirements.txt`)

```
uv pip compile packages/ne-brain-graph/lightrag_env/requirements.in --universal \
    --python-version 3.12 --generate-hashes \
    -o packages/ne-brain-graph/lightrag_env/requirements.txt
```

## Data flow

- `project.py --input kg.json --workdir <dir>` reads an exported-graph custom-KG
  payload (entities/relations from the platform `LightRagProjector`) and calls
  `ainsert_custom_kg` into local file-based storages under `<dir>` (rebuildable
  from scratch by `ne-brain-graph rebuild-lightrag`).
- `query.py --query "..." --workdir <dir> --top-k N --output out.json` returns
  `{"entity_keys": [...], "chunk_ids": [...]}` — candidate node keys + chunk
  `source_id`s, obtained WITHOUT any LLM call. These are candidates ONLY; the
  platform re-validates them against the trimmed `EntityGraph` before use.
