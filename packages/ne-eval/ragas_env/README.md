# ragas cross-validation oracle — isolated dev venv

This directory is the **isolated, hash-pinned virtualenv payload** for a
**dev/test-only** RAGAS cross-validation oracle. It mirrors the isolation pattern
`packages/ne-brain/parser_env/` uses for docling: the heavy dependency closure is
installed into a dedicated venv (`packages/ne-eval/.ragas-venv`, git-ignored) and
run as a subprocess, so it **never enters the platform `uv.lock`/SBOM** and never
trips the C4 license gate.

- `requirements.in` — the top-level `ragas==0.4.3` pin plus the langchain **1.x**
  floor and the `langchain-community==0.4.1` pin described below.
- `requirements.txt` — fully resolved, `--generate-hashes`, installed with
  `--require-hashes`. Regenerate with:

  ```
  uv pip compile packages/ne-eval/ragas_env/requirements.in --universal \
      --python-version 3.12 --generate-hashes \
      -o packages/ne-eval/ragas_env/requirements.txt
  ```

- `cross_check.py` — runs INSIDE the venv (see below).

The platform-side driver is `ne.eval.ragas_oracle` (imports nothing from ragas);
the test is `packages/ne-eval/tests/test_ragas_oracle.py` (skip-if-unavailable).

## Why the langchain 1.x floor and the langchain-community 0.4.1 pin

This env used to pin the langchain stack to the **0.3 series**. That series' 
`langchain-core 0.3.86` is affected by **two HIGH advisories** (per OSV/GHSA,
which is authoritative):

- **CVE-2026-34070** (GHSA-qh6h-p6c9-ff54) — path traversal in `load_prompt`,
  fixed in `langchain-core >=1.2.22`.
- **CVE-2026-26013** (GHSA-2g6r-c272-w58r) — fixed in `langchain-core >=1.2.11`.

Both remediations land only on the langchain **1.x** line, so `requirements.in`
now floors the whole stack at 1.x (`langchain-core>=1.2.22,<2`, resolved 1.4.8).
This is a **full version bump**, not a suppression.

`langchain-community` is **pinned `==0.4.1`** (not floated) for a specific reason:
ragas 0.4.3 is still the newest ragas release (checked 2026-07-06 — PyPI **and**
`vibrantlabsai/ragas`; there is no langchain-1.x-aware newer ragas), and it
**eagerly imports** `langchain_community.chat_models.vertexai`
(`src/ragas/llms/base.py:12`) at package-import time. `langchain-community 0.4.2`
**removed** that module (`chat_models/vertexai.py` deleted upstream), so `import
ragas` would crash on 0.4.2. `0.4.1` is the newest community release that both
supports `langchain-core` 1.x **and** still ships the `vertexai` module.

**Unpin trigger:** a ragas release that drops the eager `vertexai` import — then
`langchain-community` can float again (and this pin can be removed).

## The cross-check seam (no score arithmetic of our own)

`cross_check.py` is fed the SAME fixed judge verdicts our native
`ne.eval.metrics` score, and returns faithfulness / context_precision /
context_recall computed by **ragas's own aggregation code**:

| metric            | ragas code invoked                                                     |
| ----------------- | ---------------------------------------------------------------------- |
| faithfulness      | `Faithfulness._compute_score(NLIStatementOutput(...))`                 |
| context_precision | `LLMContextPrecisionWithReference._calculate_average_precision([...])` |
| context_recall    | `LLMContextRecall._compute_score([ContextRecallClassification, ...])`  |

We use the **metric-internal compute** seam (the NE-025 brief's authorized
fallback): the fixed verdicts are wrapped in ragas's own parsed-output objects and
handed to ragas's score methods. `cross_check.py` contains **no score arithmetic
of its own** — it only marshals verdicts into ragas objects and reads back the
numbers. `np.nan` (ragas's degenerate-input sentinel) is mapped to `0.0`, matching
our native metric's degenerate-case return; that mapping is null-handling, not
scoring.

This proves "our metrics are RAGAS-validated" for the **metric arithmetic**
(the verdict → score aggregation), which is the part SPEC §11 keeps native. It does
NOT (and need not) revalidate the LLM judging itself.

## Monitored fact: repository ownership drift

The ragas repository moved `explodinggradients/ragas` →
`vibrantlabsai/ragas`. This is an **ownership** change only; the license is
**unchanged (Apache-2.0)**. Recorded here as a monitored fact for the dependency
review — benign for a dev-only, version-pinned, isolated venv.

## Escape hatch

Set `NE_SKIP_RAGAS_ORACLE=1` to skip the oracle test fast (e.g. on a network-less
runner where the venv cannot be built). The test also skips automatically if the
venv build fails for any reason. **Exception (NE-CKC6):** when
`NE_REQUIRE_PUBLIC_BASELINE=1` (the CI fail-closed Checkpoint-C gate),
`NE_SKIP_RAGAS_ORACLE=1` is refused as a contradictory config and the test fails
loudly rather than skipping — an armed gate that skips would defeat its purpose.
