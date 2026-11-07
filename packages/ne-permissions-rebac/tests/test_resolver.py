# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""OpenFgaSharedDocumentResolver — the fail-closed rebac leg of the C7 union."""

from __future__ import annotations

import asyncio

import pytest

from ne.contracts.types import Principal
from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.errors import BackendError, RebacDisabledError, RebacUnavailableError
from ne.permissions_rebac.resolver import OpenFgaSharedDocumentResolver
from ne.permissions_rebac.testing.fakes import (
    FakeGroupExpansion,
    FakeKnownPrincipals,
    InMemoryOpenFga,
)

JANE = Principal(id="jane")


def _settings(**over: object) -> RebacSettings:
    base = {"enabled": True, "store_id": "s1", "authorization_model_id": "m1"}
    base.update(over)
    return RebacSettings(**base)  # type: ignore[arg-type]


def _resolver(
    client: InMemoryOpenFga,
    *,
    known: set[str] | None = None,
    groups: dict[str, frozenset[str]] | None = None,
    settings: RebacSettings | None = None,
) -> OpenFgaSharedDocumentResolver:
    return OpenFgaSharedDocumentResolver(
        client,
        FakeGroupExpansion(groups or {}),
        FakeKnownPrincipals(known if known is not None else {"jane"}),
        settings or _settings(),
    )


def test_no_tuples_returns_empty() -> None:
    r = _resolver(InMemoryOpenFga())
    assert asyncio.run(r.shared_document_ids(JANE)) == frozenset()


def test_unknown_principal_gets_no_leg_despite_live_tuple() -> None:
    # T-1b: a live user: viewer tuple, but is_known(jane) is False -> ∅.
    client = InMemoryOpenFga(tuples={("user:jane", "viewer", "document:d1")})
    r = _resolver(client, known=set())
    assert asyncio.run(r.shared_document_ids(JANE)) == frozenset()


def test_direct_grant_ids_have_no_document_prefix() -> None:
    client = InMemoryOpenFga(tuples={("user:jane", "viewer", "document:d1")})
    r = _resolver(client)
    assert asyncio.run(r.shared_document_ids(JANE)) == frozenset({"d1"})


def test_group_grant_only_via_contextual_tuples() -> None:
    client = InMemoryOpenFga(tuples={("group:proj-x#member", "viewer", "document:d2")})
    # expander returns proj-x -> contextual membership -> visible.
    r = _resolver(client, groups={"jane": frozenset({"proj-x"})})
    assert asyncio.run(r.shared_document_ids(JANE)) == frozenset({"d2"})
    # T-3 membership revocation: expander stops returning proj-x -> gone, NO write.
    r2 = _resolver(client, groups={})
    assert asyncio.run(r2.shared_document_ids(JANE)) == frozenset()


def test_acl_reserved_group_never_becomes_contextual_tuple() -> None:
    # I-3: even if a viewer tuple exists for group:acl:tenant-all#member, an
    # expander claim of 'acl:tenant-all' produces NO contextual tuple -> no grant.
    client = InMemoryOpenFga(tuples={("group:acl:tenant-all#member", "viewer", "document:secret")})
    r = _resolver(client, groups={"jane": frozenset({"acl:tenant-all"})})
    assert asyncio.run(r.shared_document_ids(JANE)) == frozenset()


def test_contextual_tuples_exclude_principal_itself() -> None:
    # A share to group:jane#member must NOT resolve for user jane via a
    # self-membership contextual tuple (pid is excluded at construction).
    client = InMemoryOpenFga(tuples={("group:jane#member", "viewer", "document:d3")})
    r = _resolver(client, groups={"jane": frozenset({"jane"})})
    assert asyncio.run(r.shared_document_ids(JANE)) == frozenset()


def test_truncation_over_restricts_and_counts() -> None:
    client = InMemoryOpenFga(
        tuples={
            ("user:jane", "viewer", "document:d1"),
            ("user:jane", "viewer", "document:d2"),
            ("user:jane", "viewer", "document:d3"),
        }
    )
    r = _resolver(client, settings=_settings(max_shared_documents=2))
    result = asyncio.run(r.shared_document_ids(JANE))
    assert len(result) == 2
    assert result <= {"d1", "d2", "d3"}
    assert r.truncated_count == 1


def test_backend_failure_degrade_returns_empty_and_counts() -> None:
    # Inject the PRODUCTION exception type: _client.py raises BackendError (a
    # RebacError subclass) for every outage. Degrade mode MUST translate it to ∅
    # + a counter/log — NOT let it propagate as a platform-wide retrieval outage
    # (I-8 / §3.6). A plain RuntimeError here would dodge the real code path.
    client = InMemoryOpenFga(fail_with=BackendError("openfga list_objects failed"))
    r = _resolver(client, settings=_settings(unavailable_mode="degrade"))
    assert asyncio.run(r.shared_document_ids(JANE)) == frozenset()
    assert r.degraded_count == 1


def test_backend_failure_deny_raises() -> None:
    # deny mode maps the production BackendError to the typed RebacUnavailableError.
    client = InMemoryOpenFga(fail_with=BackendError("openfga list_objects failed"))
    r = _resolver(client, settings=_settings(unavailable_mode="deny"))
    with pytest.raises(RebacUnavailableError):
        asyncio.run(r.shared_document_ids(JANE))


def test_unexpected_backend_exception_degrade_fails_closed() -> None:
    # Defense-in-depth: even a non-RebacError leaking from the client fails CLOSED
    # in degrade mode (∅ + counter), never widening access.
    client = InMemoryOpenFga(fail_with=RuntimeError("unexpected"))
    r = _resolver(client, settings=_settings(unavailable_mode="degrade"))
    assert asyncio.run(r.shared_document_ids(JANE)) == frozenset()
    assert r.degraded_count == 1


def test_unexpected_backend_exception_deny_raises() -> None:
    # Defense-in-depth: a non-RebacError leaking in deny mode still raises the
    # typed RebacUnavailableError (fail-closed by erroring).
    client = InMemoryOpenFga(fail_with=RuntimeError("unexpected"))
    r = _resolver(client, settings=_settings(unavailable_mode="deny"))
    with pytest.raises(RebacUnavailableError):
        asyncio.run(r.shared_document_ids(JANE))


def test_constructing_disabled_raises() -> None:
    with pytest.raises(RebacDisabledError):
        OpenFgaSharedDocumentResolver(
            InMemoryOpenFga(),
            FakeGroupExpansion({}),
            FakeKnownPrincipals({"jane"}),
            RebacSettings(),  # enabled=False
        )
