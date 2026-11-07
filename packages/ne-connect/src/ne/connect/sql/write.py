# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin SQL WRITE client (NE-050) — allowlisted, bound-param UPDATE of a record.

:class:`SqlWriteClient` performs a single-row (by key) UPDATE over the validated
NE-046 SQLAlchemy engine (from ``create_validated_engine`` — construction is NOT
re-validated here). Two fail-closed gates run BEFORE any statement:

1. **Identifier allowlist** — the table, key column, and every field column must be in
   the caller-configured ``allowed_writes`` mapping (``table -> writable columns``). An
   EMPTY mapping means no write is ever possible (fail-closed). Every identifier must
   also match a bare-identifier regex.
2. **Bound parameters only** — the statement is built with SQLAlchemy Core; the key and
   every field VALUE is a bound parameter, so a hostile value like
   ``"x'; DROP TABLE tickets;--"`` is stored as literal column data, never executed.

Errors raise a :class:`SqlSyncError` naming only the offending identifier — never a
value or the connection URL (C12). This module imports NO httpx.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

import sqlalchemy as sa

from ne.connect.errors import SqlSyncError

__all__ = ["SqlWriteClient"]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class SqlWriteClient:
    """Allowlisted, bound-param single-row UPDATE over a validated NE-046 engine."""

    def __init__(self, engine: sa.Engine, *, allowed_writes: Mapping[str, tuple[str, ...]]) -> None:
        self._engine = engine
        # table -> the set of columns writable on that table (incl. any key column).
        self._allowed_writes = {table: frozenset(cols) for table, cols in allowed_writes.items()}

    async def update_record(
        self,
        *,
        table: str,
        key_column: str,
        key_value: int | str,
        fields: Mapping[str, str],
    ) -> int:
        """UPDATE ``table`` SET ``fields`` WHERE ``key_column`` = ``key_value``.

        Returns the affected rowcount (0 on no match). Every identifier is
        allowlist-checked and every value is a bound parameter (injection-inert).
        """
        allowed = self._allowed_writes.get(table)
        if allowed is None:
            raise SqlSyncError("refusing to write to a table outside the write allowlist")
        for ident in (table, key_column, *fields.keys()):
            if not _IDENTIFIER_RE.match(ident):
                raise SqlSyncError("refusing to use a malformed SQL identifier")
        if key_column not in allowed:
            raise SqlSyncError("refusing to key on a column outside the write allowlist")
        for name in fields:
            if name not in allowed:
                raise SqlSyncError("refusing to write a column outside the write allowlist")
        if not fields:
            raise SqlSyncError("refusing to issue an UPDATE with no field values")

        tbl = sa.table(table, sa.column(key_column), *[sa.column(k) for k in fields])
        stmt = (
            sa.update(tbl)
            .where(sa.column(key_column) == sa.bindparam("key_value"))
            .values({k: sa.bindparam(f"v_{k}") for k in fields})
        )
        params: dict[str, object] = {"key_value": key_value}
        for k, v in fields.items():
            params[f"v_{k}"] = v
        with self._engine.begin() as conn:
            return conn.execute(stmt, params).rowcount
