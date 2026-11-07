# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SqlConnector passes the frozen ConnectorConformanceKit (NE-046).

The kit itself is UNMODIFIED (contract-law) — only subclassed.
``incremental_state`` supplies a differing cursor basis (excluding the two
earliest ``docs`` rows and the first resolvable ``notes`` row) so
``test_incremental_respects_cursor_state`` exercises real ``>=`` cursor
semantics (a strict subset), and ``test_zero_network_replay`` proves the
file-backed SQLite replay opens no sockets (dlt's chunked reader talks to the
filesystem, not the network).
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path

import sqlalchemy as sa

from ne.connect.sql.connector import SqlConnector
from ne.connect.sql.contract import sql_data_contract
from ne.connect.testing.kits.connector import ConnectorConformanceKit
from ne.contracts.integration import Connector

from .sql_fixture import build_sql_fixture_db, sql_settings

_DB_DIR = Path(tempfile.mkdtemp(prefix="ne_sql_conformance_"))
_DB_PATH = _DB_DIR / "fixture.db"
build_sql_fixture_db(_DB_PATH)


class TestSqlConformance(ConnectorConformanceKit):
    def create_connector(self) -> Connector:
        return SqlConnector(
            sql_settings(_DB_PATH), engine=sa.create_engine(f"sqlite:///{_DB_PATH}")
        )

    def create_contract(self):
        return sql_data_contract(sql_settings(_DB_PATH))

    def expected_min_records(self) -> int:
        return 7

    def incremental_state(self) -> Mapping[str, str]:
        return {"cursor:docs": "2026-01-03T00:00:00", "cursor:notes": "10"}
