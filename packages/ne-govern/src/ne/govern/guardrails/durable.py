# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Durable, atomic guardrail state — the SqlAlchemy-backed spend ledger + rate counter.

NE-070H closes two tracked races that are fail-safe on a single-writer CI but REAL
under the pilot tier's multi-worker Postgres:

* **Race 2 (FAIL-OPEN spend/rate TOCTOU)** — the guardrail check-limit-then-charge
  is not atomic, so concurrent tool calls can BOTH pass ``check()`` and overshoot a
  cap. The fix makes the CHARGE itself the authoritative gate:
  :class:`SqlAlchemySpendLedger.charge` is ONE transaction of dialect-dispatched
  ``INSERT .. ON CONFLICT DO NOTHING`` seeding + a conditional
  ``UPDATE .. WHERE total + amount <= cap`` per scope. On Postgres the row lock
  serializes concurrent chargers, so a cap of C can NEVER be overshot; the loser's
  ``UPDATE`` matches zero rows, the multi-scope transaction rolls back
  all-or-nothing, and ``charge`` returns ``False`` (the engine audits + denies).
* **Race 4 (RateLimitCounter durability, NE-CKD residual)** — the in-memory
  :class:`~ne.govern.guardrails.rate.SlidingWindowCounter` resets on restart. The
  SqlAlchemy-backed :class:`SqlAlchemySlidingWindowCounter` persists events so rate
  caps SURVIVE a worker restart and are SHARED across all workers.

Both are injected via ``ne.runtime.factory.create_shared_guardrail_state(database_url=...)``.

Money is EXACT (NE-032 discipline): spend is stored as integer **nano-USD**
(``BigInteger`` ≈ 9.2e9 USD headroom) with PESSIMISTIC rounding — charges round UP
(``ROUND_CEILING``), caps round DOWN (``ROUND_FLOOR``) — so rounding can only ever
UNDERSHOOT a cap, never overshoot it. ``sa.Numeric`` is deliberately AVOIDED: SQLite
degrades it to a binary ``REAL`` and money would drift.

Portability: all writes use portable Core SQL + a dialect-dispatched
``INSERT .. ON CONFLICT DO NOTHING`` guarded on ``conn.dialect.name`` (precedent:
``ne.events.outbox``). Any dialect other than sqlite/postgresql raises
``NotImplementedError``. Postgres is the deployed target (``FOR UPDATE`` row
serialization); the SQLite path (CI only) is serialized by its writer lock plus an
in-process :class:`threading.Lock` belt on the counter.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from ne.govern.guardrails.rate import SlidingWindowCounter
from ne.govern.guardrails.spend import SpendLedger

metadata = sa.MetaData()

_NANO = Decimal(10) ** 9

ne_guardrail_spend = sa.Table(
    "ne_guardrail_spend",
    metadata,
    sa.Column("scope", sa.String, primary_key=True),
    sa.Column("total_nanousd", sa.BigInteger, nullable=False),
)

ne_guardrail_rate_keys = sa.Table(
    "ne_guardrail_rate_keys",
    metadata,
    sa.Column("key", sa.String, primary_key=True),
)

ne_guardrail_rate_events = sa.Table(
    "ne_guardrail_rate_events",
    metadata,
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    ),
    sa.Column("key", sa.String, nullable=False),
    sa.Column("ts", sa.Float, nullable=False),
    sa.Index("ix_guardrail_rate_events_key_ts", "key", "ts"),
)


def ensure_guardrail_state_schema(engine: sa.Engine) -> None:
    """Create the durable guardrail-state tables. Idempotent (``create_all`` skips)."""
    metadata.create_all(engine)


def _insert_ignore(table: sa.Table, conn: sa.Connection, *, index_elements: Sequence[str]):  # type: ignore[no-untyped-def]
    """Dialect-dispatched ``INSERT .. ON CONFLICT DO NOTHING`` for ``table``."""
    dialect = conn.dialect.name
    if dialect == "postgresql":
        return postgresql.insert(table).on_conflict_do_nothing(index_elements=list(index_elements))
    if dialect == "sqlite":
        return sqlite.insert(table).on_conflict_do_nothing(index_elements=list(index_elements))
    raise NotImplementedError("ne-govern durable guardrail state supports sqlite and postgresql")


def _nanousd_charge(amount: Decimal) -> int:
    """Convert a USD charge to integer nano-USD, rounding UP (pessimistic for caps)."""
    return int((amount * _NANO).to_integral_value(rounding=ROUND_CEILING))


def _nanousd_cap(cap: Decimal) -> int:
    """Convert a USD cap to integer nano-USD, rounding DOWN (pessimistic for caps)."""
    return int((cap * _NANO).to_integral_value(rounding=ROUND_FLOOR))


class _ChargeDenied(Exception):
    """Internal: a scope's conditional charge matched zero rows (cap would be exceeded)."""


class SqlAlchemySpendLedger(SpendLedger):
    """Durable, atomic :class:`SpendLedger` over SqlAlchemy-Core (Postgres/SQLite)."""

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    def recorded_spend(self, scope: str) -> Decimal:
        with self._engine.connect() as conn:
            total = conn.execute(
                sa.select(ne_guardrail_spend.c.total_nanousd).where(
                    ne_guardrail_spend.c.scope == scope
                )
            ).scalar()
        return Decimal(0) if total is None else Decimal(int(total)) / _NANO

    def record(self, scope: str, amount: Decimal) -> None:
        # Unconditional upsert+add (kept for interface compatibility; the atomic
        # cap enforcement lives in ``charge``).
        amt = _nanousd_charge(amount)
        with self._engine.begin() as conn:
            conn.execute(
                _insert_ignore(ne_guardrail_spend, conn, index_elements=["scope"]).values(
                    scope=scope, total_nanousd=0
                )
            )
            conn.execute(
                ne_guardrail_spend.update()
                .where(ne_guardrail_spend.c.scope == scope)
                .values(total_nanousd=ne_guardrail_spend.c.total_nanousd + amt)
            )

    def charge(self, charges: Sequence[tuple[str, Decimal, Decimal]]) -> bool:
        # ONE transaction, multi-scope all-or-nothing: seed each scope then apply a
        # CONDITIONAL update guarded by ``total + amount <= cap``. A zero-rowcount
        # update means the cap would be exceeded → raise _ChargeDenied, which rolls
        # the whole transaction back (no scope charged) → return False. On Postgres
        # the per-row lock serializes concurrent chargers so the cap holds exactly.
        try:
            with self._engine.begin() as conn:
                for scope, amount, cap in charges:
                    amt = _nanousd_charge(amount)
                    cap_nano = _nanousd_cap(cap)
                    conn.execute(
                        _insert_ignore(ne_guardrail_spend, conn, index_elements=["scope"]).values(
                            scope=scope, total_nanousd=0
                        )
                    )
                    result = conn.execute(
                        ne_guardrail_spend.update()
                        .where(
                            ne_guardrail_spend.c.scope == scope,
                            ne_guardrail_spend.c.total_nanousd + amt <= cap_nano,
                        )
                        .values(total_nanousd=ne_guardrail_spend.c.total_nanousd + amt)
                    )
                    if result.rowcount != 1:
                        raise _ChargeDenied(scope)
        except _ChargeDenied:
            return False
        return True


class SqlAlchemySlidingWindowCounter(SlidingWindowCounter):
    """Durable, atomic :class:`SlidingWindowCounter` over SqlAlchemy-Core.

    Uses a WALL-CLOCK epoch (``time.time``) by default so the sliding window is
    shared coherently across workers on different processes/hosts. Caveat: the
    window is only as tight as the workers' clock skew — keep hosts NTP-synced;
    bounded skew widens/narrows the effective window by at most that skew, never
    breaks the cap enforcement (which is a COUNT, not a clock comparison alone).
    """

    def __init__(self, engine: sa.Engine, clock: Callable[[], float] = time.time) -> None:
        self._engine = engine
        self._clock = clock
        self._lock = threading.Lock()

    def count(self, key: str, window_s: float) -> int:
        now = self._clock()
        cutoff = now - window_s
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_guardrail_rate_events)
                    .where(
                        ne_guardrail_rate_events.c.key == key,
                        ne_guardrail_rate_events.c.ts > cutoff,
                    )
                ).scalar_one()
            )

    def record(self, key: str) -> None:
        now = self._clock()
        with self._engine.begin() as conn:
            conn.execute(
                _insert_ignore(ne_guardrail_rate_keys, conn, index_elements=["key"]).values(key=key)
            )
            conn.execute(ne_guardrail_rate_events.insert().values(key=key, ts=now))

    def record_if_within(self, key: str, window_s: float, max_calls: int) -> bool:
        # ONE transaction guarded by an in-process lock (the SQLite-CI belt): seed
        # the key row, claim it ``FOR UPDATE`` (row-serializes Postgres; a harmless
        # no-op on SQLite whose writer lock already serializes), prune expired
        # events, COUNT in-window, and conditionally append. Under K concurrent
        # reservations at most ``max_calls`` admit.
        now = self._clock()
        cutoff = now - window_s
        with self._lock, self._engine.begin() as conn:
            conn.execute(
                _insert_ignore(ne_guardrail_rate_keys, conn, index_elements=["key"]).values(key=key)
            )
            conn.execute(
                sa.select(ne_guardrail_rate_keys.c.key)
                .where(ne_guardrail_rate_keys.c.key == key)
                .with_for_update()
            ).first()
            conn.execute(
                ne_guardrail_rate_events.delete().where(
                    ne_guardrail_rate_events.c.key == key,
                    ne_guardrail_rate_events.c.ts <= cutoff,
                )
            )
            count = int(
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_guardrail_rate_events)
                    .where(
                        ne_guardrail_rate_events.c.key == key,
                        ne_guardrail_rate_events.c.ts > cutoff,
                    )
                ).scalar_one()
            )
            if count >= max_calls:
                return False
            conn.execute(ne_guardrail_rate_events.insert().values(key=key, ts=now))
            return True
