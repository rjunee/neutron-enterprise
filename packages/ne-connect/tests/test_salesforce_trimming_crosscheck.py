# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The D2 data-source cross-check (NE-047): synced Salesforce ACLs actually trim retrieval.

After a real Salesforce fixture sync into a real KnowledgeWriter/Qdrant, a REAL
ne-permissions PrincipalStore/PrincipalExpander + ne.brain.trimming.PrincipalAclFilter
is wired into the same store's search path — proving the connector-stamped
OBJECT-LEVEL ACLs compose with C7 trimming exactly as NE-042/043 promise: a
profile-group member retrieves the profile-granted object's chunks, a
permission-set-group member retrieves everything that permission set also
grants, a non-member cannot, an unknown principal retrieves nothing, and the
quarantined objects' records are retrievable by NOBODY (they never entered the
store). Also locks that no Salesforce chunk ever carries a reserved-namespace
(``acl:``) label — this connector's v1 mapper emits none.

Mirrors ``test_slack_trimming_crosscheck.py``'s composition root.
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
from ne.connect.salesforce import run_salesforce_sync
from ne.contracts.types import Principal
from ne.events import ensure_outbox_schema
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore, ensure_permissions_schema

from .salesforce_transport import (
    OBJ_ACCOUNT,
    OBJ_FORGED,
    OBJ_NOPERM,
    OBJ_OPP,
    PERMSET_ID,
    PROFILE_ID,
    build_salesforce_transport,
    salesforce_settings,
)

_SOURCE = "salesforce"
NOW = datetime(2026, 7, 6, tzinfo=UTC)

_PROFILE_GROUP = f"sf-profile:{PROFILE_ID}"
_PERMSET_GROUP = f"sf-permset:{PERMSET_ID}"


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


ITEM_ACCOUNT_1 = _item_id(OBJ_ACCOUNT, "001000000000001AAA")  # acl (profile, permset)
ITEM_OPP_1 = _item_id(OBJ_OPP, "006000000000001AAA")  # acl (permset,)
ITEM_Q_NOPERM = _item_id(OBJ_NOPERM, "n/a")
ITEM_Q_FORGED = _item_id(OBJ_FORGED, "n/a")

P_ADA = Principal(id="U_ADA")  # member of the PROFILE group only
P_BOB = Principal(id="U_BOB")  # member of the PERMSET group only
P_GHOST = Principal(id="U_GHOST")  # never synced -> unknown/fail-closed


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
            SyncedPrincipal(principal_id="U_ADA", kind=PrincipalKind.USER),
            SyncedPrincipal(principal_id="U_BOB", kind=PrincipalKind.USER),
        ],
        source="test",
        now=NOW,
    )
    pstore.set_member_groups("U_ADA", [_PROFILE_GROUP], source="test", now=NOW)
    pstore.set_member_groups("U_BOB", [_PERMSET_GROUP], source="test", now=NOW)
    expander = PrincipalExpander(pstore)

    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    store = QdrantChunkStore(
        client, f"sf_{uuid.uuid4().hex}", embedder, HashSparseEncoder(), group_expander=expander
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
        await run_salesforce_sync(
            salesforce_settings(),
            writer=writer,
            state_store=ConnectorStateStore(),
            audit_sink=sink,
            actor="connector:test",
            transport=build_salesforce_transport(),
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
    result = await env.retriever.search("record", principal=principal, top_k=50)
    return {sc.chunk.chunk_id for sc in result.chunks}


async def _retrieve_ids(env: _Env, principal: Principal, ids: list[str]) -> set[str]:
    result = await env.retriever.retrieve(ids, principal=principal)
    return {sc.chunk.chunk_id for sc in result.chunks}


def test_profile_group_member_retrieves_account_but_not_opportunity(env: _Env) -> None:
    async def scenario() -> None:
        searched = await _search_ids(env, P_ADA)
        assert ITEM_ACCOUNT_1 in searched
        assert ITEM_OPP_1 not in searched
        assert await _retrieve_ids(env, P_ADA, [ITEM_ACCOUNT_1]) == {ITEM_ACCOUNT_1}
        assert await _retrieve_ids(env, P_ADA, [ITEM_OPP_1]) == set()

    asyncio.run(scenario())


def test_permset_group_member_retrieves_both_objects(env: _Env) -> None:
    async def scenario() -> None:
        searched = await _search_ids(env, P_BOB)
        assert ITEM_ACCOUNT_1 in searched  # Account also grants the permset
        assert ITEM_OPP_1 in searched

    asyncio.run(scenario())


def test_unknown_principal_retrieves_nothing(env: _Env) -> None:
    async def scenario() -> None:
        searched = await _search_ids(env, P_GHOST)
        assert searched == set()
        assert await _retrieve_ids(env, P_GHOST, [ITEM_ACCOUNT_1, ITEM_OPP_1]) == set()

    asyncio.run(scenario())


def test_quarantined_object_records_are_retrievable_by_nobody(env: _Env) -> None:
    async def scenario() -> None:
        # They never entered the store (no record was ever emitted for a
        # quarantined SObject), so no principal can reach them either way.
        assert await env.store.get([ITEM_Q_NOPERM, ITEM_Q_FORGED]) == []
        for principal in (P_ADA, P_BOB, P_GHOST):
            searched = await _search_ids(env, principal)
            assert ITEM_Q_NOPERM not in searched
            assert ITEM_Q_FORGED not in searched
            assert await _retrieve_ids(env, principal, [ITEM_Q_NOPERM, ITEM_Q_FORGED]) == set()

    asyncio.run(scenario())


def test_no_chunk_carries_a_reserved_namespace_label(env: _Env) -> None:
    async def scenario() -> None:
        for item_id in (ITEM_ACCOUNT_1, ITEM_OPP_1):
            chunk = (await env.store.get([item_id]))[0]
            assert all(not label.startswith(("acl:", "role:")) for label in chunk.acl)

    asyncio.run(scenario())
