# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pod-kill-during-cron LIVE recovery proof (NE-LP-DBOS-REALPG).

Gated on ``NE_RUNTIME_DBOS_DATABASE_URL`` — the ``live-postgres`` CI lane (direct)
and the ``live-postgres-pooled`` CI lane (through a transaction-mode PgBouncer —
NE-POOLER-TIMEOUTS) both run this. SIGKILLs
``python -m ne.worker`` while the ALWAYS-ON ``identity-credential-rotation`` cron
job is blocked mid-execution, restarts the worker, and asserts the recovery
contract actually implemented by :class:`~ne.runtime.scheduler.CronScheduler`.

THE BLOCKING POINT IS THE GOVERNED PDP DECISION, NOT THE MODEL GATEWAY. The
rotation job is ``scheduled_execution='sole-tool'`` (63279abe / 51844fa9 F6):
the scheduler invokes ``write.internal.identity.credential_rotation`` directly
and no model loop — and therefore no ``ChoreographyGateway`` call — ever runs
on the cron path. ``NE_WORKER_TEST_MODE=cron-block`` (see ``ne.worker.testing``)
makes the scripted OPA transport hold that one action's decision open: the body
marks ``start`` AFTER the claim CAS committed and blocks until ``release``,
which is the same mid-body SIGKILL window the retired agent-mode choreography
provided.

DISCOVERED SEMANTICS (the honest recovery contract this test pins):
``CronScheduler._claim`` CAS-advances a job's ``next_run_at`` to the NEXT cron
window in the SAME transaction that inserts the ``ne_job_runs`` started row —
BEFORE the job body (``_run_claimed_scheduled_sole_tool``) ever executes. There
is no ``@ne_step`` boundary inside ``_dbos_cron_tick``/``run_due_jobs``, so a
SIGKILL mid-body leaves:

* the killed ``sched-cron.tick-%`` DBOS workflow PENDING, and its claimed
  ``ne_job_runs`` row honestly unfinished (``finished_at`` stays NULL forever —
  no scheduler code ever revisits an abandoned run row);
* on restart, DBOS recovery REPLAYS the whole tick workflow function from the
  top; it calls ``run_due_jobs()`` again, but the claim already advanced
  ``next_run_at`` past "now" (the CAS committed before the crash), so the
  replay finds nothing due and the recovered tick workflow reaches SUCCESS
  quickly WITHOUT re-running the job body;
* the job body actually re-executes (a SECOND ``start`` marker) only at the
  NEXT scheduled window, under a brand-new tick workflow and a brand-new
  claimed run row, which completes successfully once released.

Net: the job body runs AT-MOST-ONCE PER WINDOW (never replayed by DBOS
step-recovery — this is NOT step-level exactly-once), while the WORK ITSELF
completes EXACTLY ONCE overall (one succeeded ``ne_job_runs`` row; the killed
run row never flips to success). That is the contract this test pins — not
"the killed job magically resumes," but "the tick workflow recovers cleanly
and the scheduler's claim-before-run design makes a mid-body crash safe by
construction: no lost job, no double-run job body."

Uses ``NE_AGENT_IDENTITY_CREDENTIALS__ROTATE_AFTER_SECONDS=1440`` so
``rotation_check_cron`` (``1440 // 24 = 60`` seconds) encodes to
``*/1 * * * *`` (see ``ne.worker.credential_rotation.rotation_check_cron`` /
``ne.worker.principal_sync.interval_to_cron``) — the ALWAYS-ON
``identity-credential-rotation`` job fires every minute instead of hourly.
``grace_seconds`` defaults to 300s (< 1440s), so ``AgentCredentialConfig``
still validates.

The pre-clean is STRICTLY SCOPED (never TRUNCATE): only ``sched-cron.tick-%``
rows, the marker table, and the ``identity-credential-rotation`` job's own rows
are deleted — the sibling cron-tick proof (``test_worker_cron_dbos_live.py``)
and the SIGKILL/HITL proofs sharing ``ne_worker_dbos``/the session DB are
unharmed. Alphabetical pytest file ordering
(``..._cron_dbos_live`` < ``..._cron_podkill_dbos_live`` < ``..._dbos_live`` <
``..._hitl_live``) means no other worker is live during this test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

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

_JOB_NAME = "identity-credential-rotation"


def _worker_env(marker_path: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "NE_WORKER_SEAMS": "ne.worker.testing:ci_seams",
            "NE_WORKER_TEST_MODE": "cron-block",
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
            # 1440 // 24 = 60s -> interval_to_cron(60) = '*/1 * * * *': the
            # ALWAYS-ON rotation job fires every minute instead of hourly.
            # grace_seconds defaults to 300s (< 1440s) so it still validates.
            "NE_AGENT_IDENTITY_CREDENTIALS__ROTATE_AFTER_SECONDS": "1440",
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


def _preclean() -> None:
    """Scoped pre-clean: prior tick rows, test markers, and the rotation job's own rows.

    Never TRUNCATE — every DELETE is scoped to this test's own state, guarded by a
    ``to_regclass`` existence check (the sibling live suites' precedent), so a
    fresh/never-migrated DB is a silent no-op rather than an error.
    """
    dbos_engine = sa.create_engine(_WORKER_DBOS_URL)
    try:
        with dbos_engine.connect() as conn:
            present = conn.execute(sa.text("SELECT to_regclass('dbos.workflow_status')")).scalar()
        if present is not None:
            with dbos_engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "DELETE FROM dbos.workflow_status "
                        "WHERE workflow_uuid LIKE 'sched-cron.tick-%'"
                    )
                )
    finally:
        dbos_engine.dispose()

    session_engine = sa.create_engine(_SESSION_URL)
    try:
        # Unconditionally ensure the marker table exists (mirrors the sibling
        # SIGKILL proof's ``_clear_markers``) — NOT guarded by ``to_regclass``
        # like the job-row cleanup below. ``_marker_count`` polls this table
        # BEFORE the worker's first cron-block ``start`` marker write would
        # otherwise create it (e.g. on a fresh DB where no prior test happened to
        # trigger a marker write), and an ungated poll would hit
        # ``UndefinedTable`` while racing that first write.
        from ne.worker.testing import ensure_marker_table

        ensure_marker_table(session_engine)
        with session_engine.connect() as conn:
            jobs_present = conn.execute(sa.text("SELECT to_regclass('ne_scheduled_jobs')")).scalar()
            runs_present = conn.execute(sa.text("SELECT to_regclass('ne_job_runs')")).scalar()
        with session_engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM ne_worker_test_markers"))
            if jobs_present is not None:
                job_id = conn.execute(
                    sa.text("SELECT job_id FROM ne_scheduled_jobs WHERE name = :n"),
                    {"n": _JOB_NAME},
                ).scalar()
                if job_id is not None:
                    if runs_present is not None:
                        conn.execute(
                            sa.text("DELETE FROM ne_job_runs WHERE job_id = :j"), {"j": job_id}
                        )
                    conn.execute(
                        sa.text("DELETE FROM ne_scheduled_jobs WHERE job_id = :j"), {"j": job_id}
                    )
    finally:
        session_engine.dispose()


def _marker_count(kind: str) -> int:
    engine = sa.create_engine(_SESSION_URL)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    sa.text("SELECT count(*) FROM ne_worker_test_markers WHERE kind = :k"),
                    {"k": kind},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _insert_release() -> None:
    from ne.worker.testing import _write_marker

    _write_marker(_SESSION_URL, "release")


def _rotation_job_id() -> str:
    engine = sa.create_engine(_SESSION_URL)
    try:
        with engine.connect() as conn:
            job_id = conn.execute(
                sa.text("SELECT job_id FROM ne_scheduled_jobs WHERE name = :n"), {"n": _JOB_NAME}
            ).scalar()
        assert job_id is not None, f"{_JOB_NAME!r} job was never seeded"
        return str(job_id)
    finally:
        engine.dispose()


def _unfinished_run_ids(job_id: str) -> list[str]:
    engine = sa.create_engine(_SESSION_URL)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT run_id FROM ne_job_runs WHERE job_id = :j AND finished_at IS NULL"),
                {"j": job_id},
            ).all()
        return [str(r.run_id) for r in rows]
    finally:
        engine.dispose()


def _succeeded_run_count(job_id: str) -> int:
    engine = sa.create_engine(_SESSION_URL)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    sa.text(
                        "SELECT count(*) FROM ne_job_runs WHERE job_id = :j AND succeeded = TRUE"
                    ),
                    {"j": job_id},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _run_succeeded(job_id: str, run_id: str) -> bool | None:
    engine = sa.create_engine(_SESSION_URL)
    try:
        with engine.connect() as conn:
            value = conn.execute(
                sa.text("SELECT succeeded FROM ne_job_runs WHERE job_id = :j AND run_id = :r"),
                {"j": job_id, "r": run_id},
            ).scalar_one()
        return None if value is None else bool(value)
    finally:
        engine.dispose()


def _pending_tick_uuids() -> list[str]:
    engine = sa.create_engine(_WORKER_DBOS_URL)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT workflow_uuid FROM dbos.workflow_status "
                    "WHERE workflow_uuid LIKE 'sched-cron.tick-%' AND status = 'PENDING'"
                )
            ).all()
        return [str(r.workflow_uuid) for r in rows]
    finally:
        engine.dispose()


def _tick_status(workflow_uuid: str) -> str | None:
    engine = sa.create_engine(_WORKER_DBOS_URL)
    try:
        with engine.connect() as conn:
            value = conn.execute(
                sa.text("SELECT status FROM dbos.workflow_status WHERE workflow_uuid = :w"),
                {"w": workflow_uuid},
            ).scalar()
        return None if value is None else str(value)
    finally:
        engine.dispose()


def test_podkill_mid_cron_job_recovers_exactly_once(tmp_path: Path) -> None:
    """SIGKILL the worker mid-cron-job-body; assert the honest recovery contract.

    See the module docstring for the DISCOVERED SEMANTICS this test pins (and
    for where the body blocks now that the rotation job is sole-tool):
    at-most-once-per-window body execution (claim-CAS precedes the body, so DBOS
    step-recovery never replays it), exactly-once completion overall (one
    succeeded ``ne_job_runs`` row; the killed run row stays unfinished forever),
    and the killed tick workflow itself recovering cleanly to SUCCESS.
    """
    _preclean()

    marker_path = str(tmp_path / "worker.health")
    proc = subprocess.Popen([sys.executable, "-m", "ne.worker"], env=_worker_env(marker_path))
    try:
        _wait_marker(marker_path)

        # First cron window claims + starts the rotation job's body — the
        # cron-block OPA seam marks 'start' at the governed PDP decision inside
        # the sole-tool invocation, then blocks for 'release'.
        deadline = time.time() + 150.0
        while time.time() < deadline and _marker_count("start") < 1:
            time.sleep(0.2)
        assert _marker_count("start") == 1, "the rotation job never reached the blocking point"

        # Capture the PENDING tick workflow AND the (unique) unfinished run row
        # BEFORE the kill — both are asserted post-recovery below.
        pending = _pending_tick_uuids()
        assert len(pending) == 1, f"expected exactly one PENDING tick, got {pending!r}"
        killed_tick = pending[0]

        job_id = _rotation_job_id()
        unfinished = _unfinished_run_ids(job_id)
        assert len(unfinished) == 1, f"expected exactly one unfinished run, got {unfinished!r}"
        killed_run_id = unfinished[0]

        # SIGKILL mid-body — no graceful shutdown, no chance to finish the run row.
        proc.kill()
        proc.wait(timeout=30)

        # Restart. DBOS recovery replays the killed tick workflow from the top.
        proc = subprocess.Popen([sys.executable, "-m", "ne.worker"], env=_worker_env(marker_path))
        _wait_marker(marker_path)

        # The job body re-executes ONLY at the next scheduled window (a NEW tick
        # workflow, a NEW claimed run row) — a SECOND 'start' marker.
        deadline = time.time() + 150.0
        while time.time() < deadline and _marker_count("start") < 2:
            time.sleep(0.2)
        assert _marker_count("start") == 2, (
            "the rotation job did not re-execute at the next scheduled window"
        )

        # Release the (second window's) blocked run.
        _insert_release()

        # Exactly ONE succeeded run; the killed run row stays unfinished forever
        # (claim-before-run means the scheduler never revisits an abandoned run row).
        deadline = time.time() + 120.0
        while time.time() < deadline and _succeeded_run_count(job_id) < 1:
            time.sleep(0.5)
        assert _succeeded_run_count(job_id) == 1, "the released run never completed successfully"
        assert _run_succeeded(job_id, killed_run_id) is not True, (
            "the SIGKILLed run row must never flip to success"
        )

        # The killed tick workflow itself recovers to SUCCESS (fail fast on ERROR).
        deadline = time.time() + 120.0
        status: str | None = None
        while time.time() < deadline:
            status = _tick_status(killed_tick)
            if status == "ERROR":
                raise AssertionError(f"killed tick workflow {killed_tick} recovered to ERROR")
            if status == "SUCCESS":
                break
            time.sleep(0.5)
        assert status == "SUCCESS", f"killed tick {killed_tick} never reached SUCCESS ({status!r})"

        # Stop worker #2 IMMEDIATELY — no further scheduled windows fire (the next
        # one is ~a minute away, well outside the margin below).
        proc.kill()
        proc.wait(timeout=30)

        # Final counts are stable post-kill: a 3rd start marker or a 2nd succeeded
        # run would mean the job body double-executed.
        assert _marker_count("start") == 2
        assert _succeeded_run_count(job_id) == 1
    finally:
        proc.kill()
        proc.wait(timeout=30)
