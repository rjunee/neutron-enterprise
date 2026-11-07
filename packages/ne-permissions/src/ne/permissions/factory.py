# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Config-driven construction of the permissions stack (mirror ne-audit/ne-identity).

``build_permissions_stack`` is the convenience wiring for a hosting process: it
creates the engine, ensures the schema, and returns the store + expander + sync
service ready to run. ``build_principal_source`` dispatches on ``settings.mode``.
"""

from __future__ import annotations

import httpx
import sqlalchemy as sa

from ne.contracts.governance import AuditSink
from ne.permissions.config import PermissionsSettings
from ne.permissions.entra import EntraPrincipalSource
from ne.permissions.expand import PrincipalExpander
from ne.permissions.oidc import OidcClaimsPrincipalSource
from ne.permissions.sources import PrincipalSource
from ne.permissions.store import PrincipalStore, ensure_permissions_schema
from ne.permissions.sync import PrincipalSyncService
from ne.permissions.testing.fixtures import FixturePrincipalSource

__all__ = [
    "build_engine",
    "build_permissions_stack",
    "build_principal_source",
]


def build_engine(settings: PermissionsSettings) -> sa.Engine:
    """Create the SQLAlchemy engine for the configured database URL."""
    return sa.create_engine(settings.database_url)


def build_principal_source(
    settings: PermissionsSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PrincipalSource:
    """Construct the ``PrincipalSource`` selected by ``settings.mode``."""
    if settings.mode == "entra":
        return EntraPrincipalSource(settings, transport=transport)
    if settings.mode == "oidc-claims":
        return OidcClaimsPrincipalSource(settings)
    # mode == "fixture" (validator guarantees fixture_path is set).
    assert settings.fixture_path is not None
    return FixturePrincipalSource.from_path(settings.fixture_path)


def build_permissions_stack(
    settings: PermissionsSettings, sink: AuditSink
) -> tuple[PrincipalStore, PrincipalExpander, PrincipalSyncService]:
    """Return ``(store, expander, sync_service)`` over a schema-ensured engine."""
    engine = build_engine(settings)
    ensure_permissions_schema(engine)
    store = PrincipalStore(engine)
    expander = PrincipalExpander(store, max_depth=settings.max_expansion_depth)
    service = PrincipalSyncService(store, sink)
    return store, expander, service
