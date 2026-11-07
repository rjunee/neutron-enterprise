# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-permissions — principal store + query-time group expansion (SPEC §5 NE-042).

Principals and groups are synced from the customer IdP (Entra ID via Microsoft
Graph, or a generic OIDC-claims mode) into a SQLAlchemy store. Chunks keep their
SOURCE ACLs as-is; the querying principal's group memberships expand at QUERY time
(:class:`PrincipalExpander.expand`) so NE-043's filter matches a chunk when the
principal OR any of its groups is named in the chunk ACL. Membership changes do
NOT trigger re-index — revocation lag = the sync interval (default 900s).
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.permissions import errors, factory
from ne.permissions.config import PermissionsSettings
from ne.permissions.entra import EntraPrincipalSource
from ne.permissions.expand import PrincipalExpander
from ne.permissions.factory import (
    build_engine,
    build_permissions_stack,
    build_principal_source,
)
from ne.permissions.mapping import AclMappingSource
from ne.permissions.models import (
    PUBLIC_LINK,
    TENANT_ALL,
    MembershipEdge,
    PrincipalKind,
    PrincipalSnapshot,
    SyncedPrincipal,
    SyncReport,
)
from ne.permissions.oidc import OidcClaimsPrincipalSource
from ne.permissions.resolver import StorePrincipalResolver
from ne.permissions.sources import PrincipalSource
from ne.permissions.store import PrincipalStore, ensure_permissions_schema
from ne.permissions.sync import PrincipalSyncService

__all__ = [
    "PUBLIC_LINK",
    "TENANT_ALL",
    "AclMappingSource",
    "EntraPrincipalSource",
    "MembershipEdge",
    "OidcClaimsPrincipalSource",
    "PermissionsSettings",
    "PrincipalExpander",
    "PrincipalKind",
    "PrincipalSnapshot",
    "PrincipalSource",
    "PrincipalStore",
    "PrincipalSyncService",
    "StorePrincipalResolver",
    "SyncReport",
    "SyncedPrincipal",
    "__version__",
    "build_engine",
    "build_permissions_stack",
    "build_principal_source",
    "ensure_permissions_schema",
    "errors",
    "factory",
]
