# ne-serve

The vLLM serving profile for the **private-vllm** lane (SPEC §3.1/§3.3/§5
NE-013): it makes the open-weight models in the NE-011 registry servable
in-VPC. Ships a compose serving profile, a thin OpenAI-compatible client with
xgrammar-backed structured output, air-gap weight pre-staging tooling, and the
servable model menu.

## Architecture — ADOPT-VIA-ISOLATION (SPEC §11)

vLLM is an **external container service, never a platform Python dependency**.
Its closure (`vllm → torch → proprietary NVIDIA CUDA wheels`) can never classify
FREE under SPEC §2 C4 — the same finding that isolated Docling/RAGAS in a
separate venv. So `ne-serve` adds **zero** vllm/torch/nvidia to `uv.lock`
(grep-verified 0). What it ships instead:

- **A compose serving profile** — `deploy/compose/serve/compose.yaml`, an
  additive file with `gpu` and `cpu` profiles running vLLM's OpenAI-compatible
  server (images pinned tag@digest). The base dev stack is untouched.
- **A thin client** — `ServeClient`, plain `httpx` (already a dep), speaking
  only the OpenAI-compatible HTTP surface.
- **Air-gap tooling** — `download_weights` / `offline_env` / the
  `python -m ne.serve download` CLI, using `huggingface-hub` (already in the
  lock at 1.22.0).
- **The model menu** — `MODEL_SOURCES`, kept consistent with the NE-011 registry
  by a test.

## C3 accounting (why this is not a gateway bypass)

vLLM is an **in-VPC inference service**: EXEMPT from gateway ROUTING but
registry-tagged by NE-011 (the menu) and AIR_GAP-accounted by NE-012 (the
egress/SSRF guard). `ne-serve` stands up the service the gateway's
`private-vllm` lane routes to — it is never a bypass. `ne.serve` imports no
provider SDK / litellm / openai (the root C3 import-linter contract lists
`ne.serve` as a source module); `ne-gateway` is a **test-only** dev-group dep
(the menu-consistency test), never a runtime import.

### C2 endpoint validation (construction-time, fail-closed)

`ServeClient` validates its config-supplied `base_url` **at construction** via
`ne.serve.sovereignty.validate_in_vpc_endpoint` (SSRF shape + in-VPC/allowlist).
A metadata IP (`169.254.169.254`), a packed-integer IPv4 form (`2852039166`),
embedded credentials, a query/fragment, or any host that is not provably in-VPC
raises `UnsafeEndpointError` **before any request is issued** — a
compromised/misauthored endpoint never receives a byte. This closes the ne-serve
half of the audit corpus-exfil finding (the ne-brain half is NE-CKC2). Private
DNS reachable only inside the VPC is added via the escape hatch
`ServeSettings.allowed_endpoint_hosts` (env `NE_SERVE_ALLOWED_ENDPOINT_HOSTS`,
JSON list). The structural guarantee is locked by `tests/test_c2_egress_guardian.py`
(no module but `client.py` imports a raw HTTP client; every `self._base_url`
assignment is a `validate_in_vpc_endpoint(...)` call; hostile endpoints emit zero
requests).

The validator is a **package-local mirror**, not `from ne.gateway.ssrf import ...`:
importing `ne.gateway` executes its `__init__` → `ne.gateway.cost` →
`ne.gateway._litellm` → `litellm`, which `test_import_pulls_no_provider_sdk`
forbids at runtime and the C3 import-linter contract bans (ne-gateway stays a
test-only dev dep). `tests/test_sovereignty_parity.py` is a tests-only tripwire
that asserts every vector the canonical `ne.gateway.ssrf` rejects is also rejected
by the mirror (one-directional superset — ne-serve's Phase B is stricter). A
future unit may hoist a single shared validator into a litellm-free package.

## Client usage

```python
import asyncio
from ne.serve import ServeClient

async def main() -> None:
    client = ServeClient("http://localhost:8090", model="qwen3.6-35b-a3b")
    await client.health()
    print(await client.models())

    # Structured output (current vLLM surface; strict json_schema):
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "tier": {"type": "integer"}},
        "required": ["name", "tier"],
    }
    result = await client.generate_json(
        [{"role": "user", "content": "Return a name and tier."}],
        schema=schema,
    )
    print(result)  # -> {"name": ..., "tier": ...}
    await client.aclose()

asyncio.run(main())
```

## Structured-output surface (API drift)

Use `response_format={"type":"json_schema","json_schema":{...,"strict":true}}`.
vLLM **removed** the legacy `guided_json` / `guided_regex` / `guided_choice` /
`guided_grammar` request fields and the `--guided-decoding-backend` server flag
in **v0.12.0**; the server backend flag is now
`--structured-outputs-config.backend xgrammar`. `ServeClient.generate_json`
emits only the current surface, and both a below-seam test and the CI fake
server (rejects `guided_json` with 400) lock that in.

## Air-gap download tooling

```sh
# Pre-stage weights to a local path; writes a v2 manifest.json alongside.
python -m ne.serve download Qwen/Qwen3-0.6B --dest ./models

# Re-check staged weights against the manifest (integrity at the air-gap boundary).
python -m ne.serve verify Qwen/Qwen3-0.6B --dest ./models
```

Then serve with `HF_HUB_OFFLINE=1` (set by the compose profiles) for **zero
runtime egress** — the C2/AIR_GAP sovereignty story. `offline_env()` returns the
exact env block; `vllm_serve_args(local_path, served_name)` builds the argv
pointing vLLM at the local weights and **binds `127.0.0.1` by default** (`host=`
parameter to expose beyond the host; mirrors the NE-013 compose loopback default
`NE_VLLM_BIND_ADDR`).

**Manifest v2 — "which weights, exactly".** The manifest records the **resolved
immutable commit SHA** (`repo_info(...).sha`, into which `snapshot_download` is
then pinned — closing the resolve/download race) and a **per-file sha256** of the
actually-staged files, enumerated from the repo's own sibling list (never an
`rglob` that could absorb stragglers from a prior staging). This extends
digest-pinning (C11) to the one artifact class the lockfile can't cover.
`verify_weights` / `python -m ne.serve verify` re-hash the staged bytes and
fail-closed on a tampered, extra, or missing file (or a pre-v2 manifest) — the
one place C11 digest-pinning otherwise breaks: **the air-gap boundary**.

## TEI hub-cache pre-staging (NE-TEI-ONNX)

`stage-tei` builds the EXACT hub-cache tree TEI's hf-hub 0.4.2 client reads
(cache-first `get()`), optionally injecting a sha256-verified, self-exported
fast-ort ONNX pair so TEI boots its ort backend instead of the slow candle CPU
fallback (17-64s/embed):

```sh
# Official snapshot only (safetensors; TEI stays on the candle fallback).
python -m ne.serve stage-tei Qwen/Qwen3-Embedding-0.6B --dest ./tei-cache

# + the pinned, parity-gated ONNX artifact (see tools/model-export) — TEI
# boots the fast ort backend. --export-manifest is REQUIRED with
# --onnx-artifact: it is the C11 authority the artifact's bytes are verified
# against BEFORE anything is staged.
python -m ne.serve stage-tei Qwen/Qwen3-Embedding-0.6B --dest ./tei-cache \
    --onnx-artifact tools/model-export/dist/qwen3-embedding-0.6b-onnx-fp32-97b0c61.tar.gz \
    --export-manifest tools/model-export/manifests/qwen3-embedding-0.6b-onnx-fp32.json

# Re-check the staged tree at the air-gap boundary.
python -m ne.serve stage-tei-verify --dest ./tei-cache
```

Layout built under `--dest`:

```
models--Qwen--Qwen3-Embedding-0.6B/
  refs/main                      # 40-hex resolved sha, NO trailing newline (byte-exact)
  snapshots/<resolved-sha>/
    config.json  tokenizer.json  ...      # the repo's own official siblings
    model.safetensors                     # kept by default (--skip-safetensors omits it)
    onnx/model.onnx  onnx/model.onnx_data # only when --onnx-artifact is given
  ne-stage-manifest.json         # manifest-v2-style: full-tree sha256 coverage
```

Reuses `ne.serve.download`'s `_sha256_file`/`_is_resolved_sha`/
`WeightIntegrityError` and the same resolve-then-pin-then-enumerate-from-
siblings conventions as `download_weights`/`verify_weights`. **Idempotent**:
re-running `stage-tei` against an already-clean, same-revision tree (with the
onnx pair already present if requested) is a no-op — no download, no
subprocess. `stage-tei-verify` re-checks tamper/missing/extra files AND
`refs/main` formatting (40-hex, no trailing newline, matches the manifest's
resolved revision) across every staged `models--*` dir.

**Per-tier delivery** (the pinned compose files stay byte-untouched — the ONNX
is injected into the cache volume, never the image): `ne demo up` stages
`<workdir>/tei-cache` and bind-mounts it over the tei-embed cache in the
runtime compose overlay (`tools/ne-cli/src/ne/cli/demo/live.py`); the pilot/
air-gap operator flow stages on a staging host then copies into the
`tei-embed-cache` volume/PVC using the already-pinned TEI image as the copy
container (see `deploy/compose/pilot/README.md`); no-egress CI stages before
the tei-embed prewarm and asserts the ORT backend started (see
`.github/workflows/no-egress.yml`).

### Auth posture (live-parked doc item)

vLLM's OpenAI-compatible server ships **no authentication**. The C2 controls are
the layers around it, not vLLM itself: the ne-gateway routing seam (the only
sanctioned path to the `private-vllm` lane), the compose loopback bind
(`127.0.0.1` by default), and the deployment network policy (in-VPC only). A
real API-key/mTLS front for the server is live-parked pending GPU provisioning
(NE-000); see the ledger below.

## CI story

The serving path (`client → server → guided JSON`) is CI-testable **without a
GPU** via an in-process fake OpenAI-compatible server (stdlib `http.server`;
`tests/conftest.py`). Real-GPU health + generation + guided-JSON + a 4-way
concurrent throughput smoke are **live-parked** in `tests/test_vllm_live.py`
(`NE_VLLM_URL`-gated skip). GPU provisioning is NE-000 (owner-deferred).

## Live-verification ledger (parked)

The build host had the Docker CLI but no running daemon and no GPU, so live
bring-up was not exercised. Parked for a follow-up pass:

- Real-GPU `--profile gpu up` → `/health` 200, `/v1/models`, small completion,
  guided JSON, 4-way concurrent throughput smoke (`test_vllm_live.py`).
- CPU `--profile cpu up` with pre-staged `Qwen/Qwen3-0.6B` (NE-073 no-egress).
- Real `python -m ne.serve download Qwen/Qwen3-0.6B` against huggingface.co
  resolving a genuine commit SHA + a live `python -m ne.serve verify` roundtrip
  (the injectable `repo_info`/`snapshot_download` keep CI hermetic; the network
  path is parked per fixture-first policy).
- **Auth front for the vLLM server** — vLLM ships no auth; the gateway routing
  seam + compose loopback bind + network policy are the current C2 controls. A
  dedicated API-key/mTLS proxy is parked pending GPU provisioning (NE-000).

Static validation is green:
`docker compose -f deploy/compose/serve/compose.yaml --profile gpu --profile cpu config`
exits 0; the pytest suite passes.

## Drift flagged for the spec owner

Two vLLM API changes vs the spec-time snapshot, folded into this build:

1. `guided_json` / `guided_*` request fields **removed in v0.12.0** → use
   `response_format` json_schema.
2. Server flag renamed → `--structured-outputs-config.backend xgrammar` (old
   `--guided-decoding-backend` gone).
3. Pinned vLLM **v0.24.0** (spec-time snapshot referenced an older line).
