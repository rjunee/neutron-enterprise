# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SQL connector (NE-046): dlt ``sql_database`` over Postgres/MSSQL, fail-closed
config-driven ACL, table -> document mapping.

Importing THIS subpackage imports dlt via the ``ne.connect._dlt`` chokepoint —
deliberately opt-in (``ne.connect`` itself stays dlt-free; see its module
docstring). Consumers import explicitly: ``from ne.connect.sql import ...``.
"""

from __future__ import annotations

from ne.connect.sql.acl import TENANT_ALL, RowAclOutcome, resolve_row_principals
from ne.connect.sql.config import SqlConnectorSettings, SqlTableAclConfig, SqlTableConfig
from ne.connect.sql.connector import SqlConnector
from ne.connect.sql.contract import sql_data_contract
from ne.connect.sql.engine import create_validated_engine, validate_connection_url
from ne.connect.sql.sync import SqlSyncOutcome, make_sql_record_mapper, run_sql_sync
from ne.connect.sql.write import SqlWriteClient

__all__ = [
    "TENANT_ALL",
    "RowAclOutcome",
    "SqlConnector",
    "SqlConnectorSettings",
    "SqlSyncOutcome",
    "SqlTableAclConfig",
    "SqlTableConfig",
    "SqlWriteClient",
    "create_validated_engine",
    "make_sql_record_mapper",
    "resolve_row_principals",
    "run_sql_sync",
    "sql_data_contract",
    "validate_connection_url",
]
