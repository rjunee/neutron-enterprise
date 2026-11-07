# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The D2 data-source cross-check (NE-045): synced Slack ACLs actually trim retrieval.

After a real Slack fixture sync into a real KnowledgeWriter/Qdrant, a REAL
ne-permissions PrincipalStore/PrincipalExpander + ne.brain.trimming.PrincipalAclFilter
is wired into the same store's search path — proving the connector-stamped ACLs
compose with C7 trimming exactly as NE-042/043 promise: a private-channel member
retrieves that channel's messages, a non-member cannot, TENANT_ALL resolves for
store-known principals only, an unknown principal sees nothing, and the
quarantined channels' messages are retrievable by NOBODY (they never entered
the store).

Mirrors ``test_m365_trimming_crosscheck.py``'s composition root.
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
from ne.connect.slack import run_slack_sync
from ne.contracts.types import Principal
from ne.events import ensure_outbox_schema
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore, ensure_permissions_schema

from .slack_transport import (
    C0000FORGED,
    C0000PRIV,
    C0000PUB,
    C0000QMEM,
    C0000QTYPE,
    G0000MPIM,
    build_slack_transport,
    slack_settings,
)

_SOURCE = "slack"
NOW = datetime(2026, 7, 6, tzinfo=UTC)


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


ITEM_PRIV_1 = _item_id(C0000PRIV, "1720000100.000100")  # acl (U00000ADA, U00000BOB)
ITEM_PRIV_2 = _item_id(C0000PRIV, "1720000110.000200")
ITEM_MPIM = _item_id(G0000MPIM, "1720000200.000100")  # acl (U00000ADA, U00000EVE)
ITEM_PUB = _item_id(C0000PUB, "1720000000.000100")  # acl (TENANT_ALL,)
ITEM_Q_TYPE = _item_id(C0000QTYPE, "n/a")
ITEM_Q_MEM = _item_id(C0000QMEM, "n/a")
ITEM_Q_FORGED = _item_id(C0000FORGED, "n/a")

P_ADA = Principal(id="U00000ADA")  # member of C0000PRIV + G0000MPIM, store-known
P_BOB = Principal(id="U00000BOB")  # member of C0000PRIV only, store-known
P_GHOST = Principal(id="U00000GHOST")  # never synced -> unknown/fail-closed


@dataclass
class _Env:
    retriever: HybridRetriever
    store: QdrantChunkStore


@pytest.fixture
def env(tmp_path: Path, frozen_config: str) -> _Env:
    # -- real write stack (no group expansion needed — Slack ACLs are flat
    #    member-id lists, not groups; the expander still resolves TENANT_ALL). --
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'principals.db'}")
    ensure_permissions_schema(engine)
    pstore = PrincipalStore(engine)
    pstore.upsert_principals(
        [
            SyncedPrincipal(principal_id="U00000ADA", kind=PrincipalKind.USER),
            SyncedPrincipal(principal_id="U00000BOB", kind=PrincipalKind.USER),
        ],
        source="test",
        now=NOW,
    )
    expander = PrincipalExpander(pstore)

    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    store = QdrantChunkStore(
        client, f"slack_{uuid.uuid4().hex}", embedder, HashSparseEncoder(), group_expander=expander
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
        await run_slack_sync(
            slack_settings(),
            writer=writer,
            state_store=ConnectorStateStore(),
            audit_sink=sink,
            actor="connector:test",
            transport=build_slack_transport(),
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
    result = await env.retriever.search("message", principal=principal, top_k=50)
    return {sc.chunk.chunk_id for sc in result.chunks}


async def _retrieve_ids(env: _Env, principal: Principal, ids: list[str]) -> set[str]:
    result = await env.retriever.retrieve(ids, principal=principal)
    return {sc.chunk.chunk_id for sc in result.chunks}


def test_member_retrieves_private_channel_message(env: _Env) -> None:
    async def scenario() -> None:
        assert ITEM_PRIV_1 in await _search_ids(env, P_BOB)
        assert await _retrieve_ids(env, P_BOB, [ITEM_PRIV_1]) == {ITEM_PRIV_1}

    asyncio.run(scenario())


def test_non_member_cannot_retrieve_private_channel_message(env: _Env) -> None:
    async def scenario() -> None:
        searched = await _search_ids(env, P_GHOST)
        assert ITEM_PRIV_1 not in searched
        assert ITEM_PRIV_2 not in searched
        assert await _retrieve_ids(env, P_GHOST, [ITEM_PRIV_1, ITEM_PRIV_2]) == set()

    asyncio.run(scenario())


def test_mpim_member_retrieves_but_non_member_of_mpim_cannot(env: _Env) -> None:
    async def scenario() -> None:
        assert ITEM_MPIM in await _search_ids(env, P_ADA)  # ADA is an mpim member
        assert ITEM_MPIM not in await _search_ids(env, P_BOB)  # BOB is not
        assert await _retrieve_ids(env, P_BOB, [ITEM_MPIM]) == set()

    asyncio.run(scenario())


def test_tenant_all_resolves_for_every_store_known_principal(env: _Env) -> None:
    async def scenario() -> None:
        assert ITEM_PUB in await _search_ids(env, P_ADA)
        assert ITEM_PUB in await _search_ids(env, P_BOB)

    asyncio.run(scenario())


def test_unknown_principal_sees_nothing_incl_tenant_all(env: _Env) -> None:
    async def scenario() -> None:
        searched = await _search_ids(env, P_GHOST)
        assert ITEM_PUB not in searched
        assert searched == set()  # a fully unknown principal retrieves nothing

    asyncio.run(scenario())


def test_quarantined_channel_messages_are_retrievable_by_nobody(env: _Env) -> None:
    async def scenario() -> None:
        # They never entered the store (no record was ever emitted for a
        # quarantined channel), so no principal can reach them either way.
        assert await env.store.get([ITEM_Q_TYPE, ITEM_Q_MEM, ITEM_Q_FORGED]) == []
        for principal in (P_ADA, P_BOB, P_GHOST):
            searched = await _search_ids(env, principal)
            assert ITEM_Q_TYPE not in searched
            assert ITEM_Q_MEM not in searched
            assert ITEM_Q_FORGED not in searched
            assert (
                await _retrieve_ids(env, principal, [ITEM_Q_TYPE, ITEM_Q_MEM, ITEM_Q_FORGED])
                == set()
            )

    asyncio.run(scenario())
