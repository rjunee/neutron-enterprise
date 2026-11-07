# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The C7 data-source cross-check (NE-046): synced SQL ACLs actually trim retrieval.

After a real fixture-SQLite sync into a real KnowledgeWriter/Qdrant, a REAL
``ne-permissions`` ``PrincipalStore``/``PrincipalExpander`` +
``ne.brain.trimming.PrincipalAclFilter`` is wired into the same store's search
path — proving the connector-stamped ACLs compose with C7 trimming exactly as
NE-042/043 promise: a group member retrieves the group-ACL ``docs`` chunks, a
non-member cannot; ``acl:tenant-all`` (``announcements``) resolves for
store-known principals only; an unknown principal sees nothing; and the
quarantined ``notes`` rows are retrievable by NOBODY (they never entered the
store).

Mirrors ``test_m365_trimming_crosscheck.py``'s composition root wholesale.
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
from ne.connect.sql import run_sql_sync
from ne.contracts.types import Principal
from ne.events import ensure_outbox_schema
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore, ensure_permissions_schema

from .sql_fixture import build_sql_fixture_db, sql_settings

_SOURCE = "sql-acme"
NOW = datetime(2026, 7, 6, tzinfo=UTC)


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


DOC_1 = _item_id("docs:1", "1")  # acl (g-eng,)
DOC_2 = _item_id("docs:2", "2")  # acl (g-eng,)
DOC_3 = _item_id("docs:3", "3")  # acl (g-eng,)
NOTE_1 = _item_id("notes:1", "1")  # acl (u-bob,)
NOTE_10 = _item_id("notes:10", "10")  # acl (u-ada, g-eng)
ANN_1 = _item_id("announcements:1", "1")  # acl (acl:tenant-all,)
ANN_2 = _item_id("announcements:2", "2")  # acl (acl:tenant-all,)
NOTE_2_Q = _item_id("notes:2", "2")  # quarantined: no principal
NOTE_9_Q = _item_id("notes:9", "9")  # quarantined: forged acl:tenant-all in data

P_ADA = Principal(id="u-ada")  # member of g-eng, store-known
P_BOB = Principal(id="u-bob")  # store-known, groupless
P_GHOST = Principal(id="u-ghost")  # never synced -> unknown/fail-closed


@dataclass
class _Env:
    retriever: HybridRetriever
    store: QdrantChunkStore


@pytest.fixture
def env(tmp_path: Path, frozen_config: str) -> _Env:
    db_path = tmp_path / "fixture.db"
    build_sql_fixture_db(db_path)

    # -- real write stack with the group expander wired into the store ------
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
        client, f"sql_{uuid.uuid4().hex}", embedder, HashSparseEncoder(), group_expander=expander
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
        await run_sql_sync(
            sql_settings(db_path),
            writer=writer,
            state_store=ConnectorStateStore(),
            audit_sink=sink,
            actor="connector:test",
            engine=sa.create_engine(f"sqlite:///{db_path}"),
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


def test_group_member_retrieves_group_acl_docs(env: _Env) -> None:
    async def scenario() -> None:
        searched = await _search_ids(env, P_ADA)
        assert {DOC_1, DOC_2, DOC_3} <= searched
        assert await _retrieve_ids(env, P_ADA, [DOC_1]) == {DOC_1}
        assert NOTE_10 in searched  # her direct + group grant on notes:10 too

    asyncio.run(scenario())


def test_non_member_cannot_retrieve_group_docs(env: _Env) -> None:
    async def scenario() -> None:
        searched = await _search_ids(env, P_BOB)
        assert not ({DOC_1, DOC_2, DOC_3} & searched)
        assert await _retrieve_ids(env, P_BOB, [DOC_1]) == set()
        assert NOTE_1 in searched  # bob's own direct grant still resolves

    asyncio.run(scenario())


def test_store_known_principal_sees_tenant_all_announcements(env: _Env) -> None:
    async def scenario() -> None:
        searched_ada = await _search_ids(env, P_ADA)
        searched_bob = await _search_ids(env, P_BOB)
        assert {ANN_1, ANN_2} <= searched_ada
        assert {ANN_1, ANN_2} <= searched_bob

    asyncio.run(scenario())


def test_unknown_principal_sees_nothing(env: _Env) -> None:
    async def scenario() -> None:
        searched = await _search_ids(env, P_GHOST)
        assert searched == set()

    asyncio.run(scenario())


def test_quarantined_rows_are_retrievable_by_nobody(env: _Env) -> None:
    async def scenario() -> None:
        assert await env.store.get([NOTE_2_Q, NOTE_9_Q]) == []
        for principal in (P_ADA, P_BOB, P_GHOST):
            searched = await _search_ids(env, principal)
            assert NOTE_2_Q not in searched
            assert NOTE_9_Q not in searched
            assert await _retrieve_ids(env, principal, [NOTE_2_Q, NOTE_9_Q]) == set()

    asyncio.run(scenario())
