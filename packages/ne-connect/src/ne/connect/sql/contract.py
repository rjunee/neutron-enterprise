# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ODCS data contract for SQL connector records (NE-046).

``sql_data_contract`` builds the :class:`~ne.connect.contracts.OdcsDataContract`
the SyncRunner validates records against and the AclMapper resolves ACLs under.

Permission model = ``SOURCE_ACL`` with an EMPTY ``default_acl``: this is the
fail-closed BACKSTOP. The connector itself quarantines any row it cannot resolve
a non-empty ACL for (so a resolvable row always arrives with a crawled
``AclEntry``), and the empty default means that if an unlabeled record ever
somehow reached the runner, the ``AclMapper`` raises ``AclResolutionError`` and
aborts the run LOUDLY rather than defaulting the record open.

SQL ACL is inherently coarse/config-driven (v1 limitation, documented in
README): unlike a Graph driveItem or a document-store record, a SQL row has no
native per-row permission payload — the operator's
:class:`~ne.connect.sql.config.SqlTableAclConfig` IS the source of truth.
"""

from __future__ import annotations

from ne.connect.contracts import OdcsDataContract, PermissionMode, PermissionModelSpec
from ne.connect.sql.config import SqlConnectorSettings

__all__ = ["sql_data_contract"]

#: Table schemas differ arbitrarily by configuration; per-row integrity (the
#: id column, required content) is the connector's job (``connector.py``), not
#: this contract's. The record payload is the raw row dict, so the schema is
#: deliberately permissive.
_SCHEMA_DEF: dict[str, object] = {"type": "object"}


def sql_data_contract(settings: SqlConnectorSettings) -> OdcsDataContract:
    """Return the ODCS contract for this connector's configured tables."""
    return OdcsDataContract(
        source=settings.source_name,
        owner=settings.owner,
        schema_def=_SCHEMA_DEF,
        freshness_sla=settings.freshness_sla,
        pii=settings.pii,
        permissions=PermissionModelSpec(
            mode=PermissionMode.SOURCE_ACL,
            default_acl=(),
            principal_namespace=settings.principal_namespace,
        ),
    )
