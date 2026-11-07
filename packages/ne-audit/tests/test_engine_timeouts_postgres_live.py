# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE proof that ``create_engine_with_timeouts``'s statement/lock timeouts are
genuinely applied SERVER-SIDE, direct-connection shape (NE-POOLER-TIMEOUTS).

Gated on ``NE_TEST_POSTGRES_URL`` (the existing precedent in
``test_postgres_audit.py``). This file is wired into BOTH the ``live-postgres``
job (direct, ``:5432``) and the ``live-postgres-pooled`` job (through a
transaction-mode PgBouncer, ``:6432``) — the SAME assertions must hold true in
both shapes, since the point of NE-POOLER-TIMEOUTS is that the single
per-transaction ``SET LOCAL`` mechanism works identically whether the
connection is direct or pooled: SQLAlchemy autobegins a transaction (firing
``"begin"``) on the first statement of every connection use either way.

See ``packages/ne-runtime/tests/test_dbos_pooler_finding_live.py`` for the
pooled-lane-only regression proof (the historical FATAL this unit fixes) and
``packages/ne-audit/src/ne/audit/engine.py::_pg_timeout_listener`` for the full
mechanism writeup, including why an earlier connect-time-``SET`` variant of
this fix was tried and reverted (it leaked onto shared pooled backends).

A third test, ``test_autocommit_connection_gets_no_timeout``, pins the
documented AUTOCOMMIT gap: the begin-only listener deliberately emits nothing
on a DBAPI-autocommit connection (``SET LOCAL`` outside a real transaction
block would be a silently-unapplied no-op WARNING), so such a connection gets
the server default, never the configured timeout.

NE-POOLER-PREPING-BOUND (2026-07-24) adds five more live proofs, all riding
this same gated file so they run in BOTH CI shapes automatically:

* ``test_preping_probe_succeeds_against_real_healthy_connection`` — the
  healthy-path parity proof (Argus review, 2026-07-24): every other live test
  in this file exercises the FAILURE shapes (wedged, dead) against a fake or
  proxied connection; none proved the bounded probe genuinely SUCCEEDS
  against a real psycopg3 connection to a healthy server — a blind spot
  verified empirically by monkeypatching ``_bounded_dbapi_ping`` to always
  return False and observing every live test in this file AND
  ``test_dbos_pooler_finding_live.py`` still pass unchanged, because a
  permanently-failing probe just makes the pool transparently reconnect on
  every checkout. This test closes that gap directly against the real
  backend, without relying on ``pg_backend_pid()`` identity (which
  transaction-mode PgBouncer is free to change between transactions even on
  a perfectly healthy client-side pooled connection, making it an unsound
  signal in the pooled lane).
* ``test_inconclusive_timeout_does_not_invalidate_whole_pool`` — THE P2
  regression proof (Argus review, 2026-07-24, round 2): reproduces the
  reviewer's own live repro (force ``_bounded_dbapi_ping`` to always return
  ``None``, i.e. permanently inconclusive) and asserts, against a real
  healthy Postgres connection, that ``engine.pool._invalidate_time`` stays
  ``0`` — proving a probe TIMEOUT (as opposed to a definitive failure) never
  invalidates the whole pool, only the one connection it actually hit.
* ``test_wedged_preping_probe_times_out`` — THE hang-vs-fast-fail regression
  proof. A local
  ``_FreezableProxy`` sits in front of the real Postgres URL; once frozen it
  keeps every socket open but stops forwarding bytes (TCP-alive, wedged).
  Before this unit, a checkout against such a server hung forever (SQLAlchemy's
  native ``pool_pre_ping`` runs its ``SELECT 1`` liveness probe unbounded on
  the raw DBAPI connection). After this unit, the bounded checkout-listener
  probe (``ne.audit.engine._bounded_dbapi_ping``) times out inconclusively
  under ``pre_ping_timeout_s`` and raises
  ``DisconnectionError(invalidate_pool=False)`` (a timeout is NOT treated as
  a definitive failure — see ``ne.audit.engine._pre_ping_listeners``'s
  docstring, Argus P2 2026-07-24 review round 2), which the pool turns into
  an invalidate-this-connection-only + fresh-connection-retry — that retry,
  in turn, reconnects through the SAME frozen proxy and is bounded by libpq
  ``connect_timeout``, so the overall checkout still fails within a small
  bounded window rather than hanging. The second checkout runs on a joined
  background thread with an explicit timeout (Argus P3, same review round) so
  a reintroduced unbounded probe fails this test with a named assertion
  rather than burning the whole CI job's timeout budget.
* ``test_preping_replacement_still_recycles_dead_connections`` — the
  never-silently-downgrade proof: a genuinely DEAD pooled connection (the
  proxy's ``sever()`` closes the established sockets but keeps ACCEPTING new
  ones) is still transparently invalidated and replaced on checkout, exactly
  as native ``pool_pre_ping`` did — the dead-connection-recycle guarantee
  survived the mechanism swap.
* ``test_raw_connection_reports_no_session_timeout`` — the behavioral
  anti-leak pin (P3-2), replacing the old name-only
  ``_set_session_timeouts`` blacklist in ``test_engine_timeouts.py``: a raw
  DBAPI connection off ``create_engine_with_timeouts`` reports
  ``SHOW statement_timeout == '0'`` (never a session-wide value) in BOTH the
  direct and pooled lanes — catching a reintroduced connect-time ``SET``
  regardless of what it is named.

``test_preping_probe_leaves_connection_transaction_neutral`` (Codex C1,
2026-07-25) is the live rebuttal of a review finding that the probe's raw
``pgconn.send_query`` call (bypassing psycopg3's ``Connection``-level
autocommit/BEGIN bookkeeping entirely) would leave the connection mid
transaction (``INTRANS``) on checkout, breaking a subsequent
``execution_options(isolation_level=...)`` and pinning a pooler backend for
the whole checkout. It does not: a bare ``send_query`` with no preceding
``BEGIN`` runs as Postgres's own implicit, auto-committing single-statement
transaction at the wire-protocol level (independent of the driver's
client-side ``autocommit`` attribute, a concept ``pgconn`` has no notion of),
so the probe never opens a transaction block to begin with. Verified live,
not by argument.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import time

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from ne.audit.engine import EngineTimeouts, _bounded_dbapi_ping, create_engine_with_timeouts

_URL = os.environ.get("NE_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="live Postgres not configured (set NE_TEST_POSTGRES_URL); "
    "pending-live-verification ledger",
)


def test_default_timeouts_apply_inside_transactions() -> None:
    """SHOW statement_timeout/lock_timeout report the configured values inside a
    transaction, AND again in a SECOND transaction on the SAME checked-out
    connection — proving per-transaction reissue rather than first-transaction
    luck (the exact distinction a connect-only listener would get wrong)."""
    assert _URL is not None
    engine = create_engine_with_timeouts(_URL)
    try:
        with engine.connect() as conn:
            for _ in range(2):
                stmt = conn.execute(sa.text("SHOW statement_timeout")).scalar_one()
                lock = conn.execute(sa.text("SHOW lock_timeout")).scalar_one()
                assert stmt == "30s"
                assert lock == "10s"
                conn.commit()
    finally:
        engine.dispose()


def test_statement_timeout_actually_fires() -> None:
    """A short statement_timeout genuinely aborts a long-running statement
    server-side — the P2-16 guarantee the whole mechanism exists to keep."""
    assert _URL is not None
    engine = create_engine_with_timeouts(_URL, timeouts=EngineTimeouts(statement_timeout_ms=500))
    try:
        with engine.connect() as conn, pytest.raises(OperationalError) as exc_info:
            conn.execute(sa.text("SELECT pg_sleep(5)"))
        assert "statement timeout" in str(exc_info.value)
    finally:
        engine.dispose()


def test_autocommit_connection_gets_no_timeout() -> None:
    """Live pin of the documented AUTOCOMMIT gap (see
    ``ne.audit.engine._pg_timeout_listener``'s docstring, "NOT covered"
    paragraph): the ``"begin"`` event still fires on a DBAPI-autocommit
    connection (SQLAlchemy's autobegin bookkeeping is client-side and
    dialect-agnostic), but the listener detects
    ``driver_connection.autocommit`` and deliberately emits NOTHING, because no
    server-side transaction block opens for ``SET LOCAL`` to apply inside —
    issuing it there would be a silently-unapplied no-op WARNING, which is
    worse than skipping. So an AUTOCOMMIT connection off a
    ``create_engine_with_timeouts`` engine gets the Postgres server default
    (``'0'``, unbounded) rather than the configured timeout — never ``'30s'``.
    Nothing else in these live lanes issues session-level SETs after the
    connect-time-``SET`` revert, so the server default is exactly what should
    be observed here."""
    assert _URL is not None
    engine = create_engine_with_timeouts(_URL)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            stmt = conn.execute(sa.text("SHOW statement_timeout")).scalar_one()
        assert stmt != "30s"
        assert stmt == "0"
    finally:
        engine.dispose()


def test_preping_probe_succeeds_against_real_healthy_connection() -> None:
    """Healthy-path parity proof for the pre-ping replacement (Argus P2,
    2026-07-24): every other live pre-ping test here proves a FAILURE shape
    (wedged, dead); this one proves the common case — the bounded probe
    genuinely reports a healthy real connection as healthy — with two
    independent checks:

    (1) ``_bounded_dbapi_ping`` called directly against a real raw
        connection's ``dbapi_connection`` returns True — exercises the exact
        libpq send_query/flush/is_busy/consume_input/get_result drive
        against a real ``PGconn``, not the hand-written fake pgconn the
        unit-level tests in ``test_engine_timeouts.py`` use.
    (2) the checkout-listener-driven probe, exercised via two consecutive
        ``engine.connect()`` calls, does NOT force the pool to discard and
        reconnect: the SAME physical DBAPI connection object (identity, via
        ``id(conn.connection.driver_connection)``) is checked out both
        times. A permanently-failing probe would raise
        ``InvalidatePoolError`` on every non-fresh checkout, forcing a brand
        new physical connection every time and changing this identity —
        exactly the silent-downgrade shape the brief's invariant warns
        about. (Deliberately NOT ``pg_backend_pid()`` identity: under
        transaction-mode PgBouncer the SAME healthy client-side pooled
        connection can legitimately be handed a DIFFERENT backend server
        connection between transactions, making backend-pid equality an
        unsound signal in the pooled lane — this identity check is about
        SQLAlchemy's own client-side pool, unaffected by that.)
    """
    assert _URL is not None
    engine = create_engine_with_timeouts(_URL)
    try:
        raw = engine.raw_connection()
        try:
            assert _bounded_dbapi_ping(raw.dbapi_connection, 5.0) is True
        finally:
            raw.close()

        with engine.connect() as conn:
            first_id = id(conn.connection.driver_connection)
            conn.execute(sa.text("SELECT 1"))
            conn.commit()
        with engine.connect() as conn:
            second_id = id(conn.connection.driver_connection)
            conn.execute(sa.text("SELECT 1"))
            conn.commit()
        assert first_id == second_id, (
            "expected the pool to reuse the SAME physical DBAPI connection "
            "across two checkouts on a healthy engine (a successful pre-ping "
            f"does not invalidate); identity changed ({first_id} != "
            f"{second_id}), meaning the checkout-listener probe silently "
            "failed and forced a reconnect on a healthy connection"
        )
    finally:
        engine.dispose()


def test_inconclusive_timeout_does_not_invalidate_whole_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE P2 regression proof (Argus review, 2026-07-24, round 2), reproduced
    live against a real, healthy Postgres connection exactly as the reviewer
    described: monkeypatching ``_bounded_dbapi_ping`` to make the deadline
    permanently unreachable (returns ``None`` — inconclusive/timed-out,
    distinct from a definitive failure) and observing the pool's OWN
    ``_invalidate_time`` bookkeeping.

    SQLAlchemy's checkout retry loop (``sqlalchemy/pool/base.py``,
    ``_ConnectionFairy._checkout``) only bumps ``pool._invalidate_time`` (via
    ``pool._invalidate(fairy, e, _checkin=False)``) when the raised exception
    has ``invalidate_pool=True`` — i.e. only for ``InvalidatePoolError``, the
    exception this module raises on a DEFINITIVE probe failure. A plain
    ``DisconnectionError`` (``invalidate_pool=False``, what this module raises
    on an INCONCLUSIVE probe timeout) invalidates only the current fairy's own
    connection record and leaves ``pool._invalidate_time`` untouched — meaning
    every OTHER currently-idle pooled connection is left alone. This is the
    fix for the P2 finding: "three consecutive checkouts each got a distinct
    physical DBAPI connection ... degrading a pooled engine to
    connection-per-checkout" was the reviewer's own live reproduction of the
    OLD (pre-fix) behavior, where a probe timeout was indistinguishable from a
    definitive failure and always raised ``InvalidatePoolError`` (whole-pool
    invalidate).

    The checkout ITSELF still succeeds transparently, with no exception
    surfacing to the caller (empirically confirmed, not assumed): the failed
    probe discards and reconnects the ONE connection, and the freshly
    reconnected physical connection is marked fresh by ``_mark_fresh`` (the
    pool "connect" event), so SQLAlchemy's retry loop skips re-probing it on
    the immediate retry — the SAME freshness-skip parity native pre-ping
    itself relies on. So the observable, load-bearing signal isn't an
    exception; it's (1) the physical DBAPI connection identity changing
    (proving a real discard+reconnect happened, not a silent no-op) while (2)
    ``pool._invalidate_time`` stays at its initial ``0`` (proving no OTHER
    pooled connection was swept up in the invalidation)."""
    assert _URL is not None
    import ne.audit.engine as engine_module

    engine = create_engine_with_timeouts(_URL)
    try:
        with engine.connect() as conn:
            first_id = id(conn.connection.driver_connection)
            conn.execute(sa.text("SELECT 1"))
            conn.commit()
        assert engine.pool._invalidate_time == 0, "pool should start uninvalidated"

        # Make the deadline permanently unreachable -- every subsequent probe
        # times out inconclusively (mirrors the reviewer's live repro).
        monkeypatch.setattr(engine_module, "_bounded_dbapi_ping", lambda *_a, **_kw: None)

        with engine.connect() as conn:
            second_id = id(conn.connection.driver_connection)
            conn.execute(sa.text("SELECT 1"))
            conn.commit()

        assert second_id != first_id, (
            "expected the inconclusive probe to discard and reconnect the "
            "connection (a real effect, not a silent no-op)"
        )
        assert engine.pool._invalidate_time == 0, (
            "an INCONCLUSIVE probe timeout must NOT bump pool._invalidate_time "
            "-- that would invalidate every OTHER pooled connection too, on "
            "the mere strength of one probe running too slowly, not a proven "
            "dead connection"
        )
    finally:
        engine.dispose()


class _FreezableProxy:
    """A local, threaded TCP proxy in front of the real Postgres URL, letting
    tests simulate the two liveness-probe failure shapes that matter for
    NE-POOLER-PREPING-BOUND:

    * :meth:`freeze` — a TCP-alive-but-WEDGED server: every socket stays
      OPEN, but bytes stop being forwarded either direction. This is exactly
      the condition ``pool_pre_ping`` exists to detect and that this unit's
      bounded probe must fail FAST against instead of hanging.
    * :meth:`sever` — a genuinely DEAD connection: the currently-established
      pump sockets are closed, but the listener keeps ACCEPTING new
      connections (so a reconnect attempt through the same proxy succeeds).
      This is the shape native ``pool_pre_ping`` already handled correctly,
      and the replacement mechanism must not regress it.

    All threads are daemons and all sockets are closed in :meth:`close`.
    """

    def __init__(self, target_host: str, target_port: int) -> None:
        self._target_host = target_host
        self._target_port = target_port
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self._frozen = threading.Event()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._pump_sockets: list[socket.socket] = []
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        self._listener.settimeout(0.2)
        while not self._closed.is_set():
            try:
                client_sock, _addr = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                upstream_sock = socket.create_connection(
                    (self._target_host, self._target_port), timeout=5
                )
            except OSError:
                client_sock.close()
                continue
            with self._lock:
                self._pump_sockets.extend([client_sock, upstream_sock])
            threading.Thread(
                target=self._pump, args=(client_sock, upstream_sock), daemon=True
            ).start()
            threading.Thread(
                target=self._pump, args=(upstream_sock, client_sock), daemon=True
            ).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        src.settimeout(0.1)
        while not self._closed.is_set():
            try:
                data = src.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                return
            if not data:
                return
            # The freeze check happens HERE — right before forwarding — not
            # before `recv()`. Checking only before `recv()` races an
            # already-in-progress blocking recv() call: if this thread was
            # sitting inside `recv()` (waiting for the next message) at the
            # moment `freeze()` ran, it would return with the newly-arrived
            # data having never re-consulted the flag, and forward it anyway
            # — bytes would keep flowing right through a "frozen" proxy.
            # Checking immediately before `sendall` closes that race: no
            # matter when `freeze()` lands relative to `recv()`, these bytes
            # are held here and NEVER forwarded while frozen (KEEPING both
            # sockets open — just not passing anything across).
            while self._frozen.is_set():
                if self._closed.is_set():
                    return
                time.sleep(0.02)
            try:
                dst.sendall(data)
            except OSError:
                return

    def freeze(self) -> None:
        self._frozen.set()

    def sever(self) -> None:
        with self._lock:
            sockets, self._pump_sockets = self._pump_sockets, []
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.close()

    def close(self) -> None:
        self._closed.set()
        with contextlib.suppress(OSError):
            self._listener.close()
        with self._lock:
            sockets, self._pump_sockets = self._pump_sockets, []
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.close()
        self._accept_thread.join(timeout=2)


def _proxied_url(proxy: _FreezableProxy) -> str:
    assert _URL is not None
    parsed = sa.make_url(_URL)
    return parsed.set(host="127.0.0.1", port=proxy.port).render_as_string(hide_password=False)


def test_wedged_preping_probe_times_out() -> None:
    """THE regression proof for NE-POOLER-PREPING-BOUND: against a
    TCP-alive-but-wedged server, a checkout now FAILS within a small bounded
    window instead of hanging indefinitely.

    Sequence: one connection through the (live) proxy populates the pool;
    ``proxy.freeze()`` then wedges it (sockets stay open, nothing forwards);
    the second ``engine.connect()`` triggers a checkout, whose bounded
    pre-ping probe (``pre_ping_timeout_s=1``) times out inconclusively and
    raises ``DisconnectionError(invalidate_pool=False)`` (Argus P2, 2026-07-24
    review round 2 — a probe TIMEOUT is distinct from a definitive failure and
    must not invalidate the whole pool; see
    ``ne.audit.engine._pre_ping_listeners``'s docstring), which the pool turns
    into an invalidate-this-connection-only + fresh-connection RETRY — that
    retry attempts a brand-new physical connection through the SAME frozen
    proxy and is itself bounded (psycopg3 enforces libpq ``connect_timeout``
    client-side via its own async connect generator's deadline, since the
    proxy accepts the TCP handshake instantly but then never forwards the
    startup-packet response). Observed live against the exact CI Postgres
    image (2026-07-24): the retry raises ``sqlalchemy.exc.OperationalError``
    wrapping ``psycopg.errors.ConnectionTimeout`` ("connection timeout
    expired"), which is what this test pins (via the ``OperationalError``
    SQLAlchemy wraps it in — the psycopg-specific subclass is not asserted,
    only the SQLAlchemy-level type the brief requires).

    Pre-fix demonstration (recorded manually, not re-run by this test): with
    the bounded-checkout-listener change to ``engine.py`` reverted (native
    ``pool_pre_ping=True`` restored, no replacement listeners), the SAME
    sequence hangs indefinitely at the second ``engine.connect()`` — native
    pre-ping's ``SELECT 1`` blocks on the raw DBAPI connection with no
    deadline, confirmed by observing the process sit unresponsive for over 60s
    (killed manually) where this fixed version now completes in low
    single-digit seconds.

    The regression this proves is a HANG, not a wrong return value — a
    reintroduced unbounded probe makes ``engine.connect()`` never return at
    all (Argus P3, 2026-07-24 review round 2: verified by forcing
    ``_bounded_dbapi_ping`` to always return ``True``, which reproduces the
    unbounded probe's behavior against a wedged connection and hangs this
    test for 2+ minutes). A hang inside the main test thread would just burn
    the CI job's full ``timeout-minutes`` and report as a job timeout, not a
    named test failure — this workspace has no ``pytest-timeout`` plugin. So
    the second ``engine.connect()`` runs on a background thread joined with
    an explicit, generous timeout, and a still-alive thread after that join
    is itself reported as a named assertion failure instead of silently
    consuming the whole job's time budget.
    """
    assert _URL is not None
    proxy = _FreezableProxy(sa.make_url(_URL).host or "127.0.0.1", sa.make_url(_URL).port or 5432)
    engine = create_engine_with_timeouts(
        _proxied_url(proxy), timeouts=EngineTimeouts(connect_timeout_s=2, pre_ping_timeout_s=1)
    )
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
            conn.commit()

        proxy.freeze()

        outcome: dict[str, object] = {}

        def _attempt_second_checkout() -> None:
            t0 = time.monotonic()
            try:
                with engine.connect() as conn:
                    conn.execute(sa.text("SELECT 1"))
            except BaseException as exc:  # broad on purpose: captured for the main-thread assert
                outcome["exception"] = exc
            outcome["elapsed"] = time.monotonic() - t0

        worker = threading.Thread(target=_attempt_second_checkout, daemon=True)
        worker.start()
        worker.join(timeout=20)
        assert not worker.is_alive(), (
            "second engine.connect() through a frozen (TCP-alive-but-wedged) "
            "proxy did not return within 20s -- this IS the regression this "
            "test exists to catch: the checkout-listener pre-ping is no "
            "longer bounded"
        )

        exc = outcome.get("exception")
        assert isinstance(exc, OperationalError), f"expected OperationalError, got {exc!r}"
        elapsed = outcome["elapsed"]
        assert isinstance(elapsed, float)
        # Lower bound proves the checkout genuinely went through the bounded
        # probe + a reconnect attempt rather than erroring instantly; upper
        # bound is the whole point of this unit (pre-fix, this hung forever —
        # demonstrated by hand locally and recorded in the PR body).
        assert 0.9 <= elapsed <= 15, f"expected checkout to fail in 0.9-15s, took {elapsed}s"
    finally:
        engine.dispose()
        proxy.close()


def test_preping_replacement_still_recycles_dead_connections() -> None:
    """Never-silently-downgrade proof: a genuinely DEAD pooled connection
    (proxy severed, but still accepting new connections) is transparently
    invalidated and replaced on checkout — the original ``pool_pre_ping``
    dead-connection-recycle guarantee was not lost when the mechanism was
    swapped for the bounded replacement."""
    assert _URL is not None
    proxy = _FreezableProxy(sa.make_url(_URL).host or "127.0.0.1", sa.make_url(_URL).port or 5432)
    engine = create_engine_with_timeouts(
        _proxied_url(proxy), timeouts=EngineTimeouts(connect_timeout_s=2, pre_ping_timeout_s=1)
    )
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
            conn.commit()

        proxy.sever()

        # Probe on the severed socket fails fast -> InvalidatePoolError ->
        # invalidate + reconnect through the still-LIVE (accepting) proxy ->
        # transparent success, no exception surfaced to the caller.
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT 1")).scalar() == 1
            conn.commit()
    finally:
        engine.dispose()
        proxy.close()


def test_raw_connection_reports_no_session_timeout() -> None:
    """Behavioral anti-leak pin (P3-2), replacing the old name-only
    ``_set_session_timeouts`` blacklist in ``test_engine_timeouts.py``: a raw
    DBAPI connection off ``create_engine_with_timeouts`` never carries a
    session-wide ``statement_timeout`` — ``SHOW statement_timeout`` reports
    the Postgres default (``'0'``, unbounded), the same expectation
    ``test_autocommit_connection_gets_no_timeout`` above pins for the
    AUTOCOMMIT case. This file runs in BOTH the direct and pooled CI lanes, so
    this single test catches a reintroduced connect-time session-wide ``SET``
    (the exact #329 revert) under ANY function name, in EITHER shape."""
    assert _URL is not None
    engine = create_engine_with_timeouts(_URL)
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("SHOW statement_timeout")
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "0"
    finally:
        raw.close()
        engine.dispose()


def test_preping_probe_leaves_connection_transaction_neutral() -> None:
    """C1 (Codex review, 2026-07-25): does the bounded pre-ping probe leave the
    DBAPI connection mid-transaction (``INTRANS``) on checkout, breaking a
    caller that subsequently applies ``execution_options(isolation_level=...)``
    and pinning a pooler backend for the whole checkout?

    No — verified live, not by argument. ``_bounded_dbapi_ping`` drives
    ``psycopg``'s raw ``pgconn`` API (``send_query``/``flush``/``is_busy``/
    ``consume_input``/``get_result``) directly, bypassing psycopg3's
    ``Connection``-level autocommit/BEGIN-wrapping entirely — unlike
    SQLAlchemy's *native* ``do_ping``
    (``sqlalchemy/dialects/postgresql/_psycopg_common.py``), which toggles
    ``dbapi_connection.autocommit`` around a ``cursor.execute("SELECT 1")``
    call specifically because THAT path (a normal ``cursor.execute()``) makes
    psycopg3 auto-issue a client-side ``BEGIN`` first when ``autocommit`` is
    False. A bare ``pgconn.send_query`` with no preceding ``BEGIN`` runs as
    Postgres's own implicit, auto-committing single-statement transaction
    (protocol-level, independent of the driver's client-side ``autocommit``
    flag, which ``pgconn`` has no concept of) — so the probe never opens a
    transaction block to begin with, and there is nothing for the connection
    to be left inside of.

    Two independent live assertions prove this, on the SECOND checkout of a
    populated pool (the first is fresh-skipped by ``_mark_fresh``, so the
    second is the checkout where ``_pre_ping`` actually drives the probe):

    1. ``pgconn.transaction_status`` immediately after checkout is
       ``TransactionStatus.IDLE`` — never ``INTRANS`` — read from psycopg's
       own server-reported status (the wire protocol's ``ReadyForQuery``
       byte), not assumed.
    2. ``execution_options(isolation_level="AUTOCOMMIT")`` on that SAME
       checked-out connection succeeds and runs a query without error — the
       exact failure C1 predicted ("psycopg refuses to change those settings
       during an active transaction") does not occur, because there is no
       active transaction to refuse it during.
    """
    assert _URL is not None
    pq = pytest.importorskip("psycopg.pq")
    engine = create_engine_with_timeouts(_URL)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
            conn.commit()

        with engine.connect() as conn:
            driver_connection = conn.connection.driver_connection
            assert driver_connection is not None  # narrows Optional for pyright
            status = driver_connection.pgconn.transaction_status
            assert status == pq.TransactionStatus.IDLE, (
                "expected the connection to be IDLE (not INTRANS) immediately "
                f"after a checkout that ran the bounded pre-ping probe, got {status!r}"
            )
            autocommit_conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            assert autocommit_conn.execute(sa.text("SELECT 2")).scalar() == 2
            conn.commit()
    finally:
        engine.dispose()
