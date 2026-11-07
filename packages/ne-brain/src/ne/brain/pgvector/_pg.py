# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The psycopg chokepoint for the pgvector store — the ONLY module importing psycopg.

Confines the LGPL ``psycopg`` surface to one auditable file (the
:mod:`ne.brain_graph._pg` role, enforced by the additive ``.importlinter``
``c3-psycopg-chokepoint`` contract). :mod:`ne.brain.pgvector.store` uses ONLY the
helpers here plus the connection's own transaction/cursor context managers (which
are attribute access on the returned object, NOT a ``psycopg`` import), so the
store module never imports ``psycopg``.

No error-translation layer: the frozen ``ChunkStore`` Protocol has no error
taxonomy, so driver faults propagate raw (the Qdrant-adapter precedent — it does
not wrap ``qdrant_client`` exceptions either).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg

__all__ = ["connect", "execute", "executemany"]


async def connect(dsn: str) -> psycopg.AsyncConnection[Any]:
    """Open an autocommit async connection to the customer Postgres (C2: in-network).

    Best-effort ``SET hnsw.iterative_scan = 'strict_order'`` (a pgvector >= 0.8
    retrieval-QUALITY knob — makes filtered HNSW scans return more of the true
    top-k, never a security control): on pgvector < 0.8 the GUC is unknown and the
    ``SET`` raises ``UndefinedObject``/``UndefinedParameter``, which is swallowed
    (autocommit means a failed statement never poisons a transaction). The store
    re-applies it after ``CREATE EXTENSION`` (a freshly-created extension may not
    have registered the GUC at connect time).
    """
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    await _try_set_iterative_scan(conn)
    return conn


async def _try_set_iterative_scan(conn: psycopg.AsyncConnection[Any]) -> None:
    try:
        async with conn.cursor() as cur:
            await cur.execute("SET hnsw.iterative_scan = 'strict_order'")
    except (psycopg.errors.UndefinedObject, psycopg.errors.UndefinedParameter):
        pass  # pgvector < 0.8 — retrieval-quality only, never security.


async def try_set_iterative_scan(conn: psycopg.AsyncConnection[Any]) -> None:
    """Public re-apply hook the store calls once after ``CREATE EXTENSION`` (best-effort)."""
    await _try_set_iterative_scan(conn)


async def execute(
    conn: psycopg.AsyncConnection[Any],
    statement: str,
    params: Sequence[Any] | None = None,
) -> list[tuple[Any, ...]]:
    """Run one statement; return its rows (or ``[]`` for a non-SELECT)."""
    async with conn.cursor() as cur:
        await cur.execute(statement.encode(), params)  # type: ignore[arg-type]
        if cur.description is None:
            return []
        return await cur.fetchall()


async def executemany(
    conn: psycopg.AsyncConnection[Any],
    statement: str,
    seq_params: Sequence[Sequence[Any]],
) -> None:
    """Run one statement against many parameter tuples (batched INSERT/DELETE)."""
    async with conn.cursor() as cur:
        await cur.executemany(statement.encode(), seq_params)  # type: ignore[arg-type]
