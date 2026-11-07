# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The shared engine-timeout helper + the to_thread offload primitives (P2-16).

Proves the timeout connect_args are emitted per dialect and that a slow sync DB
body offloaded via ``call_db`` does NOT pin the asyncio event loop (a concurrent
ticker coroutine still advances). Async seams via ``asyncio.run``; pytest-asyncio
is NOT a dependency.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DisconnectionError, InvalidatePoolError

from ne.audit import PostgresAuditSink
from ne.audit.engine import (
    _PQ_TUPLES_OK,
    EngineTimeouts,
    _bounded_dbapi_ping,
    _pg_timeout_listener,
    call_db,
    create_engine_with_timeouts,
    default_offload,
    engine_kwargs_for_url,
    ensure_transactional_sqlite,
)

_PG_URL = "postgresql+psycopg://u:p@h/db"


def test_engine_kwargs_postgresql_sets_connect_timeout_and_no_options_string() -> None:
    """NE-POOLER-TIMEOUTS: statement/lock timeouts move OFF the libpq ``options``
    startup parameter (transaction-mode PgBouncer >=1.20 FATALs on it) and onto
    the event listeners installed by ``create_engine_with_timeouts`` instead — see
    ``test_pg_engine_installs_timeout_listeners`` below.

    NE-POOLER-PREPING-BOUND: ``pool_pre_ping`` is no longer emitted here at all —
    SQLAlchemy's native pre-ping runs its liveness probe unbounded on the raw
    DBAPI connection. It is replaced by the bounded checkout-listener probe
    ``create_engine_with_timeouts`` installs instead (see
    ``test_pg_engine_installs_timeout_listeners``)."""
    kw = engine_kwargs_for_url(_PG_URL, EngineTimeouts())
    assert kw["connect_args"] == {"connect_timeout": 10}
    assert "options" not in kw["connect_args"]
    assert "pool_pre_ping" not in kw
    assert kw["pool_timeout"] == 30


def test_engine_kwargs_sqlite_sets_busy_timeout() -> None:
    # SQLite gets ONLY the busy timeout — no pool kwargs (SingletonThreadPool rejects them).
    assert engine_kwargs_for_url("sqlite://", EngineTimeouts()) == {"connect_args": {"timeout": 10}}


def test_engine_kwargs_unknown_backend_returns_empty() -> None:
    assert engine_kwargs_for_url("mysql://u:p@h/db", EngineTimeouts()) == {}


def test_engine_kwargs_honours_overridden_timeouts() -> None:
    kw = engine_kwargs_for_url(
        _PG_URL,
        EngineTimeouts(
            connect_timeout_s=3, statement_timeout_ms=1000, lock_timeout_ms=500, pool_timeout_s=7
        ),
    )
    assert kw["connect_args"] == {"connect_timeout": 3}
    assert "options" not in kw["connect_args"]
    assert kw["pool_timeout"] == 7


def test_pre_ping_timeout_s_rejects_non_positive_values() -> None:
    """``pre_ping_timeout_s=0`` does NOT mean "unbounded"/"disabled" — a
    non-positive deadline is already-expired to ``_wait_socket``, so ``0``
    actually means "every checkout probe fails instantly" (confirmed by
    reading ``_wait_socket``: ``remaining <= 0`` returns False immediately).
    ``EngineTimeouts`` is config-surfaced with ``extra="forbid"`` precisely so
    a misconfiguration like this fails LOUDLY rather than silently degrading
    pooling — pin that via ``Field(gt=0)`` (Argus P3, 2026-07-24 review
    round 2)."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        EngineTimeouts(pre_ping_timeout_s=0)
    with pytest.raises(pydantic.ValidationError):
        EngineTimeouts(pre_ping_timeout_s=-1)
    assert EngineTimeouts(pre_ping_timeout_s=1).pre_ping_timeout_s == 1


def test_create_engine_with_timeouts_sqlite_smoke() -> None:
    engine = create_engine_with_timeouts("sqlite://")
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("select 1")).scalar() == 1
    finally:
        engine.dispose()


def test_pg_engine_installs_timeout_listeners() -> None:
    """NE-POOLER-TIMEOUTS + NE-POOLER-PREPING-BOUND: a postgresql engine gets
    (1) a ``begin`` listener installed (the pooler-safe per-transaction SET
    LOCAL mechanism), and (2) the bounded pre-ping listener pair
    (``_mark_fresh`` on pool ``"connect"``, ``_pre_ping`` on pool
    ``"checkout"``) that replaces SQLAlchemy's native, unbounded
    ``pool_pre_ping``.

    The anti-leak pin against a reintroduced session-wide connect-time ``SET``
    (a prior revision installed one; it leaked onto shared pooled backends and
    poisoned OTHER clients' connections — Argus review, 2026-07-24, reverted)
    used to be a NAME-only blacklist here (``assert "_set_session_timeouts" not
    in connect_listener_names``), which a differently-named reintroduction
    would sail through unchanged. That pin is now BEHAVIORAL and LIVE instead:
    ``test_engine_timeouts_postgres_live.py::test_raw_connection_reports_no_session_timeout``
    asserts a fresh ``raw_connection()`` reports ``SHOW statement_timeout`` ==
    ``'0'`` in both the direct and pooled CI lanes, which catches a leaky
    connect-time ``SET`` under ANY function name."""
    engine = create_engine_with_timeouts(_PG_URL)
    try:
        assert bool(engine.dispatch.begin) is True
        connect_listener_names = {fn.__name__ for fn in engine.pool.dispatch.connect}
        checkout_listener_names = {fn.__name__ for fn in engine.pool.dispatch.checkout}
        # Subset checks: other internal pool listeners may legitimately exist
        # (e.g. SQLAlchemy's own dialect init hooks); we only assert OURS are
        # present, not that nothing else is.
        assert "_mark_fresh" in connect_listener_names
        assert "_pre_ping" in checkout_listener_names
    finally:
        engine.dispose()


def test_sqlite_engine_gets_no_pg_listeners_and_transactional_recipe_still_installs() -> None:
    """A sqlite engine gets NO postgres timeout listeners, so
    ``ensure_transactional_sqlite``'s foreign-recipe guard (``bool(engine.dispatch.begin)``)
    still sees a clean engine and installs its OWN required BEGIN recipe — pins
    the dual-append-guard interaction NE-POOLER-TIMEOUTS must not regress."""
    engine = create_engine_with_timeouts("sqlite://")
    try:
        assert not engine.dispatch.begin
        ensure_transactional_sqlite(engine)
        assert bool(engine.dispatch.begin) is True
    finally:
        engine.dispose()


def test_pg_timeout_listener_emits_exact_sql() -> None:
    """Directly exercise ``_pg_timeout_listener`` against a stub ``Connection`` —
    no live Postgres needed. Pins the exact SET LOCAL strings for overridden
    timeouts."""
    on_begin = _pg_timeout_listener(EngineTimeouts(statement_timeout_ms=1234, lock_timeout_ms=567))

    class _FakeDriverConnection:
        def __init__(self, autocommit: bool) -> None:
            self.autocommit = autocommit

    class _FakeInnerConnection:
        def __init__(self, autocommit: bool) -> None:
            self.driver_connection = _FakeDriverConnection(autocommit)

    class _FakeConnection:
        def __init__(self, autocommit: bool) -> None:
            self.connection = _FakeInnerConnection(autocommit)
            self.sql_calls: list[str] = []

        def exec_driver_sql(self, sql: str) -> None:
            self.sql_calls.append(sql)

    fake_txn_conn = _FakeConnection(autocommit=False)
    on_begin(fake_txn_conn)  # type: ignore[arg-type]
    assert fake_txn_conn.sql_calls == [
        "SET LOCAL statement_timeout = 1234; SET LOCAL lock_timeout = 567",
    ]


def test_begin_listener_skips_autocommit_connections() -> None:
    """SET LOCAL outside a real transaction is a Postgres no-op WARNING; autocommit
    connections never open one for the ``begin`` event to represent, so the
    listener must skip emitting SQL entirely rather than firing dead statements."""
    on_begin = _pg_timeout_listener(EngineTimeouts())

    class _FakeDriverConnection:
        autocommit = True

    class _FakeInnerConnection:
        driver_connection = _FakeDriverConnection()

    class _FakeConnection:
        def __init__(self) -> None:
            self.connection = _FakeInnerConnection()
            self.sql_calls: list[str] = []

        def exec_driver_sql(self, sql: str) -> None:
            self.sql_calls.append(sql)

    fake_conn = _FakeConnection()
    on_begin(fake_conn)  # type: ignore[arg-type]
    assert fake_conn.sql_calls == []


def test_pq_tuples_ok_constant_matches_psycopg() -> None:
    """``_PQ_TUPLES_OK`` is a plain int (not a psycopg import) in ne-audit's
    source, duck-typing psycopg's ``pgconn`` surface. This pins it against the
    real enum value it stands in for — a psycopg import IN THE TEST ONLY.

    ``ne-audit`` declares no ``psycopg`` dependency (neither in
    ``dependencies`` nor the ``dev`` group) — psycopg is only present in this
    workspace venv because ``uv sync --all-packages`` pulls it in via
    ne-runtime. Under strict per-package dependency isolation this import
    would be a collection ERROR, not a graceful skip, so use
    ``pytest.importorskip`` rather than a bare ``import`` (Argus review,
    2026-07-24)."""
    pq = pytest.importorskip("psycopg.pq")

    assert _PQ_TUPLES_OK == pq.ExecStatus.TUPLES_OK


class _FakePgResult:
    def __init__(self, status: int) -> None:
        self.status = status


def test_bounded_ping_returns_true_on_healthy_fake_pgconn() -> None:
    """A healthy fake ``pgconn`` (flush completes immediately, one TUPLES_OK
    result, then None) reports the connection alive."""

    class _FakeHealthyPgconn:
        socket = -1  # never touched: flush()==0 and is_busy() immediately False

        def __init__(self) -> None:
            self._results: list[_FakePgResult | None] = [_FakePgResult(_PQ_TUPLES_OK), None]
            self._i = 0

        def send_query(self, command: bytes) -> None:
            pass

        def flush(self) -> int:
            return 0

        def is_busy(self) -> bool:
            return False

        def consume_input(self) -> None:
            pass

        def get_result(self) -> _FakePgResult | None:
            result = self._results[self._i]
            self._i += 1
            return result

    class _FakeDbapiConn:
        pgconn = _FakeHealthyPgconn()

    assert _bounded_dbapi_ping(_FakeDbapiConn(), timeout_s=1.0) is True


def test_bounded_ping_times_out_on_wedged_fake_pgconn() -> None:
    """The deterministic wedge: a real socketpair with no data on it, and a
    fake ``pgconn`` reporting ``is_busy() == True`` forever, so the probe waits
    on the readable-socket select until it genuinely times out — proving the
    bound is enforced by ``_wait_socket``'s deadline, not by any driver
    behavior.

    Returns ``None`` (INCONCLUSIVE), not ``False`` (Argus P2, 2026-07-24
    review round 2): a probe that timed out without a conclusive answer is
    distinct from one that got a definitive bad answer — see
    ``_bounded_dbapi_ping``'s docstring for why that distinction matters
    (only ``False`` should invalidate the WHOLE pool).

    Asserts a LOWER bound on ``elapsed`` too (Argus P3, 2026-07-24 review
    round 2): without it, a mutant where ``_wait_socket`` ignores its
    deadline and returns immediately would pass unchanged, proving nothing
    about the bound actually being honored — mirrors the live counterpart
    ``test_wedged_preping_probe_times_out``'s ``0.9 <= elapsed`` lower bound.
    A small margin below the 0.1s timeout absorbs monotonic-clock/scheduler
    jitter without weakening what the assertion proves."""
    server_sock, client_sock = socket.socketpair()
    try:

        class _FakeWedgedPgconn:
            socket = server_sock.fileno()

            def send_query(self, command: bytes) -> None:
                pass

            def flush(self) -> int:
                return 0

            def is_busy(self) -> bool:
                return True

            def consume_input(self) -> None:
                pass

            def get_result(self) -> None:
                return None

        class _FakeDbapiConn:
            pgconn = _FakeWedgedPgconn()

        t0 = time.monotonic()
        result = _bounded_dbapi_ping(_FakeDbapiConn(), timeout_s=0.1)
        elapsed = time.monotonic() - t0
        assert result is None
        assert 0.09 <= elapsed < 1.0, f"expected ~0.1s honored deadline, took {elapsed}s"
    finally:
        server_sock.close()
        client_sock.close()


def test_bounded_ping_falls_back_for_non_psycopg_driver() -> None:
    """A DBAPI connection with no ``pgconn`` attribute (any non-psycopg3
    driver) gets the status-quo cursor ping instead — unbounded, but at least
    honest about it (documented residual)."""

    class _FakeCursor:
        def __init__(self, calls: list[str]) -> None:
            self._calls = calls

        def execute(self, sql: str) -> None:
            self._calls.append(sql)

        def close(self) -> None:
            self._calls.append("close")

    class _FakeHealthyConn:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def cursor(self) -> _FakeCursor:
            return _FakeCursor(self.calls)

        def rollback(self) -> None:
            self.calls.append("rollback")

    healthy = _FakeHealthyConn()
    assert _bounded_dbapi_ping(healthy, timeout_s=1.0) is True
    assert healthy.calls == ["SELECT 1", "close", "rollback"]

    class _FakeRaisingCursor:
        def execute(self, sql: str) -> None:
            raise RuntimeError("boom")

        def close(self) -> None:
            pass

    class _FakeRaisingConn:
        def cursor(self) -> _FakeRaisingCursor:
            return _FakeRaisingCursor()

        def rollback(self) -> None:
            pass

    assert _bounded_dbapi_ping(_FakeRaisingConn(), timeout_s=1.0) is False


class _FakeConnectionRecord:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}


def test_pre_ping_listener_skips_fresh_connection_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The freshness flag ``_mark_fresh`` sets suppresses exactly ONE probe on
    the next checkout — parity with native pre-ping's own fresh-connection
    skip (a connection that was JUST opened does not need a liveness check)."""
    import ne.audit.engine as engine_module

    def _fail_if_called(dbapi_connection: Any, timeout_s: float) -> bool:
        raise AssertionError("probe must not run on a freshly-connected connection")

    monkeypatch.setattr(engine_module, "_bounded_dbapi_ping", _fail_if_called)
    mark_fresh, pre_ping = engine_module._pre_ping_listeners(EngineTimeouts())

    record = _FakeConnectionRecord()
    mark_fresh(object(), record)
    pre_ping(object(), record, object())  # freshness consumed here: no probe, no raise

    called = {"n": 0}

    def _count_call(dbapi_connection: Any, timeout_s: float) -> bool:
        called["n"] += 1
        return True

    monkeypatch.setattr(engine_module, "_bounded_dbapi_ping", _count_call)
    pre_ping(object(), record, object())  # freshness already spent: probe DOES run
    assert called["n"] == 1


def test_pre_ping_listener_raises_invalidate_pool_on_failed_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DEFINITIVELY failed probe (``_bounded_dbapi_ping`` returns ``False``)
    raises ``InvalidatePoolError`` — the EXACT exception native
    ``pool_pre_ping`` raises on a failed ping, giving identical
    whole-pool-invalidate + fresh-connection-retry semantics. A healthy probe
    (``True``) raises nothing."""
    import ne.audit.engine as engine_module

    _, pre_ping = engine_module._pre_ping_listeners(EngineTimeouts())
    record = _FakeConnectionRecord()  # never marked fresh: probe always runs

    monkeypatch.setattr(engine_module, "_bounded_dbapi_ping", lambda *_a, **_kw: False)
    with pytest.raises(InvalidatePoolError):
        pre_ping(object(), record, object())

    monkeypatch.setattr(engine_module, "_bounded_dbapi_ping", lambda *_a, **_kw: True)
    pre_ping(object(), record, object())  # no raise


def test_pre_ping_listener_raises_non_pool_invalidating_disconnect_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An INCONCLUSIVE probe (``_bounded_dbapi_ping`` returns ``None`` — timed
    out without a conclusive answer) must NOT be treated the same as a
    definitive failure (Argus P2, 2026-07-24 review round 2): raising
    ``InvalidatePoolError`` here would invalidate every OTHER pooled
    connection too, on the mere strength of one probe running too slowly —
    reproduced live against a healthy Postgres by making the deadline
    unreachable: three consecutive checkouts each got a distinct physical
    DBAPI connection, degrading a pooled engine to connection-per-checkout.
    So a timeout raises the weaker ``DisconnectionError`` with
    ``invalidate_pool=False`` — only THIS connection is discarded and
    retried; every other pooled connection is left alone."""
    import ne.audit.engine as engine_module

    _, pre_ping = engine_module._pre_ping_listeners(EngineTimeouts())
    record = _FakeConnectionRecord()  # never marked fresh: probe always runs

    monkeypatch.setattr(engine_module, "_bounded_dbapi_ping", lambda *_a, **_kw: None)
    with pytest.raises(DisconnectionError) as exc_info:
        pre_ping(object(), record, object())
    assert not isinstance(exc_info.value, InvalidatePoolError), (
        "a probe TIMEOUT must raise plain DisconnectionError, not "
        "InvalidatePoolError -- InvalidatePoolError.invalidate_pool is True "
        "and would wrongly invalidate every OTHER pooled connection too"
    )
    assert exc_info.value.invalidate_pool is False


def test_default_offload_true_for_non_sqlite_false_for_sqlite() -> None:
    pg = sa.create_engine(_PG_URL)
    sqlite = sa.create_engine("sqlite://")
    try:
        assert default_offload(pg) is True
        assert default_offload(sqlite) is False
    finally:
        pg.dispose()
        sqlite.dispose()


def test_call_db_inline_runs_on_loop_when_not_offloaded() -> None:
    async def scenario() -> str:
        return await call_db(False, lambda: "inline")

    assert asyncio.run(scenario()) == "inline"


def test_call_db_offload_runs_off_loop() -> None:
    """A slow sync body offloaded to a thread must not block a concurrent coroutine."""

    async def scenario() -> tuple[str, int]:
        counter = {"n": 0}
        stop = asyncio.Event()

        async def ticker() -> None:
            while not stop.is_set():
                await asyncio.sleep(0.01)
                counter["n"] += 1

        def slow() -> str:
            time.sleep(0.3)
            return "done"

        tick_task = asyncio.create_task(ticker())
        result = await call_db(True, slow)
        stop.set()
        await tick_task
        return result, counter["n"]

    result, ticks = asyncio.run(scenario())
    assert result == "done"
    # ~30 ticks in 0.3s if the loop kept running; a blocked loop would show ~0.
    assert ticks > 5


def test_sink_append_offload_does_not_block_event_loop() -> None:
    """PostgresAuditSink.append(offload) keeps the loop live during a slow write."""
    engine = sa.create_engine("sqlite://")
    from ne.audit import ensure_audit_schema

    ensure_audit_schema(engine)
    sink = PostgresAuditSink(engine, offload_db_io=True)

    # Never run the real sqlite body cross-thread — monkeypatch it to a pure sleep.
    def _slow(_event: object) -> None:
        time.sleep(0.3)

    sink._append_sync = _slow  # type: ignore[method-assign]

    async def scenario() -> int:
        counter = {"n": 0}
        stop = asyncio.Event()

        async def ticker() -> None:
            while not stop.is_set():
                await asyncio.sleep(0.01)
                counter["n"] += 1

        tick_task = asyncio.create_task(ticker())
        await sink.append(object())  # type: ignore[arg-type]
        stop.set()
        await tick_task
        return counter["n"]

    try:
        ticks = asyncio.run(scenario())
    finally:
        engine.dispose()
    assert ticks > 5
