# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The D2 data-source cross-check (NE-093): synced Jira ACLs actually trim retrieval.

After a real Jira fixture sync into a real KnowledgeWriter/Qdrant, a REAL
ne-permissions PrincipalStore/PrincipalExpander + PrincipalAclFilter is wired
into the same store's search path — proving the connector-stamped PROJECT-LEVEL
ACLs compose with C7 trimming: a jira-group member retrieves ACME chunks, a
jira-role member retrieves, a jira-project member retrieves, a store-known
non-member cannot, an unknown principal retrieves nothing, and the quarantined
projects'/items' records are retrievable by NOBODY (they never entered the
store). Also locks that no ACME chunk carries a reserved-namespace label (this
fixture's ACME scheme grants no applicationRole/anyone, so no TENANT_ALL leaks).
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
from ne.connect.jira import run_jira_sync
from ne.contracts.types import Principal
from ne.events import ensure_outbox_schema
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore, ensure_permissions_schema

from .jira_transport import ACME_PROJECT_ID, GROUP_ID, ROLE_ID, build_jira_transport, jira_settings

_SOURCE = "jira"
NOW = datetime(2026, 7, 6, tzinfo=UTC)

_GROUP = f"jira-group:{GROUP_ID}"
_ROLE = f"jira-role:{ACME_PROJECT_ID}:{ROLE_ID}"
_PROJECT = f"jira-project:{ACME_PROJECT_ID}"


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


ITEM_ACME_1 = _item_id("issues/ACME", "ACME-1")
ITEM_SPRINT = _item_id("sprints/ACME", "501")
ITEM_Q_SECURITY = _item_id("issues/ACME", "ACME-2")  # security-level quarantine
ITEM_Q_COMMENT = _item_id("comments/ACME", "40002")  # restricted-comment quarantine
ITEM_Q_SECRET = _item_id("project/SECRET", "10002")  # quarantined project
ITEM_Q_FORGED = _item_id("project/FORGED", "10003")  # quarantined project

P_ADA = Principal(id="U_ADA")  # jira-group member
P_BOB = Principal(id="U_BOB")  # jira-role member
P_DAN = Principal(id="U_DAN")  # jira-project member
P_EVE = Principal(id="U_EVE")  # store-known non-member
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
            for p in ("U_ADA", "U_BOB", "U_DAN", "U_EVE")
        ],
        source="test",
        now=NOW,
    )
    pstore.set_member_groups("U_ADA", [_GROUP], source="test", now=NOW)
    pstore.set_member_groups("U_BOB", [_ROLE], source="test", now=NOW)
    pstore.set_member_groups("U_DAN", [_PROJECT], source="test", now=NOW)
    # U_EVE is store-known but a member of NOTHING.
    expander = PrincipalExpander(pstore)

    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    store = QdrantChunkStore(
        client, f"jira_{uuid.uuid4().hex}", embedder, HashSparseEncoder(), group_expander=expander
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
        await run_jira_sync(
            jira_settings(),
            writer=writer,
            state_store=ConnectorStateStore(),
            audit_sink=sink,
            actor="connector:test",
            transport=build_jira_transport(),
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
    result = await env.retriever.search("issue", principal=principal, top_k=50)
    return {sc.chunk.chunk_id for sc in result.chunks}


async def _retrieve_ids(env: _Env, principal: Principal, ids: list[str]) -> set[str]:
    result = await env.retriever.retrieve(ids, principal=principal)
    return {sc.chunk.chunk_id for sc in result.chunks}


def test_group_member_retrieves_acme(env: _Env) -> None:
    async def scenario() -> None:
        assert ITEM_ACME_1 in await _search_ids(env, P_ADA)
        assert await _retrieve_ids(env, P_ADA, [ITEM_ACME_1]) == {ITEM_ACME_1}

    asyncio.run(scenario())


def test_role_member_retrieves_acme(env: _Env) -> None:
    async def scenario() -> None:
        assert await _retrieve_ids(env, P_BOB, [ITEM_ACME_1, ITEM_SPRINT]) == {
            ITEM_ACME_1,
            ITEM_SPRINT,
        }

    asyncio.run(scenario())


def test_project_member_retrieves_acme(env: _Env) -> None:
    async def scenario() -> None:
        assert await _retrieve_ids(env, P_DAN, [ITEM_ACME_1]) == {ITEM_ACME_1}

    asyncio.run(scenario())


def test_store_known_non_member_cannot_retrieve(env: _Env) -> None:
    async def scenario() -> None:
        assert ITEM_ACME_1 not in await _search_ids(env, P_EVE)
        assert await _retrieve_ids(env, P_EVE, [ITEM_ACME_1, ITEM_SPRINT]) == set()

    asyncio.run(scenario())


def test_unknown_principal_retrieves_nothing(env: _Env) -> None:
    async def scenario() -> None:
        assert await _search_ids(env, P_GHOST) == set()
        assert await _retrieve_ids(env, P_GHOST, [ITEM_ACME_1]) == set()

    asyncio.run(scenario())


def test_quarantined_records_are_retrievable_by_nobody(env: _Env) -> None:
    async def scenario() -> None:
        quarantined = [ITEM_Q_SECURITY, ITEM_Q_COMMENT, ITEM_Q_SECRET, ITEM_Q_FORGED]
        assert await env.store.get(quarantined) == []
        for principal in (P_ADA, P_BOB, P_DAN, P_EVE, P_GHOST):
            assert await _retrieve_ids(env, principal, quarantined) == set()

    asyncio.run(scenario())


def test_no_acme_chunk_carries_a_reserved_namespace_label(env: _Env) -> None:
    async def scenario() -> None:
        for item_id in (ITEM_ACME_1, ITEM_SPRINT):
            chunk = (await env.store.get([item_id]))[0]
            assert all(not label.startswith(("acl:", "role:")) for label in chunk.acl)

    asyncio.run(scenario())
