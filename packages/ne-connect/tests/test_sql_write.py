# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SqlWriteClient.update_record: identifier allowlist + bound params, fail-closed (NE-050)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from ne.connect.errors import SqlSyncError
from ne.connect.sql.write import SqlWriteClient

_ALLOWED = {"tickets": ("id", "status", "public_reply")}


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(
            sa.text("CREATE TABLE tickets (id INTEGER PRIMARY KEY, status TEXT, public_reply TEXT)")
        )
        conn.execute(
            sa.text("INSERT INTO tickets (id, status, public_reply) VALUES (1, 'open', '')")
        )
    try:
        yield eng
    finally:
        eng.dispose()


def _rows(engine: sa.Engine) -> list[tuple]:
    with engine.connect() as conn:
        return [
            tuple(r) for r in conn.execute(sa.text("SELECT id, status, public_reply FROM tickets"))
        ]


def test_happy_path_updates_and_returns_rowcount(engine: sa.Engine) -> None:
    client = SqlWriteClient(engine, allowed_writes=_ALLOWED)
    rows = asyncio.run(
        client.update_record(
            table="tickets",
            key_column="id",
            key_value=1,
            fields={"status": "resolved", "public_reply": "done"},
        )
    )
    assert rows == 1
    assert _rows(engine) == [(1, "resolved", "done")]


def test_no_match_returns_zero(engine: sa.Engine) -> None:
    client = SqlWriteClient(engine, allowed_writes=_ALLOWED)
    rows = asyncio.run(
        client.update_record(
            table="tickets", key_column="id", key_value=999, fields={"status": "closed"}
        )
    )
    assert rows == 0
    assert _rows(engine) == [(1, "open", "")]


def test_reapplying_same_update_is_idempotent(engine: sa.Engine) -> None:
    client = SqlWriteClient(engine, allowed_writes=_ALLOWED)
    for _ in range(2):
        asyncio.run(
            client.update_record(
                table="tickets", key_column="id", key_value=1, fields={"status": "closed"}
            )
        )
    assert _rows(engine) == [(1, "closed", "")]


def test_unlisted_table_rejected(engine: sa.Engine) -> None:
    client = SqlWriteClient(engine, allowed_writes=_ALLOWED)
    with pytest.raises(SqlSyncError):
        asyncio.run(
            client.update_record(
                table="secrets", key_column="id", key_value=1, fields={"status": "x"}
            )
        )
    assert _rows(engine) == [(1, "open", "")]


def test_unlisted_column_rejected(engine: sa.Engine) -> None:
    client = SqlWriteClient(engine, allowed_writes=_ALLOWED)
    with pytest.raises(SqlSyncError):
        asyncio.run(
            client.update_record(
                table="tickets", key_column="id", key_value=1, fields={"secret_col": "x"}
            )
        )
    assert _rows(engine) == [(1, "open", "")]


def test_bad_identifier_rejected_before_sql(engine: sa.Engine) -> None:
    client = SqlWriteClient(engine, allowed_writes={"tickets; DROP": ("id", "status")})
    with pytest.raises(SqlSyncError):
        asyncio.run(
            client.update_record(
                table="tickets; DROP", key_column="id", key_value=1, fields={"status": "x"}
            )
        )
    # The real tickets table survives (nothing was executed).
    assert _rows(engine) == [(1, "open", "")]


def test_hostile_value_is_bound_not_executed(engine: sa.Engine) -> None:
    client = SqlWriteClient(engine, allowed_writes=_ALLOWED)
    hostile = "x'; DROP TABLE tickets;--"
    rows = asyncio.run(
        client.update_record(
            table="tickets", key_column="id", key_value=1, fields={"public_reply": hostile}
        )
    )
    assert rows == 1
    # The table survives AND the hostile string is stored literally as data.
    assert _rows(engine) == [(1, "open", hostile)]


def test_empty_allowlist_can_never_write(engine: sa.Engine) -> None:
    client = SqlWriteClient(engine, allowed_writes={})
    with pytest.raises(SqlSyncError):
        asyncio.run(
            client.update_record(
                table="tickets", key_column="id", key_value=1, fields={"status": "x"}
            )
        )
    assert _rows(engine) == [(1, "open", "")]
