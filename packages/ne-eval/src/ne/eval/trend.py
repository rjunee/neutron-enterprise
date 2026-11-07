# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Regression-baseline ratchet + trend readout for eval runs (SPEC §5 NE-025).

A committed :class:`BaselineRecord` snapshots the metrics a suite achieved on a
fixed golden set, plus a per-metric tolerance. :func:`check_regression` fails
(returns messages) when a later run drops any metric below ``baseline - tolerance``
— the same drift-ratchet shape as ``ne.eval.promptfoo`` (a committed byte-stable
artifact the CI re-checks). The serialization convention (``sort_keys``, 2-space
indent, trailing newline) matches ``ne.eval.promptfoo._serialize`` so the file is
git-diffable and reproducible.

:func:`trend_rows` reads the append-only :class:`~ne.eval.store.EvalRunStore` back
into an oldest-first series so a human (or the ``ne-eval trend`` CLI) can see a
metric's history and spot a slow regression the ratchet's single threshold misses.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ne.eval.store import EvalRunStore

#: Metric floats are rounded to this many decimals before a baseline is written.
BASELINE_PRECISION = 4


class BaselineRecord(BaseModel):
    """A committed metric snapshot for a suite on a fixed golden set."""

    model_config = ConfigDict(frozen=True)

    suite: str
    golden_set_hash: str
    judge_model_id: str
    metrics: dict[str, float]
    tolerances: dict[str, float]


def _serialize(record: BaselineRecord) -> str:
    return json.dumps(record.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"


def write_baseline(path: str | Path, record: BaselineRecord) -> None:
    """Write ``record`` as byte-stable JSON (sorted keys, 2-space indent, newline).

    Metric values are rounded to :data:`BASELINE_PRECISION` decimals first so a
    tiny nondeterministic wobble never rewrites the committed file.
    """
    rounded = record.model_copy(
        update={"metrics": {k: round(v, BASELINE_PRECISION) for k, v in record.metrics.items()}}
    )
    Path(path).write_text(_serialize(rounded), encoding="utf-8")


def load_baseline(path: str | Path) -> BaselineRecord:
    """Load a :class:`BaselineRecord` from ``path``."""
    return BaselineRecord.model_validate_json(Path(path).read_text(encoding="utf-8"))


def check_regression(baseline: BaselineRecord, metrics: Mapping[str, float]) -> list[str]:
    """Return a regression message per metric that dropped below its tolerance floor.

    A metric regresses when it is MISSING from ``metrics`` or its value is below
    ``baseline.metrics[k] - baseline.tolerances.get(k, 0.0)``. An empty list means
    no regression (the ratchet holds).
    """
    messages: list[str] = []
    for metric, baseline_value in baseline.metrics.items():
        floor = baseline_value - baseline.tolerances.get(metric, 0.0)
        if metric not in metrics:
            messages.append(f"{metric}: missing from run (baseline {baseline_value:.4f})")
            continue
        value = metrics[metric]
        if value < floor:
            messages.append(
                f"{metric}: {value:.4f} < floor {floor:.4f} "
                f"(baseline {baseline_value:.4f} - tolerance "
                f"{baseline.tolerances.get(metric, 0.0):.4f})"
            )
    return messages


def trend_rows(
    store: EvalRunStore, suite: str, limit: int = 50
) -> list[tuple[datetime, dict[str, float], bool]]:
    """Return ``(created_at, metrics, passed)`` rows for ``suite``, oldest-first."""
    runs = store.list_runs(suite=suite, limit=limit)
    runs.reverse()
    return [(run.created_at, dict(run.metrics), run.passed) for run in runs]
