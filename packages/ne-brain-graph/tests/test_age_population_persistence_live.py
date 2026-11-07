# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED M365 -> admitted AGE population and restart acceptance.

The test uses a zero-network Graph replay, the production M365 graph host, the
production retained-authority provider, and the production AGE store factory.
Its fresh child process starts only after the lane-owned PostgreSQL container is
restarted, so neither an in-memory graph nor process-local authority can pass.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

_REQUIRED_ENV = (
    "NE_AGE_DSN",
    "NE_AGE_ADMISSION_RECEIPT",
    "NE_AGE_EXPECTED_IMAGE_DIGEST",
    "NE_AGE_EXPECTED_SOURCE_REVISION",
    "NE_AGE_CONTAINER_NAME",
)
_missing = tuple(name for name in _REQUIRED_ENV if not os.environ.get(name))
_PARKED_REASON = "LIVE-PARKED: persistent AGE acceptance requires " + ", ".join(_missing)

from ne.api.platform import (  # noqa: E402
    _default_graph_store,
    _RetainedM365GraphSourceAuthorityProvider,
)
from ne.brain.current_source_authority import (  # noqa: E402
    SqlAlchemyCurrentSourceAuthorityReader,
    SqlAlchemyCurrentSourceAuthorityWriter,
    bind_deployment_construction_id,
    ensure_source_authority_schema,
)
from ne.brain.lineage import SqlAlchemyDocumentLineageStore  # noqa: E402
from ne.brain_graph import _pg  # noqa: E402
from ne.brain_graph.age.store import AgeGraphStore  # noqa: E402
from ne.brain_graph.config import (  # noqa: E402
    BrainGraphSettings,
    GraphEntityMapping,
    GraphSourceMapping,
)
from ne.brain_graph.construct.ledger import GraphSyncLedger  # noqa: E402
from ne.brain_graph.construct.runner import M365GraphSyncHost  # noqa: E402
from ne.brain_graph.model import EntityKey, entity_key_for  # noqa: E402
from ne.brain_graph.read import EntityGraph  # noqa: E402
from ne.connect.identity import (  # noqa: E402
    source_acl_hash,
    source_ref_for,
    source_snapshot_hash,
)
from ne.connect.m365.config import M365ConnectorSettings  # noqa: E402
from ne.connect.state import ConnectorStateStore  # noqa: E402
from ne.contracts.integration import Record  # noqa: E402
from ne.contracts.source_authority import (  # noqa: E402
    M365SourcePoint,
    SourceAdmissionFence,
    SourceAuthorityCandidate,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
    SourceReconciliationScope,
)
from ne.contracts.types import Chunk, Principal  # noqa: E402
from ne.schema import SCHEMA_VERSION  # noqa: E402

_SHA256 = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"\A[0-9a-f]{40}\Z")
_CONTAINER = re.compile(r"\Ane-age-population-[a-z0-9-]+\Z")
_BODY_MARKER = "PRIVATE-M365-AGE-BODY-MUST-NOT-ENTER-RECEIPTS"
_SOURCE = "m365-sharepoint"
_DRIVE = "drive-a"
_ITEM = "item-1"
_ACL = ("g-risk",)


class _AuditSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, **_kwargs: Any) -> Any:  # pragma: no cover - Protocol completeness
        raise NotImplementedError


def _item() -> dict[str, Any]:
    return {
        "id": _ITEM,
        "name": "Policy.unsupported",
        "webUrl": "https://contoso.example/Policy.unsupported",
        "lastModifiedDateTime": "2026-08-22T12:00:00Z",
        "createdDateTime": "2026-08-22T11:00:00Z",
        "size": 17,
        "eTag": "etag-1",
        "file": {"mimeType": "application/octet-stream"},
        "parentReference": {"path": f"/drives/{_DRIVE}/root:/Policies"},
    }


def _record() -> Record:
    item = _item()
    return Record(
        record_id=_ITEM,
        source_id=f"{_DRIVE}:{_ITEM}",
        payload={
            "id": _ITEM,
            "name": item["name"],
            "web_url": item["webUrl"],
            "last_modified": item["lastModifiedDateTime"],
            "graph_doc_type": "other",
            "created": item["createdDateTime"],
            "size": item["size"],
            "mime_type": "application/octet-stream",
            "path": item["parentReference"]["path"],
            "etag": item["eTag"],
            "body_ingestion_status": "unsupported",
        },
    )


def _settings() -> M365ConnectorSettings:
    return M365ConnectorSettings(
        tenant_id="tenant-live",
        client_id="client-live",
        auth_method="secret",
        client_secret="fixture-secret-not-real",  # type: ignore[arg-type]
        drive_ids=(_DRIVE,),
        owner="acceptance@neutron.invalid",
    )


def _mapping() -> GraphSourceMapping:
    return GraphSourceMapping(
        source_system=_SOURCE,
        entities=(
            GraphEntityMapping(
                source_id_prefix=_DRIVE,
                entity_class="Document",
                fields={"id": "id", "title": "name", "doc_type": "graph_doc_type"},
            ),
        ),
    )


def _transport() -> httpx.MockTransport:
    settings = _settings()
    token_url = f"{settings.login_base_url}/{settings.tenant_id}/oauth2/v2.0/token"
    delta_url = f"{settings.graph_base_url}/drives/{_DRIVE}/root/delta"
    permission_url = f"{settings.graph_base_url}/drives/{_DRIVE}/items/{_ITEM}/permissions"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == token_url:
            return httpx.Response(
                200,
                json={"access_token": "fixture-token", "token_type": "Bearer", "expires_in": 3600},
            )
        assert request.headers.get("Authorization") == "Bearer fixture-token"
        if url == delta_url:
            return httpx.Response(
                200,
                json={
                    "value": [_item()],
                    "@odata.deltaLink": f"{delta_url}?$deltatoken=complete",
                },
            )
        if url == permission_url:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "perm-1",
                            "roles": ["read"],
                            "grantedToV2": {"group": {"id": _ACL[0]}},
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"code": "fixtureMiss"}})

    return httpx.MockTransport(handler)


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


async def _seed_t1_authority(
    runtime_engine: sa.Engine,
) -> tuple[
    SqlAlchemyDocumentLineageStore,
    SqlAlchemyCurrentSourceAuthorityReader,
    str,
]:
    record = _record()
    acl_hash = source_acl_hash(_ACL)
    snapshot_hash = source_snapshot_hash(_SOURCE, record)
    lineage = SqlAlchemyDocumentLineageStore(runtime_engine)
    ensure_source_authority_schema(runtime_engine)
    construction_id = bind_deployment_construction_id(runtime_engine)
    document_id = hashlib.sha256(source_ref_for(record).encode()).hexdigest()
    prepared = lineage.prepare(
        Chunk(
            chunk_id=f"{document_id}#0",
            document_id=document_id,
            text=_BODY_MARKER,
            acl=_ACL,
            metadata={
                "connector": _SOURCE,
                "source_ref": source_ref_for(record),
                "source_snapshot_hash": snapshot_hash,
                "source_acl_hash": acl_hash,
            },
        )
    )
    lineage.finalize(
        prepared,
        audit_event_id="t1-live-write",
        occurred_at=datetime.now(UTC),
    )

    identity = SourceIdentity(
        deployment_id=construction_id,
        source_kind=_SOURCE,
        connector_instance_id="connector-live",
        point=M365SourcePoint(
            graph_tenant_id=_settings().tenant_id,
            site_id="",
            drive_id=_DRIVE,
            item_id=_ITEM,
        ),
        generation=1,
    )
    contract = SourceContractIdentity(
        source_kind=_SOURCE,
        descriptor_schema_version=1,
        descriptor_hash="c" * 64,
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=identity,
        source_snapshot_hash=snapshot_hash,
        content_snapshot_hash=hashlib.sha256(_BODY_MARKER.encode()).hexdigest(),
        acl_snapshot_hash=acl_hash,
        source_contract=contract,
        artifact_schema_version=1,
    )
    scope = SourceReconciliationScope(
        path=(_SOURCE, construction_id, "connector-live", _DRIVE, _ITEM)
    )
    writer = SqlAlchemyCurrentSourceAuthorityWriter(
        runtime_engine,
        construction_id,
        freshness_sla_seconds=Decimal(3600),
    )
    claim = await writer.claim(scope, expected_head_revision=None, lease_seconds=60)
    candidate = SourceAuthorityCandidate(
        source_identity=identity,
        artifact_identity=artifact,
        source_revision="etag-1",
        source_contract=contract,
        contract_epoch=1,
        acl_snapshot_hash=acl_hash,
        state=SourceAuthorityState.PRESENT,
        authority_as_of=datetime(2026, 8, 22, 12, tzinfo=UTC),
    )
    fence = SourceAdmissionFence(
        deployment_id=construction_id,
        connector_instance_id="connector-live",
        source_generation=1,
        external_source_revision="etag-1",
        contract_epoch=1,
        reconciliation_scope=scope,
        reconciliation_epoch=claim.reconciliation_epoch,
        fencing_token=claim.fencing_token,
    )
    receipt = await writer.finalize(claim, fence, candidate)
    await writer.release(claim)
    assert lineage.bind_source_authority(document_id, receipt=receipt)
    return (
        lineage,
        SqlAlchemyCurrentSourceAuthorityReader(runtime_engine, construction_id),
        receipt.receipt_hash,
    )


def _wait_after_restart(dsn: str, container_name: str, expected_system_id: str) -> str:
    deadline = time.monotonic() + 45
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "ne",
                    "-d",
                    "neutron",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert ready.returncode == 0
            published = subprocess.run(
                ["docker", "port", container_name, "5432/tcp"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            port = published.rsplit(":", 1)[-1]
            authority, _host = dsn.rsplit("@", 1)
            _old_host, database = _host.split("/", 1)
            restarted_dsn = f"{authority}@127.0.0.1:{port}/{database}"
            _extension, system_id = asyncio.run(_server_identity(restarted_dsn))
            assert system_id == expected_system_id
            return restarted_dsn
        except Exception as exc:  # the owned database is expected to be briefly unavailable
            last_error = exc
            time.sleep(0.5)
    raise AssertionError("owned AGE container did not recover after restart") from last_error


def _run_child() -> None:
    async def scenario() -> dict[str, Any]:
        dsn = os.environ["NE_AGE_DSN"]
        runtime_engine = sa.create_engine(f"sqlite:///{os.environ['NE_AGE_RUNTIME_DB']}")
        construction_id = bind_deployment_construction_id(runtime_engine)
        reader = SqlAlchemyCurrentSourceAuthorityReader(runtime_engine, construction_id)
        settings = BrainGraphSettings(
            enabled=True,
            database_url=dsn,
            graph_name=os.environ["NE_AGE_GRAPH_NAME"],
        )
        store = _default_graph_store(settings)
        assert isinstance(store, AgeGraphStore)
        graph = EntityGraph(
            store,
            group_expander=None,
            settings=settings,
            audit_sink=None,
            source_authority=reader,
            access_context_hash=lambda _principal: source_acl_hash(_ACL),
        )
        document_key = os.environ["NE_AGE_DOCUMENT_KEY"]
        try:
            await store.ensure_graph()
            allowed = await graph.lookup(
                [EntityKey(entity_class="Document", key=document_key)],
                principal=Principal(id="live-allowed", groups=_ACL),
            )
            denied = await graph.lookup(
                [EntityKey(entity_class="Document", key=document_key)],
                principal=Principal(id="live-denied", groups=("g-denied",)),
            )
            nodes = await store.scan_nodes()
            return {
                "allowed_keys": [node.key for node in allowed],
                "authority_hashes": sorted(
                    {
                        str(stamp["authority_receipt_hash"])
                        for node in nodes
                        for stamp in node["source_authority_stamps"]
                    }
                ),
                "denied_count": len(denied),
                "node_count": len(nodes),
                "schema_versions": sorted({str(node["schema_version"]) for node in nodes}),
            }
        finally:
            await store.close()
            runtime_engine.dispose()

    print(json.dumps(asyncio.run(scenario()), sort_keys=True))


@pytest.mark.skipif(bool(_missing), reason=_PARKED_REASON)
def test_governed_m365_population_survives_owned_age_restart(tmp_path: Path) -> None:
    dsn = os.environ["NE_AGE_DSN"]
    image_digest, source_revision, admitted_system_id = _admission()
    extension, actual_system_id = asyncio.run(_server_identity(dsn))
    assert extension == "1.7.0"
    assert actual_system_id == admitted_system_id
    container_name = os.environ["NE_AGE_CONTAINER_NAME"]
    assert _CONTAINER.fullmatch(container_name), "refusing to restart an unowned container name"

    graph_name = f"ne_m365_persist_{uuid.uuid4().hex[:10]}"
    runtime_path = tmp_path / "runtime.sqlite"
    runtime_engine = sa.create_engine(f"sqlite:///{runtime_path}")
    lineage, reader, receipt_hash = asyncio.run(_seed_t1_authority(runtime_engine))
    graph_settings = BrainGraphSettings(enabled=True, database_url=dsn, graph_name=graph_name)
    store = _default_graph_store(graph_settings)
    assert isinstance(store, AgeGraphStore)
    state = ConnectorStateStore(tmp_path / "connector-state.sqlite")
    audit = _AuditSink()
    host = M365GraphSyncHost(
        store=store,
        ledger=GraphSyncLedger(sa.create_engine(f"sqlite:///{tmp_path / 'graph-ledger.sqlite'}")),
        mappings=(_mapping(),),
        evidence_recorder=lineage,
        source_authority_provider=_RetainedM365GraphSourceAuthorityProvider(lineage, reader),
        audit_sink=audit,
        config_hash=hashlib.sha256(f"{image_digest}:{source_revision}".encode()).hexdigest(),
    )

    async def populate() -> None:
        try:
            await store.ensure_graph()
            report = await host.run(
                _settings(),
                state_store=state,
                actor="age-m365-acceptance",
                transport=_transport(),
            )
            assert (report.seen, report.upserted, report.quarantined) == (1, 1, 0)
            assert report.evidence_pending == 0
            nodes = await store.scan_nodes()
            assert len(nodes) == 1
            assert await store.scan_edges() == []
            assert nodes[0]["source_system"] == _SOURCE
            assert nodes[0]["acl"] == list(_ACL)
            stamps = nodes[0]["source_authority_stamps"]
            assert len(stamps) == 1
            assert stamps[0]["authority_receipt_hash"] == receipt_hash
        finally:
            state.close()
            await store.close()

    asyncio.run(populate())
    runtime_engine.dispose()
    assert [event.action for event in audit.events] == ["graph.sync.intent", "graph.sync"]
    audit_wire = json.dumps(
        [event.model_dump(mode="json") for event in audit.events], sort_keys=True
    )
    for forbidden in (_BODY_MARKER, dsn, image_digest, source_revision):
        assert forbidden not in audit_wire

    document_key = entity_key_for(_SOURCE, "Document", f"{_DRIVE}:{_ITEM}", _ITEM)
    subprocess.run(
        ["docker", "restart", container_name],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
    )
    dsn = _wait_after_restart(dsn, container_name, admitted_system_id)
    os.environ["NE_AGE_DSN"] = dsn

    child_env = dict(os.environ)
    child_env.update(
        {
            "NE_AGE_PERSISTENCE_CHILD": "1",
            "NE_AGE_GRAPH_NAME": graph_name,
            "NE_AGE_DOCUMENT_KEY": document_key,
            "NE_AGE_RUNTIME_DB": str(runtime_path),
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
        "allowed_keys": [document_key],
        "authority_hashes": [receipt_hash],
        "denied_count": 0,
        "node_count": 1,
        "schema_versions": [SCHEMA_VERSION],
    }
    assert _BODY_MARKER not in child.stdout
    assert dsn not in child.stdout


if __name__ == "__main__" and os.environ.get("NE_AGE_PERSISTENCE_CHILD"):
    _run_child()
