# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""InMemoryOpenFga implements the §1.1 model semantics exactly (CI stand-in)."""

from __future__ import annotations

import asyncio

import pytest

from ne.permissions_rebac.errors import TupleAbsentError
from ne.permissions_rebac.testing.fakes import InMemoryOpenFga


def _list(client: InMemoryOpenFga, user: str, contextual: list[tuple[str, str, str]]) -> set[str]:
    return set(
        asyncio.run(client.list_objects(user, "viewer", "document", contextual_tuples=contextual))
    )


def test_owner_folds_into_viewer() -> None:
    client = InMemoryOpenFga(tuples={("user:jane", "owner", "document:d1")})
    assert _list(client, "user:jane", []) == {"document:d1"}


def test_group_member_resolves_only_via_contextual_tuples() -> None:
    client = InMemoryOpenFga(tuples={("group:g#member", "viewer", "document:d2")})
    # no contextual membership -> nothing.
    assert _list(client, "user:jane", []) == set()
    # with the contextual membership tuple -> visible.
    assert _list(client, "user:jane", [("user:jane", "member", "group:g")]) == {"document:d2"}


def test_stored_membership_tuple_grants_nothing() -> None:
    # A membership-like tuple STORED (not contextual) must not resolve group access.
    client = InMemoryOpenFga(
        tuples={
            ("group:g#member", "viewer", "document:d3"),
            ("user:jane", "member", "group:g"),  # stored, not contextual
        }
    )
    assert _list(client, "user:jane", []) == set()


def test_direct_viewer_grant() -> None:
    client = InMemoryOpenFga(tuples={("user:jane", "viewer", "document:d4")})
    assert _list(client, "user:jane", []) == {"document:d4"}
    assert _list(client, "user:bob", []) == set()  # no deny needed, absence = no access


def test_duplicate_write_raises() -> None:
    client = InMemoryOpenFga()
    asyncio.run(client.write_tuples([("user:jane", "viewer", "document:d1")]))
    with pytest.raises(ValueError, match="duplicate"):
        asyncio.run(client.write_tuples([("user:jane", "viewer", "document:d1")]))


def test_delete_of_absent_tuple_raises() -> None:
    client = InMemoryOpenFga()
    with pytest.raises(TupleAbsentError):
        asyncio.run(client.delete_tuples([("user:jane", "viewer", "document:none")]))


def test_delete_is_transactional_on_partial_absence() -> None:
    present = ("user:jane", "viewer", "document:d1")
    absent = ("user:bob", "viewer", "document:d1")
    client = InMemoryOpenFga(tuples={present})
    with pytest.raises(TupleAbsentError):
        asyncio.run(client.delete_tuples([present, absent]))
    assert present in client.tuples  # nothing deleted — the batch is transactional


def test_close_marks_closed() -> None:
    client = InMemoryOpenFga()
    assert client.closed is False
    asyncio.run(client.close())
    assert client.closed is True


def test_fail_with_raises_on_every_method() -> None:
    boom = RuntimeError("backend down")
    client = InMemoryOpenFga(fail_with=boom)
    with pytest.raises(RuntimeError):
        asyncio.run(client.list_objects("user:jane", "viewer", "document", contextual_tuples=[]))
    with pytest.raises(RuntimeError):
        asyncio.run(client.write_tuples([("user:jane", "viewer", "document:d1")]))
    with pytest.raises(RuntimeError):
        asyncio.run(client.delete_tuples([("user:jane", "viewer", "document:d1")]))
    with pytest.raises(RuntimeError):
        asyncio.run(client.read_tuples())
    with pytest.raises(RuntimeError):
        asyncio.run(client.read_authorization_model())
    with pytest.raises(RuntimeError):
        asyncio.run(client.create_store("neutron"))
    with pytest.raises(RuntimeError):
        asyncio.run(client.write_authorization_model({}))
