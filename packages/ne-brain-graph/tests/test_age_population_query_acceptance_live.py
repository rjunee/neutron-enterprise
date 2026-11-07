# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED end-to-end query acceptance for the admitted Apache AGE runtime.

The parent process populates a real AGE graph through ``GraphSyncRunner``.  A
fresh child process then reads that durable population through the production
``/api/v1/graph/query`` and ``/api/v1/graph/paths`` routes.  Runtime admission
binds the database cluster to an immutable image digest and exact source
revision; missing inputs park the test, while configured inputs fail loudly.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

_REQUIRED_ENV = (
    "NE_AGE_DSN",
    "NE_AGE_ADMISSION_RECEIPT",
    "NE_AGE_EXPECTED_IMAGE_DIGEST",
    "NE_AGE_EXPECTED_SOURCE_REVISION",
)
_missing = tuple(name for name in _REQUIRED_ENV if not os.environ.get(name))
_PARKED_REASON = "LIVE-PARKED: admitted AGE query acceptance requires " + ", ".join(_missing)

from ne.api.deps import api_state, current_principal  # noqa: E402
from ne.api.platform import _default_graph_store  # noqa: E402
from ne.api.routers.graph import router as graph_router  # noqa: E402
from ne.brain_graph import _pg  # noqa: E402
from ne.brain_graph.age.store import AgeGraphStore  # noqa: E402
from ne.brain_graph.config import (  # noqa: E402
    BrainGraphSettings,
    GraphEdgeMapping,
    GraphEntityMapping,
    GraphSourceMapping,
)
from ne.brain_graph.construct.ledger import GraphSyncLedger  # noqa: E402
from ne.brain_graph.construct.mappers import MappedEntity, map_record  # noqa: E402
from ne.brain_graph.construct.runner import GraphSyncRunner  # noqa: E402
from ne.brain_graph.model import entity_key_for  # noqa: E402
from ne.brain_graph.read import EntityGraph  # noqa: E402
from ne.brain_graph.testing.fixtures import FixtureConnector  # noqa: E402
from ne.connect.contracts import (  # noqa: E402
    OdcsDataContract,
    PermissionMode,
    PermissionModelSpec,
    PiiClass,
)
from ne.connect.state import ConnectorStateStore  # noqa: E402
from ne.contracts import compute_config_hash, freeze_config_hash  # noqa: E402
from ne.contracts.integration import AclEntry, Record  # noqa: E402
from ne.contracts.types import Principal  # noqa: E402
from ne.schema import SCHEMA_VERSION  # noqa: E402

_SHA256 = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"\A[0-9a-f]{40}\Z")
_BODY_MARKER = "PRIVATE-AGE-QUERY-BODY-MUST-NOT-ENTER-RECEIPTS"
_ENDPOINT_LABEL = "group-endpoint"
_BRIDGE_LABEL = "group-bridge"


class _AcceptanceGeneration(BaseModel):
    """Exact admitted runtime and projection inputs behind this population."""

    model_config = ConfigDict(frozen=True)

    image_digest: str
    source_revision: str
    system_identifier: str
    schema_version: str
    mapping: dict[str, Any]


class _AuditSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, **_kwargs: Any) -> Any:  # pragma: no cover - protocol completeness
        raise NotImplementedError


def _mapping() -> GraphSourceMapping:
    return GraphSourceMapping(
        source_system="salesforce",
        entities=(
            GraphEntityMapping(
                source_id_prefix="Account",
                entity_class="Account",
                fields={"id": "Id", "name": "Name", "status": "Status"},
            ),
            GraphEntityMapping(
                source_id_prefix="Contact",
                entity_class="Person",
                fields={
                    "id": "Id",
                    "given_name": "FirstName",
                    "family_name": "LastName",
                    "email": "Email",
                },
                edges=(
                    GraphEdgeMapping(
                        payload_key="AccountId",
                        edge_kind="WORKS_AT",
                        target_class="Account",
                        target_source_id_template="Account",
                    ),
                ),
            ),
            GraphEntityMapping(
                source_id_prefix="Document",
                entity_class="Document",
                fields={"id": "Id", "title": "Title", "doc_type": "Type"},
                edges=(
                    GraphEdgeMapping(
                        payload_key="AuthorId",
                        edge_kind="AUTHOR",
                        target_class="Person",
                        target_source_id_template="Contact",
                    ),
                ),
            ),
        ),
    )


def _records() -> tuple[Record, ...]:
    return (
        Record(
            record_id="A1",
            source_id="Account",
            payload={"Id": "A1", "Name": "Acme", "Status": "active"},
        ),
        Record(
            record_id="C1",
            source_id="Contact",
            payload={
                "Id": "C1",
                "FirstName": "Ada",
                "LastName": "Lovelace",
                "Email": "ada@example.test",
                "AccountId": "A1",
            },
        ),
        Record(
            record_id="D1",
            source_id="Document",
            payload={
                "Id": "D1",
                "Title": _BODY_MARKER,
                "Type": "contract",
                "AuthorId": "C1",
            },
        ),
    )


def _permissions() -> tuple[AclEntry, ...]:
    return (
        AclEntry(resource_id="Account", principals=(_ENDPOINT_LABEL,)),
        AclEntry(resource_id="Contact", principals=(_BRIDGE_LABEL,)),
        AclEntry(resource_id="Document", principals=(_ENDPOINT_LABEL,)),
    )


def _contract() -> OdcsDataContract:
    return OdcsDataContract(
        source="salesforce",
        owner="acceptance@neutron.invalid",
        schema_def={"type": "object"},
        freshness_sla="PT1H",
        pii=PiiClass.PERSONAL,
        permissions=PermissionModelSpec(mode=PermissionMode.SOURCE_ACL),
    )


def _admission() -> tuple[str, str, str]:
    digest = os.environ["NE_AGE_EXPECTED_IMAGE_DIGEST"]
    revision = os.environ["NE_AGE_EXPECTED_SOURCE_REVISION"]
    assert _SHA256.fullmatch(digest), "expected image must be an immutable sha256 digest"
    assert _REVISION.fullmatch(revision), "expected source revision must be an exact git SHA"
    receipt_path = Path(os.environ["NE_AGE_ADMISSION_RECEIPT"])
    receipt_wire = receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(receipt_wire)
    assert set(receipt) == {"image_digest", "source_revision", "system_identifier"}
    assert receipt["image_digest"] == digest
    assert receipt["source_revision"] == revision
    assert isinstance(receipt["system_identifier"], str)
    assert receipt["system_identifier"].isdigit()
    assert _BODY_MARKER not in receipt_wire
    return digest, revision, receipt["system_identifier"]


async def _server_identity(dsn: str) -> tuple[str, str]:
    conn = await _pg.connect(dsn)
    try:
        extension = await _pg.execute_sql(
            conn, "SELECT extversion FROM pg_extension WHERE extname = 'age'"
        )
        identity = await _pg.execute_sql(conn, "SELECT system_identifier FROM pg_control_system()")
    finally:
        await conn.close()
    assert len(extension) == 1, "Apache AGE extension is not loaded in the admitted database"
    assert len(identity) == 1
    return str(extension[0][0]), str(identity[0][0])


def _generation(image_digest: str, source_revision: str, system_identifier: str) -> str:
    return compute_config_hash(
        _AcceptanceGeneration(
            image_digest=image_digest,
            source_revision=source_revision,
            system_identifier=system_identifier,
            schema_version=SCHEMA_VERSION,
            mapping=_mapping().model_dump(mode="json"),
        )
    )


def _expected_population(generation: str) -> tuple[dict[str, MappedEntity], set[str]]:
    labels = {
        "Account": (_ENDPOINT_LABEL,),
        "Contact": (_BRIDGE_LABEL,),
        "Document": (_ENDPOINT_LABEL,),
    }
    mapped: dict[str, MappedEntity] = {}
    edge_keys: set[str] = set()
    mapping = _mapping()
    for record in _records():
        entity_mapping = mapping.resolve(record.source_id)
        assert entity_mapping is not None
        entity = map_record(
            record,
            entity_mapping,
            source_system="salesforce",
            acl=labels[record.source_id],
            config_hash=generation,
            schema_version=SCHEMA_VERSION,
        )
        assert isinstance(entity, MappedEntity)
        mapped[entity.key] = entity
        edge_keys.update(str(edge.props["key"]) for edge in entity.edges)
    return mapped, edge_keys


def _assert_no_governance_leak(value: Any) -> None:
    if isinstance(value, dict):
        for key, member in value.items():
            assert key not in {
                "acl",
                "config_hash",
                "content_hash",
                "schema_version",
                "source_authority_stamps",
                "synced_at",
            }
            _assert_no_governance_leak(member)
    elif isinstance(value, list):
        for member in value:
            _assert_no_governance_leak(member)


def test_acceptance_fixture_has_the_exact_two_hop_edge_chain() -> None:
    generation = "a" * 64
    nodes, edges = _expected_population(generation)
    document_key = entity_key_for("salesforce", "Document", "Document", "D1")
    contact_key = entity_key_for("salesforce", "Person", "Contact", "C1")
    account_key = entity_key_for("salesforce", "Account", "Account", "A1")
    assert set(nodes) == {document_key, contact_key, account_key}
    assert len(edges) == 2
    assert {
        (edge.props["from_key"], edge.props["to_key"])
        for mapped in nodes.values()
        for edge in mapped.edges
    } == {(document_key, contact_key), (contact_key, account_key)}


async def _request_receipt(
    *,
    dsn: str,
    graph_name: str,
    generation: str,
    image_digest: str,
    source_revision: str,
    admitted_system_id: str,
) -> dict[str, Any]:
    import httpx
    from fastapi import FastAPI

    extension, actual_system_id = await _server_identity(dsn)
    assert actual_system_id == admitted_system_id
    expected_generation = _generation(image_digest, source_revision, actual_system_id)
    assert generation == expected_generation
    frozen_generation = freeze_config_hash(
        _AcceptanceGeneration(
            image_digest=image_digest,
            source_revision=source_revision,
            system_identifier=actual_system_id,
            schema_version=SCHEMA_VERSION,
            mapping=_mapping().model_dump(mode="json"),
        )
    )
    assert frozen_generation == generation

    audit = _AuditSink()
    settings = BrainGraphSettings(enabled=True, database_url=dsn, graph_name=graph_name, max_hops=2)
    store = _default_graph_store(settings)
    assert isinstance(store, AgeGraphStore), "Graph-on must use the persistent AGE adapter"
    graph = EntityGraph(
        store,
        group_expander=None,
        settings=settings,
        audit_sink=audit,
    )
    principal: dict[str, Principal] = {"value": Principal(id="unset")}
    inner = FastAPI()
    inner.include_router(graph_router)
    inner.dependency_overrides[api_state] = lambda: SimpleNamespace(entity_graph=graph)
    inner.dependency_overrides[current_principal] = lambda: principal["value"]
    app = FastAPI()
    app.mount("/api", inner)

    graph_off_inner = FastAPI()
    graph_off_inner.include_router(graph_router)
    graph_off_inner.dependency_overrides[api_state] = lambda: SimpleNamespace(entity_graph=None)
    graph_off_inner.dependency_overrides[current_principal] = lambda: Principal(id="graph-off")
    graph_off_app = FastAPI()
    graph_off_app.mount("/api", graph_off_inner)

    expected_nodes, expected_edges = _expected_population(generation)
    document_key = entity_key_for("salesforce", "Document", "Document", "D1")
    account_key = entity_key_for("salesforce", "Account", "Account", "A1")

    async def post(path: str, body: dict[str, Any], as_principal: Principal) -> dict[str, Any]:
        principal["value"] = as_principal
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.invalid") as client:
            response = await client.post(path, json=body)
        assert response.status_code == 200, response.text
        return response.json()

    try:
        await store.ensure_graph()
        allowed = await post(
            "/api/v1/graph/query",
            {
                "anchor_keys": [document_key],
                "hops": 2,
                "limit_nodes": 3,
                "limit_edges": 2,
            },
            Principal(id="live-allowed", groups=(_ENDPOINT_LABEL, _BRIDGE_LABEL)),
        )
        assert allowed["truncated"] is False
        assert {node["key"] for node in allowed["nodes"]} == set(expected_nodes)
        assert {edge["key"] for edge in allowed["edges"]} == expected_edges
        for node in allowed["nodes"]:
            expected = expected_nodes[node["key"]]
            assert node["entity_class"] == expected.entity_class
            assert node["source_system"] == expected.source_system
            assert node["source_ref"] == expected.source_ref
            assert (
                node["properties"]["projection_revision"]
                == expected.node_props["projection_revision"]
            )
        document = next(node for node in allowed["nodes"] if node["key"] == document_key)
        assert document["properties"]["title"] == _BODY_MARKER
        _assert_no_governance_leak(allowed)

        paths = await post(
            "/api/v1/graph/paths",
            {"from_key": document_key, "to_key": account_key, "max_hops": 2, "limit": 1},
            Principal(id="live-allowed", groups=(_ENDPOINT_LABEL, _BRIDGE_LABEL)),
        )
        assert paths["truncated"] is False
        assert len(paths["paths"]) == 1
        assert [node["key"] for node in paths["paths"][0]["nodes"]] == [
            document_key,
            entity_key_for("salesforce", "Person", "Contact", "C1"),
            account_key,
        ]
        assert {edge["key"] for edge in paths["paths"][0]["edges"]} == expected_edges
        _assert_no_governance_leak(paths)

        partial = await post(
            "/api/v1/graph/paths",
            {"from_key": document_key, "to_key": account_key, "max_hops": 2, "limit": 1},
            Principal(id="live-partial", groups=(_ENDPOINT_LABEL,)),
        )
        assert partial == {"paths": [], "truncated": False}

        denied = await post(
            "/api/v1/graph/query",
            {"anchor_keys": [document_key], "hops": 2},
            Principal(id="live-denied", groups=("group-denied",)),
        )
        assert denied == {"nodes": [], "edges": [], "truncated": False}

        off_transport = httpx.ASGITransport(app=graph_off_app)
        async with httpx.AsyncClient(
            transport=off_transport, base_url="http://ne.invalid"
        ) as client:
            graph_off = await client.post(
                "/api/v1/graph/query",
                json={"anchor_keys": [document_key], "hops": 2},
            )
        assert graph_off.status_code == 404
        assert graph_off.json() == {"detail": "graph tier not enabled"}

        assert len(audit.events) == 4
        assert [event.action for event in audit.events] == ["knowledge.graph_read"] * 4
        assert {event.config_hash for event in audit.events} == {generation}
        audit_wire = json.dumps(
            [event.model_dump(mode="json") for event in audit.events], sort_keys=True
        )
        for forbidden in (
            _BODY_MARKER,
            _ENDPOINT_LABEL,
            _BRIDGE_LABEL,
            document_key,
            account_key,
            dsn,
            image_digest,
            source_revision,
        ):
            assert forbidden not in audit_wire

        return {
            "audit_receipts": len(audit.events),
            "edge_count": len(expected_edges),
            "extension_version": extension,
            "generation_hash": generation,
            "graph_off_status": graph_off.status_code,
            "image_digest": image_digest,
            "node_count": len(expected_nodes),
            "source_revision": source_revision,
            "system_identifier": actual_system_id,
        }
    finally:
        await store.close()


def _run_child() -> None:
    receipt = asyncio.run(
        _request_receipt(
            dsn=os.environ["NE_AGE_DSN"],
            graph_name=os.environ["NE_AGE_QUERY_GRAPH_NAME"],
            generation=os.environ["NE_AGE_QUERY_GENERATION"],
            image_digest=os.environ["NE_AGE_EXPECTED_IMAGE_DIGEST"],
            source_revision=os.environ["NE_AGE_EXPECTED_SOURCE_REVISION"],
            admitted_system_id=os.environ["NE_AGE_QUERY_SYSTEM_IDENTIFIER"],
        )
    )
    print(json.dumps(receipt, sort_keys=True))


@pytest.mark.skipif(bool(_missing), reason=_PARKED_REASON)
def test_admitted_population_is_durable_and_principal_scoped_through_graph_api(
    tmp_path: Path,
) -> None:
    dsn = os.environ["NE_AGE_DSN"]
    image_digest, source_revision, admitted_system_id = _admission()
    extension, actual_system_id = asyncio.run(_server_identity(dsn))
    assert extension
    assert actual_system_id == admitted_system_id
    generation = _generation(image_digest, source_revision, actual_system_id)
    graph_name = f"ne_query_{uuid.uuid4().hex[:12]}"
    settings = BrainGraphSettings(enabled=True, database_url=dsn, graph_name=graph_name)
    store = _default_graph_store(settings)
    assert isinstance(store, AgeGraphStore), "population must use the production AGE store factory"
    state = ConnectorStateStore(tmp_path / "connector-state.sqlite")
    audit = _AuditSink()
    runner = GraphSyncRunner(
        FixtureConnector(_records(), _permissions()),
        _contract(),
        store,
        GraphSyncLedger(sa.create_engine(f"sqlite:///{tmp_path / 'graph-ledger.sqlite'}")),
        state,
        _mapping(),
        actor="age-query-acceptance",
        audit_sink=audit,
        config_hash=generation,
        run_id="admitted-query-population",
    )

    async def populate() -> None:
        try:
            await store.ensure_graph()
            report = await runner.run(mode="backfill")
            assert (report.seen, report.upserted, report.pruned) == (3, 3, 0)
            nodes = await store.scan_nodes()
            edges = await store.scan_edges()
            assert len(nodes) == 3
            assert len(edges) == 2
            assert {str(node["config_hash"]) for node in nodes} == {generation}
            assert {str(edge["config_hash"]) for edge in edges} == {generation}
        finally:
            state.close()
            await store.close()

    asyncio.run(populate())
    assert [event.action for event in audit.events] == ["graph.sync.intent", "graph.sync"]
    assert {event.config_hash for event in audit.events} == {generation}
    sync_audit_wire = json.dumps(
        [event.model_dump(mode="json") for event in audit.events], sort_keys=True
    )
    for forbidden in (
        _BODY_MARKER,
        _ENDPOINT_LABEL,
        _BRIDGE_LABEL,
        dsn,
        image_digest,
        source_revision,
    ):
        assert forbidden not in sync_audit_wire

    child_env = dict(os.environ)
    child_env.update(
        {
            "NE_AGE_QUERY_CHILD": "1",
            "NE_AGE_QUERY_GENERATION": generation,
            "NE_AGE_QUERY_GRAPH_NAME": graph_name,
            "NE_AGE_QUERY_SYSTEM_IDENTIFIER": admitted_system_id,
        }
    )
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        check=True,
        capture_output=True,
        text=True,
        env=child_env,
        timeout=60,
    )
    receipt = json.loads(child.stdout)
    assert receipt == {
        "audit_receipts": 4,
        "edge_count": 2,
        "extension_version": extension,
        "generation_hash": generation,
        "graph_off_status": 404,
        "image_digest": image_digest,
        "node_count": 3,
        "source_revision": source_revision,
        "system_identifier": admitted_system_id,
    }
    assert _BODY_MARKER not in child.stdout
    assert dsn not in child.stdout


if __name__ == "__main__" and os.environ.get("NE_AGE_QUERY_CHILD"):
    _run_child()
