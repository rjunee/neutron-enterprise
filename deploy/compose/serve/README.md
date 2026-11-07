# deploy/compose/serve — vLLM serving profile (NE-013)

The vLLM OpenAI-compatible serving profile for the **private-vllm** lane
(SPEC §3.1/§3.3/§5). This is a **separate, additive** compose file — the base
dev stack in `deploy/compose/dev/compose.yaml` is untouched (and frozen by
`tools/ne-cli/tests/test_dev_stack.py`). Two profiles gate two services:

| Profile | Service | Host port | Image | Use |
|---|---|---|---|---|
| `gpu` | `vllm` | `8090` | `vllm/vllm-openai:v0.24.0` | Real GPU serving of a menu model |
| `cpu` | `vllm-cpu` | `8091` | `vllm/vllm-openai-cpu:v0.24.0` | GPU-free tiny-model bring-up (CI / NE-073 no-egress harness) |

Both services are considered healthy only after the engine returns exactly one
generated token from a deterministic, bounded `/v1/completions` request. A bare
`/health` response is intentionally insufficient: it proves the API front process,
not generation readiness. Successful completion probes are cached for 60 seconds,
so the 30-second health poll consumes at most one token per minute.

Nothing is exposed unless you pass `--profile gpu` and/or `--profile cpu`.

## ADOPT-VIA-ISOLATION (why vLLM is not a Python dep)

vLLM is consumed **only** as an external OCI container. Its dependency closure
(`vllm → torch → proprietary NVIDIA CUDA wheels`) can never classify FREE under
SPEC §2 C4, so it is never added to `uv.lock` (the same Docling finding that
kept the parser in an isolated venv). `packages/ne-serve` ships a thin `httpx`
client, the model menu, and air-gap tooling — and adds **zero** vllm/torch/
nvidia packages to the platform lock.

## In-VPC only (never expose the port)

Serve on a private network only. The gateway's `private-vllm` lane routes to
this server, and the NE-012 egress/SSRF validator guards the endpoints. Do NOT
publish `8090`/`8091` to the public internet.

Host port publishing binds **loopback (`127.0.0.1`) by default**, so the
unauthenticated OpenAI-compatible endpoint is never exposed on all interfaces
by accident. To serve the gateway across a private/VPC network, set
`NE_VLLM_BIND_ADDR` to the private interface address (never `0.0.0.0` on a
host with a public interface).

## Air-gap workflow (C2 — zero runtime egress)

Pre-stage weights once, then serve fully offline:

```sh
# 1. Pre-stage weights to a local path (writes a manifest.json alongside).
python -m ne.serve download Qwen/Qwen3-0.6B --dest ./models

# 2. Bring up a profile (weights are mounted read-only at /models).
NE_VLLM_WEIGHTS_ROOT=./models \
  docker compose -f deploy/compose/serve/compose.yaml --profile cpu up

# GPU equivalent (pre-stage a menu model first):
python -m ne.serve download Qwen/Qwen3.6-35B-A3B --dest ./models
NE_VLLM_WEIGHTS_ROOT=./models \
  docker compose -f deploy/compose/serve/compose.yaml --profile gpu up
```

Both services set `HF_HUB_OFFLINE=1` (plus the telemetry-off block below), so a
running container makes **no** call to huggingface.co: the pre-staged weights
under `/models` are the only source. This is the C2/AIR_GAP sovereignty story —
the private-vllm lane stays alive under AIR_GAP (NE-012's egress guard enforces
it; this profile supplies the sovereign serving side).

## Telemetry posture (C1: no phone-home)

Both services set, and the tests assert:

- `HF_HUB_OFFLINE=1`
- `HF_HUB_DISABLE_TELEMETRY=1`
- `VLLM_NO_USAGE_STATS=1`
- `DO_NOT_TRACK=1`

## Environment variables

| Var | Default | Applies to | Meaning |
|---|---|---|---|
| `NE_VLLM_WEIGHTS_ROOT` | `./models` | both | Host dir mounted read-only at `/models` |
| `NE_VLLM_MODEL_DIR` | `Qwen/Qwen3.6-35B-A3B` | gpu | Sub-path under `/models` for the GPU model |
| `NE_VLLM_SERVED_NAME` | `qwen3.6-35b-a3b` | gpu | `--served-model-name` for the GPU model |
| `NE_VLLM_CPU_MODEL_DIR` | `Qwen/Qwen3-0.6B` | cpu | Sub-path under `/models` for the CPU tiny model |
| `NE_VLLM_BIND_ADDR` | `127.0.0.1` | both | Host interface the published ports bind to (loopback by default; set a private/VPC address to serve the gateway lane) |

The CPU service also sets `VLLM_CPU_KVCACHE_SPACE=4` (GiB of CPU KV-cache).

## Structured output (xgrammar)

Both services pass `--structured-outputs-config.backend xgrammar`. Clients
request structured output with the **current** vLLM surface —
`response_format={"type":"json_schema","json_schema":{...,"strict":true}}`.

> **API drift (folded in):** vLLM **removed** the legacy `guided_json` /
> `guided_regex` / `guided_choice` / `guided_grammar` request fields and the
> `--guided-decoding-backend` server flag in **v0.12.0**. Do not emit them —
> `ne.serve.ServeClient.generate_json` uses `response_format` json_schema, and
> the CI fake server rejects any `guided_json` body with 400 to lock this in.

## Image provenance (verified 2026-07-05)

| Image | Tag | Digest | License | Notes |
|---|---|---|---|---|
| `vllm/vllm-openai` | `v0.24.0` | `sha256:251eba5cc7c12fed0b75da22a9240e582b1c9e39f6fbc064f86781b963bd814f` | Apache-2.0 | GPU server. Manifest-list digest (Docker Hub API, pushed 2026-06-29). Released 2026-06-30. |
| `vllm/vllm-openai-cpu` | `v0.24.0` | `sha256:b0e6f8604413a8bfcca8836bb4b57b47f1aa653957e07f8903ece531309981a1` | Apache-2.0 | Official CPU server. Multi-arch manifest-list digest. |

`xgrammar` (Apache-2.0, github.com/mlc-ai/xgrammar) ships **inside** the vLLM
image as its structured-output backend — it is not a Python dep of ours.

### Security (CVE / KEV)

- **CVE-2026-22778** (CVSS 9.8, RCE via a media URL) — fixed in vLLM **0.14.1**.
- **CVE-2026-24779** (SSRF bypass) — fixed in vLLM **0.15.1**.

Both are patched well below the pinned **v0.24.0**. No vLLM entry was found in
the CISA KEV catalog (searched 2026-07-05). Serve in-VPC only; the gateway SSRF
validator (NE-012) further guards endpoints.

## Hardware-tier guidance

Rough single-node bring-up guidance (from the research brief; tune to your
tensor-parallel and quantization choices):

- **JUDGMENT tier** (GLM-5.2, Qwen3.5-397B-A17B, DeepSeek-V4-Flash) — multi-GPU
  (e.g. 8×80GB) with tensor parallelism; the highest-capability menu.
- **BALANCED tier** (Qwen3.6-35B-A3B, Mistral-Large-3) — 1–4×80GB; the default
  everyday model (`qwen3.6-35b-a3b`).
- **CHEAP-FAST tier** (gpt-oss-120b) — single high-memory GPU / MoE-efficient.
- **CPU tiny model** (Qwen3-0.6B) — no GPU; CI / dev / no-egress harness only,
  never a production menu entry.

See `docs/model-menu.md` for the full menu and per-model provenance.

## Live-verification ledger (parked)

The build host had the Docker CLI present but **no running Docker daemon**, so
live container bring-up could not be exercised. Recorded as pending for a
follow-up pass with a live daemon + GPU (GPU provisioning is NE-000,
owner-deferred):

- **Parked (GPU):** `--profile gpu up` reaches healthy; `/health` 200;
  `/v1/models` lists the served name; a small completion returns non-empty; a
  4-way concurrent 64-token throughput smoke completes. Driven by
  `packages/ne-serve/tests/test_vllm_live.py` (`NE_VLLM_URL`-gated skip).
- **Parked (CPU):** `--profile cpu up` with pre-staged `Qwen/Qwen3-0.6B` reaches
  healthy and serves guided JSON with zero egress — the NE-073 no-egress
  harness.

Static validation **run and green** on the build host:
`docker compose -f deploy/compose/serve/compose.yaml --profile gpu --profile cpu config`
exits 0, and the enumerated pytest suite passes.
