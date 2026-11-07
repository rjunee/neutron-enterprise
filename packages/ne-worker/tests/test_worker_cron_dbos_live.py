# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""P1-6 LIVE proof: the DBOS-scheduled cron tick ACTUALLY fires in dbos mode.

Gated on ``NE_RUNTIME_DBOS_DATABASE_URL`` — the ``live-postgres`` CI lane (direct)
and the ``live-postgres-pooled`` CI lane (through a transaction-mode PgBouncer —
NE-POOLER-TIMEOUTS) both run this. Boots
``python -m ne.worker`` as a SUBPROCESS with ``NE_RUNTIME_DURABILITY=dbos`` and the CI
stub seams, then asserts that a ``dbos.workflow_status`` row with
``workflow_uuid LIKE 'sched-cron.tick-%'`` and ``name='cron.tick'`` reaches status
``SUCCESS`` within a boundary-relative deadline (next minute boundary + jitter +
execution budget), measured from the worker's heartbeat rather than from boot.

Non-vacuity: on revert to scheduling the PLAIN function, dbos 2.26 raises
``DBOSWorkflowFunctionNotFoundError`` inside the scheduler thread BEFORE any row is
inserted, so NO ``sched-cron.tick-%`` row ever appears and this test fails at the
deadline — proving the tick executes as a real governed workflow only with the fix.

The pre-clean deletes ONLY ``sched-cron.tick-%`` rows (never TRUNCATE) so the sibling
SIGKILL proof (``test_worker_dbos_live.py``) sharing the ``ne_worker_dbos`` DB is
unharmed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

_URL = os.environ.get("NE_RUNTIME_DBOS_DATABASE_URL")


def _reachable(url: str) -> bool:
    try:
        engine = sa.create_engine(url)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


if not (_URL and _reachable(_URL)):
    pytest.skip(
        "pending-live-verification: set NE_RUNTIME_DBOS_DATABASE_URL (the live-postgres "
        f"and live-postgres-pooled CI lanes both run this) (url {_URL!r} unset/unreachable)",
        allow_module_level=True,
    )

assert _URL is not None


def _swap_db(url: str, db: str) -> str:
    base, _, _old = url.rpartition("/")
    return f"{base}/{db}"


_SESSION_URL = _swap_db(_URL, "postgres")
_WORKER_DBOS_URL = _swap_db(_URL, "ne_worker_dbos")


def _worker_env(marker_path: str, *, mode: str = "block") -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "NE_WORKER_SEAMS": "ne.worker.testing:ci_seams",
            "NE_WORKER_TEST_MODE": mode,
            "NE_WORKER_DBOS_DATABASE_URL": _WORKER_DBOS_URL,
            "NE_WORKER_HEARTBEAT_PATH": marker_path,
            "NE_RUNTIME_DURABILITY": "dbos",
            "NE_RUNTIME_SESSION_DATABASE_URL": _SESSION_URL,
            "NE_AUDIT_DATABASE_URL": _SESSION_URL,
            "NE_PERMISSIONS_DATABASE_URL": _SESSION_URL,
            "NE_API_QDRANT_URL": "http://qdrant.invalid",
            "NE_API_QDRANT_COLLECTION": "t",
            "NE_API_TEI_URL": "http://tei.invalid",
            "NE_AGENT_PRIVATE_PAYLOAD_KEY_ID": "worker-live-k1",
            "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64": ("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s"),
            "NE_AGENT_TRANSPORT_GENERATION": "agent-transport-v1",
        }
    )
    return env


def _wait_marker(path: str, *, timeout_s: float = 90.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 60.0:
            return
        time.sleep(0.5)
    raise AssertionError(f"worker heartbeat marker {path} never appeared")


def _preclean_cron_tick_rows() -> None:
    """Scoped delete of prior ``sched-cron.tick-%`` rows ONLY (never TRUNCATE).

    The sibling SIGKILL proof shares ``ne_worker_dbos``; a wider delete would destroy
    its recovery state if the two lanes ever interleave.
    """
    engine = sa.create_engine(_WORKER_DBOS_URL)
    try:
        with engine.connect() as conn:
            present = conn.execute(sa.text("SELECT to_regclass('dbos.workflow_status')")).scalar()
        if present is not None:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "DELETE FROM dbos.workflow_status "
                        "WHERE workflow_uuid LIKE 'sched-cron.tick-%'"
                    )
                )
    finally:
        engine.dispose()


def test_dbos_cron_tick_fires_and_succeeds(tmp_path: Path) -> None:
    _preclean_cron_tick_rows()

    marker_path = str(tmp_path / "worker.health")
    proc = subprocess.Popen([sys.executable, "-m", "ne.worker"], env=_worker_env(marker_path))
    engine = sa.create_engine(_WORKER_DBOS_URL)
    try:
        _wait_marker(marker_path)

        # '* * * * *' fires at the next minute boundary + <=10s dbos jitter + the
        # internal-queue dispatch. Fail FAST on an ERROR row rather than burning the
        # whole deadline.
        #
        # The deadline is measured FROM THE NEXT MINUTE BOUNDARY, not as a flat
        # wall-clock budget, because a flat budget silently folds "where the second
        # hand happened to be when the worker came up" into the execution margin.
        # Counting the jitter allowance consistently on both ends, the previous flat
        # 150s left ~139s of execution margin for a run that reached this line at :59
        # (150 - 1 - 10) and only ~81s for one that reached it at :01 (150 - 59 - 10).
        # The same constant meant different things run to run, so the run that failed
        # was the unlucky one rather than the broken one. Observed 2026-07-31: this
        # test went red on a loaded CI runner (job in run 30597743622, on a branch
        # touching only ne-schema-bootstrap and ne-cli) and green on rerun with no
        # code change.
        #
        # _EXECUTION_BUDGET_S is set to the OLD SCHEME'S MAXIMUM, not to a round
        # number. That is the constraint that makes this a fix rather than a
        # re-roll: any value below ~139 would make the deadline TIGHTER than it used
        # to be for late-phase starts, so a change sold as de-flaking would introduce
        # reds on a required check. Raising it is safe; lowering it below the old max
        # is not, and that is a property a future editor can check rather than guess.
        #
        # HONEST STATUS OF THE NUMBER ITSELF: the tick's actual execution duration has
        # never been measured. The phase-dependence above is arithmetic and certain;
        # "120s of execution was enough and the phase was the problem" was NOT
        # established. The assertion below therefore reports the elapsed time and every
        # budget term, so the next failure says which one was actually exceeded instead
        # of leaving "the number wasn't big enough".
        _JITTER_BUDGET_S = 10.0  # dbos scheduler jitter, per the '* * * * *' schedule
        _EXECUTION_BUDGET_S = 150.0  # >= the old scheme's max margin; see above
        seconds_to_next_boundary = 60.0 - (time.time() % 60.0)
        row: Any = None
        rows: Any = []  # bound before the loop so the failure message is always renderable
        started_at = time.time()
        deadline = started_at + seconds_to_next_boundary + _JITTER_BUDGET_S + _EXECUTION_BUDGET_S
        while time.time() < deadline:
            with engine.connect() as conn:
                rows = conn.execute(
                    sa.text(
                        "SELECT workflow_uuid, status, name FROM dbos.workflow_status "
                        "WHERE workflow_uuid LIKE 'sched-cron.tick-%'"
                    )
                ).all()
            for candidate in rows:
                if candidate.status == "ERROR":
                    raise AssertionError(f"cron.tick workflow errored: {candidate!r}")
            success = [r for r in rows if r.status == "SUCCESS"]
            if success:
                row = success[0]
                break
            time.sleep(1.0)

        assert row is not None, (
            "no SUCCESS sched-cron.tick-% workflow row within the boundary-relative "
            f"deadline. Waited {time.time() - started_at:.1f}s of "
            f"{seconds_to_next_boundary + _JITTER_BUDGET_S + _EXECUTION_BUDGET_S:.1f}s "
            f"(boundary wait {seconds_to_next_boundary:.1f}s + jitter "
            f"{_JITTER_BUDGET_S}s + execution {_EXECUTION_BUDGET_S}s). "
            f"Rows seen at timeout: {rows!r}. If a row is present but not SUCCESS, the "
            "execution budget was exceeded; if none is present, the tick never fired "
            "and the boundary/jitter side is the suspect."
        )
        assert row.name == "cron.tick"
        assert row.workflow_uuid.startswith("sched-cron.tick-")
    finally:
        engine.dispose()
        proc.kill()
        proc.wait(timeout=30)
