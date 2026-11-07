# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""promptfoo CI-gate kit — config generation + anti-drift ratchet (SPEC §5 NE-024).

promptfoo is an EXTERNAL, pinned npm CLI (``promptfoo@0.121.17``, MIT), invoked
by ``promptfoo/run_gate.sh`` with telemetry and update-checks disabled. It is
NEVER a Python dependency and must not appear in ``uv.lock`` (heavy-closure
isolation §11).

The gate config is GENERATED from the same :class:`ThresholdSpec` values the
suite uses, and each assertion binds at ``threshold - tolerance`` — identical
semantics to :func:`ne.eval.thresholds.gate_passed` (WARN band still passes).
:func:`check_promptfoo_config` is the drift ratchet: the committed
``promptfooconfig.json`` must byte-equal the freshly generated config.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from ne.eval.thresholds import DEFAULT_THRESHOLDS, ThresholdSpec

#: The single source of truth for the pinned promptfoo CLI version.
PROMPTFOO_VERSION = "0.121.17"

#: Environment that disables promptfoo telemetry + update checks (C1 no phone-home).
PROMPTFOO_ENV: dict[str, str] = {
    "PROMPTFOO_DISABLE_TELEMETRY": "1",
    "PROMPTFOO_DISABLE_UPDATE": "1",
}


def _bound(spec: ThresholdSpec) -> float:
    """The gate binding value: threshold minus tolerance (the WARN-band floor)."""
    return round(spec.threshold - spec.tolerance, 6)


def build_promptfoo_config(
    thresholds: Sequence[ThresholdSpec] = DEFAULT_THRESHOLDS,
) -> dict[str, object]:
    """Build the promptfoo gate config dict from ``thresholds``."""
    assertions = [
        {
            "type": "javascript",
            "value": f'JSON.parse(output).metrics["{spec.metric}"] >= {_bound(spec):g}',
        }
        for spec in thresholds
    ]
    return {
        "description": "ne-eval RAG gate",
        "providers": [{"id": "file://gate_provider.py", "label": "ne-eval-suite"}],
        "tests": [{"vars": {"suite": "rag"}, "assert": assertions}],
    }


def _serialize(config: dict[str, object]) -> str:
    return json.dumps(config, sort_keys=True, indent=2) + "\n"


def write_promptfoo_config(
    path: str | Path, thresholds: Sequence[ThresholdSpec] = DEFAULT_THRESHOLDS
) -> None:
    """Write the generated gate config (sorted keys, 2-space indent) to ``path``."""
    Path(path).write_text(_serialize(build_promptfoo_config(thresholds)), encoding="utf-8")


def check_promptfoo_config(
    path: str | Path, thresholds: Sequence[ThresholdSpec] = DEFAULT_THRESHOLDS
) -> bool:
    """Return True iff the committed config at ``path`` byte-equals the generated one."""
    generated = _serialize(build_promptfoo_config(thresholds))
    try:
        committed = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return committed == generated
