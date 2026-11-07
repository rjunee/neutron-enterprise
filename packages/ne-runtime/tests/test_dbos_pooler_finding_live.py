# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FIXED (NE-POOLER-TIMEOUTS, 2026-07-24): proves ``create_engine_with_timeouts``
now applies its server-side statement/lock timeouts THROUGH a transaction-mode
PgBouncer, rather than pinning the FATAL this file used to assert.

HISTORY — the finding (NE-LP-DBOS-REALPG, 2026-07-24): ``create_engine_with_timeouts``
(``ne.audit.engine``, the P2-16 chokepoint ``ne.api.platform.build_platform``
routes ``runtime_engine``/``audit_engine``/``skills_engine`` through, i.e. the
WHOLE ne-worker/ne-api composition root) used to set Postgres server-side
``statement_timeout``/``lock_timeout`` via the libpq ``options`` startup
parameter: ``options="-c statement_timeout=30000 -c lock_timeout=10000"``.
PgBouncer >=1.20 (github.com/pgbouncer/pgbouncer PR #878, a deliberate security
tightening against arbitrary-GUC startup-parameter injection) REJECTS any ``-c``
setting in ``options`` that is not on its own tracked/allowed list — neither
``statement_timeout`` nor ``lock_timeout`` is on that list — with a FATAL
``unsupported startup parameter in options: statement_timeout`` at connection
time, before a single query runs. A worker subprocess pointed at a pooled
session/audit/runtime URL crashed on this exact FATAL inside ``build_platform``
and never wrote its heartbeat marker.

THE FIX (NE-POOLER-TIMEOUTS): ``ne.audit.engine.engine_kwargs_for_url`` no longer
emits an ``options`` connect_arg for postgresql at all.
``create_engine_with_timeouts`` instead installs a single SQLAlchemy engine
``"begin"`` event listener (see ``ne.audit.engine._pg_timeout_listener`` for the
full mechanism writeup) that re-issues both GUCs as ``SET LOCAL`` at the start
of EVERY transaction — direct or pooled alike, since SQLAlchemy autobegins a
transaction (firing ``"begin"``) on the first statement of any connection use.
This is the pooler-CORRECT mechanism: transaction-mode PgBouncer may hand each
transaction a DIFFERENT backend, so only a per-transaction reissue is safe, and
``SET LOCAL`` evaporates at commit/rollback so it can never leak onto another
pooler client's transaction.

A prior revision of this fix ALSO installed a pool ``"connect"`` listener
issuing a session-wide ``SET`` once per physical DBAPI connection (reasoning it
preserved old direct-connection semantics for a wedged client). That was WRONG
and was reverted (Argus review, 2026-07-24): transaction-mode PgBouncer does
NOT run ``DISCARD ALL`` between clients by default, so that connect-time
``SET`` persisted on whichever backend it landed on and silently applied to
EVERY OTHER pooler client later handed that same backend — reproduced by hand:
an ne-audit engine with a 500ms timeout connecting through the pool caused a
COMPLETELY UNRELATED client of the same pool to have its own unrelated
``pg_sleep(2)`` cancelled with "canceling statement due to statement timeout".
The begin-only, ``SET LOCAL``-only mechanism has no such leak.

THE TEMPTING WRONG FIX, still forbidden: PgBouncer's ``ignore_startup_parameters``.
Separately verified by hand (2026-07-24): configuring it to include
``statement_timeout``/``lock_timeout`` makes the connection succeed, but
``SHOW statement_timeout`` then reports the Postgres default (``0`` —
unbounded) instead of the configured value. That "fix" does not restore the
timeout — it SILENTLY DISCARDS it, re-opening the exact unbounded-hang P2-16
exists to prevent. This CI lane's PgBouncer config deliberately leaves
``IGNORE_STARTUP_PARAMETERS`` at its default (see ``.github/workflows/ci.yml``)
— ``test_pooler_still_rejects_untracked_startup_options_guc`` below is the
canary that keeps that honest (P3-3, redesigned NE-POOLER-PREPING-BOUND,
2026-07-24, with sound BEHAVIORAL detection rather than a URL-sniffing
heuristic): a FATAL on connect means the pooler rejected the GUC outright
(pass); a successful connect whose ``SHOW statement_timeout`` reports
``'30s'`` means there is no pooler in front at all (direct Postgres applies
the GUC — skip, pooler-only); a successful connect reporting anything else
(in practice ``'0'``) means the pooler accepted-then-silently-DISCARDED the
untracked GUC — exactly the accept-then-discard misconfiguration this
paragraph warns about — and the canary now FAILS LOUDLY naming it, rather
than the old design's would-be silent green. If it ever fails on the pooled
lane, investigate the pooler config — do NOT delete the canary.

Gated on ``NE_RUNTIME_DBOS_DATABASE_URL`` (the same env-gate precedent as the
promoted DBOS-live suites), and wired ONLY into the ``live-postgres-pooled`` CI
job — never ``live-postgres``. The canary itself now tolerates a direct URL
(it SKIPS rather than failing confusingly — see its own docstring), but this
file's OTHER tests exist specifically to prove pooled-lane behavior, so there
is no reason to also wire it into the unpooled lane.
"""

from __future__ import annotations

import os

import pytest

_URL = os.environ.get("NE_RUNTIME_DBOS_DATABASE_URL")


def _reachable(url: str) -> bool:
    try:
        import sqlalchemy as sa

        engine = sa.create_engine(url)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


if not (_URL and _reachable(_URL)):
    pytest.skip(
        "pending-live-verification: set NE_RUNTIME_DBOS_DATABASE_URL to a reachable "
        "Postgres (the live-postgres-pooled CI lane runs this through a transaction-"
        f"mode PgBouncer) (url {_URL!r} unset/unreachable)",
        allow_module_level=True,
    )

assert _URL is not None


def test_engine_with_timeouts_applies_timeouts_through_pooler() -> None:
    """The anti-silent-downgrade regression stop: through the pooler, the
    configured timeouts genuinely apply (never ``'0'`` / unbounded) and persist
    across TWO consecutive transactions on one checkout — the exact case a
    connect-only listener would silently get wrong under transaction pooling."""
    import sqlalchemy as sa

    from ne.audit.engine import create_engine_with_timeouts

    assert _URL is not None
    engine = create_engine_with_timeouts(_URL)
    try:
        with engine.connect() as conn:
            for _ in range(2):
                stmt = conn.execute(sa.text("SHOW statement_timeout")).scalar_one()
                lock = conn.execute(sa.text("SHOW lock_timeout")).scalar_one()
                assert stmt == "30s", f"expected 30s, got {stmt!r} (silent downgrade?)"
                assert lock == "10s", f"expected 10s, got {lock!r} (silent downgrade?)"
                conn.commit()
    finally:
        engine.dispose()


def test_statement_timeout_fires_through_pooler() -> None:
    """A short statement_timeout genuinely aborts a long-running statement
    THROUGH the pooler — proves the SET LOCAL mechanism is not just present but
    load-bearing."""
    import sqlalchemy as sa
    from sqlalchemy.exc import OperationalError

    from ne.audit.engine import EngineTimeouts, create_engine_with_timeouts

    assert _URL is not None
    engine = create_engine_with_timeouts(_URL, timeouts=EngineTimeouts(statement_timeout_ms=500))
    try:
        with engine.connect() as conn, pytest.raises(OperationalError) as exc_info:
            conn.execute(sa.text("SELECT pg_sleep(5)"))
        assert "statement timeout" in str(exc_info.value)
    finally:
        engine.dispose()


def test_pooler_still_rejects_untracked_startup_options_guc() -> None:
    """CANARY (redesigned, P3-3): pins that this lane's PgBouncer still FATALs
    on an untracked ``-c`` GUC carried in the libpq ``options`` startup
    parameter — i.e. that ``IGNORE_STARTUP_PARAMETERS`` has NOT been
    configured to accept-then-discard ``statement_timeout``/``lock_timeout``.

    Detection is BEHAVIORAL, not a URL-sniffing heuristic (a heuristic that
    guesses wrong on a pooled-but-direct-looking URL would silently re-open
    the exact leak this canary exists to catch): if the connect attempt
    raises ``OperationalError``, the pooler REJECTED the GUC — pass, the
    happy/expected case in this lane. If it instead SUCCEEDS, the connection
    is interrogated to find out WHY:

    * ``SHOW statement_timeout == '30s'`` means the GUC was actually APPLIED
      — this is what a DIRECT (non-pooled) Postgres does (there is no
      PgBouncer to reject it), so the canary correctly does not apply outside
      a pooled lane and SKIPS with a clear reason rather than failing
      confusingly.
    * anything else (in practice ``'0'``) means the pooler ACCEPTED the
      connection but SILENTLY DISCARDED the untracked GUC —
      ``ignore_startup_parameters`` has been configured to accept-then-drop
      it, which is the exact "THE TEMPTING WRONG FIX" the module docstring
      warns about (a silent P2-16 downgrade). This FAILS loudly, strictly
      stronger than the old design (which only ever asserted the FATAL
      message and would have hung or errored confusingly on this path
      instead of naming the misconfiguration).

    Investigate the pooler config on a FAIL; do not delete this canary.
    """
    import sqlalchemy as sa
    from sqlalchemy.exc import OperationalError

    assert _URL is not None
    engine = sa.create_engine(_URL, connect_args={"options": "-c statement_timeout=30000"})
    try:
        try:
            with engine.connect() as conn:
                stmt = conn.execute(sa.text("SHOW statement_timeout")).scalar_one()
        except OperationalError as exc:
            message = str(exc)
            assert "unsupported startup parameter in options: statement_timeout" in message, (
                f"expected PgBouncer's startup-parameter rejection naming the GUC, got: {message}"
            )
            return  # pooler behaved: rejected the untracked GUC outright.

        if stmt == "30s":
            pytest.skip(
                "direct (non-pooled) Postgres applies the options GUC instead of "
                "rejecting it — this canary is pooler-only; run against a "
                "transaction-mode PgBouncer to exercise it"
            )
        pytest.fail(
            "pooler ACCEPTED the connection but the untracked options GUC did not "
            f"apply (SHOW statement_timeout == {stmt!r}, expected '30s' or a FATAL): "
            "ignore_startup_parameters was configured to accept-then-DISCARD it — "
            "see this module's docstring, 'THE TEMPTING WRONG FIX' section"
        )
    finally:
        engine.dispose()


def test_no_cross_client_timeout_leak_through_pooler() -> None:
    """The live no-leak proof NE-POOLER-PREPING-BOUND's brief demands: an
    ``ne.audit.engine.create_engine_with_timeouts`` engine (A, a 500ms
    ``statement_timeout``) uses the pool — including a SECOND checkout so the
    bounded pre-ping listener's checkout path actually runs against a REUSED
    connection, not just a freshly-connected one — and then a completely
    separate, plain ``sa.create_engine`` client (B) through the SAME
    transaction-mode PgBouncer must see NEITHER A's timeout config
    (``SHOW statement_timeout == '0'``) NOR its effects (a full
    ``pg_sleep(1)`` completes uncancelled). This is the exact scenario the
    #329 revert was about: a session-wide connect-time ``SET`` on ANY engine
    leaks onto whichever OTHER pooler client is later handed the same backend
    connection under transaction pooling."""
    import time

    import sqlalchemy as sa

    from ne.audit.engine import EngineTimeouts, create_engine_with_timeouts

    assert _URL is not None
    engine_a = create_engine_with_timeouts(_URL, timeouts=EngineTimeouts(statement_timeout_ms=500))
    try:
        with engine_a.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
            conn.commit()
        # A second checkout on the SAME engine: the pre-ping checkout
        # listener's non-fresh probe path runs here (see
        # ``ne.audit.engine._pre_ping_listeners``), on a REUSED connection.
        with engine_a.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
            conn.commit()
    finally:
        engine_a.dispose()

    engine_b = sa.create_engine(_URL)
    try:
        with engine_b.connect() as conn:
            stmt = conn.execute(sa.text("SHOW statement_timeout")).scalar_one()
            assert stmt == "0", f"leak: B saw statement_timeout={stmt!r} after A used the pool"
            # NOT `result is not None`: psycopg3 loads pg_sleep's void return as
            # `''`, so that check is vacuously true and asserts nothing about
            # leakage (Argus review, 2026-07-24). The actual no-leak signal is
            # that the statement was never cancelled -- i.e. it genuinely ran
            # for ~1s rather than being aborted around the 500ms mark by a
            # leaked statement_timeout from engine A.
            t0 = time.monotonic()
            conn.execute(sa.text("SELECT pg_sleep(1)"))
            elapsed = time.monotonic() - t0
            assert elapsed >= 0.9, (
                f"pg_sleep(1) returned after only {elapsed}s -- looks cancelled, "
                "i.e. engine A's 500ms statement_timeout leaked onto this client"
            )
            conn.commit()
    finally:
        engine_b.dispose()
