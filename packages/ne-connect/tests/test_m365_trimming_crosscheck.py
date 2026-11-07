# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The D2 data-source cross-check (NE-044): synced ACLs actually trim retrieval.

After a real M365 fixture sync into a real KnowledgeWriter/Qdrant, a REAL
ne-permissions PrincipalStore/PrincipalExpander + ne.brain.trimming.PrincipalAclFilter
is wired into the same store's search path — proving the connector-stamped ACLs
compose with C7 trimming exactly as NE-042/043 promise: a group member retrieves
the group-ACL chunk, a non-member cannot, TENANT_ALL/PUBLIC_LINK resolve for
store-known principals only, an unknown principal sees nothing, and the
quarantined items are retrievable by NOBODY (they never entered the store).

This mirrors packages/ne-brain/tests/test_c7_provable_trimming.py's composition
root (``QdrantChunkStore(group_expander=...)`` + ``HybridRetriever(acl_filters=...)``).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from qdrant_client import AsyncQdrantClient

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.retriever import HybridRetriever
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.trimming import PrincipalAclFilter
from ne.brain.write import (
    KnowledgeWriter,
    OutboxShapeViolationSink,
    ProvenanceClass,
    SqliteWriteQuarantine,
)
from ne.connect import CONNECTOR_RECORD_SHAPE, ConnectorStateStore
from ne.connect.m365 import run_m365_sync
from ne.contracts.types import Principal
from ne.events import ensure_outbox_schema
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore, ensure_permissions_schema

from .m365_transport import RecordedBodyParser, build_m365_transport, m365_settings

_SOURCE = "m365-sharepoint"
NOW = datetime(2026, 7, 6, tzinfo=UTC)


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


ITEM_USER = _item_id("drive-1:item-user", "item-user")  # acl (u-ada,)
ITEM_GROUP = _item_id("drive-1:item-group", "item-group")  # acl (g-eng,)
ITEM_ORG = _item_id("drive-1:item-org", "item-org")  # acl (TENANT_ALL,)
ITEM_EVERYONE = _item_id("drive-1:item-everyone", "item-everyone")  # acl (TENANT_ALL,)
ITEM_ANON = _item_id("drive-1:item-anon", "item-anon")  # acl (PUBLIC_LINK,)
ITEM_Q_UNRES = _item_id("drive-1:item-q-unresolvable", "item-q-unresolvable")
ITEM_Q_FORGED = _item_id("drive-1:item-q-forged", "item-q-forged")

P_ADA = Principal(id="u-ada")  # member of g-eng, store-known
P_BOB = Principal(id="u-bob")  # store-known, groupless
P_GHOST = Principal(id="u-ghost")  # never synced -> unknown/fail-closed


@dataclass
class _Env:
    retriever: HybridRetriever
    store: QdrantChunkStore


@pytest.fixture
def env(tmp_path: Path, frozen_config: str) -> _Env:
    # -- real write stack with the group expander wired into the store ---------
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'principals.db'}")
    ensure_permissions_schema(engine)
    pstore = PrincipalStore(engine)
    pstore.upsert_principals(
        [
            SyncedPrincipal(principal_id="u-ada", kind=PrincipalKind.USER),
            SyncedPrincipal(principal_id="u-bob", kind=PrincipalKind.USER),
        ],
        source="test",
        now=NOW,
    )
    pstore.set_member_groups("u-ada", ["g-eng"], source="test", now=NOW)  # u-ada ∈ g-eng
    expander = PrincipalExpander(pstore)

    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    store = QdrantChunkStore(
        client, f"m365_{uuid.uuid4().hex}", embedder, HashSparseEncoder(), group_expander=expander
    )
    quarantine = SqliteWriteQuarantine(tmp_path / f"q_{uuid.uuid4().hex}.db")
    audit_engine = sa.create_engine("sqlite://")
    ensure_outbox_schema(audit_engine)
    ensure_audit_schema(audit_engine)
    sink = PostgresAuditSink(audit_engine)
    writer = KnowledgeWriter(
        store,
        quarantine,
        {CONNECTOR_RECORD_SHAPE.name: CONNECTOR_RECORD_SHAPE},
        default_shape="ne.connect.record",
        actor="connector:test",
        default_provenance_class=ProvenanceClass.INGESTED,
        audit_sink=sink,
        violation_sink=OutboxShapeViolationSink(audit_engine),
    )

    async def sync() -> None:
        await store.ensure_collection()
        await run_m365_sync(
            m365_settings(),
            writer=writer,
            state_store=ConnectorStateStore(),
            audit_sink=sink,
            actor="connector:test",
            transport=build_m365_transport(),
            parser=RecordedBodyParser(),
        )

    asyncio.run(sync())

    retriever = HybridRetriever(
        store,
        embedder,
        HashSparseEncoder(),
        acl_filters=PrincipalAclFilter(expander),
        audit_sink=None,
    )
    return _Env(retriever=retriever, store=store)


async def _search_ids(env: _Env, principal: Principal) -> set[str]:
    result = await env.retriever.search("document", principal=principal, top_k=50)
    return {sc.chunk.chunk_id for sc in result.chunks}


async def _retrieve_ids(env: _Env, principal: Principal, ids: list[str]) -> set[str]:
    result = await env.retriever.retrieve(ids, principal=principal)
    return {sc.chunk.chunk_id for sc in result.chunks}


def test_group_member_retrieves_group_acl_chunk(env: _Env) -> None:
    async def scenario() -> None:
        assert ITEM_GROUP in await _search_ids(env, P_ADA)
        assert await _retrieve_ids(env, P_ADA, [ITEM_GROUP]) == {ITEM_GROUP}
        assert ITEM_USER in await _search_ids(env, P_ADA)  # her direct grant too

    asyncio.run(scenario())


def test_non_member_cannot_retrieve_group_chunk(env: _Env) -> None:
    async def scenario() -> None:
        assert ITEM_GROUP not in await _search_ids(env, P_BOB)
        assert await _retrieve_ids(env, P_BOB, [ITEM_GROUP]) == set()
        assert ITEM_USER not in await _search_ids(env, P_BOB)  # not u-ada either

    asyncio.run(scenario())


def test_store_known_principal_sees_tenant_all_and_public_link(env: _Env) -> None:
    async def scenario() -> None:
        searched = await _search_ids(env, P_BOB)
        assert {ITEM_ORG, ITEM_EVERYONE, ITEM_ANON} <= searched

    asyncio.run(scenario())


def test_unknown_principal_sees_neither_tenant_all_nor_public_link(env: _Env) -> None:
    async def scenario() -> None:
        searched = await _search_ids(env, P_GHOST)
        assert ITEM_ORG not in searched
        assert ITEM_EVERYONE not in searched
        assert ITEM_ANON not in searched
        assert searched == set()  # a fully unknown principal retrieves nothing

    asyncio.run(scenario())


def test_quarantined_items_are_retrievable_by_nobody(env: _Env) -> None:
    async def scenario() -> None:
        # They never entered the store, so no principal — member, tenant user, or
        # unknown — can reach them on either path.
        assert await env.store.get([ITEM_Q_UNRES, ITEM_Q_FORGED]) == []
        for principal in (P_ADA, P_BOB, P_GHOST):
            searched = await _search_ids(env, principal)
            assert ITEM_Q_UNRES not in searched
            assert ITEM_Q_FORGED not in searched
            assert await _retrieve_ids(env, principal, [ITEM_Q_UNRES, ITEM_Q_FORGED]) == set()

    asyncio.run(scenario())
