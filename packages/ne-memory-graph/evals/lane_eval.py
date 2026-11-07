# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Per-lane extraction-quality + per-event-cost eval harness (R1).

Scores entity/fact recall + precision of the Graphiti extraction stage against a
small fixed business-memory corpus, and records PER-EVENT cost (prompt/completion
tokens + cost_usd). Two modes:

* ``--lane frontier|air_gap --base-url ...`` — LIVE (parked until endpoints exist):
  feeds ``fixtures/episodes.json`` through the sidecar ``/episodes`` + ``/search``
  and scores the real extraction output;
* ``--fixtures-only`` — CI: validates the fixture schema and the deterministic
  scoring arithmetic over recorded outputs in ``fixtures/recorded_*.json`` (no
  network, no model).

ROUTING (see evals/README.md): frontier = gateway-routed frontier model via the
llm_facade; AIR_GAP = an open-weight in-VPC vLLM model on a REASONING-CRITICAL
stage — quality UNMEASURED until the live eval runs (LIVE-PARKABLE).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = [
    "LaneScore",
    "fixtures_dir",
    "load_json",
    "score_extraction",
    "validate_fixture_schema",
]


def fixtures_dir() -> Path:
    """The ``evals/fixtures`` directory next to this module."""
    return Path(__file__).resolve().parent / "fixtures"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class LaneScore(BaseModel):
    """Deterministic recall/precision + cost for one lane's extraction run."""

    model_config = ConfigDict(frozen=True)

    entity_recall: float
    entity_precision: float
    fact_recall: float
    fact_precision: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


def _recall_precision(expected: list[str], actual: list[str]) -> tuple[float, float]:
    exp = {e.strip().lower() for e in expected}
    act = {a.strip().lower() for a in actual}
    recall = 1.0 if not exp else len(exp & act) / len(exp)
    precision = (1.0 if not exp else 0.0) if not act else len(exp & act) / len(act)
    return recall, precision


def score_extraction(expected: dict[str, Any], recorded: dict[str, Any]) -> LaneScore:
    """Pure, deterministic scoring of a recorded extraction against expectations."""
    e_recall, e_prec = _recall_precision(
        list(expected.get("entities", [])), list(recorded.get("entities", []))
    )
    f_recall, f_prec = _recall_precision(
        list(expected.get("facts", [])), list(recorded.get("facts", []))
    )
    cost = recorded.get("cost", {})
    return LaneScore(
        entity_recall=e_recall,
        entity_precision=e_prec,
        fact_recall=f_recall,
        fact_precision=f_prec,
        prompt_tokens=int(cost.get("prompt_tokens", 0)),
        completion_tokens=int(cost.get("completion_tokens", 0)),
        cost_usd=float(cost.get("cost_usd", 0.0)),
    )


def validate_fixture_schema(episodes: Any, expected: Any) -> None:
    """Assert the fixture corpus + expectations have the required shape."""
    if not isinstance(episodes, list) or not episodes:
        raise ValueError("episodes.json must be a non-empty list")
    for ep in episodes:
        for key in ("name", "content", "occurred_at"):
            if key not in ep:
                raise ValueError(f"episode missing key {key!r}: {ep!r}")
    if not isinstance(expected, dict):
        raise ValueError("expected.json must be an object")
    for key in ("entities", "facts"):
        if not isinstance(expected.get(key), list):
            raise ValueError(f"expected.json must have a list under {key!r}")


def run_fixtures_only() -> LaneScore:
    """CI mode: validate the schema and score the recorded frontier output."""
    fx = fixtures_dir()
    episodes = load_json(fx / "episodes.json")
    expected = load_json(fx / "expected.json")
    validate_fixture_schema(episodes, expected)
    recorded = load_json(fx / "recorded_frontier.json")
    return score_extraction(expected, recorded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lane_eval", description=__doc__)
    parser.add_argument("--fixtures-only", action="store_true")
    parser.add_argument("--lane", choices=("frontier", "air_gap"), default=None)
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args(argv)

    if args.fixtures_only or args.lane is None:
        score = run_fixtures_only()
        print(score.model_dump_json(indent=2))
        return 0

    # LIVE lane path is LIVE-PARKABLE: no vLLM / gateway endpoint in CI.
    print(
        f"lane={args.lane} base_url={args.base_url}: LIVE-PARKED — run against a real "
        "sidecar + endpoint; record the row in the pending-live-verification ledger."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
