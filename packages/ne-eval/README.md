# ne-eval

The evaluation harness for Neutron Enterprise: a JSONL golden-set library, four
retrieval/generation quality metrics implemented as a **native RAGAS methodology**
(not the RAGAS library), threshold specs with tolerance bands, a full pluggable
judge policy (versioned rubrics + judge-vs-human calibration + mandatory
cross-family judging), an append-only eval-run store stamped with `config_hash`,
and a pinned promptfoo CI-gate kit.

## Why native RAGAS methodology (SPEC §11)

The RAGAS *library* (Apache-2.0) is deliberately **not** a dependency. Its
transitive closure (langchain, the OpenAI SDK, `datasets`, `pillow`, `networkx`,
and `scipy` — the last a false-positive but still banned) trips the C4 license
gate and pulls a heavy ML closure into a package that only needs arithmetic over
judge verdicts. Per the SPEC §11 orchestrator decision this unit **reimplements
the metric algorithms natively** over the platform's own pluggable gateway judge.
The decision is reversible: the formulas are pinned by hand-computed tests, so a
future switch to the library (or another implementation) is a test-visible change,
not silent drift.

Nothing under `packages/ne-eval/src` imports `ragas`, `scipy`, `langchain`,
`openai`, `litellm`, `datasets`, or `numpy` — the only model seam is
`ne.contracts.models.ModelGateway`.

## Golden-set format

One canonical JSON object per line (`.jsonl`), git-diffable (stable sort by `id`,
sorted keys). See `goldensets/sample.jsonl`.

| field | type | notes |
|-------|------|-------|
| `id` | str | non-empty slug (`[A-Za-z0-9][A-Za-z0-9._-]*`), unique |
| `question` | str | non-empty |
| `reference_answer` | str | the gold answer |
| `relevant_document_ids` | list[str] | document ids that should be retrieved (default `[]`) |
| `tags` | list[str] | free-form labels, e.g. `permission-trap` (default `[]`) |
| `notes` | str \| null | optional annotation |

```json
{"id":"q-pto-policy","question":"How many PTO days does a new hire accrue per month?","reference_answer":"A new hire accrues 1.5 PTO days per month during their first year.","relevant_document_ids":["docB"],"tags":["hr","policy"]}
```

`golden_set_hash(gs)` is a SHA-256 over the canonicalized set (sorted by id) and
is recorded on every eval run for replayability.

## Metrics (exact formulas implemented)

| metric | judge? | formula |
|--------|--------|---------|
| `recall_at_10` | no (deterministic) | `|relevant ∩ docs(top-k)| / |relevant|`; items with no relevant docs are excluded from the mean |
| `faithfulness` | yes | decompose the answer into atomic statements, judge each supported/not by the contexts; `Σ supported / #statements` |
| `context_precision` | yes | one usefulness verdict `v_i` per ranked context; `Σ (precision@i · v_i) / Σ v_i`, `precision@i = (Σ_{j≤i} v_j)/i` |
| `context_recall` | yes | decompose the *reference* answer into statements (reuses the statements rubric), judge each attributable to the contexts; `Σ attributed / #statements` |

Every per-item judge fault is caught and scored `0.0` with an error string
(fail-closed); the run-level `judge_error_rate` (> `max_judge_error_rate`, default
0.2) is an independent kill-switch that fails the run.

## Thresholds and tolerance bands

Defaults (SPEC §5 NE-024 row):

| metric | threshold | tolerance |
|--------|-----------|-----------|
| `recall_at_10` | 0.85 | 0.02 |
| `faithfulness` | 0.80 | 0.03 |
| `context_precision` | 0.70 | 0.03 |
| `context_recall` | 0.80 | 0.03 |

Band semantics (absorb judge nondeterminism without flapping the gate):

- `value ≥ threshold` → **PASS**
- `threshold − tolerance ≤ value < threshold` → **WARN** (gate still passes)
- `value < threshold − tolerance` → **FAIL**
- metric absent from the computed metrics → **FAIL** (fail-closed)

The gate passes iff no metric is FAIL.

## Judge policy

- **Pluggable** — the `Judge` protocol + the `ne.plugins.eval_judge` entry point
  (`gateway` → `GatewayJudge`). The only model seam is the platform
  `ModelGateway`; ne-eval imports no provider SDK.
- **Cross-family judging is mandatory** — `enforce_cross_family(judge_family,
  evaluated_family)` refuses a same-family judge (no v1 override). Both families
  are config-declared (`NE_EVAL_JUDGE_FAMILY`, `NE_EVAL_EVALUATED_FAMILY`).
- **Versioned rubrics in git** — `src/ne/eval/rubrics/*.md` indexed by
  `rubrics.toml`; each rubric carries a `content_hash` (SHA-256 of the template
  bytes). **Any** change to a template requires a NEW version file (e.g.
  `_v2.md`), a `rubrics.toml` bump, and a calibration re-run.
- **Calibration lifecycle** — label → `run_calibration` → artifact → enforced
  hash match. `check_calibration` runs **before any scoring**: each judge-scored
  rubric must have an artifact matching `rubric_id`, `judge_model_id`, and
  `rubric_hash`. A missing artifact raises `CalibrationMissingError`; a
  different-judge or edited-rubric artifact raises `CalibrationStaleError`
  (fail-closed, no override). Human labels are JSONL rows
  `{item_id, variables, human_verdict, verdict_path?}`.
- **No-egress CI judge** — an open-weight model on the `PRIVATE_VLLM` lane, routed
  through the gateway task `eval-judge` (config default). SPEC §3.3 menu
  recommends Qwen (Apache-2.0) / GLM (MIT); the concrete serving pin lands with
  NE-013/NE-025. Only the config seam ships here.

## promptfoo CI gate

`promptfoo` is an **external, pinned npm CLI** (`promptfoo@0.121.17`, MIT) — never
a Python dependency, never in `uv.lock`. The gate config is **generated** from the
same `ThresholdSpec` values the suite uses, and each assertion binds at
`threshold − tolerance` (identical to `gate_passed`). `check_promptfoo_config`
is the anti-drift ratchet: the committed `promptfoo/promptfooconfig.json` must
byte-equal the generated config.

```bash
packages/ne-eval/promptfoo/run_gate.sh   # needs Node >= 22
```

The runner exports `PROMPTFOO_DISABLE_TELEMETRY=1` and `PROMPTFOO_DISABLE_UPDATE=1`
(C1 no phone-home) and invokes `npx -y promptfoo@0.121.17`. **Node ≥ 22** is
required (promptfoo drops Node 20 support 2026-07-30). Wiring `run_gate.sh` into a
CI workflow is a **root-lane** concern (NE-025 / orchestrator), not this package.
End-to-end live execution against a deployed retrieval stack + gateway is parked
for NE-025.

## Eval-run recording

Every run is persisted (append-only) to `ne_eval_runs` via `EvalRunStore`
(SQLite in tests, Postgres live) carrying `config_hash`, `golden_set_hash`, judge
identity, per-rubric versions + content hashes, metrics, thresholds, statuses,
`judge_error_rate`, and `passed`. There is no update/delete surface.

## CLI

```
ne-eval validate-golden <path>          # exit 0/1
ne-eval hash-golden <path>              # print the golden-set hash
ne-eval calibrate --rubric <id> --labels <jsonl> --out-dir <dir> [--scripted-fixture <json>]
ne-eval check-calibration --dir <dir> --rubric <id> [...] --judge-model <id>   # exit 0/1
ne-eval promptfoo-config --check [--path P] | --write [--path P]              # exit 1 on drift
ne-eval baseline --write --path <json> --suite S --golden-set-hash H \
    --judge-model M --metrics-json <run.json> --tolerance metric=val ...      # write ratchet
ne-eval baseline --check --path <json> --metrics-json <run.json>             # exit 1 on regression
ne-eval trend --db <sqlalchemy-url> --suite S [--limit N]                    # print run history
```

## Public-corpus retrieval baseline (Checkpoint C, NE-025)

`goldensets/public/` holds a committed **public** golden set derived from the
**SQuAD v1.1 dev set** (CC BY-SA 4.0 — redistributable with attribution; see
`goldensets/public/DATA-LICENSE.md` and the package `NOTICE`). `generate_squad_subset.py`
is the deterministic generator (pure function of the recorded-SHA-256 source);
`squad_corpus.jsonl` (159 docs) + `squad_golden.jsonl` (100 questions) are its
committed output, reproducible byte-for-byte.

`tests/test_retrieval_baseline_public.py` is the **Checkpoint-C threshold proof**:
it runs the WHOLE T1 stack — `StructuralChunker` → `QdrantChunkStore(':memory:')`
(HashEmbedder dense + FastEmbed BM25 sparse, RRF) → `HybridRetriever` →
`RetrievalPipeline(LexicalReranker)` → `KnowledgeReadEvalTarget` → `RagEvalSuite`
with the deterministic `LexicalJudge` — over the 100 questions and asserts the
SPEC gate (recall@10 ≥ .85, faithfulness ≥ .80, context_precision ≥ .70,
context_recall ≥ .80, tolerance bands). It is green in CI with **no external
server**.

### Deterministic-CI vs live-parked split

| Stage        | CI (deterministic, this proof)          | Live-parked counterpart              |
| ------------ | --------------------------------------- | ------------------------------------ |
| Vector store | `QdrantChunkStore(':memory:')`          | real Qdrant server (`NE_QDRANT_URL`) |
| Dense embed  | `HashEmbedder(64)`                      | `TEIEmbedder` (`NE_TEI_EMBED_URL`)   |
| Sparse embed | FastEmbed BM25 (real; HF asset fetch)   | FastEmbed BM25 (same)                |
| Rerank       | `LexicalReranker` (IDF lexical)         | `TEIReranker` (`NE_TEI_RERANK_URL`)  |
| Judge        | `LexicalJudge` (calibrated heuristic)   | vLLM gateway judge (NE-013)          |
| Answerer     | extractive `KnowledgeReadEvalTarget`    | generation model                     |

`tests/test_retrieval_baseline_live.py` runs the real-server recall proof and is
module-skipped unless the Qdrant/TEI endpoints are reachable. The judged metrics on
the live stack stay parked until the NE-013 vLLM gateway judge exists. The one
network dependency of the CI proof is the FastEmbed BM25 HF asset fetch.

### Fail-closed in CI (`NE_REQUIRE_PUBLIC_BASELINE`, NE-CKC6)

The BM25-asset (and ragas-oracle) fixtures are gated by the
`NE_REQUIRE_PUBLIC_BASELINE` flag. Unset (local dev) they SKIP if the asset is
unreachable — the old skip-if-unavailable ergonomics. Set to `"1"` (CI) an
unreachable asset is a HARD FAILURE, so a missing asset can never silently green
the Checkpoint-C gate ("gate green" is now distinguishable from "gate never ran").
The `ci.yml` `check` job sets the flag job-wide, caches the fastembed model and the
ragas venv (SHA-pinned `actions/cache`) so the cold fetch that caused the flake
disappears, and adds a **Checkpoint-C executing gate** step that reruns the two
modules with `-rs` and fails on any `skipped`/`no tests ran` outcome (defense in
depth alongside the flag).

## Regression trend + baseline ratchet (NE-025)

`ne.eval.trend` provides a committed **baseline ratchet**
(`goldensets/public/baseline.rag-public.json`, byte-stable JSON, same drift-ratchet
shape as the promptfoo config): `baseline --write` snapshots a run's metrics +
per-metric tolerance; `baseline --check` (and `check_regression`) fail CI when a
later run drops any metric below `baseline − tolerance`. `trend`/`trend_rows` read
the append-only `EvalRunStore` back oldest-first to surface slow regressions the
single-threshold ratchet would miss.

## Acme golden set (NE-075)

The Acme **permanent regression suite** is 112 authored `ne.eval.golden.GoldenItem`
rows over the committed synthetic `acme-v1` corpus at `tools/acme/fixtures/acme-v1/`
(500 docs), split across **two files that are scored by different scorers and must
never be mixed**. The corpus is **first-party synthetic data** generated in-repo by
`ne-acme 0.1.0` (seed 74, sha256-pinned by its `manifest.json`), so no external
attribution is required (`NOTICE` is untouched, unlike the public SQuAD set).
Everything here runs in a plain `uv run pytest` — no network, no Docker, no live
tenant.

- **`acme_golden.jsonl` — 100 general RETRIEVAL items.** One per ground-truth doc
  whose numeric id is divisible by 5 (a fixed 1-in-5 SPREAD across the corpus, not a
  handful of docs), each taking that doc's first fact. Tagged with the doc's
  `acl.json` visibility class: `public` / `department` / `exec_only` / `named`. This
  is the ONLY file fed to `RagEvalSuite` / `ne-eval baseline` for `recall_at_10` &c.:
  every `relevant_document_ids` entry is a doc the answer genuinely lives in, so it is
  legitimately retrieval-relevant. It carries **zero** `permission-trap` rows by
  construction (enforced by `test_retrieval_set_excludes_permission_traps`).
- **`acme_permission_traps.jsonl` — 12 permission-trap items** (tag `permission-trap`)
  — the heart of the unit (*"user A must NOT see doc X"*). Each pins one of the four
  canonical personas — **P1-exec** `per-0000` (group `executive`), **P2-eng**
  `per-0006` (`dept-engineering`), **P3-sales** `per-0016` (`dept-sales`),
  **P4-legal** `per-0038` (`dept-legal`) — to an EXPECTED-DENIED doc plus an
  EXPECTED-VISIBLE control. The matrix is carried on the item **tags** —
  `persona:<id>`, `denied:<doc>`, `visible:<doc>`, `allowed:<id>` — so this file (not
  a second copy in the test) is the single source of the matrix (SPEC §11
  derive-not-transliterate). These rows are scored **only** by the persona-aware
  permission gate and are deliberately kept OUT of the retrieval file: each
  `relevant_document_ids` names the doc the persona must NOT retrieve, so feeding a
  trap row to `recall_at_k` would score a correctly-DENIED (secure) result as a
  retrieval miss and contaminate the retrieval/judge metrics.

**How the trap gate is proven.** `tests/test_acme_permission_traps.py` drives the
REAL production ACL logic — `ne.brain.trimming.acl_match_labels` +
`PrincipalAclFilter` fed by a `FixtureGroupExpander` built from the fixture's org
memberships — and asserts the denied persona's match-set is DISJOINT from the doc's
ACL labels while the same persona (and the doc's `allowed` persona) INTERSECTS. A
2000-cell cross-check (500 docs × 4 personas) ties the production match rule to the
fixture's independently-authored `reader_ids` ground truth. `named` docs are visible
only via `allowed_people` (never a group, never P1-exec); `exec_only` only to
`executive`; `department` to the dept group + `executive`.

**Signed integrity.** `tests/test_acme_golden_set.py` pins
`golden_set_hash(acme_golden.jsonl)` as `ACME_GOLDEN_SET_SHA256`,
`golden_set_hash(acme_permission_traps.jsonl)` as `ACME_TRAP_SET_SHA256`, and the four
fixture-file sha256s — editing either data file without re-pinning fails CI by design.

**Regenerate + re-pin:**

```
uv run python packages/ne-eval/goldensets/acme/generate_acme_golden.py
uv run ne-eval hash-golden packages/ne-eval/goldensets/acme/acme_golden.jsonl
uv run ne-eval hash-golden packages/ne-eval/goldensets/acme/acme_permission_traps.jsonl
# → paste the two printed digests into ACME_GOLDEN_SET_SHA256 / ACME_TRAP_SET_SHA256
#   in packages/ne-eval/tests/test_acme_golden_set.py
```

The generator writes both files, is deterministic (two runs are byte-identical), and
asserts the four fixture digests unless `--allow-sha-drift` is passed.

**Thresholds (`goldensets/acme/thresholds.acme.json`).** The NE-077 gate numbers as
data: `recall_at_10 ≥ 0.85`, `faithfulness ≥ 0.80`, `context_precision ≥ 0.70`,
`context_recall ≥ 0.80` — asserted EQUAL to `ne.eval.thresholds.DEFAULT_THRESHOLDS`
where shared (drift fails) — plus `permission_trap_pass_rate` which is **HARD** at
`threshold 1.0 / tolerance 0.0`: the WARN band is impossible, so one leak (or an
absent metric) FAILs the gate.

**How the NE-077 gate consumes it.** The two files feed two DISTINCT scorers — never
score the trap file with retrieval metrics:

- *Retrieval baseline.* The gate loads **`acme_golden.jsonl`** (100 general items,
  no trap rows) via `load_golden_set`, runs retrieval over the seeded tenant, and
  evaluates `recall_at_10` / `faithfulness` / `context_precision` / `context_recall`
  against `thresholds.acme.json` with `evaluate_thresholds` / `ne-eval baseline
  --check` (both already operate generically on paths — nothing needed wiring in
  `src`).
- *Permission gate.* The gate loads **`acme_permission_traps.jsonl`** and computes
  `permission_trap_pass_rate` through the persona-aware ACL logic (see
  `test_acme_permission_traps.py`) — the denied docs are graded on being correctly
  DENIED, not retrieved. This file is never passed to `recall_at_k`.

The **live** seeded-tenant permission assertion (driving each connector's
`.permissions()` + `acl_match_labels` against the seeded dev tenants, via the
already-validated connector-permissions round-trip harness) is a separate NE-077
gate step, recorded as pending live verification and deliberately NOT built here.

## RAGAS cross-validation oracle (dev-only, SPEC §11)

`ragas_env/` is an **isolated, hash-pinned dev venv** (built via the shared
`ne.envbuild` sidecar leaf, NE-CKC13 — lock-serialised rebuild + atomic marker,
mirroring ne-brain's `parser_env/`) that installs the real `ragas==0.4.3`. Under
`AIR_GAP` a missing/stale `.ragas-venv` fails closed with a typed
`ne.brain.sovereignty.AirGapViolationError` before any build egress (C2; interim
first-party `ne-brain` edge for the sovereignty primitive, hoist deferred to the
SSRF-consolidation unit). `tests/test_ragas_oracle.py`
feeds the SAME fixed judge verdicts to BOTH ragas and our native `ne.eval.metrics`
and asserts they agree to `1e-6` — proving our metric **arithmetic** is
RAGAS-validated. The claim is scoped to the verdict→score aggregation (the part
SPEC §11 keeps native), NOT the LLM judging. The oracle is skip-if-unavailable
(`NE_SKIP_RAGAS_ORACLE=1` or an unbuildable venv) when `NE_REQUIRE_PUBLIC_BASELINE`
is unset, but under `NE_REQUIRE_PUBLIC_BASELINE=1` (CI, NE-CKC6) an unbuildable
venv is a hard failure and `NE_SKIP_RAGAS_ORACLE=1` is refused as a contradictory
config (REQUIRE wins). `test_ragas_isolation.py`
proves ragas/scipy/langchain* never enter the platform `uv.lock` — the "grep-0"
sovereignty guarantee, made permanent. See `ragas_env/README.md` for the chosen
seam and the ragas repo-ownership note.

## Deferred / live-parkable

- CI-workflow wiring of `run_gate.sh` (root lane, NE-025-adjacent).
- The concrete open-weight judge serving pin on the `PRIVATE_VLLM` lane
  (NE-013 serving profile / NE-025).
- Live end-to-end promptfoo run and real-Postgres `ne_eval_runs` exercise.
- Real-TEI / real-Qdrant recall proof (`test_retrieval_baseline_live.py`, NE-070 wave).
- Real-vLLM gateway-judge judged-metric baseline run (post-NE-013); the CI proof
  uses the deterministic `LexicalJudge` stand-in.
