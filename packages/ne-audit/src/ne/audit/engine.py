# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared pysqlite transactional-isolation recipe for the dual-append pattern.

The governed dual-append pattern writes a business/outbox row AND an ne-audit
hash-chain row in ONE ``engine.begin()`` transaction, doing the idempotent inner
write via a ``conn.begin_nested()`` SAVEPOINT. On PLAIN pysqlite, the DBAPI
defers the outer ``BEGIN``, so the SAVEPOINT writes are emitted BEFORE the real
outer transaction opens — meaning if the audit-append leg then FAILS and the
``engine.begin()`` block rolls back, the SAVEPOINT-era rows are NOT undone
(stranded, unaudited). Production Postgres is SAFE (real nested transactions);
this bites pysqlite (dev/CI and any sqlite deployment). This is the tracked
SPEC §11 gap referenced in the NE-034/035/064/065/066 AS-BUILT entries.

:func:`ensure_transactional_sqlite` installs the documented fix — the NE-069
reference recipe (``_ensure_transactional_ddl`` at
``tools/ne-cli/src/ne/cli/migrate.py``, deliberately NOT modified here) hoisted
into a shared helper so every dual-append engine factory applies the SAME recipe
instead of copy-pasting it:

* The ``"checkout"`` pool event (NOT the docs' ``"connect"`` event) is used
  deliberately so that connections ALREADY POOLED before the listener is
  installed are covered too — ``"connect"`` fires only for DBAPI connections
  created after the listener exists (NE-069 Codex P2-A).
* A plain ``BEGIN`` is emitted (NOT ``BEGIN IMMEDIATE`` — the write-concurrency
  variant in ``packages/ne-audit/tests/conftest.py`` ``_make_serialized`` is a
  DIFFERENT recipe with different locking semantics and stays local).
* Idempotent: a module-level ``WeakSet`` tracks patched engines so re-applying
  (e.g. factory + defensive constructor on the same engine) is a no-op and never
  stacks listeners (stacked ``begin`` listeners would double-emit ``BEGIN`` and
  raise ``sqlite3.OperationalError``).
* Defensive against FOREIGN recipes: if the engine already carries a ``begin``
  listener installed by other code (e.g. the ``BEGIN IMMEDIATE`` audit fixture or
  the NE-069 migration helper's explicit-``BEGIN`` listener), this helper skips
  entirely rather than stacking a second ``begin`` listener on top — that engine
  is already transaction-instrumented, and a second ``BEGIN`` would fire inside
  an open transaction and raise ``sqlite3.OperationalError`` (Codex NE-CLEAN2 P2).
* NO-OP on every non-sqlite dialect: Postgres behavior is byte-identical and
  unchanged (guarded on ``engine.dialect.name == "sqlite"``).
"""

from __future__ import annotations

import asyncio
import selectors
import time
import weakref
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DisconnectionError, InvalidatePoolError

# Engines already patched by ``ensure_transactional_sqlite`` (a WeakSet so we
# never keep a test/app engine alive past its natural lifetime).
_patched_engines: weakref.WeakSet[Engine] = weakref.WeakSet()


class EngineTimeouts(BaseModel):
    """Config-surfaced connect/statement/lock/pool timeouts for a SQLAlchemy engine.

    P2-16 (2026-07-10 bulletproof review): a ``create_engine`` with library
    DEFAULTS has NO connect timeout and NO server-side statement/lock timeout, so
    a Postgres failover/partition or a held row lock blocks ``engine.begin()``
    indefinitely — and because the sync adapters run their blocking I/O ON the
    ASGI event loop (see :func:`call_db`), that stalls EVERY concurrent request,
    stops SSE keep-alives, and even a DB-free ``/healthz`` cannot be served until
    the OS TCP timeout fires. These bounds surface sane, overridable defaults so
    every in-scope engine fails FAST instead of hanging the loop.

    Frozen + ``extra='forbid'`` so a typo in ``ne.toml`` fails loudly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: libpq ``connect_timeout`` — seconds to wait for a new TCP connection.
    connect_timeout_s: int = 10
    #: Postgres server-side ``statement_timeout`` — ms before a running statement aborts.
    statement_timeout_ms: int = 30_000
    #: Postgres server-side ``lock_timeout`` — ms before waiting on a row lock aborts.
    lock_timeout_ms: int = 10_000
    #: SQLAlchemy pool checkout timeout — seconds to wait for a pooled connection.
    pool_timeout_s: int = 30
    #: Client-side deadline (seconds) on the pool liveness (pre-ping) probe.
    #: This CANNOT be a server-side bound: the probe runs ``SELECT 1`` straight
    #: on the raw DBAPI connection (see :func:`_bounded_dbapi_ping`), before any
    #: SQLAlchemy ``Connection``/``"begin"`` event exists to carry a ``SET
    #: LOCAL statement_timeout``, and any bound-establishing round-trip issued
    #: against a wedged server would itself hang. NE-POOLER-PREPING-BOUND.
    #: Must be > 0 (``Field(gt=0)``, Argus P3 2026-07-24): ``_wait_socket``
    #: treats a non-positive deadline as ALREADY EXPIRED, so ``0`` does NOT mean
    #: "unbounded"/"disabled" — it means every single checkout probe fails
    #: instantly, forcing a reconnect on every checkout. Also see the
    #: "inconclusive vs. definitively dead" split documented on
    #: :func:`_bounded_dbapi_ping` and :func:`_pre_ping_listeners`: this value
    #: must be set ABOVE the worst-case latency a merely-slow-but-alive server
    #: can legitimately exhibit (e.g. a saturated transaction-mode PgBouncer
    #: queuing this probe behind other clients' transactions up to its own
    #: ``query_wait_timeout``), or a healthy-but-busy backend will routinely
    #: trip the inconclusive-timeout path.
    pre_ping_timeout_s: int = Field(default=5, gt=0)


def engine_kwargs_for_url(url: str, timeouts: EngineTimeouts) -> dict[str, Any]:
    """Return the dialect-appropriate ``create_engine`` kwargs for ``url``.

    * ``postgresql*`` → libpq ``connect_timeout`` + ``pool_timeout``. This
      deliberately does **NOT** include ``pool_pre_ping`` (NE-POOLER-PREPING-BOUND,
      2026-07-24): SQLAlchemy's native pre-ping runs ``SELECT 1`` straight on
      the raw DBAPI connection, with no client-side bound and no way to apply
      one server-side (the same "NOT covered (1)" raw-DBAPI gap
      :func:`_pg_timeout_listener` documents), so against a TCP-alive-but-wedged
      server the liveness probe that exists to detect exactly that condition
      blocked forever. :func:`create_engine_with_timeouts` installs a
      *replacement* pre-ping instead — a pool ``"checkout"`` listener
      (:func:`_pre_ping_listeners`) that runs the same probe under an explicit
      client-side deadline (``EngineTimeouts.pre_ping_timeout_s``). On a
      DEFINITIVE probe failure it raises the identical ``InvalidatePoolError``
      native pre-ping raises; on an INCONCLUSIVE probe TIMEOUT (distinct from
      a definitive failure — see :func:`_bounded_dbapi_ping`'s docstring) it
      raises the weaker ``DisconnectionError(invalidate_pool=False)`` instead,
      so a merely-slow-but-alive backend does not invalidate every OTHER
      pooled connection along with it.
      It must REPLACE, not supplement, the native flag: SQLAlchemy's pre-ping
      and checkout listeners share one retry loop
      (``sqlalchemy.pool.base._ConnectionFairy._checkout``) and the native ping
      runs *before* checkout events in that loop, so leaving it enabled would
      still block unbounded ahead of our listener ever running.
      The server-side ``statement_timeout``/``lock_timeout`` GUCs are **NOT**
      carried here as a libpq ``options`` startup string (NE-POOLER-TIMEOUTS,
      2026-07-24): transaction-mode PgBouncer >=1.20 (upstream PR #878, a
      deliberate security tightening against arbitrary-GUC startup-parameter
      injection) FATALs on any ``-c`` GUC in ``options`` it does not itself
      track — neither ``statement_timeout`` nor ``lock_timeout`` is tracked —
      with ``unsupported startup parameter in options: statement_timeout`` at
      CONNECTION time, before a single query runs (empirically confirmed by
      NE-LP-DBOS-REALPG; see ``packages/ne-runtime/tests/test_dbos_pooler_finding_live.py``).
      :func:`create_engine_with_timeouts` applies both GUCs via a single
      SQLAlchemy ``"begin"`` event listener instead, re-issued as ``SET LOCAL``
      at the start of every transaction — pooler-safe (see that function's and
      :func:`_pg_timeout_listener`'s docstrings for why, including why an
      earlier connect-time-``SET`` variant of this fix was tried and reverted).
    * ``sqlite`` → the pysqlite ``timeout`` busy-wait only; NO pool kwargs, since
      SQLite may use ``SingletonThreadPool``/``NullPool`` which reject
      ``pool_timeout``.
    * any other backend → ``{}`` (documented pass-through; the caller gets library
      defaults for an unrecognized dialect rather than a wrong-dialect kwarg).
    """
    backend = sa.make_url(url).get_backend_name()
    if backend == "postgresql":
        return {
            "connect_args": {"connect_timeout": timeouts.connect_timeout_s},
            "pool_timeout": timeouts.pool_timeout_s,
        }
    if backend == "sqlite":
        return {"connect_args": {"timeout": timeouts.connect_timeout_s}}
    return {}


def _pg_timeout_listener(timeouts: EngineTimeouts) -> Callable[[Connection], None]:
    """Build the pooler-safe ``"begin"`` listener for a postgresql engine.

    Kept as a free function (not a closure inline in
    :func:`create_engine_with_timeouts`) so it is independently unit testable
    against a stub ``Connection`` without a live Postgres
    (``packages/ne-audit/tests/test_engine_timeouts.py::test_pg_timeout_listener_emits_exact_sql``).

    ``stmt_ms``/``lock_ms`` are pulled to plain ``int`` up front from a frozen,
    ``extra="forbid"`` pydantic model (:class:`EngineTimeouts`) — there is no
    string/user input anywhere near the f-strings below, so there is no SQL
    injection surface despite the values being interpolated directly (Postgres
    ``SET``/``SET LOCAL`` do not accept a bind parameter for a GUC value).

    ONE listener, not two — a prior revision of this fix ALSO installed a pool
    ``"connect"`` listener issuing a session-wide ``SET`` once per physical DBAPI
    connection, reasoning that it preserved old direct-connection semantics. That
    was WRONG and was reverted (Argus review, 2026-07-24): transaction-mode
    PgBouncer does NOT run ``server_reset_query`` (``DISCARD ALL``) by default
    (``server_reset_query_always = 0``), so a session-level ``SET`` issued at
    connect time PERSISTS on that backend for the rest of its
    ``server_lifetime`` (default 1h) and silently applies to EVERY OTHER
    pooler client that is later handed the same backend — reproduced by hand
    against the exact CI images (postgres:18.4-alpine +
    edoburu/pgbouncer:v1.25.2-p0, ``POOL_MODE=transaction``): an unrelated,
    completely separate ``sa.create_engine`` client observed its own
    ``pg_sleep(2)`` cancelled with ``QueryCanceled: canceling statement due to
    statement timeout`` purely because an ne-audit engine had earlier connected
    through the SAME shared pool. "Only ever tightens, never unbounds" does not
    make that harmless — tightening a stranger's timeout is exactly how you
    kill their long-running query.

    ``on_begin`` (engine ``"begin"`` event) is therefore the ONLY mechanism, and
    it is sufficient on its own for every real caller: it re-issues both GUCs as
    ``SET LOCAL`` at the START of every transaction — direct or pooled alike,
    since SQLAlchemy autobegins a transaction (firing ``"begin"``) on the first
    statement of every ``engine.connect()``/``engine.begin()`` use, so a
    connect-only listener bought no coverage a begin-only one does not already
    have. Under transaction-mode PgBouncer this is also the CORRECT mechanism.
    PgBouncer pins a backend connection to a client only for the duration of ONE
    transaction and may hand ANY backend (last touched by ANY other client, with
    whatever GUCs that client happened to leave behind) to the NEXT transaction
    on the SAME logical connection — a connect-time SET would apply once to
    whichever backend happened to be attached at connect time and then never
    again, silently leaving every subsequent transaction on a pooled connection
    with WHATEVER timeout (or none) its previous tenant set. ``SET LOCAL`` rides
    the transaction PgBouncer pins to exactly one backend and evaporates at
    commit/rollback, so it cannot leak onto another pooler client's transaction
    either — both the direct-connection parity claim and the pooler-correctness
    claim are enforced live, through a real transaction-mode PgBouncer, by
    ``packages/ne-runtime/tests/test_dbos_pooler_finding_live.py`` and
    ``packages/ne-audit/tests/test_engine_timeouts_postgres_live.py`` (both
    assert the timeout re-applies across two *consecutive* transactions on one
    checkout).

    NOT covered (1) — RAW-DBAPI STATEMENT PATHS, GENERICALLY, BY DESIGN. Any
    statement executed straight on the DBAPI connection bypasses the SQLAlchemy
    ``Connection`` layer, so no ``"begin"`` event fires and no ``SET LOCAL`` is
    issued: ``create_engine_with_timeouts(URL).raw_connection()`` then
    ``SHOW statement_timeout`` reports ``'0'``, where the OLD ``options``
    connect-arg mechanism reported ``'30s'``. **No server-side bound can be
    added for this path**: a session-wide connect-time ``SET`` was tried and
    reverted (see above — it leaks onto other pooler clients under
    transaction-mode PgBouncer), and a per-use ``SET`` requires the caller to
    be inside the SQLAlchemy ``Connection`` layer in the first place, which a
    raw-DBAPI caller by definition is not. ``SHOW statement_timeout`` reporting
    ``'0'`` on a raw connection is therefore the **PINNED-CORRECT** behavior —
    not an open gap — enforced live by
    ``test_engine_timeouts_postgres_live.py::test_raw_connection_reports_no_session_timeout``
    in both the direct and pooled CI lanes.

    The one instance of this pattern that MATTERED — and the reason this
    paragraph used to end in an open TODO — is now CLOSED
    (NE-POOLER-PREPING-BOUND, 2026-07-24): ``engine_kwargs_for_url`` no longer
    sets ``pool_pre_ping=True``, because SQLAlchemy's native pre-ping runs
    ``SELECT 1`` directly on the raw DBAPI connection with no bound, and
    against a server that is TCP-alive but wedged — exactly the failover case
    ``pool_pre_ping`` exists to detect — that probe blocked forever (libpq
    ``connect_timeout`` bounds only NEW connections, not a probe on an existing
    one). :func:`create_engine_with_timeouts` now installs a *replacement*
    pre-ping as a pool ``"checkout"`` listener (:func:`_pre_ping_listeners`,
    :func:`_bounded_dbapi_ping`) that runs the identical ``SELECT 1`` probe but
    under an explicit client-side deadline (``EngineTimeouts.pre_ping_timeout_s``)
    — a client-side deadline is the only mechanism that CAN bound a probe on an
    already-open connection to a wedged server, since any bound-establishing
    round-trip would itself hang. A bounded probe has a failure mode native
    pre-ping never did — timing out INCONCLUSIVELY against a merely slow (not
    dead) backend — so :func:`_pre_ping_listeners` distinguishes that from a
    definitive ping failure and invalidates only the ONE connection, not the
    whole pool, in that case (Argus P2, 2026-07-24 review round 2; see
    :func:`_bounded_dbapi_ping`'s and :func:`_pre_ping_listeners`'s
    docstrings). ``EngineTimeouts.pre_ping_timeout_s`` must therefore be tuned
    above the worst-case latency a healthy-but-busy backend can legitimately
    show. Pinned by
    ``test_wedged_preping_probe_times_out`` (live, both lanes) and
    ``test_preping_replacement_still_recycles_dead_connections`` (proves the
    original pool_pre_ping dead-connection-recycle guarantee was not lost in
    the swap). Recorded here because an earlier revision of this paragraph
    named AUTOCOMMIT as "the one case" not covered, which was empirically
    false — and a normative claim that overstates coverage in the P2-16
    chokepoint is precisely what stops the next reviewer from looking; do not
    let this paragraph regress into overclaiming again.

    NOT covered (2): SQLAlchemy autobegins a transaction (dispatching ``"begin"``)
    on the first statement of ANY connection use, including one whose DBAPI
    driver connection is in ``AUTOCOMMIT`` isolation — that bookkeeping is
    client-side and dialect-agnostic, so the event still FIRES there
    (empirically confirmed on SQLAlchemy 2.0.51: a stub ``"begin"`` listener
    observes the event under ``execution_options(isolation_level="AUTOCOMMIT")``
    just as it does under the default isolation level). What does NOT happen is
    a server-side transaction block opening, so a ``SET LOCAL`` issued there
    would be a Postgres no-op WARNING with its timeout silently unapplied —
    strictly worse than skipping, since it would look like coverage while
    providing none. The listener therefore detects
    ``driver_connection.autocommit`` and deliberately emits NOTHING, so a
    ``create_engine_with_timeouts`` engine put into DBAPI ``AUTOCOMMIT`` gets no
    server-side timeout from this mechanism at all — pinned at stub level by
    ``test_begin_listener_skips_autocommit_connections`` and live by
    ``test_engine_timeouts_postgres_live.py::test_autocommit_connection_gets_no_timeout``.
    Grepped and confirmed at fix time
    (``grep -rn isolation_level packages tools --include='*.py'``): the one
    AUTOCOMMIT caller in the codebase, ``ne.audit.pgaudit.enable_pgaudit``,
    always runs on a plain, separately-constructed ``sa.create_engine`` (see
    ``packages/ne-audit/tests/test_pgaudit_live.py``'s ``pgaudit_engine``
    fixture) — never on a ``create_engine_with_timeouts`` engine — so this gap
    is currently unreachable from any in-scope caller. If a future caller needs
    an AUTOCOMMIT ``create_engine_with_timeouts`` engine, it must add its own
    explicit timeout handling for that path rather than relying on this
    listener.
    """
    stmt_ms = int(timeouts.statement_timeout_ms)
    lock_ms = int(timeouts.lock_timeout_ms)

    def _set_local_timeouts(conn: Connection) -> None:
        # The "begin" event DOES fire here even under DBAPI AUTOCOMMIT (it is
        # SQLAlchemy's client-side autobegin bookkeeping, dialect-agnostic) —
        # but no server-side transaction block opens, so ``SET LOCAL`` outside
        # one is a Postgres no-op WARNING, not an error, and its timeout would
        # be silently unapplied. Skip explicitly rather than fire a statement
        # that looks like coverage while providing none; see the docstring's
        # "NOT covered" paragraph above.
        driver_connection = conn.connection.driver_connection
        if getattr(driver_connection, "autocommit", False):
            return
        # ONE round trip for both GUCs (perf/audit-intent-path): this listener
        # fires at the start of EVERY transaction on every in-scope engine, so
        # the second statement was a systemwide per-transaction round-trip tax.
        # psycopg executes the two-statement string as a single command;
        # SET LOCAL semantics are unchanged.
        conn.exec_driver_sql(
            f"SET LOCAL statement_timeout = {stmt_ms}; SET LOCAL lock_timeout = {lock_ms}"
        )

    return _set_local_timeouts


# psycopg.pq.ExecStatus.TUPLES_OK — pinned by test_pq_tuples_ok_constant_matches_psycopg.
# A plain int (not a psycopg import) so ne-audit's source adds no new dependency;
# the probe duck-types the DBAPI connection's ``pgconn`` surface instead.
_PQ_TUPLES_OK = 2

# Key set in a pool ``_ConnectionRecord.info`` dict by ``_mark_fresh`` and popped
# by ``_pre_ping`` to skip probing a connection that was JUST established.
_PRE_PING_FRESH_KEY = "ne_preping_fresh"


def _wait_socket(
    sel: selectors.BaseSelector, fileno: int, *, want_write: bool, deadline: float
) -> bool:
    """Block on ``sel`` until ``fileno`` is ready for read/write or
    ``deadline`` (a ``time.monotonic()`` timestamp) passes; returns False on
    timeout.

    ``sel`` is a single ``selectors.DefaultSelector`` created ONCE per probe
    by :func:`_bounded_dbapi_ping` and passed down here to be
    registered/unregistered on each poll — deliberately NOT a fresh
    ``DefaultSelector()`` per call (Argus review, 2026-07-24: that
    constructed-and-closed a new kqueue/epoll fd on EVERY poll iteration; on
    a peer that holds the socket permanently ready for the WRONG event while
    ``is_busy()`` stays true, this hot checkout-path probe would spin
    creating/tearing down a kernel object for up to the full deadline).
    Uses the selector API deliberately, NOT ``select.select`` — that is
    bounded by ``FD_SETSIZE`` (typically 1024) and can silently misbehave on
    a high-numbered fd from a busy connection pool; the selector-based API
    has no such ceiling.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    sel.register(fileno, selectors.EVENT_WRITE if want_write else selectors.EVENT_READ)
    try:
        return bool(sel.select(timeout=remaining))
    finally:
        sel.unregister(fileno)


def _unbounded_cursor_ping(dbapi_connection: Any) -> bool:
    """Status-quo cursor-based liveness ping for a non-psycopg3 DBAPI driver
    (no ``pgconn`` surface to bound a probe against). Deliberately UNBOUNDED —
    a documented residual of NE-POOLER-PREPING-BOUND (see that unit's PR body
    and :func:`_bounded_dbapi_ping`'s docstring): every postgres engine in this
    workspace's lock uses psycopg3 today, so this path is not currently
    reachable from any in-scope caller, but is kept as a safety net matching
    native ``pool_pre_ping``'s own generic-DBAPI ping behavior rather than
    raising for an unrecognized driver.
    """
    try:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SELECT 1")
        finally:
            cursor.close()
        dbapi_connection.rollback()
    except Exception:  # broad on purpose: any driver/cursor error means "not usable"
        return False
    return True


def _bounded_dbapi_ping(dbapi_connection: Any, timeout_s: float) -> bool | None:
    """Run a ``SELECT 1`` liveness probe on ``dbapi_connection``, bounded by a
    client-side monotonic deadline of ``timeout_s`` seconds.

    This is the fix for NE-POOLER-PREPING-BOUND: SQLAlchemy's native
    ``pool_pre_ping`` runs its ``SELECT 1`` probe via the DBAPI driver's own
    blocking call, with NO deadline — against a server that is TCP-alive but
    wedged (exactly the failover condition pre-ping exists to detect), that
    call blocks forever. No SERVER-side bound can substitute here: the probe
    runs on the raw DBAPI connection, outside the SQLAlchemy ``Connection``
    layer that :func:`_pg_timeout_listener`'s ``SET LOCAL`` mechanism depends
    on, and any bound-establishing round-trip (e.g. a session-wide ``SET``)
    issued against a wedged server would itself hang — so a CLIENT-side
    deadline enforced by this process, independent of the server, is the only
    mechanism that can work at all.

    Returns a TRI-STATE result, not a plain bool (Argus P2, 2026-07-24 review
    round 2). A bounded probe genuinely has a THIRD outcome native pre-ping's
    unbounded one never does — "ran out of time without an answer" is NOT the
    same fact as "got a definitive bad answer", and collapsing them would make
    a merely-slow-but-alive connection look identical to a proven-dead one:

    * ``True`` — the probe completed within the deadline and reports the
      connection healthy.
    * ``False`` — the probe completed (or errored) within the deadline and
      reports the connection DEFINITIVELY unusable (a driver/libpq exception,
      or a non-``TUPLES_OK`` result). This is the same conclusive "dead"
      signal native ``pool_pre_ping`` acts on, and :func:`_pre_ping_listeners`
      responds to it exactly as native pre-ping does: ``InvalidatePoolError``,
      invalidating the WHOLE pool, on the theory that one dead backend often
      means a failover just happened and every other pooled connection is
      stale too.
    * ``None`` — the probe did NOT reach a conclusive answer before
      ``timeout_s`` elapsed. This is INCONCLUSIVE, not proof of death: a
      merely slow-but-alive server (e.g. a saturated transaction-mode
      PgBouncer legitimately queuing this ``SELECT 1`` behind other clients'
      transactions, up to its own ``query_wait_timeout``) trips this
      identically to a genuinely wedged one — native pre-ping never had this
      failure mode at all, because it has no deadline to exceed; it simply
      waits and eventually gets a real answer. Treating ``None`` the same as
      ``False`` would mean every transient slowdown invalidates the ENTIRE
      pool and forces every subsequent checkout to reconnect — a
      self-amplifying failure mode against an already-saturated pooler.
      :func:`_pre_ping_listeners` therefore raises the weaker
      ``DisconnectionError(invalidate_pool=False)`` for this case: only THIS
      one connection is discarded and retried, every other pooled connection
      is left alone.

    For a psycopg3 DBAPI connection (duck-typed via
    ``getattr(dbapi_connection, "pgconn", None)`` — no psycopg import is added
    to this module), this drives psycopg's own public libpq async API
    (``PGconn.send_query`` / ``.flush`` / ``.is_busy`` / ``.consume_input`` /
    ``.get_result``) directly, waiting on the raw socket with
    :func:`_wait_socket` between polls instead of psycopg's blocking
    (unbounded) wrappers. This is MODELLED ON — not identical to — the
    generator-driven pattern ``psycopg.generators`` uses internally for its
    own async connections: psycopg's send generator waits on
    read-OR-write and calls ``consume_input()`` as soon as the socket is
    readable, whereas the loop below waits write-only while draining
    ``flush()``. That divergence is safe here specifically because the probe
    payload (``SELECT 1``) is far smaller than the socket send buffer, so
    ``flush()`` never legitimately returns non-zero on a healthy connection —
    and a stalled ``flush()`` on an unhealthy one still terminates at
    ``deadline`` either way, it just cannot also opportunistically notice an
    early server response while waiting to finish sending. Any wait that
    times out returns ``None`` (inconclusive — see above); any driver/libpq
    error is treated as a definitive "not usable" and returns ``False``
    (broad ``except Exception`` is deliberate here — BLE lint is not enabled
    in this workspace).

    For any other DBAPI driver (no ``pgconn`` attribute), falls back to
    :func:`_unbounded_cursor_ping` — status-quo, UNBOUNDED, documented
    residual, so it can only ever return a definitive ``True``/``False``
    (there is no deadline to time out against).

    Documented residual this function does NOT cover (NE-POOLER-PREPING-BOUND
    PR body): pool CHECK-IN, not just checkout. SQLAlchemy's default
    ``reset_on_return="rollback"`` issues a plain ``ROLLBACK`` straight on the
    raw DBAPI connection when a connection is returned to the pool — the same
    unbounded raw-DBAPI shape this function fixes for the checkout/pre-ping
    side, but on the way IN. A server that wedges while a connection is
    checked out therefore still hangs the check-in path with no client-side
    bound. Left unaddressed here (out of this unit's scope; a candidate
    follow-up alongside the non-psycopg3 fallback and the independent
    ``pool_pre_ping=True`` engine at
    ``packages/ne-connect/src/ne/connect/sql/engine.py``).
    """
    pgconn = getattr(dbapi_connection, "pgconn", None)
    if pgconn is None:
        return _unbounded_cursor_ping(dbapi_connection)

    deadline = time.monotonic() + timeout_s
    sel = selectors.DefaultSelector()
    try:
        pgconn.send_query(b"SELECT 1")
        while pgconn.flush():
            if not _wait_socket(sel, pgconn.socket, want_write=True, deadline=deadline):
                return None
        results = []
        while True:
            while pgconn.is_busy():
                if not _wait_socket(sel, pgconn.socket, want_write=False, deadline=deadline):
                    return None
                pgconn.consume_input()
            result = pgconn.get_result()
            if result is None:
                break
            results.append(result)
        return bool(results) and all(int(r.status) == _PQ_TUPLES_OK for r in results)
    except Exception:  # broad on purpose: any driver/libpq error means "definitively unusable"
        return False
    finally:
        sel.close()


def _pre_ping_listeners(
    timeouts: EngineTimeouts,
) -> tuple[Callable[[Any, Any], None], Callable[[Any, Any, Any], None]]:
    """Build the ``(connect, checkout)`` pool-event listener pair that replaces
    SQLAlchemy's native ``pool_pre_ping`` with a bounded probe
    (NE-POOLER-PREPING-BOUND). This is SQLAlchemy's own documented
    "disconnect handling - pessimistic" recipe (a checkout listener that
    raises ``InvalidatePoolError``/``DisconnectionError`` to force an
    invalidate-and-retry), just with the probe swapped for
    :func:`_bounded_dbapi_ping` instead of the library default.

    * ``_mark_fresh`` (pool ``"connect"`` event) — fires once per NEW physical
      DBAPI connection, immediately after it is established, and records that
      in ``connection_record.info`` so the very FIRST checkout of that
      connection skips probing — parity with native pre-ping's own
      freshness-skip (``_ConnectionFairy._checkout``: a connection that was
      JUST opened does not need a liveness check). This is safe against the
      SQLAlchemy-internal ``info.clear()`` inside
      ``_ConnectionRecord.get_connection``: that clear runs BEFORE
      ``__connect()`` fires the ``"connect"`` event (verified by reading the
      installed 2.0.51 ``sqlalchemy/pool/base.py``), so the flag this listener
      sets is never wiped out from under it before ``_pre_ping`` reads it.
    * ``_pre_ping`` (pool ``"checkout"`` event) — fires on every checkout. Pops
      the freshness flag (so a SUBSEQUENT checkout of the same physical
      connection DOES get probed); otherwise calls :func:`_bounded_dbapi_ping`
      (referenced by its module-global name, not captured in a closure, so
      tests can monkeypatch it) under ``timeouts.pre_ping_timeout_s`` and acts
      on its TRI-STATE result (Argus P2, 2026-07-24 review round 2 — see that
      function's docstring for the full "inconclusive vs. definitively dead"
      rationale):

      - ``True`` — healthy, no raise.
      - ``False`` — the probe DEFINITIVELY reports the connection unusable
        (an error, or a bad result status). Raises ``InvalidatePoolError`` —
        the EXACT exception class native pre-ping raises on a failed ping —
        invalidating the WHOLE pool (a dead backend often means every other
        pooled connection is stale too, e.g. after a failover).
      - ``None`` — the probe TIMED OUT without a conclusive answer. Unlike
        native pre-ping (which has no deadline and simply waits for a real
        answer), a bounded probe can time out against a server that is merely
        SLOW, not dead — e.g. a saturated transaction-mode PgBouncer queuing
        this ``SELECT 1`` behind other clients' transactions. Treating that
        identically to a proven-dead connection would invalidate the entire
        pool on every transient slowdown, forcing every other checkout to
        reconnect too — a self-amplifying failure mode against an
        already-saturated pooler. So this raises the weaker
        ``DisconnectionError(invalidate_pool=False)`` instead: only THIS one
        connection is discarded and retried; every other pooled connection is
        left untouched.

      Both exception types give the pool's own
      invalidate-then-retry-with-a-fresh-connection semantics
      (``_ConnectionFairy._checkout``, 2 attempts) — they differ ONLY in
      whether OTHER, currently-idle pooled connections are also invalidated;
      this fairy's own retry proceeds identically either way (verified by
      reading ``sqlalchemy/pool/base.py``'s checkout retry loop).

    Installed as ENGINE-target pool listeners (``event.listen(engine, "connect"
    | "checkout", ...)``, not ``event.listen(engine.pool, ...)``) — the same
    pattern :func:`ensure_transactional_sqlite` already relies on — so they
    survive ``engine.dispose()``/``pool.recreate()`` (SQLAlchemy re-attaches
    engine-target pool listeners to the new pool automatically).
    """

    def _mark_fresh(dbapi_connection: Any, connection_record: Any) -> None:
        connection_record.info[_PRE_PING_FRESH_KEY] = True

    def _pre_ping(dbapi_connection: Any, connection_record: Any, connection_proxy: Any) -> None:
        if connection_record.info.pop(_PRE_PING_FRESH_KEY, False):
            return
        result = _bounded_dbapi_ping(dbapi_connection, float(timeouts.pre_ping_timeout_s))
        if result is True:
            return
        if result is None:
            # Inconclusive (probe timed out, no conclusive answer): could be a
            # merely-slow-but-alive backend, not a proven-dead one. Invalidate
            # ONLY this connection, not the whole pool — see this function's
            # docstring and _bounded_dbapi_ping's for the full rationale.
            raise DisconnectionError(
                f"NE-POOLER-PREPING-BOUND: pre-ping probe exceeded "
                f"pre_ping_timeout_s={timeouts.pre_ping_timeout_s}s without a "
                "conclusive answer (inconclusive, not proven dead) -- "
                "invalidating this connection only"
            )
        # False: the probe DEFINITIVELY reports the connection unusable —
        # matches native pre-ping's own failed-ping response exactly.
        raise InvalidatePoolError()

    return _mark_fresh, _pre_ping


def create_engine_with_timeouts(url: str, *, timeouts: EngineTimeouts | None = None) -> sa.Engine:
    """``sa.create_engine(url)`` with connect/statement/lock/pool timeouts applied.

    The single chokepoint every in-scope engine factory routes through so no
    ``create_engine`` ships with unbounded blocking defaults (P2-16). Behavior is
    otherwise identical to ``sa.create_engine`` — the caller still applies
    :func:`ensure_transactional_sqlite` and its schema-ensure as before.

    For a ``postgresql*`` URL, this additionally installs:

    * the pooler-safe per-transaction ``"begin"`` timeout listener built by
      :func:`_pg_timeout_listener` (see that function's docstring for the full
      mechanism justification, including why a connect-time listener was tried
      and reverted); and
    * the bounded pre-ping listener pair built by :func:`_pre_ping_listeners`
      (pool ``"connect"``/``"checkout"``) — the REPLACEMENT for SQLAlchemy's
      native ``pool_pre_ping`` (NE-POOLER-PREPING-BOUND), which
      :func:`engine_kwargs_for_url` deliberately no longer enables because it
      runs its liveness probe on the raw DBAPI connection with no bound. See
      :func:`_bounded_dbapi_ping`'s docstring for why a client-side deadline is
      the only mechanism that can bound a probe on an already-open connection
      to a wedged server.

    Both are installed ONLY on postgresql — never sqlite/other — for two
    reasons: (1) they would be meaningless (SQLite has no
    ``statement_timeout``/``lock_timeout`` GUCs and pysqlite connections do not
    wedge the way a TCP Postgres connection can), and (2) installing a
    ``"begin"`` listener on a sqlite engine would make
    :func:`ensure_transactional_sqlite`'s foreign-recipe guard
    (``bool(engine.dispatch.begin)``) see an existing listener and skip
    installing its OWN required ``BEGIN`` recipe — a silent dual-append-guard
    regression. Pinned by
    ``test_sqlite_engine_gets_no_pg_listeners_and_transactional_recipe_still_installs``.
    """
    resolved_timeouts = timeouts or EngineTimeouts()
    engine = sa.create_engine(url, **engine_kwargs_for_url(url, resolved_timeouts))
    if sa.make_url(url).get_backend_name() == "postgresql":
        event.listen(engine, "begin", _pg_timeout_listener(resolved_timeouts))
        mark_fresh, pre_ping = _pre_ping_listeners(resolved_timeouts)
        event.listen(engine, "connect", mark_fresh)
        event.listen(engine, "checkout", pre_ping)
    return engine


def default_offload(engine: sa.Engine) -> bool:
    """Whether an adapter over ``engine`` should offload sync DB I/O to a thread.

    True for every dialect EXCEPT sqlite. pysqlite connections are thread-bound
    and an in-memory ``sqlite://`` engine typically rides a ``SingletonThreadPool``,
    so handing each ``asyncio.to_thread`` worker a connection would give it a
    DIFFERENT (empty) database — SQLite therefore stays ON the loop BY DESIGN
    (dev/CI only). Production is Postgres, where the offload is what keeps a slow
    query or failover from pinning the ASGI event loop.
    """
    return engine.dialect.name != "sqlite"


async def call_db[T](offload: bool, fn: Callable[..., T], /, *args: Any) -> T:
    """Run the sync DB body ``fn(*args)`` off-loop when ``offload`` else inline.

    Mirrors the ne-worker outbox drain precedent
    (``packages/ne-worker/src/ne/worker/main.py`` ``drain_once`` via
    ``asyncio.to_thread``): a blocking SQLAlchemy body must not execute on the
    event loop for a non-sqlite dialect. On sqlite (:func:`default_offload` False)
    the body runs inline so thread-bound pysqlite connections stay on one thread.
    """
    if offload:
        return await asyncio.to_thread(fn, *args)
    return fn(*args)


def ensure_transactional_sqlite(engine: Engine) -> None:
    """Make pysqlite SAVEPOINT writes nest inside the outer ``engine.begin()``.

    Installs the NE-069 reference recipe on ``engine`` so that on pysqlite,
    SQLAlchemy issues an explicit ``BEGIN`` and a ``begin_nested()`` SAVEPOINT
    nests correctly INSIDE the outer transaction — so an outer rollback undoes
    the SAVEPOINT writes too, closing the dual-append stranding gap (SPEC §11).

    No-op unless ``engine.dialect.name == "sqlite"`` (Postgres byte-identical),
    and idempotent per engine via a module-level WeakSet.
    """
    if (
        engine.dialect.name != "sqlite"
        or engine in _patched_engines
        # A foreign transaction recipe (BEGIN IMMEDIATE fixture, migrate.py's
        # explicit-BEGIN listener, etc.) is already installed — do NOT stack a
        # second ``begin`` listener; the engine is already instrumented and a
        # duplicate ``BEGIN`` would raise sqlite3.OperationalError (Codex P2).
        or bool(engine.dispatch.begin)
    ):
        return
    _patched_engines.add(engine)

    @event.listens_for(engine, "checkout")
    def _do_checkout(dbapi_connection: Any, connection_record: Any, connection_proxy: Any) -> None:
        if dbapi_connection.isolation_level is not None:
            dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _do_begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN")
