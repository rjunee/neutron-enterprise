# Docling parser env — isolated hash-pinned venv payload

This directory is the **isolated, hash-pinned virtualenv payload** for the NE-020
PARSE stage (`ne.brain.ingest.docling_parser.DoclingParser`). It mirrors the
isolation pattern `packages/ne-govern/presidio_env/` uses for presidio: docling's
heavy **torch / proprietary NVIDIA-CUDA-runtime** closure is installed into a
dedicated venv (`packages/ne-brain/.parser-venv`, git-ignored) and driven as a
subprocess, so it **never enters the platform `uv.lock`/SBOM** and never trips the
C4 license gate (SPEC §2 C4 / §11). The platform package imports nothing from
docling.

- `requirements.in` — the top-level docling pins.
- `requirements.txt` — fully resolved, `--generate-hashes`, installed with
  `--require-hashes`. Regenerate with:

  ```
  uv pip compile packages/ne-brain/parser_env/requirements.in --python-platform linux \
      --python-version 3.12 --generate-hashes \
      -o packages/ne-brain/parser_env/requirements.txt
  ```

  The lock is **linux-scoped, not `--universal`** (2026-09-02): the deploy
  surface is linux only (`deploy/docker/ne-app` stages this payload at
  `/opt/ne/parser_env`), and upstream docling-core / docling-ibm-models cap
  `transformers <5.9.0` on `sys_platform == "darwin"` only — a universal
  resolve could never reach the CVE-2026-9856 fixed floor
  (`transformers >= 5.10.0`). Mac-local sidecar builds are no longer
  guaranteed by this lock.

  **Never hand-edit `requirements.txt`.**
- `parse_doc.py` — runs INSIDE the venv over an `--input`/`--output` JSON-file
  protocol.

The platform-side driver is `ne.brain.ingest.docling_parser` (imports nothing from
docling).

The child process always sets `HF_HUB_OFFLINE=1` before importing Docling. Source
connector egress does not authorize implicit model downloads. Stage Docling's
PDF layout artifacts in the runtime cache when full OCR/layout processing is
required. If those artifacts are absent, a text-bearing PDF uses the pinned
`pypdfium2` embedded-text fallback; a scanned/text-free PDF fails closed.

## Locating the payload + venv (dev checkout vs installed wheel)

The default resolution walks up from the driver module for a directory containing
`uv.lock` (the monorepo dev checkout — CI, `uv run`, the OCI-image workspace copy).
That walk-up is **unavailable** once `ne-brain` is installed as a standalone wheel
outside a monorepo checkout. For that shape, supply an explicit override — either
constructor kwargs (`parser_env_dir=`/`venv_dir=`) or the **`NE_PARSER_ENV_DIR`** /
**`NE_PARSER_VENV_DIR`** environment variables — pointing at a vendored copy of
this `parser_env/` payload and a writable venv location.

## Build

The venv is built on demand by `ne.envbuild.ensure_sidecar_venv` (filelock-
serialised, atomic marker, AIR_GAP `pre_lock_check` hook). To build it manually /
verify:

```
uv venv packages/ne-brain/.parser-venv --python 3.12
uv pip install --python packages/ne-brain/.parser-venv/bin/python \
    --require-hashes -r packages/ne-brain/parser_env/requirements.txt
```

`AIR_GAP=1` makes `ensure_sidecar_venv` fail closed (no runtime egress/install) via
the `pre_lock_check` hook — the venv build needs network, so under air-gap the
sanctioned path is a pre-built venv (build it at image-build/pre-staging time).

## Writable venv root for hardened deploys (`NE_SIDECAR_VENV_ROOT`)

The ne-app production image installs into a **root-owned, read-only** site-packages
tree and runs as a non-root `USER 10001`, so the in-package default venv location
(`packages/ne-brain/.parser-venv`) is **unwritable at runtime**. Set
**`NE_SIDECAR_VENV_ROOT`** to a writable mount and the parser resolves its venv to
`$NE_SIDECAR_VENV_ROOT/.parser-venv` instead.

Resolution rule (NE-SVENV): when `NE_SIDECAR_VENV_ROOT` is **set-and-nonempty** the
venv is `$NE_SIDECAR_VENV_ROOT/.parser-venv`; otherwise it is the in-package sibling
above (default byte-identical). Precedence: an explicit `venv_dir=` constructor
kwarg, the `root=` kwarg, and the **per-sidecar `NE_PARSER_VENV_DIR`** env var all
win over the generic `NE_SIDECAR_VENV_ROOT` (specific beats generic).

Pre-build at the resolved path (mirror the recipe above, at the writable root):

```
uv venv "$NE_SIDECAR_VENV_ROOT/.parser-venv" --python 3.12
uv pip install --python "$NE_SIDECAR_VENV_ROOT/.parser-venv/bin/python" \
    --require-hashes -r packages/ne-brain/parser_env/requirements.txt
```

**MUST MATCH:** the runtime's `NE_SIDECAR_VENV_ROOT` must equal the root the venv was
pre-built under. If they differ, an AIR_GAP deploy **fail-closes** on the "missing"
venv (C2) and a non-air-gap deploy attempts an unwanted `uv pip install`
rebuild/egress.
