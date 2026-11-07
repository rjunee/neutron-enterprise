# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""RAGAS cross-validation oracle — runs INSIDE the isolated ragas dev venv.

Given fixed judge verdicts (the SAME verdicts our native ne.eval.metrics score),
this script obtains faithfulness / context_precision / context_recall scores from
RAGAS's OWN aggregation code and emits them. It contains **no score arithmetic of
its own**: every returned number comes from a ragas metric method

* faithfulness         -> ragas ``Faithfulness._compute_score``
* context_precision    -> ragas ``LLMContextPrecisionWithReference._calculate_average_precision``
* context_recall       -> ragas ``LLMContextRecall._compute_score``

fed ragas's own parsed-output objects (``NLIStatementOutput`` /
``Verification`` / ``ContextRecallClassification``). This is the "metric-internal
compute" seam the NE-025 brief authorizes: it proves our metric arithmetic
matches ragas's exactly, with zero runtime / C3 / sovereignty cost (ragas never
enters the platform lock; see ``README.md`` for the ownership note).

NaN mapping: ragas returns ``np.nan`` for a degenerate input (no statements); we
map NaN -> 0.0 here (with this comment) because our native metric returns 0.0 for
that same degenerate case — the mapping is null-handling, not score arithmetic.

Usage (driven by ``ne.eval.ragas_oracle`` via subprocess)::

    python cross_check.py --input fixture.json --output result.json
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import warnings
from typing import Any

warnings.filterwarnings("ignore")

from ragas.metrics import (  # noqa: E402
    Faithfulness,
    LLMContextPrecisionWithReference,
    LLMContextRecall,
)
from ragas.metrics._context_precision import Verification  # noqa: E402
from ragas.metrics._context_recall import ContextRecallClassification  # noqa: E402
from ragas.metrics._faithfulness import (  # noqa: E402
    NLIStatementOutput,
    StatementFaithfulnessAnswer,
)

_FAITHFULNESS = Faithfulness()
_CONTEXT_PRECISION = LLMContextPrecisionWithReference()
_CONTEXT_RECALL = LLMContextRecall()


def _nan_to_zero(value: Any) -> float:
    number = float(value)
    return 0.0 if math.isnan(number) else number


def _faithfulness_score(statements: list[str], verdicts: list[int]) -> float:
    answers = NLIStatementOutput(
        statements=[
            StatementFaithfulnessAnswer(statement=s, reason="fixed-verdict", verdict=int(v))
            for s, v in zip(statements, verdicts, strict=True)
        ]
    )
    return _nan_to_zero(_FAITHFULNESS._compute_score(answers))


def _context_precision_score(verdicts: list[int]) -> float:
    verifications = [Verification(reason="fixed-verdict", verdict=int(v)) for v in verdicts]
    return _nan_to_zero(_CONTEXT_PRECISION._calculate_average_precision(verifications))


def _context_recall_score(statements: list[str], attributed: list[int]) -> float:
    classifications = [
        ContextRecallClassification(statement=s, reason="fixed-verdict", attributed=int(a))
        for s, a in zip(statements, attributed, strict=True)
    ]
    return _nan_to_zero(_CONTEXT_RECALL._compute_score(classifications))


def run(fixture: dict[str, Any]) -> dict[str, Any]:
    out_cases = []
    for case in fixture["cases"]:
        faith = case["faithfulness"]
        prec = case["context_precision"]
        recall = case["context_recall"]
        out_cases.append(
            {
                "id": case["id"],
                "faithfulness": _faithfulness_score(faith["statements"], faith["verdicts"]),
                "context_precision": _context_precision_score(prec["verdicts"]),
                "context_recall": _context_recall_score(recall["statements"], recall["attributed"]),
            }
        )
    return {"ragas_version": importlib.metadata.version("ragas"), "cases": out_cases}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    with open(args.input, encoding="utf-8") as handle:
        fixture = json.loads(handle.read())
    result = run(fixture)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
