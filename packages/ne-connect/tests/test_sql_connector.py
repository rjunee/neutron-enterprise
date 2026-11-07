# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SqlConnector crawl semantics over a file-backed SQLite fixture (NE-046)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.connect.errors import InvalidRecordError, SqlSyncError
from ne.connect.sql.config import SqlTableAclConfig, SqlTableConfig
from ne.connect.sql.connector import SqlConnector
from ne.contracts.integration import AclEntry, Connector, Record

from .sql_fixture import build_sql_fixture_db, sql_settings


async def _drain_backfill(connector: Connector) -> list[Record]:
    return [record async for record in connector.backfill()]


async def _drain_incremental(connector: Connector, state: dict[str, str]) -> list[Record]:
    return [record async for record in connector.incremental(state)]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.db"
    build_sql_fixture_db(path)
    return path


def test_backfill_yields_expected_records_and_source_ids(db_path: Path) -> None:
    connector = SqlConnector(sql_settings(db_path), engine=sa.create_engine(f"sqlite:///{db_path}"))
    records = asyncio.run(_drain_backfill(connector))
    assert len(records) == 7
    source_ids = {r.source_id for r in records}
    assert source_ids == {
        "docs:1",
        "docs:2",
        "docs:3",
        "notes:1",
        "notes:10",
        "announcements:1",
        "announcements:2",
    }
    # per-row source_id shape: f"{table_ref}:{id}"
    for record in records:
        assert record.source_id == f"{record.source_id.rsplit(':', 1)[0]}:{record.record_id}"


def test_row_filter_excludes_draft_doc(db_path: Path) -> None:
    connector = SqlConnector(sql_settings(db_path), engine=sa.create_engine(f"sqlite:///{db_path}"))
    records = asyncio.run(_drain_backfill(connector))
    doc_ids = {r.record_id for r in records if r.source_id.startswith("docs:")}
    assert doc_ids == {"1", "2", "3"}  # id=4 (draft) excluded


def test_quarantine_counts_and_ids_null_and_forged_principal(db_path: Path) -> None:
    connector = SqlConnector(sql_settings(db_path), engine=sa.create_engine(f"sqlite:///{db_path}"))
    asyncio.run(_drain_backfill(connector))
    assert connector.acl_quarantined == 2
    assert set(connector.acl_quarantined_ids) == {"notes:2", "notes:9"}
    # quarantined rows produce NEITHER a Record NOR an AclEntry.
    records = asyncio.run(_drain_backfill(connector))
    assert not any(r.source_id in ("notes:2", "notes:9") for r in records)


def test_multi_principal_row_carries_both(db_path: Path) -> None:
    connector = SqlConnector(sql_settings(db_path), engine=sa.create_engine(f"sqlite:///{db_path}"))
    asyncio.run(_drain_backfill(connector))
    entries = asyncio.run(_collect_permissions(connector))
    note10 = next(e for e in entries if e.resource_id == "notes:10")
    assert note10.principals == ("u-ada", "g-eng")


async def _collect_permissions(connector: SqlConnector) -> list[AclEntry]:
    return [entry async for entry in connector.permissions()]


def test_static_tables_emit_configured_principals(db_path: Path) -> None:
    connector = SqlConnector(sql_settings(db_path), engine=sa.create_engine(f"sqlite:///{db_path}"))
    asyncio.run(_drain_backfill(connector))
    entries = {e.resource_id: e.principals for e in asyncio.run(_collect_permissions(connector))}
    assert entries["docs:1"] == ("g-eng",)
    assert entries["announcements:1"] == ("acl:tenant-all",)
    assert entries["announcements:2"] == ("acl:tenant-all",)


def test_pending_cursors_are_typed_not_lexicographic(db_path: Path) -> None:
    connector = SqlConnector(sql_settings(db_path), engine=sa.create_engine(f"sqlite:///{db_path}"))
    asyncio.run(_drain_backfill(connector))
    # notes int cursor: id 10 > id 9 (9 is quarantined but ADVANCES the cursor
    # anyway — documented v1 limitation); a lexicographic compare would wrongly
    # keep "9" > "10".
    assert connector.pending_cursors == {
        "docs": "2026-01-03T00:00:00",
        "notes": "10",
    }


def test_incremental_yields_strict_subset_from_cursor_state(db_path: Path) -> None:
    connector = SqlConnector(sql_settings(db_path), engine=sa.create_engine(f"sqlite:///{db_path}"))
    full = asyncio.run(_drain_backfill(connector))

    connector2 = SqlConnector(
        sql_settings(db_path), engine=sa.create_engine(f"sqlite:///{db_path}")
    )
    state = {"cursor:docs": "2026-01-02T00:00:00", "cursor:notes": "9"}
    incremental = asyncio.run(_drain_incremental(connector2, state))

    full_ids = {(r.source_id, r.record_id) for r in full}
    incremental_ids = {(r.source_id, r.record_id) for r in incremental}
    assert incremental_ids < full_ids
    assert incremental_ids  # non-empty


def test_null_id_column_raises_invalid_record_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE broken (id TEXT, val TEXT)"))
        conn.execute(sa.text("INSERT INTO broken (id, val) VALUES (NULL, 'x')"))

    settings = sql_settings(
        path,
        tables=(
            SqlTableConfig(
                table="broken",
                id_column="id",
                content_columns=("val",),
                acl=SqlTableAclConfig(principals=("g-eng",)),
            ),
        ),
    )
    connector = SqlConnector(settings, engine=engine)
    with pytest.raises(InvalidRecordError, match="broken"):
        asyncio.run(_drain_backfill(connector))


def test_db_read_failure_is_sanitized_sql_sync_error(db_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE docs"))

    connector = SqlConnector(sql_settings(db_path), engine=engine)
    with pytest.raises(SqlSyncError) as excinfo:
        asyncio.run(_drain_backfill(connector))
    message = str(excinfo.value)
    assert "docs" in message
    assert "Error" in message  # the exception CLASS name is present
    assert str(db_path) not in message
