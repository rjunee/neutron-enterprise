# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The load-bearing token-estimator ORACLE (NE-CTXB-1, design §2.3/§7.1).

Reads the checked-in ``goldens.json`` — REAL Qwen3 + o200k_base token counts
produced offline by ``fixtures/token_oracle/generate_goldens.py`` — and asserts
the normative one-sidedness invariant ``ByteTokenEstimator().estimate(text) >=
max(actual_tokens)`` for EVERY vendored sample across all script classes. This is
the test the estimator's one-sidedness claim traces to (not prose). It imports NO
tokenizer library, so CI stays hermetic and egress-free.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from ne.contracts.tokens import (
    DEFAULT_BYTES_PER_TOKEN,
    DEFAULT_PER_CALL_OVERHEAD,
    ByteTokenEstimator,
)

_GOLDENS_PATH = Path(__file__).parent / "fixtures" / "token_oracle" / "goldens.json"


def _load_goldens() -> dict[str, Any]:
    with _GOLDENS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_goldens_are_real_and_complete() -> None:
    goldens = _load_goldens()
    samples = goldens["samples"]
    by_class = Counter(s["script_class"] for s in samples)

    # >=5 script classes, >=4 samples each.
    assert len(by_class) >= 5, f"expected >=5 script classes, got {sorted(by_class)}"
    for script_class, count in by_class.items():
        assert count >= 4, f"class {script_class!r} has only {count} samples (<4)"

    # Every sample carries BOTH real tokenizer counts, strictly positive.
    for s in samples:
        actual = s["actual_tokens"]
        assert set(actual) == {"qwen3", "o200k_base"}, s["id"]
        assert actual["qwen3"] > 0, s["id"]
        assert actual["o200k_base"] > 0, s["id"]

    meta = goldens["meta"]
    # The oracle SELECTS the divisor; tokens.py must be pinned to it.
    assert meta["chosen_bytes_per_token"] == DEFAULT_BYTES_PER_TOKEN
    assert meta["per_call_overhead"] == DEFAULT_PER_CALL_OVERHEAD
    # Provenance is diff-inspectable.
    assert meta["qwen_tokenizer"]["repo"] == "Qwen/Qwen3-0.6B"
    assert len(meta["qwen_tokenizer"]["sha256"]) == 64
    assert meta["o200k"]["encoding"] == "o200k_base"
    assert meta["tokenizers_version"]
    assert meta["o200k"]["tiktoken_version"]


def test_estimate_is_one_sided_per_sample() -> None:
    """THE normative invariant: estimate >= max real count, for every sample."""
    goldens = _load_goldens()
    estimator = ByteTokenEstimator()
    for s in goldens["samples"]:
        actual_max = max(s["actual_tokens"].values())
        est = estimator.estimate(s["text"])
        assert est >= actual_max, (
            f"UNDERESTIMATE on sample {s['id']!r} (class {s['script_class']!r}): "
            f"estimate={est} < max_actual={actual_max} "
            f"(qwen3={s['actual_tokens']['qwen3']}, "
            f"o200k_base={s['actual_tokens']['o200k_base']})"
        )


def test_default_estimator_uses_module_defaults() -> None:
    estimator = ByteTokenEstimator()
    assert estimator.bytes_per_token == DEFAULT_BYTES_PER_TOKEN
    assert estimator.per_call_overhead == DEFAULT_PER_CALL_OVERHEAD
