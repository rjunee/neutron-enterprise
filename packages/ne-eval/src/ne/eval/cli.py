# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne-eval`` command-line interface (stdlib argparse).

Subcommands:

* ``validate-golden <path>``  — validate a golden-set JSONL file (exit 0/1).
* ``hash-golden <path>``      — print the golden-set content hash.
* ``calibrate``               — run judge-vs-human calibration; ``--scripted-fixture``
  drives a :class:`ScriptedJudge` offline (the live gateway path is NE-025's).
* ``check-calibration``       — assert calibration artifacts exist + match (exit 0/1).
* ``promptfoo-config``        — ``--check`` for drift (exit 1 on drift) / ``--write``.
* ``baseline``                — ``--write`` a regression-baseline ratchet from a run's
  metrics, or ``--check`` a run's metrics against it (exit 1 on regression).
* ``trend``                   — print a suite's eval-run metric history (oldest-first).

``main(argv)`` returns an int so it is directly testable; ``__main__`` calls
``sys.exit(main())``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import sqlalchemy as sa
from pydantic import BaseModel

from ne.eval.calibration import check_calibration, run_calibration, save_artifact
from ne.eval.errors import EvalConfigError, NeEvalError
from ne.eval.golden import golden_set_hash, load_golden_set
from ne.eval.judge import out_model_for
from ne.eval.promptfoo import check_promptfoo_config, write_promptfoo_config
from ne.eval.rubrics import Rubric, get_rubric
from ne.eval.store import EvalRunStore
from ne.eval.trend import (
    BaselineRecord,
    check_regression,
    load_baseline,
    trend_rows,
    write_baseline,
)

_DEFAULT_PROMPTFOO_CONFIG = (
    Path(__file__).resolve().parents[3] / "promptfoo" / "promptfooconfig.json"
)


class _CliJudge:
    """A minimal :class:`~ne.eval.judge.Judge` carrying identity + a canned output.

    Used by the CLI: ``check-calibration`` needs only ``judge_model_id``;
    ``calibrate --scripted-fixture`` reuses ``canned`` for every judge call.
    """

    def __init__(
        self,
        judge_model_id: str,
        judge_family: str = "cli",
        canned: BaseModel | None = None,
    ) -> None:
        self._judge_model_id = judge_model_id
        self._judge_family = judge_family
        self._canned = canned

    @property
    def judge_model_id(self) -> str:
        return self._judge_model_id

    @property
    def judge_family(self) -> str:
        return self._judge_family

    async def judge(
        self, rubric: Rubric, variables: Mapping[str, str], out_model: type[BaseModel]
    ) -> BaseModel:
        if self._canned is None:
            raise NeEvalError("CLI judge has no canned output configured")
        return self._canned


def _cmd_validate_golden(args: argparse.Namespace) -> int:
    try:
        gs = load_golden_set(args.path)
    except NeEvalError as exc:
        print(f"INVALID: {exc}")
        return 1
    print(f"OK: {len(gs.items)} golden items")
    return 0


def _cmd_hash_golden(args: argparse.Namespace) -> int:
    gs = load_golden_set(args.path)
    print(golden_set_hash(gs))
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    rubric = get_rubric(args.rubric)
    if args.scripted_fixture:
        payload = json.loads(Path(args.scripted_fixture).read_text(encoding="utf-8"))
        canned = out_model_for(rubric.id).model_validate(payload)
        judge = _CliJudge(args.judge_model or "scripted-cli-judge", args.judge_family, canned)
    else:  # pragma: no cover - live gateway path is NE-025's concern
        raise NeEvalError(
            "live calibration requires the NE-025 gateway wiring; pass "
            "--scripted-fixture to calibrate offline"
        )
    artifact = run_calibration(judge, rubric, args.labels)
    path = save_artifact(artifact, args.out_dir)
    print(f"OK: wrote {path} (accuracy={artifact.accuracy:.3f}, kappa={artifact.cohen_kappa:.3f})")
    return 0


def _cmd_check_calibration(args: argparse.Namespace) -> int:
    rubrics = [get_rubric(rid) for rid in args.rubric]
    judge = _CliJudge(args.judge_model)
    try:
        check_calibration(args.dir, judge, rubrics)
    except NeEvalError as exc:
        print(f"STALE/MISSING: {exc}")
        return 1
    print(f"OK: calibration valid for {len(rubrics)} rubric(s) + judge {args.judge_model!r}")
    return 0


def _cmd_promptfoo_config(args: argparse.Namespace) -> int:
    path = Path(args.path) if args.path else _DEFAULT_PROMPTFOO_CONFIG
    if args.write:
        write_promptfoo_config(path)
        print(f"OK: wrote {path}")
        return 0
    if check_promptfoo_config(path):
        print(f"OK: {path} matches generated config")
        return 0
    print(f"DRIFT: {path} does not match the generated config")
    return 1


def _load_metrics_json(path: str) -> dict[str, float]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise EvalConfigError(f"metrics JSON at {path} must be an object of metric -> float")
    return {str(k): float(v) for k, v in payload.items()}


def _parse_tolerances(raw: Sequence[str] | None) -> dict[str, float]:
    tolerances: dict[str, float] = {}
    for entry in raw or ():
        if "=" not in entry:
            raise EvalConfigError(f"--tolerance expects METRIC=VALUE; got {entry!r}")
        metric, _, value = entry.partition("=")
        tolerances[metric] = float(value)
    return tolerances


def _cmd_baseline(args: argparse.Namespace) -> int:
    if args.write == args.check:
        raise EvalConfigError("baseline requires exactly one of --write / --check")
    if args.write:
        record = BaselineRecord(
            suite=args.suite,
            golden_set_hash=args.golden_set_hash,
            judge_model_id=args.judge_model,
            metrics=_load_metrics_json(args.metrics_json),
            tolerances=_parse_tolerances(args.tolerance),
        )
        write_baseline(args.path, record)
        print(f"OK: wrote baseline {args.path} ({len(record.metrics)} metric(s))")
        return 0
    baseline = load_baseline(args.path)
    regressions = check_regression(baseline, _load_metrics_json(args.metrics_json))
    if regressions:
        for line in regressions:
            print(f"REGRESSION: {line}")
        return 1
    print(f"OK: no regression vs {args.path} ({len(baseline.metrics)} metric(s))")
    return 0


def _cmd_trend(args: argparse.Namespace) -> int:
    store = EvalRunStore(sa.create_engine(args.db))
    rows = trend_rows(store, args.suite, limit=args.limit)
    if not rows:
        print(f"(no runs for suite {args.suite!r})")
        return 0
    for created_at, metrics, passed in rows:
        pairs = " ".join(f"{k}={metrics[k]:.4f}" for k in sorted(metrics))
        status = "PASS" if passed else "FAIL"
        print(f"{created_at.isoformat()} {status} {pairs}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """Delegate to the worker-hosted composition root; never compose a second platform."""

    command = [
        sys.executable,
        "-m",
        "ne.worker",
        "eval-run",
        "--principal-id",
        args.principal_id,
        "--suite",
        args.suite,
    ]
    if args.golden:
        command.extend(("--golden", args.golden))
    return subprocess.call(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ne-eval", description="ne-eval harness CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate-golden", help="validate a golden-set JSONL file")
    p_validate.add_argument("path")
    p_validate.set_defaults(func=_cmd_validate_golden)

    p_hash = sub.add_parser("hash-golden", help="print a golden-set content hash")
    p_hash.add_argument("path")
    p_hash.set_defaults(func=_cmd_hash_golden)

    p_cal = sub.add_parser("calibrate", help="run judge-vs-human calibration")
    p_cal.add_argument("--rubric", required=True)
    p_cal.add_argument("--labels", required=True)
    p_cal.add_argument("--out-dir", required=True)
    p_cal.add_argument("--judge-plugin", default="gateway")
    p_cal.add_argument("--judge-model", default="")
    p_cal.add_argument("--judge-family", default="cli")
    p_cal.add_argument("--scripted-fixture", default="")
    p_cal.set_defaults(func=_cmd_calibrate)

    p_check = sub.add_parser("check-calibration", help="assert calibration artifacts match")
    p_check.add_argument("--dir", required=True)
    p_check.add_argument("--rubric", action="append", required=True)
    p_check.add_argument("--judge-model", required=True)
    p_check.set_defaults(func=_cmd_check_calibration)

    p_pf = sub.add_parser("promptfoo-config", help="check/write the promptfoo gate config")
    p_pf.add_argument("--check", action="store_true")
    p_pf.add_argument("--write", action="store_true")
    p_pf.add_argument("--path", default="")
    p_pf.set_defaults(func=_cmd_promptfoo_config)

    p_base = sub.add_parser("baseline", help="write/check a regression-baseline ratchet")
    p_base.add_argument("--path", required=True)
    p_base.add_argument("--write", action="store_true")
    p_base.add_argument("--check", action="store_true")
    p_base.add_argument("--suite", default="rag")
    p_base.add_argument("--golden-set-hash", default="")
    p_base.add_argument("--judge-model", default="")
    p_base.add_argument("--metrics-json", required=True)
    p_base.add_argument("--tolerance", action="append")
    p_base.set_defaults(func=_cmd_baseline)

    p_trend = sub.add_parser("trend", help="print the eval-run metric trend for a suite")
    p_trend.add_argument("--db", required=True, help="SQLAlchemy URL of the eval-run store")
    p_trend.add_argument("--suite", required=True)
    p_trend.add_argument("--limit", type=int, default=50)
    p_trend.set_defaults(func=_cmd_trend)

    p_run = sub.add_parser("run", help="run Route A in the worker composition root")
    p_run.add_argument("--principal-id", required=True)
    p_run.add_argument("--suite", default="route-a")
    p_run.add_argument("--golden", default="")
    p_run.set_defaults(func=_cmd_run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except NeEvalError as exc:
        print(f"ERROR: {exc}")
        return 1
