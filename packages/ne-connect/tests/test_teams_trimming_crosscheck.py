# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE fail-closed ACL proof (NE-080): synced Teams ACLs actually trim retrieval.

After a real Teams fixture sync into a real KnowledgeWriter/Qdrant, a REAL
ne-permissions PrincipalStore/PrincipalExpander + PrincipalAclFilter is wired
into the same store's search path — proving the connector-stamped ACLs compose
with C7 trimming: a standard-channel member retrieves the standard message but
NOT the private-channel message; a private-channel member retrieves both; an
outsider and an unknown principal retrieve nothing; and the quarantined
(shared / unknown-membershipType / forged-prefix) channels' messages were never
written and are retrievable by NOBODY (the planted-hidden-channel non-leak).
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
from ne.connect.teams import run_teams_sync
from ne.contracts.types import Principal
from ne.events import ensure_outbox_schema
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore, ensure_permissions_schema

from .teams_transport import build_teams_transport, teams_settings

_SOURCE = "teams"
NOW = datetime(2026, 7, 7, tzinfo=UTC)


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


ITEM_STD = _item_id("team/T0001/channel/C0STD", "m1")  # teams-team:T0001
ITEM_PRIV = _item_id("team/T0001/channel/C0PRIV", "mp1")  # teams-channel:T0001/C0PRIV
# A message that WOULD live in a quarantined channel — proving it never entered.
ITEM_HIDDEN = _item_id("team/T0001/channel/C0SHARED", "planted")

P_MEMBER = Principal(id="U_MEMBER")  # teams-team:T0001
P_PRIV = Principal(id="U_PRIV")  # teams-team:T0001 + teams-channel:T0001/C0PRIV
P_OUTSIDER = Principal(id="U_OUTSIDER")  # store-known, no groups
P_GHOST = Principal(id="U_GHOST")  # never synced


@dataclass
class _Env:
    retriever: HybridRetriever
    store: QdrantChunkStore


@pytest.fixture
def env(tmp_path: Path, frozen_config: str) -> _Env:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'principals.db'}")
    ensure_permissions_schema(engine)
    pstore = PrincipalStore(engine)
    pstore.upsert_principals(
        [
            SyncedPrincipal(principal_id=p, kind=PrincipalKind.USER)
            for p in ("U_MEMBER", "U_PRIV", "U_OUTSIDER")
        ],
        source="test",
        now=NOW,
    )
    pstore.set_member_groups("U_MEMBER", ["teams-team:T0001"], source="test", now=NOW)
    pstore.set_member_groups(
        "U_PRIV", ["teams-team:T0001", "teams-channel:T0001/C0PRIV"], source="test", now=NOW
    )
    # U_OUTSIDER is store-known but a member of nothing.
    expander = PrincipalExpander(pstore)

    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    store = QdrantChunkStore(
        client, f"teams_{uuid.uuid4().hex}", embedder, HashSparseEncoder(), group_expander=expander
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
        await run_teams_sync(
            teams_settings(),
            writer=writer,
            state_store=ConnectorStateStore(),
            audit_sink=sink,
            actor="connector:test",
            transport=build_teams_transport(),
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


async def _retrieve_ids(env: _Env, principal: Principal, ids: list[str]) -> set[str]:
    result = await env.retriever.retrieve(ids, principal=principal)
    return {sc.chunk.chunk_id for sc in result.chunks}


def test_standard_member_sees_standard_not_private(env: _Env) -> None:
    async def scenario() -> None:
        assert await _retrieve_ids(env, P_MEMBER, [ITEM_STD]) == {ITEM_STD}
        assert await _retrieve_ids(env, P_MEMBER, [ITEM_PRIV]) == set()

    asyncio.run(scenario())


def test_private_member_sees_both(env: _Env) -> None:
    async def scenario() -> None:
        assert await _retrieve_ids(env, P_PRIV, [ITEM_STD, ITEM_PRIV]) == {ITEM_STD, ITEM_PRIV}

    asyncio.run(scenario())


def test_outsider_and_ghost_see_nothing(env: _Env) -> None:
    async def scenario() -> None:
        assert await _retrieve_ids(env, P_OUTSIDER, [ITEM_STD, ITEM_PRIV]) == set()
        assert await _retrieve_ids(env, P_GHOST, [ITEM_STD, ITEM_PRIV]) == set()

    asyncio.run(scenario())


def test_quarantined_channels_leak_to_nobody(env: _Env) -> None:
    async def scenario() -> None:
        # The quarantined channels' messages were never fetched or written.
        assert await env.store.get([ITEM_HIDDEN]) == []
        for principal in (P_MEMBER, P_PRIV, P_OUTSIDER, P_GHOST):
            assert await _retrieve_ids(env, principal, [ITEM_HIDDEN]) == set()

    asyncio.run(scenario())
