# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""factory dispatch, entry-point resolution, and StorePrincipalResolver."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path

from ne.contracts.types import Principal
from ne.permissions.config import PermissionsSettings
from ne.permissions.entra import EntraPrincipalSource
from ne.permissions.expand import PrincipalExpander
from ne.permissions.factory import build_principal_source
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.oidc import OidcClaimsPrincipalSource
from ne.permissions.resolver import StorePrincipalResolver
from ne.permissions.store import PrincipalStore
from ne.permissions.testing.fixtures import FixturePrincipalSource

FIXTURE = Path(__file__).parent / "fixtures" / "principals.json"
NOW = datetime(2026, 7, 6, tzinfo=UTC)


def test_build_principal_source_dispatch() -> None:
    assert isinstance(
        build_principal_source(
            PermissionsSettings(mode="entra", entra_tenant_id="t", entra_client_id="c")
        ),
        EntraPrincipalSource,
    )
    assert isinstance(
        build_principal_source(PermissionsSettings(mode="oidc-claims")),
        OidcClaimsPrincipalSource,
    )
    src = build_principal_source(PermissionsSettings(mode="fixture", fixture_path=str(FIXTURE)))
    assert isinstance(src, FixturePrincipalSource)


def test_fixture_source_round_trips() -> None:
    src = FixturePrincipalSource.from_path(FIXTURE)
    snap = asyncio.run(src.fetch({}))
    ids = {p.principal_id for p in snap.principals}
    assert {"u-ada", "u-bob", "g-eng"} <= ids
    assert snap.is_full is True
    assert ("u-ada", "g-eng") in {(e.member_id, e.group_id) for e in snap.edges}


def test_principal_source_entry_points() -> None:
    eps = entry_points(group="ne.plugins.principal_source")
    names = {ep.name for ep in eps}
    assert {"entra", "oidc-claims", "fixture"} <= names
    assert eps["fixture"].load() is FixturePrincipalSource


def test_principal_resolver_entry_point() -> None:
    eps = entry_points(group="ne.plugins.principal_resolver")
    assert {ep.name for ep in eps} >= {"store"}
    assert eps["store"].load() is StorePrincipalResolver


def test_store_resolver_known_and_unknown(store: PrincipalStore) -> None:
    store.upsert_principals(
        [SyncedPrincipal(principal_id="u1", kind=PrincipalKind.USER)], source="t", now=NOW
    )
    store.set_member_groups("u1", ["g-eng"], source="t", now=NOW)
    resolver = StorePrincipalResolver(PrincipalExpander(store))

    known = asyncio.run(resolver.resolve("u1"))
    assert isinstance(known, Principal)
    assert known.id == "u1"
    assert "u1" not in known.groups
    assert "g-eng" in known.groups
    assert list(known.groups) == sorted(known.groups)

    unknown = asyncio.run(resolver.resolve("nobody"))
    assert unknown == Principal(id="nobody", groups=())
