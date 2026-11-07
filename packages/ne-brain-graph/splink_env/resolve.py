# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""Splink dedupe resolver — runs INSIDE the isolated splink sidecar venv.

Reads a JSON payload ``{entity_class, records, compare_fields, blocking_fields,
threshold, seed}`` and writes sorted candidate pairs
``[{left_key, right_key, match_probability, match_weight, blocking_rule}]``.

It builds a DETERMINISTIC DuckDB Splink dedupe model (fixed seed, exact-match
comparisons + exact-match blocking rules per field) so the same input yields the
same candidate ids on re-run (the idempotent-proposal-id requirement). This script
is stdlib + splink ONLY; nothing here is importable platform-side (C4 — igraph is
GPL and confined to this venv).

Usage (driven by ``ne.brain_graph.er._splink.SplinkSidecar`` via subprocess)::

    python resolve.py --input payload.json --output pairs.json
"""

from __future__ import annotations

import argparse
import json
from typing import Any


def _blocking_rules(blocking_fields: list[str]) -> list[str]:
    return [f"l.{f} = r.{f}" for f in blocking_fields]


def run(payload: dict[str, Any]) -> list[dict[str, Any]]:
    import duckdb  # noqa: F401  (sidecar-only; proves the closure is present)
    import splink.comparison_library as cl
    from splink import DuckDBAPI, Linker, SettingsCreator, block_on

    records = payload["records"]
    compare_fields: list[str] = list(payload["compare_fields"])
    blocking_fields: list[str] = list(payload["blocking_fields"]) or compare_fields
    threshold = float(payload["threshold"])

    # A record needs a unique_id column for Splink; use the graph key.
    frames = [{**r, "unique_id": r["key"]} for r in records]

    settings = SettingsCreator(
        link_type="dedupe_only",
        blocking_rules_to_generate_predictions=[block_on(f) for f in blocking_fields],
        comparisons=[cl.ExactMatch(f) for f in compare_fields],
    )
    linker = Linker(frames, settings, db_api=DuckDBAPI())
    linker.training.estimate_probability_two_random_records_match(
        [block_on(f) for f in blocking_fields], recall=0.9
    )
    for f in blocking_fields:
        linker.training.estimate_parameters_using_expectation_maximisation(block_on(f))

    predictions = linker.inference.predict(threshold_match_probability=threshold)
    df = predictions.as_pandas_dataframe()

    out: list[dict[str, Any]] = []
    rules = _blocking_rules(blocking_fields)
    for _, row in df.iterrows():
        left = str(row["unique_id_l"])
        right = str(row["unique_id_r"])
        out.append(
            {
                "left_key": left,
                "right_key": right,
                "match_probability": float(row["match_probability"]),
                "match_weight": float(row.get("match_weight", 0.0)),
                "blocking_rule": rules[0] if rules else "",
            }
        )
    # Sort by the unordered pair so re-runs are byte-stable.
    out.sort(key=lambda c: tuple(sorted([c["left_key"], c["right_key"]])))
    # Collapse duplicate unordered pairs (keep the highest probability).
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for cand in out:
        pair = tuple(sorted([cand["left_key"], cand["right_key"]]))
        if pair not in best or cand["match_probability"] > best[pair]["match_probability"]:
            best[pair] = cand
    return [best[k] for k in sorted(best)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    with open(args.input, encoding="utf-8") as handle:
        payload = json.loads(handle.read())
    result = run(payload)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
