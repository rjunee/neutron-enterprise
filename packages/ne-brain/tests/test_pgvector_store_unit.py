# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ALWAYS-ON: PgvectorChunkStore constructor + fail-closed guarantees (no DB).

* the constructor validates the table identifier (fail loud);
* ``query(dense=None, sparse=None)`` raises ``ValueError`` BEFORE any connection
  is opened (an unroutable DSN still raises fast — no connect attempted);
* ``permission_filter`` emits EXACTLY the ``PrincipalAclFilter`` mapping for the
  same principal/expander (no-drift), always contains ``principal.id``, falls back
  to the verified token identity without an expander, and is never empty (C7).
"""

from __future__ import annotations

import asyncio

import pytest

from ne.brain.pgvector.store import PgvectorChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.trimming import PrincipalAclFilter, acl_match_labels
from ne.contracts.types import Principal

# An intentionally unroutable DSN: if query(None, None) ever tried to connect it
# would hang/raise a connection error — instead it must raise ValueError fast.
_DEAD_DSN = "postgresql://nobody@127.0.0.1:1/none"


class _StubExpander:
    """Below-seam stub: maps u-ada -> {u-ada, g-eng}, everyone else -> {self}."""

    def expand(self, principal: Principal | str) -> frozenset[str]:
        pid = principal.id if isinstance(principal, Principal) else principal
        if pid == "u-ada":
            return frozenset({"u-ada", "g-eng"})
        return frozenset({pid})


def _store(*, group_expander: object = None) -> PgvectorChunkStore:
    return PgvectorChunkStore(
        _DEAD_DSN,
        "ne_chunks",
        HashEmbedder(32),
        HashSparseEncoder(),
        group_expander=group_expander,  # type: ignore[arg-type]
    )


def test_constructor_rejects_bad_table_identifier() -> None:
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        PgvectorChunkStore(_DEAD_DSN, "bad;drop", HashEmbedder(32), HashSparseEncoder())


def test_constructor_derives_sparse_table() -> None:
    store = _store()
    assert store._table == "ne_chunks"
    assert store._sparse_table == "ne_chunks_sparse"


def test_query_without_dense_or_sparse_raises_before_connect() -> None:
    store = _store()

    async def scenario() -> None:
        with pytest.raises(ValueError, match="requires at least one of dense/sparse"):
            await store.query(dense=None, sparse=None)
        # No connection was ever opened (the ValueError short-circuits _acquire).
        assert store._conn is None

    asyncio.run(scenario())


def test_permission_filter_equals_hook_mapping_no_drift() -> None:
    expander = _StubExpander()
    store = _store(group_expander=expander)
    hook = PrincipalAclFilter(expander)
    for pid in ["u-ada", "u-other", "u-ghost"]:
        principal = Principal(id=pid, groups=())
        mapping = store.permission_filter(principal)
        assert mapping == hook(principal)
        assert mapping == {"acl": list(acl_match_labels(principal, expander))}


def test_permission_filter_always_contains_principal_id_and_never_empty() -> None:
    store = _store(group_expander=_StubExpander())
    for pid, groups in [("u-ada", ()), ("u-other", ()), ("u-ghost", ("g-eng",))]:
        principal = Principal(id=pid, groups=groups)
        acl = store.permission_filter(principal)["acl"]
        assert acl  # never empty
        assert pid in acl


def test_permission_filter_without_expander_is_token_identity() -> None:
    store = _store()  # no expander
    principal = Principal(id="u-bob", groups=("g-eng", "g-sales"))
    acl = set(store.permission_filter(principal)["acl"])
    assert acl == {"u-bob", "g-eng", "g-sales"}
