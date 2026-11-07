# Servable open-weight model menu (NE-013)

The models the vLLM serving profile (`deploy/compose/serve/`) can host on the
**private-vllm** lane. This menu is **the same data** as the NE-011 registry
(`ne.gateway.menus.DEFAULT_MENUS`) and `ne.serve.models.MODEL_SOURCES` — a test
(`packages/ne-serve/tests/test_menu_docs_consistency.py`) enforces the
three-way consistency, so the first (`model_id`) column is machine-parsed and
must stay verbatim.

Every entry is **open-weight** and served **in-VPC** (never routed to a frontier
API). Origin tags let a customer filter by procurement constraint: the
`western` menu is the alternate offering for buyers who cannot procure
CN-origin weights regardless of license.

## Menu

| model_id | menu | provider | hf_repo | license | origin | context_window | hardware tier |
|---|---|---|---|---|---|---|---|
| glm-5.2 | capability-first | zai-org | zai-org/GLM-5.2 | MIT | CN | 1000000 | JUDGMENT |
| qwen3.5-397b-a17b | capability-first | Qwen | Qwen/Qwen3.5-397B-A17B | Apache-2.0 | CN | 262144 | JUDGMENT |
| qwen3.6-35b-a3b | capability-first | Qwen | Qwen/Qwen3.6-35B-A3B | Apache-2.0 | CN | 262144 | BALANCED |
| deepseek-v4-flash | capability-first | deepseek-ai | deepseek-ai/DeepSeek-V4-Flash | MIT | CN | 1000000 | JUDGMENT |
| mistral-large-3 | western | mistralai | mistralai/Mistral-Large-3-675B-Instruct-2512-BF16 | Apache-2.0 | FR | 256000 | BALANCED |
| gpt-oss-120b | western | openai | openai/gpt-oss-120b | Apache-2.0 | US | 131072 | CHEAP-FAST |

License/context-window figures are a spec-time snapshot (verified 2026-07-04 by
NE-011, HF repo ids re-verified 2026-07-05 by NE-013); re-check per SPEC §3.3
before a menu refresh. GLM-5.2's license is confirmed **MIT** via the
`zai-org/GLM-5.2` HuggingFace page. `mistralai/Mistral-Large-3` resolves (HF
redirect) to the dated canonical repo id shown above.

## CI / dev tiny model (NOT a menu entry)

A GPU-free tiny model for the CPU serving profile and CI. It is deliberately
**not** part of the servable menu above and never appears in the registry.

| model_id | hf_repo | license | origin | note |
|---|---|---|---|---|
| qwen3-0.6b | Qwen/Qwen3-0.6B | Apache-2.0 | CN | CPU-mode CI/dev tiny model — not a menu entry |

## Capability tiers

Guidance for task routing (the gateway maps tasks to models; this is the
menu-level intent):

- **JUDGMENT** — highest-capability reasoning/long-context models for hard
  judgment tasks: `glm-5.2`, `qwen3.5-397b-a17b`, `deepseek-v4-flash`.
- **BALANCED** — the everyday default with a strong quality/cost balance:
  `qwen3.6-35b-a3b` (the client default served name), `mistral-large-3`.
- **CHEAP-FAST** — fast, cheap, high-throughput drafting/classification:
  `gpt-oss-120b`.

## Hardware tiers

Rough single-node bring-up guidance (tune to tensor-parallel + quantization):

- **JUDGMENT** — multi-GPU (e.g. 8×80GB) with tensor parallelism.
- **BALANCED** — 1–4×80GB.
- **CHEAP-FAST** — single high-memory GPU / MoE-efficient.
- **CPU tiny** (`qwen3-0.6b`) — no GPU; CI / dev / NE-073 no-egress harness only.

## Air-gap pre-stage workflow

```sh
# Pre-stage a menu model's weights to a local path (writes manifest.json):
python -m ne.serve download Qwen/Qwen3.6-35B-A3B --dest ./models

# Then serve fully offline (HF_HUB_OFFLINE=1 => zero runtime egress):
NE_VLLM_WEIGHTS_ROOT=./models \
  docker compose -f deploy/compose/serve/compose.yaml --profile gpu up
```

## License denylist note

Only FREE-classified open-weight licenses (Apache-2.0, MIT here) are servable.
Non-commercial / weights-restricted licenses are **C4-BANNED** and never enter
the menu: e.g. **Llama 4** was rejected (its community license is not FREE), and
any model whose weights carry a non-commercial (NC) restriction is banned. The
gateway registry's `register()` fails loud on a banned license, citing C4.

## Source of record

- License/provenance verification procedure: **SPEC §3.3**.
- Per-model source snapshot and verification dates: `packages/ne-gateway/README.md`.
- Weight sources (`repo_id`): `ne.serve.models.MODEL_SOURCES`.
