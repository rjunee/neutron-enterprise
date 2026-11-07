# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Operator CLI for administrative Run settlement — ``python -m ne.worker.settle``.

Runs the two repairs in :mod:`ne.runtime.run_settlement` over the SAME composed
platform the worker itself boots (one recipe, two processes):

* drain/backfill the parked-approval audit outbox, so every park — including
  ones that predate the live drain — carries its mandatory
  ``decision_kind='approval'`` Run audit receipt and can reach a terminal;
* settle runs stranded in ``running`` with an honest FAILED terminal
  (``--settle``), classing a run whose record shows an issued effect as
  ``RunEffectSettlementUncertain`` instead of inventing an outcome.

Defaults to a DRY RUN.  Pass ``--apply`` to write.  Must run inside the worker
container environment (it composes the platform from the same settings).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ne.worker.settle",
        description="Backfill parked-approval audit receipts and settle stranded runs.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write; without this flag every action is reported as a dry run",
    )
    parser.add_argument(
        "--settle",
        action="store_true",
        help="also settle stale running runs (the outbox drain always runs)",
    )
    parser.add_argument(
        "--stale-after-minutes",
        type=int,
        default=60,
        help="only settle runs whose run row AND lease are older than this (default 60)",
    )
    parser.add_argument("--limit", type=int, default=50, help="max runs to settle per pass")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> int:
    # Heavy composition imports live here so ``--help`` stays instant.
    from ne.api.platform import build_platform
    from ne.api.settings import ApiSettings
    from ne.runtime.run_settlement import (
        drain_parked_approval_audit_outbox,
        settle_stranded_runs,
    )
    from ne.runtime.run_workflow import RunWorkflowStateStore
    from ne.worker.main import _resolve_seams
    from ne.worker.settings import WorkerSettings

    api_settings = ApiSettings()  # type: ignore[call-arg]
    worker_settings = WorkerSettings()  # type: ignore[call-arg]
    platform = build_platform(api_settings, _resolve_seams(worker_settings.seams))
    if platform.agent_run_store is None:
        print("canonical Run storage is not composed; nothing to settle", file=sys.stderr)
        return 2
    dry_run = not args.apply
    drained = await drain_parked_approval_audit_outbox(
        engine=platform.runtime_engine,
        run_store=platform.agent_run_store,
        sink=platform.sink,
        audit_engine=platform.audit_engine,
        dry_run=dry_run,
    )
    for intent in drained:
        print(
            f"outbox {intent.intent_id} approval={intent.approval_id} "
            f"run={intent.run_id} -> {intent.action}"
        )
    if not drained:
        print("approval audit outbox: nothing unclaimed")
    if args.settle:
        state = RunWorkflowStateStore(platform.runtime_engine, run_store=platform.agent_run_store)
        settlements = await settle_stranded_runs(
            state=state,
            run_store=platform.agent_run_store,
            sink=platform.sink,
            audit_engine=platform.audit_engine,
            stale_after=timedelta(minutes=args.stale_after_minutes),
            limit=args.limit,
            dry_run=dry_run,
        )
        for settlement in settlements:
            suffix = (
                "" if settlement.failure_class is None else f" class={settlement.failure_class}"
            )
            print(f"run {settlement.run_id} -> {settlement.disposition}{suffix}")
        if not settlements:
            print("stranded runs: none past the staleness horizon")
    if dry_run:
        print("dry run (pass --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
