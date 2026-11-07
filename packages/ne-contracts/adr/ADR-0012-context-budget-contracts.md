# 0012 — Context-budget contracts (`ContextBudgetExceededError` + `ne.contracts.tokens`)

## Status

Accepted (2026-07-21; NE-CTXB-1).

## Context

Context-budget management is
a three-layer platform feature: Layer-1 gateway pre-flight, Layer-2 runtime trim,
Layer-3 grounding-gate extension. Every one of those layers needs a shared token
primitive and a shared, typed refusal before its implementation unit can start.
Per SPEC §6 freeze discipline, that seam must land through the contracts lane
first — with an ADR and a version bump — exactly how `ne-contracts` has grown
since Checkpoint-A (ADR-0003…0011). This unit delivers ONLY the primitives; the
gateway wiring (NE-CTXB-2a), runtime trim (NE-CTXB-3), and gate extension
(NE-CTXB-4) are later units that import these.

## Decision

Add `ContextBudgetExceededError` to `ne/contracts/errors.py` and a NEW pure-stdlib
module `ne/contracts/tokens.py`, re-export their public names from `ne.contracts`,
and bump `ne-contracts` 1.8.0 → 1.9.0 (MINOR, additive).

1. **ONE error class, distinguished by `stage`.** `ContextBudgetExceededError(
   NeContractsError)` carries five fields — `stage` (`"runtime-trim"` |
   `"gateway-preflight"`), `estimated_tokens`, `input_budget`, `context_window`,
   `dropped_turns` (0 at gateway-preflight). It is **counts ONLY (C12): never
   message text**; the exact `str()` template is pinned by
   `tests/test_context_budget_error.py`. One class distinguished by `stage` (not
   two classes) because the same governed refusal flows end-to-end through
   `safe_error_detail` on both the inline and worker-relay SSE paths — a budget
   refusal is a named, governed error, not a raw provider 400.

2. **The error family now spans operational refusals — no broad catch.** The
   `errors.py` family docstring is rewritten: production code must catch SPECIFIC
   members, never `except NeContractsError`, because a broad family catch would
   silently swallow a governed operational refusal. This is enforced by an AST
   guardian TEST (`tests/test_error_family_guardian.py`, scanning all
   `packages/*/src` + `tools/*/src`), not by the docstring — and the guardian's
   detector is itself unit-tested against the Name/Attribute/Tuple/`except*` forms
   so it cannot rot into a no-op. The current production tree has ZERO broad
   catches; the guardian pins that going forward.

3. **`ByteTokenEstimator`, not runtime tokenizers.** The estimator is
   `ceil(len(text.encode("utf-8")) / bytes_per_token) + per_call_overhead` — pure
   stdlib, deterministic, zero egress. Rejected alternatives stand: exact HF
   tokenizers (weights not mounted in ne-api/ne-worker), tiktoken at runtime
   (first-use BPE download from `openaipublic.blob.core.windows.net` breaks the
   no-egress seal), vLLM `/tokenize` (per-count RTT, and no frontier equivalent).
   A `TokenEstimator` Protocol (plain `typing.Protocol`, not runtime-checkable —
   house style) is the seam so a calibrated estimator can be swapped in later.

4. **THE EMPIRICAL FINDING — design-time `bytes_per_token=3.0` is REFUTED; the
   default is lowered to `1.5`.** A vendored multi-script oracle
   (`tests/fixtures/token_oracle/`) with CHECKED-IN REAL token counts from BOTH
   the deployed-lane Qwen3-0.6B tokenizer (Apache-2.0) and the frontier-proxy
   `o200k_base` BPE (tiktoken 0.13.0, MIT) measures the true bytes/token ratio per
   script class. Design §2.3 proposed `3.0`; the oracle refutes it for the URI/URN
   class — demo-shaped `urn:ne:entity:order-…` table rows tokenize on Qwen3 as
   densely as **~1.77 bytes/token**, and the percent-encoded SPARQL URL at ~1.9.
   Per design §2.3's own stated contingency ("if any class fails, the unit lowers
   `bytes_per_token`"), the default is selected mechanically by the generator: the
   largest rung on `[3.0, 2.8, 2.5, 2.2, 2.0, 1.8, 1.5]` that keeps
   `estimate(text) ≥ ceil(1.10 × max(qwen3, o200k))` on EVERY sample (10%
   generation-time headroom). That rung is **`1.5`** — the URN-order-rows sample
   forces it below the planner's predicted 1.8. The value lives ONCE in this
   module as `DEFAULT_BYTES_PER_TOKEN` and is pinned to the goldens'
   `meta.chosen_bytes_per_token` by the oracle test; it is the oracle's OUTPUT,
   not a free parameter.

   **Per-class floors were REJECTED.** Runtime cannot cheaply classify arbitrary
   text into "URI" vs "prose" before estimating, so a per-class floor is not
   implementable at the seam. A single conservative divisor is the honest v1;
   NE-CTXB-9 usage-feedback calibration (EWMA of `Completion.usage.prompt_tokens /
   estimated` per model, clamped ≥ a safety floor — the signal already exists at
   `gateway.py:325`) is the designed precision-recovery path.

   **`o200k_base` is the documented frontier proxy.** The frontier lane
   (`anthropic/claude-sonnet-4-5`) has no public tokenizer; `o200k_base` is the
   conservative frontier-class BPE proxy, and design §3.3 already declares
   frontier drift absorbed by the margin.

   **Known cost (declared):** on Latin prose the divisor is now very conservative
   (~3.5× the real count at generation time), which truncates Layer-2 packing
   earlier than strictly necessary. Accepted for v1; NE-CTXB-9 is the fix.

5. **`ContextBudget` is frozen, `extra="forbid"`, and validates at construction.**
   `context_window` / `max_output_tokens` / `overhead_margin` (all bounded by
   pydantic `Field`), `input_budget = window − output − margin` as a property, and
   a `model_validator(mode="after")` that raises (counts-only message) if
   `output + margin ≥ window`. A boot-time misconfiguration therefore fails LOUD
   at composition, matching design §2.2's fail-loud posture — never a silent
   runtime surprise.

6. **Estimator params live ONCE on `GatewaySettings` downstream.** This module
   defines the primitive and its module-level DEFAULTS only. NE-CTXB-2a owns the
   configured `bytes_per_token` / `per_call_overhead` / margin policy on
   `GatewaySettings`, and the platform composition derives BOTH the runtime
   `ContextBudget` and the gateway pre-flight from that one object (design §2.3
   single-source rule). Downstream units MUST inherit `DEFAULT_BYTES_PER_TOKEN`
   from here — they must NOT hard-code the design prose's `3.0`.

7. **Residual (declared):** adversarial byte-soup text (UTF-8 byte-fallback ~1
   token/byte) can always exceed a byte-divisor estimate. That is Layer-1's typed
   refusal's job as the backstop; the oracle fixtures stay realistic by design.

## Consequences

- **Purely additive MINOR bump 1.8.0 → 1.9.0.** `tokens.py` is a NEW module and
  `ContextBudgetExceededError` is a NEW leaf; no existing Protocol, type, model,
  or signature is touched. Proven by the entire pre-existing `ne-contracts` suite
  passing unmodified except the two version-pin literals (`tests/test_import.py`,
  `tests/test_graph_citation_types.py`). `test_protocol_inventory.py` passes
  byte-unmodified — `TokenEstimator` is a Phase-context-budget addition OUTSIDE
  the SPEC §4 32-Protocol inventory (which stays at 32; precedent `SkillSubstrate`
  in ADR-0011). `ne.contracts.models.ModelEntry.context_window` (the wire/menu
  surface) is deliberately UNTOUCHED per design §3.1.
- **ZERO new dependencies.** `tokens.py` uses only `math` + `typing` + the
  already-pinned pydantic. The oracle's real token counts are DATA; the generator
  (`generate_goldens.py`) uses tiktoken/tokenizers only via manual
  `uv run --with`, never a shipped/locked dependency, and the shipped test reads
  checked-in JSON with no tokenizer import — CI stays hermetic and egress-free.
  NOTICE is byte-identical.
- **What would be BREAKING later (forbidden in this and every later unit):**
  renaming/retyping any of these surfaces; changing the `TokenEstimator.estimate`
  signature after a second implementation lands; changing the
  `ContextBudgetExceededError` field set or `str()` template (stored/relayed
  refusal contract). New OPTIONAL defaulted fields remain additive.
- **This is a one-way door on the error's message template** (it flows through the
  SSE relay). Owner-review-before-merge is the backstop; this decision list should
  surface in the PR body — in particular the `3.0 → 1.5` default change, which
  NE-CTXB-2a/3 must inherit.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the unit's
contract diff surface is `packages/ne-contracts/**`. It is indexed from the
repo-wide `docs/adr/README.md` for discovery.
