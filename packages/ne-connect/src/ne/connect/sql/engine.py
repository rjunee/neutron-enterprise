# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Validated SQLAlchemy engine construction for the SQL connector (NE-046).

``create_validated_engine`` is the ONLY ``sa.create_engine`` call site permitted
under ``ne-connect`` ``src/`` (a structural guardian test in
``test_sql_engine.py`` enforces this — the sibling of the M365 connector's
validated-construction guardian).

``validate_connection_url`` carries both fail-closed gates without importing a DBAPI.

Two fail-closed gates, both at CONSTRUCTION time, before any I/O:

1. **Dialect allowlist** — only ``postgresql``/``postgresql+psycopg`` (normalized
   to the psycopg3 driver) and ``mssql``/``mssql+pyodbc`` (normalized to pyodbc)
   are accepted for a real deployment; ``sqlite`` is accepted ONLY when
   ``settings.allow_sqlite`` is set (the fixture/test escape hatch). Anything else
   — MySQL, a bare ODBC DSN, an exotic dialect — is rejected.
2. **C2 in-VPC host policy** — a DB connection is EGRESS and must be provably
   in-VPC (or explicitly allowlisted). This REUSES the consolidated NE-CKC10
   validator, ``ne.brain.sovereignty.validate_in_vpc_endpoint`` (the SAME
   function :mod:`ne.connect.endpoints` itself wraps for the framework's
   external-source path — no fourth SSRF mirror is minted here) by wrapping the
   parsed host in a throwaway ``http://`` URL (the validator only inspects the
   host; scheme/path are irrelevant to its SSRF classification). The policy is
   already in-VPC-only, so AIR_GAP needs no additional branch here (unlike the
   M365 connector's external-source allowance).

C12: the connection URL / password NEVER appear in any error or log line raised
from this module — every ``SqlSyncError`` here names only the dialect or the
parsed hostname, never the full URL or credentials.
"""

from __future__ import annotations

import sqlalchemy as sa

from ne.brain.sovereignty import UnsafeEndpointError, validate_in_vpc_endpoint
from ne.connect.errors import SqlSyncError
from ne.connect.sql.config import SqlConnectorSettings

__all__ = ["create_validated_engine", "validate_connection_url"]

_POSTGRES_DRIVERS = frozenset({"postgresql", "postgresql+psycopg"})
_MSSQL_DRIVERS = frozenset({"mssql", "mssql+pyodbc"})


def validate_connection_url(settings: SqlConnectorSettings) -> sa.engine.URL:
    """Parse, validate, and normalize fail-closed without constructing an engine.

    This therefore imports no dialect DBAPI: driver ``.so`` availability is an
    engine-construction concern, not a validation concern.
    """
    try:
        url = sa.engine.make_url(settings.connection_url.get_secret_value())
    except Exception:  # any parse failure must not echo the value
        raise SqlSyncError("connection_url could not be parsed") from None

    drivername = url.drivername
    if drivername in _POSTGRES_DRIVERS:
        url = url.set(drivername="postgresql+psycopg")
    elif drivername in _MSSQL_DRIVERS:
        url = url.set(drivername="mssql+pyodbc")
    elif drivername == "sqlite":
        if not settings.allow_sqlite:
            raise SqlSyncError(
                f"dialect {drivername!r} is not permitted (allow_sqlite=False; sqlite "
                "is a test/fixture-only escape hatch)"
            )
    else:
        raise SqlSyncError(
            f"dialect {drivername!r} is not permitted (allowed: postgresql[+psycopg], "
            "mssql[+pyodbc], and sqlite only under allow_sqlite=True)"
        )

    if url.drivername != "sqlite":
        host = url.host
        if not host:
            raise SqlSyncError("connection_url is missing a host")
        pseudo_endpoint = f"http://[{host}]" if ":" in host else f"http://{host}"
        try:
            validate_in_vpc_endpoint(pseudo_endpoint, allowed_hosts=settings.allowed_hosts)
        except UnsafeEndpointError as exc:
            raise SqlSyncError(f"host is not a permitted in-VPC endpoint: {exc}") from None

    return url


def create_validated_engine(settings: SqlConnectorSettings) -> sa.Engine:
    """Build the ONE validated SQLAlchemy engine for a sync run, fail-closed."""
    return sa.create_engine(validate_connection_url(settings), pool_pre_ping=True)
