# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED exact Meridian M365 population and AGE restart acceptance.

The permission fixture serves the tenant's CLAIMS-ENCODED ``siteGroup`` logins,
so the frontline guarantee below is proved through the same
``map_permissions`` branch production takes. Because this file is LIVE-PARKED and
therefore never runs in ordinary CI, the mutation evidence for that branch lives
in its always-running twin,
``test_meridian_m365_claims_population.py`` (same spine, ``InMemoryGraphStore``
in place of AGE); keep the two permission fixtures in the same shape.
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
_PARKED_REASON = "LIVE-PARKED: structured Meridian AGE acceptance requires " + ", ".join(_missing)

from ne.api.platform import _default_graph_store  # noqa: E402
from ne.brain.lineage import GraphEvidenceBindResult  # noqa: E402
from ne.brain_graph import _pg  # noqa: E402
from ne.brain_graph.age.store import AgeGraphStore  # noqa: E402
from ne.brain_graph.config import BrainGraphSettings  # noqa: E402
from ne.brain_graph.construct.ledger import GraphSyncLedger  # noqa: E402
from ne.brain_graph.construct.meridian import meridian_m365_graph_mapping  # noqa: E402
from ne.brain_graph.construct.runner import M365GraphSyncHost  # noqa: E402
from ne.connect.identity import (  # noqa: E402
    source_acl_hash,
    source_snapshot_hash,
)
from ne.connect.m365.config import M365ConnectorSettings  # noqa: E402
from ne.connect.m365.operational_graph import (  # noqa: E402
    MERIDIAN_FRONTLINE_WITHHELD_CLASSES,
    OperationalGraphItemBinding,
)
from ne.connect.state import ConnectorStateStore  # noqa: E402
from ne.contracts.source_authority import (  # noqa: E402
    M365SourcePoint,
    SourceAuthorityStamp,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.meridian_seed.slice import OperationalGraphDocument, slice_corpus  # noqa: E402
from ne.schema import SCHEMA_VERSION  # noqa: E402

_SHA256 = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"\A[0-9a-f]{40}\Z")
_CONTAINER = re.compile(r"\Ane-age-meridian-[a-z0-9-]+\Z")
_SOURCE = "m365-sharepoint"
_DRIVE = "drive-meridian"


class _AuditSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, **_kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class _EvidenceRecorder:
    def bind_graph_evidence(self, batch: Any) -> GraphEvidenceBindResult:
        return GraphEvidenceBindResult(
            entities_bound=len(batch.entities),
            relationships_bound=len(batch.relationships),
            entities_unbound=0,
            relationships_unbound=0,
        )


def _documents() -> tuple[OperationalGraphDocument, ...]:
    return slice_corpus().operational_graph


def _item_id(document: OperationalGraphDocument) -> str:
    return f"item-{document.entity_class.lower()}-{document.record_id}"


#: The Entra object id of the persona group the seeder invites for each authored
#: persona id, kept identical to
#: ``tools/ne-cli/tests/test_mapping_generate_meridian_m365.py::GROUPS``. Object
#: ids rather than readable placeholders because the permission fixture below
#: serves the tenant's REAL claims-encoded shape, whose value segment IS the id.
_PERSONA_GROUPS = {
    "ops": "306773cc-b05a-43b4-bbb2-7858bfdb67dc",
    "approver": "ab98c77c-1310-4e50-9427-a91d6bbfc3e3",
    "frontline": "d9ec7a2f-41c3-48d7-b310-a13ce806c339",
}
_FRONTLINE_GROUP = _PERSONA_GROUPS["frontline"]


def _acl(document: OperationalGraphDocument) -> tuple[str, ...]:
    """Mirror the grants ``slice_corpus`` authored, never a restatement of them."""
    return tuple(_PERSONA_GROUPS[persona] for persona in document.acl.allowed_personas)


def _bindings() -> tuple[OperationalGraphItemBinding, ...]:
    return tuple(
        OperationalGraphItemBinding(
            record_id=document.record_id,
            entity_class=document.entity_class,
            item_id=_item_id(document),
            path=document.filename,
            acl=_acl(document),
            edges=document.edges,
        )
        for document in _documents()
    )


def _settings() -> M365ConnectorSettings:
    return M365ConnectorSettings(
        tenant_id="tenant-meridian",
        client_id="client-meridian",
        auth_method="secret",
        client_secret="fixture-secret-not-real",  # type: ignore[arg-type]
        drive_ids=(_DRIVE,),
        owner="acceptance@neutron.invalid",
    )


def _item(document: OperationalGraphDocument) -> dict[str, Any]:
    return {
        "id": _item_id(document),
        "name": document.filename,
        "webUrl": f"https://contoso.invalid/{document.filename}",
        "lastModifiedDateTime": "2026-08-23T12:00:00Z",
        "createdDateTime": "2026-08-23T11:00:00Z",
        "size": len(document.content),
        "eTag": f'"etag-{_item_id(document)}"',
        "file": {"mimeType": document.media_type},
        "parentReference": {"path": f"/drives/{_DRIVE}/root:/Meridian/Operational"},
    }


def _transport() -> httpx.MockTransport:
    settings = _settings()
    token_url = f"{settings.login_base_url}/{settings.tenant_id}/oauth2/v2.0/token"
    delta_url = f"{settings.graph_base_url}/drives/{_DRIVE}/root/delta"
    documents = {_item_id(document): document for document in _documents()}

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
                    "value": [_item(document) for document in documents.values()],
                    "@odata.deltaLink": f"{delta_url}?$deltatoken=complete",
                },
            )
        prefix = f"{settings.graph_base_url}/drives/{_DRIVE}/items/"
        if url.startswith(prefix):
            item_id, suffix = url.removeprefix(prefix).split("/", 1)
            document = documents[item_id]
            if suffix == "permissions":
                # THE LIVE SHAPE. A read-only dry run against the real tenant
                # returns these invites as a claims-encoded ``siteGroup`` login,
                # NOT as a direct ``grantedToV2.group.id`` — so serving the
                # direct branch here would exercise old code and leave the
                # frontline assertion below one fixture away from production.
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": f"perm-{index}",
                                "roles": ["read"],
                                "grantedToV2": {
                                    "siteGroup": {
                                        "id": str(index),
                                        "loginName": f"c:0t.c|tenant|{group}",
                                    }
                                },
                            }
                            for index, group in enumerate(_acl(document), start=1)
                        ]
                    },
                )
            if suffix == "content":
                return httpx.Response(200, content=document.content)
        return httpx.Response(404, json={"error": {"code": "fixtureMiss"}})

    return httpx.MockTransport(handler)


class _AuthorityProvider:
    async def stamps_for(
        self, record: Any, *, acl: tuple[str, ...]
    ) -> tuple[SourceAuthorityStamp, ...]:
        item_id = record.record_id
        identity = SourceIdentity(
            deployment_id="meridian-age-live",
            source_kind=_SOURCE,
            connector_instance_id="connector-meridian",
            point=M365SourcePoint(
                graph_tenant_id=_settings().tenant_id,
                site_id="",
                drive_id=_DRIVE,
                item_id=item_id,
            ),
            generation=1,
        )
        contract = SourceContractIdentity(
            source_kind=_SOURCE,
            descriptor_schema_version=1,
            descriptor_hash=hashlib.sha256(b"meridian-contract-epoch-2").hexdigest(),
        )
        acl_hash = source_acl_hash(acl)
        artifact = SourceContentArtifactIdentity(
            source_identity=identity,
            source_snapshot_hash=source_snapshot_hash(_SOURCE, record),
            content_snapshot_hash=hashlib.sha256(
                json.dumps(record.payload, sort_keys=True, default=str).encode()
            ).hexdigest(),
            acl_snapshot_hash=acl_hash,
            source_contract=contract,
            artifact_schema_version=1,
        )
        return (
            SourceAuthorityStamp(
                source_identity=identity,
                artifact_identity=artifact,
                selected_head_revision=f'"etag-{item_id}"',
                acl_snapshot_hash=acl_hash,
                source_contract=contract,
                contract_epoch=2,
                authority_receipt_id=f"receipt-{item_id}",
                authority_receipt_hash=hashlib.sha256(
                    f"authority:{item_id}:{acl_hash}".encode()
                ).hexdigest(),
            ),
        )


async def _server_identity(dsn: str) -> tuple[str, str]:
    conn = await _pg.connect(dsn)
    try:
        extension = await _pg.execute_sql(
            conn, "SELECT extversion FROM pg_extension WHERE extname = 'age'"
        )
        identity = await _pg.execute_sql(conn, "SELECT system_identifier FROM pg_control_system()")
    finally:
        await conn.close()
    return str(extension[0][0]), str(identity[0][0])


def _admission() -> tuple[str, str, str]:
    digest = os.environ["NE_AGE_EXPECTED_IMAGE_DIGEST"]
    revision = os.environ["NE_AGE_EXPECTED_SOURCE_REVISION"]
    assert _SHA256.fullmatch(digest)
    assert _REVISION.fullmatch(revision)
    wire = Path(os.environ["NE_AGE_ADMISSION_RECEIPT"]).read_text(encoding="utf-8")
    receipt = json.loads(wire)
    assert set(receipt) == {"image_digest", "source_revision", "system_identifier"}
    assert receipt["image_digest"] == digest
    assert receipt["source_revision"] == revision
    assert str(receipt["system_identifier"]).isdigit()
    assert "first_name" not in wire and "note_type" not in wire
    return digest, revision, str(receipt["system_identifier"])


def _wait_after_restart(dsn: str, container_name: str, system_id: str) -> str:
    deadline = time.monotonic() + 45
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            published = subprocess.run(
                ["docker", "port", container_name, "5432/tcp"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            port = published.rsplit(":", 1)[-1]
            authority, host_db = dsn.rsplit("@", 1)
            _host, database = host_db.split("/", 1)
            restarted = f"{authority}@127.0.0.1:{port}/{database}"
            _extension, observed = asyncio.run(_server_identity(restarted))
            assert observed == system_id
            return restarted
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise AssertionError("lane-owned AGE did not recover") from last_error


def _run_child() -> None:
    async def scenario() -> dict[str, Any]:
        store = _default_graph_store(
            BrainGraphSettings(
                enabled=True,
                database_url=os.environ["NE_AGE_DSN"],
                graph_name=os.environ["NE_AGE_GRAPH_NAME"],
            )
        )
        assert isinstance(store, AgeGraphStore)
        try:
            await store.ensure_graph()
            nodes = await store.scan_nodes()
            edges = await store.scan_edges()
            return {
                "classes": {
                    entity_class: sum(node["entity_class"] == entity_class for node in nodes)
                    for entity_class in (
                        "Customer",
                        "AutoLoan",
                        "PaymentSchedule",
                        "ServicingNote",
                    )
                },
                "edge_count": len(edges),
                "node_count": len(nodes),
                "schema_versions": sorted({str(node["schema_version"]) for node in nodes}),
            }
        finally:
            await store.close()

    print(json.dumps(asyncio.run(scenario()), sort_keys=True))


@pytest.mark.skipif(bool(_missing), reason=_PARKED_REASON)
def test_exact_meridian_population_survives_owned_age_restart(tmp_path: Path) -> None:
    dsn = os.environ["NE_AGE_DSN"]
    image_digest, source_revision, admitted_system_id = _admission()
    extension, actual_system_id = asyncio.run(_server_identity(dsn))
    assert extension == "1.7.0"
    assert actual_system_id == admitted_system_id
    container_name = os.environ["NE_AGE_CONTAINER_NAME"]
    assert _CONTAINER.fullmatch(container_name)

    graph_name = f"ne_meridian_{uuid.uuid4().hex[:10]}"
    store = _default_graph_store(
        BrainGraphSettings(enabled=True, database_url=dsn, graph_name=graph_name)
    )
    assert isinstance(store, AgeGraphStore)
    state = ConnectorStateStore(tmp_path / "connector-state.sqlite")
    ledger_engine = sa.create_engine(f"sqlite:///{tmp_path / 'graph-ledger.sqlite'}")
    audit = _AuditSink()
    host = M365GraphSyncHost(
        store=store,
        ledger=GraphSyncLedger(ledger_engine),
        mappings=(
            meridian_m365_graph_mapping(
                source_system=_SOURCE,
                drive_id=_DRIVE,
                bindings=_bindings(),
            ),
        ),
        evidence_recorder=_EvidenceRecorder(),
        source_authority_provider=_AuthorityProvider(),
        audit_sink=audit,
        config_hash=hashlib.sha256(f"{image_digest}:{source_revision}".encode()).hexdigest(),
    )

    async def populate() -> None:
        try:
            await store.ensure_graph()
            report = await host.run(
                _settings(),
                state_store=state,
                actor="meridian-age-acceptance",
                transport=_transport(),
            )
            assert (
                report.seen,
                report.upserted,
                report.quarantined,
                report.unmapped,
                report.pending_edges,
                report.evidence_pending,
            ) == (45, 45, 0, 0, 0, 0)
            nodes = await store.scan_nodes()
            assert len(nodes) == 45
            assert len(await store.scan_edges()) == 40
            stamps = [node["source_authority_stamps"][0] for node in nodes]
            assert len({stamp["authority_receipt_hash"] for stamp in stamps}) == 45
            assert {stamp["source_identity"]["point"]["item_id"] for stamp in stamps} == {
                binding.item_id for binding in _bindings()
            }
            withheld = [
                node
                for node in nodes
                if node["entity_class"] in MERIDIAN_FRONTLINE_WITHHELD_CLASSES
            ]
            # Every account-level class, one verdict: 30 of the 45 vertices carry
            # no frontline grant, so no retrieval path can serve one from AGE.
            # The ACL is pinned WHOLE, not merely checked for frontline's absence
            # -- a claims resolution that failed would leave it EMPTY, which any
            # bare absence check passes vacuously.
            assert len(withheld) == 30
            assert {tuple(sorted(node["acl"])) for node in withheld} == {
                tuple(sorted((_PERSONA_GROUPS["approver"], _PERSONA_GROUPS["ops"])))
            }
            assert all(_FRONTLINE_GROUP not in node["acl"] for node in withheld)
            # ...and the refusal is a DISCRIMINATION, not an inability: the other
            # 15 vertices ARE granted to frontline, over the same claims branch.
            served = [
                node
                for node in nodes
                if node["entity_class"] not in MERIDIAN_FRONTLINE_WITHHELD_CLASSES
            ]
            assert len(served) == 15
            assert all(_FRONTLINE_GROUP in node["acl"] for node in served)
        finally:
            state.close()
            await store.close()

    asyncio.run(populate())
    ledger_engine.dispose()
    assert [event.action for event in audit.events].count("graph.sync") == 1

    subprocess.run(
        ["docker", "restart", container_name],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
    )
    restarted_dsn = _wait_after_restart(dsn, container_name, admitted_system_id)
    child_env = dict(os.environ)
    child_env.update(
        {
            "NE_AGE_DSN": restarted_dsn,
            "NE_AGE_GRAPH_NAME": graph_name,
            "NE_AGE_MERIDIAN_CHILD": "1",
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
    assert json.loads(child.stdout) == {
        "classes": {
            "AutoLoan": 10,
            "Customer": 15,
            "PaymentSchedule": 10,
            "ServicingNote": 10,
        },
        "edge_count": 40,
        "node_count": 45,
        "schema_versions": [SCHEMA_VERSION],
    }
    assert "first_name" not in child.stdout and "note_type" not in child.stdout


if __name__ == "__main__" and os.environ.get("NE_AGE_MERIDIAN_CHILD"):
    _run_child()
