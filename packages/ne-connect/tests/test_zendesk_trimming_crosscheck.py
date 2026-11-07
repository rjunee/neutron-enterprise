# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The D2 data-source cross-check (NE-093): synced Zendesk ACLs actually trim retrieval.

After a real Zendesk fixture sync into a real KnowledgeWriter/Qdrant, a REAL
ne-permissions PrincipalStore/PrincipalExpander + PrincipalAclFilter is wired
into the same store's search path — proving the connector-stamped ACLs compose
with C7 trimming: an org member retrieves the org ticket, a staff-group member
retrieves the internal comment, an end user does NOT, a store-known tenant sees a
null-segment article via TENANT_ALL, an unknown principal retrieves nothing, and
the quarantined ticket/comment/article are retrievable by NOBODY.
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
from ne.connect.zendesk import run_zendesk_sync
from ne.contracts.types import Principal
from ne.events import ensure_outbox_schema
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore, ensure_permissions_schema

from .zendesk_transport import build_zendesk_transport, zendesk_settings

_SOURCE = "zendesk"
NOW = datetime(2026, 7, 6, tzinfo=UTC)


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


ITEM_T1001 = _item_id("ticket/1001", "1001")
ITEM_C9001 = _item_id("ticket/1001", "comment-9001")  # public comment
ITEM_C9002 = _item_id("ticket/1001/internal", "comment-9002")  # staff-only comment
ITEM_A2001 = _item_id("article/2001", "2001")  # TENANT_ALL
ITEM_Q_TICKET = _item_id("ticket/1003", "1003")  # principal-less quarantine
ITEM_Q_COMMENT = _item_id("ticket/1002/internal", "comment-9101")  # empty-staff quarantine
ITEM_Q_ARTICLE = _item_id("article/2003", "2003")  # draft quarantine

P_ORG = Principal(id="U_ORG")  # zd-org:8001
P_STAFF = Principal(id="U_STAFF")  # zd-group:7001
P_ENDUSER = Principal(id="U_ENDUSER")  # zd-user:5001
P_TENANT = Principal(id="U_TENANT")  # store-known, no groups
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
            for p in ("U_ORG", "U_STAFF", "U_ENDUSER", "U_TENANT")
        ],
        source="test",
        now=NOW,
    )
    pstore.set_member_groups("U_ORG", ["zd-org:8001"], source="test", now=NOW)
    pstore.set_member_groups("U_STAFF", ["zd-group:7001"], source="test", now=NOW)
    pstore.set_member_groups("U_ENDUSER", ["zd-user:5001"], source="test", now=NOW)
    # U_TENANT is store-known but a member of nothing (resolves TENANT_ALL only).
    expander = PrincipalExpander(pstore)

    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    store = QdrantChunkStore(
        client, f"zd_{uuid.uuid4().hex}", embedder, HashSparseEncoder(), group_expander=expander
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
        await run_zendesk_sync(
            zendesk_settings(),
            writer=writer,
            state_store=ConnectorStateStore(),
            audit_sink=sink,
            actor="connector:test",
            transport=build_zendesk_transport(),
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


def test_org_member_retrieves_org_ticket(env: _Env) -> None:
    async def scenario() -> None:
        assert await _retrieve_ids(env, P_ORG, [ITEM_T1001]) == {ITEM_T1001}
        # ...but not the staff-only internal comment.
        assert await _retrieve_ids(env, P_ORG, [ITEM_C9002]) == set()

    asyncio.run(scenario())


def test_staff_group_member_retrieves_internal_comment(env: _Env) -> None:
    async def scenario() -> None:
        assert await _retrieve_ids(env, P_STAFF, [ITEM_T1001, ITEM_C9002]) == {
            ITEM_T1001,
            ITEM_C9002,
        }

    asyncio.run(scenario())


def test_end_user_does_not_see_internal_comment(env: _Env) -> None:
    async def scenario() -> None:
        # The end user sees the ticket + its PUBLIC comment...
        assert await _retrieve_ids(env, P_ENDUSER, [ITEM_T1001, ITEM_C9001]) == {
            ITEM_T1001,
            ITEM_C9001,
        }
        # ...but NOT the internal one.
        assert await _retrieve_ids(env, P_ENDUSER, [ITEM_C9002]) == set()

    asyncio.run(scenario())


def test_tenant_all_article_resolves_for_store_known_but_not_unknown(env: _Env) -> None:
    async def scenario() -> None:
        assert await _retrieve_ids(env, P_TENANT, [ITEM_A2001]) == {ITEM_A2001}
        assert await _retrieve_ids(env, P_GHOST, [ITEM_A2001]) == set()

    asyncio.run(scenario())


def test_unknown_principal_retrieves_nothing(env: _Env) -> None:
    async def scenario() -> None:
        assert await _retrieve_ids(env, P_GHOST, [ITEM_T1001, ITEM_C9001, ITEM_A2001]) == set()

    asyncio.run(scenario())


def test_quarantined_records_are_retrievable_by_nobody(env: _Env) -> None:
    async def scenario() -> None:
        quarantined = [ITEM_Q_TICKET, ITEM_Q_COMMENT, ITEM_Q_ARTICLE]
        assert await env.store.get(quarantined) == []
        for principal in (P_ORG, P_STAFF, P_ENDUSER, P_TENANT, P_GHOST):
            assert await _retrieve_ids(env, principal, quarantined) == set()

    asyncio.run(scenario())
