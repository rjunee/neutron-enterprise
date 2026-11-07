# ne-model-export — isolated build-time ONNX export + parity gate

`tools/model-export` is a **build-time-only** tool (NE-TEI-ONNX): it exports a
fp32 ONNX graph for `Qwen/Qwen3-Embedding-0.6B` from the official Apache-2.0
checkpoint, gates it against a safetensors reference embedder, and — only on a
PASS — emits a committed export-manifest + committed golden vectors + a
`.tar.gz` artifact.

## Two-phase split

This package (`ne-model-export`) is **dep-less**: its `pyproject.toml`
declares exactly one dependency, `ne-envbuild` (the shared sidecar-venv
builder — see `tests/test_env_policy.py`, the permanent C4/C11 isolation
guardian). It contains ALL of the tool's logic — specs, manifest, goldens,
tarball, CLI orchestration — and is fully unit-tested with synthetic fixtures
and injected runners, no real venv/subprocess/network anywhere in
`tests/`.

The actual heavy lifting — downloading the checkpoint, running the
`optimum-onnx` exporter, computing embeddings with `torch`/
`sentence-transformers`, running the exported graph under `onnxruntime` —
happens in `export_env/export_and_gate.py`, which runs **only** inside the
isolated, hash-pinned sidecar venv (`tools/model-export/.export-venv`, git-
ignored, built on demand by `ne.envbuild.ensure_sidecar_venv`). This mirrors
`packages/ne-govern/presidio_env` / `packages/ne-brain/parser_env` exactly:
the platform-side package never imports torch/optimum/onnxruntime/
transformers/sentence-transformers — it drives them as a subprocess over a
JSON-file protocol (`build-report.json`).

**The real export run is an ORCHESTRATOR post-merge step, not part of the
unit that ships this tool.** This build ships: the tool, its fixture-based
unit tests, the committed golden **texts** (`golden/*.texts.json`), and the
anti-vacuous CI wiring that fails loud (never silently skips) if the
downstream artifact is selected while unpublished. `manifests/*.json` and the
golden **vectors** file (`golden/*.json`, no `.texts.` infix) are deliberately
**absent** until that post-merge run commits them.

## Usage (the real, heavy run)

```
uv run --project tools/model-export ne-model-export export qwen3-embedding-0.6b
```

This:

1. Fail-closed refuses under `AIR_GAP=1` — this tool requires egress
   (pypi.org to build the sidecar venv, huggingface.co to download the
   checkpoint) and must never run inside a sealed deployment.
2. Builds/refreshes the isolated `export_env` sidecar venv (skip-if-current,
   hash-pinned install).
3. Runs `export_env/export_and_gate.py` in that venv: downloads the pinned
   revision, exports to ONNX (task `feature-extraction`, fp32, external
   data), computes the safetensors reference embeddings for the 16 committed
   golden texts, re-embeds them through the exported ONNX graph replicating
   TEI's own last-token-pool + L2-normalize semantics, and enforces per-pair
   cosine ≥ the spec's threshold (`0.9999` for Qwen3-Embedding-0.6B) — the
   **truth gate**. A failing gate writes a `build-report.json` documenting
   the failure and exits non-zero; nothing downstream runs.
4. On a PASS: packs `onnx/model.onnx` + `onnx/model.onnx_data` + the CORE
   export-manifest into a deterministic `.tar.gz`
   (`tools/model-export/dist/<artifact_filename>`), then writes the FULL
   manifest (core + an `artifact` section: filename/sha256/size) and the golden
   vectors NEXT TO the artifact (`export-manifest.json` / `golden-vectors.json`
   in the out dir — the files a cache entry needs) and, for the committed
   `export` run, ALSO to the COMMITTED paths
   (`tools/model-export/manifests/…json`, `tools/model-export/golden/…json`).

The committed manifest is the C11 authority: every consumer (`ne-serve
stage-tei`) verifies the artifact's bytes against it before staging — the
artifact path is convenience, the manifest is the source of truth. NE-TEI-GEN
(owner decision 2026-07-19): the artifact is NOT hosted anywhere; it is
generated on demand + cached locally by egress-OK consumers, and carried in the
sealed bundle for air-gap deploys.

## Generate-on-demand + local cache (NE-TEI-GEN)

`ne-serve stage-tei` never fetches a hosted asset. On an egress-OK host it
resolves the ONNX pair through this tool's cache:

```
uv run --project tools/model-export ne-model-export ensure-cached qwen3-embedding-0.6b
uv run --project tools/model-export ne-model-export cache-info    qwen3-embedding-0.6b
```

- `ensure-cached` probes the cache and, on a MISS, runs the same parity-gated
  export (above) INTO the cache (atomically), then prints one machine-readable
  JSON object as its last stdout line (`{model_key, repo_id, revision,
  cache_dir, artifact, manifest, hit}`); all human progress goes to stderr.
  Under `AIR_GAP=1` a cache MISS **fail-closes** (exit 1, no side effects) —
  a sealed deploy must supply the pre-staged bundle artifact + export manifest.
  `cache-info` is a read-only probe (never generates, never fails on a miss).
- **Cache layout** (keyed by repo + resolved revision, so a `specs.py` revision
  bump auto-regenerates):

  ```
  $NE_TEI_ONNX_CACHE/<org>--<name>@<revision>/
    <artifact_filename>.tar.gz
    export-manifest.json      # the C11 pin/hash authority (verified on every probe)
    golden-vectors.json
  ```

- **`NE_TEI_ONNX_CACHE`** overrides the cache root (default
  `~/.cache/neutron-enterprise/tei-onnx/`; an empty value counts as unset).
- **`NE_SIDECAR_VENV_ROOT`** (shared with the other sidecars) relocates the
  isolated `.export-venv` build to a writable mount for hardened/read-only-
  rootfs deploys.

## Parity-gate contract

- Threshold: per-pair cosine similarity between the fp32-ONNX embedding and
  the safetensors-reference embedding must be **≥ 0.9999** across all 16
  golden texts (build-time gate, `export_env/export_and_gate.py`). A serving-
  time smoke test (`deploy/airgap/tests/test_airgap_phase_b1.py::
  test_tei_embed_parity_smoke`) re-checks the LIVE TEI `/embed` endpoint
  against the same goldens with headroom for ort-vs-torch numeric noise
  (**≥ 0.999**).
- A failing gate emits **nothing** downstream — no artifact, no manifest, no
  golden vectors (`tools/model-export/tests/test_cli.py::
  test_parity_failure_emits_no_artifact_manifest_or_goldens` is the permanent
  proof).
- A resolved revision that drifts from the pinned `ModelSpec.revision` is
  ALSO refused (the checkpoint moved out from under a pinned rev) — never
  silently re-pinned.

## Owner-gated fallback (do NOT auto-take)

`onnx-community/Qwen3-Embedding-0.6B-ONNX` is a community export that proves
the graph runs, but its export repo declares **no license**. It is **never**
pinned or shipped as the served artifact. If the pinned self-export ever
fails despite the known-good toolchain versions, that is an **owner sign-off
event** — stop and escalate; do not silently fall back to the undeclared-
license artifact. `export_and_gate.py`'s optional `--cross-check-community`
flag is informational only (flags upstream drift) and gates nothing.

## Sidecar env (`export_env/`)

- `requirements.in` — the four orchestrator-approved top-level pins
  (`optimum-onnx`, `onnxruntime`, `torch`, `sentence-transformers`).
- `requirements.txt` — fully resolved, `--generate-hashes`, installed with
  `--require-hashes`. Regenerate with:

  ```
  uv pip compile tools/model-export/export_env/requirements.in --universal \
      --python-version 3.12 --generate-hashes \
      -o tools/model-export/export_env/requirements.txt
  ```

  **Never hand-edit `requirements.txt`.**
- `export_and_gate.py` — the in-venv worker (see "Two-phase split" above).

See `NOTICE` for the sidecar-only third-party component list (never entering
the platform lock/SBOM — `tests/test_env_policy.py` is the permanent proof).
