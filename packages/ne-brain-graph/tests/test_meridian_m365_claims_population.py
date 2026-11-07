# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Meridian M365 population over the LIVE claims-encoded permission shape.

``test_meridian_m365_age_population_live.py`` pins the same frontline guarantee
against a real AGE server, but it is LIVE-PARKED: without ``NE_AGE_DSN`` and
friends it never executes, so in ordinary CI it protects nothing. This twin runs
the SAME connector -> ``map_permissions`` -> ``AclMapper`` -> ``GraphSyncRunner``
spine against :class:`InMemoryGraphStore`, with the permission payload the live
tenant actually returns: a ``siteGroup`` whose ``loginName`` is the tenant
claim-provider encoding ``c:0t.c|tenant|<object-id>``, NOT a direct
``grantedToV2.group.id``.

That distinction is the whole point of this file. The direct branch is old code;
the claims branch is what NE-M365-CLAIMS added, and the frontline guarantee is
the highest-stakes property in the item — so the end-to-end evidence for it must
run through the code the change introduced.

MUTATION-CHECK: emptying ``ne.connect.m365.acl._CLAIMS_AAD_PREFIXES`` (i.e.
reverting to "drop every claims login") reds every test here — the connector
resolves zero principals, quarantines all 45 points, and the population is empty.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import httpx
import pytest

from ne.brain.lineage import GraphEvidenceBatch, GraphEvidenceBindResult
from ne.brain_graph.construct.ledger import GraphSyncLedger
from ne.brain_graph.construct.meridian import meridian_m365_graph_mapping
from ne.brain_graph.construct.runner import M365GraphSyncHost
from ne.brain_graph.testing.fixtures import make_ledger_engine
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.connect.identity import source_acl_hash, source_snapshot_hash
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.operational_graph import (
    MERIDIAN_FRONTLINE_WITHHELD_CLASSES,
    OperationalGraphItemBinding,
)
from ne.connect.state import ConnectorStateStore
from ne.contracts.integration import Record
from ne.contracts.source_authority import (
    M365SourcePoint,
    SourceAuthorityStamp,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.meridian_seed.slice import OperationalGraphDocument, slice_corpus

_SOURCE = "m365-sharepoint"
_DRIVE = "drive-meridian"

#: The Entra object ids the demo tenant's persona groups carry, kept identical to
#: ``tools/ne-cli/tests/test_mapping_generate_meridian_m365.py::GROUPS`` so the
#: ids here are the ones the seeder actually invites.
PERSONA_GROUP_IDS = {
    "ops": "306773cc-b05a-43b4-bbb2-7858bfdb67dc",
    "approver": "ab98c77c-1310-4e50-9427-a91d6bbfc3e3",
    "frontline": "d9ec7a2f-41c3-48d7-b310-a13ce806c339",
}
FRONTLINE_GROUP_ID = PERSONA_GROUP_IDS["frontline"]


def claims_login(group_id: str) -> str:
    """The SPO tenant claim-provider login the live dry run returned for an invite."""
    return f"c:0t.c|tenant|{group_id}"


class _AuditSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, **_kwargs: Any) -> Any:  # pragma: no cover - Protocol completeness
        raise NotImplementedError


class _EvidenceRecorder:
    def bind_graph_evidence(self, batch: GraphEvidenceBatch) -> GraphEvidenceBindResult:
        return GraphEvidenceBindResult(len(batch.entities), len(batch.relationships), 0, 0)


class _SourceAuthority:
    """The independently admitted T2 stamp; an empty tuple would quarantine."""

    async def stamps_for(
        self, record: Record, *, acl: tuple[str, ...]
    ) -> tuple[SourceAuthorityStamp, ...]:
        item_id = record.record_id
        identity = SourceIdentity(
            deployment_id="meridian-claims",
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


def _documents() -> tuple[OperationalGraphDocument, ...]:
    return slice_corpus().operational_graph


def _item_id(document: OperationalGraphDocument) -> str:
    return f"item-{document.entity_class.lower()}-{document.record_id}"


def _granted_group_ids(document: OperationalGraphDocument) -> tuple[str, ...]:
    """Mirror the grants ``slice_corpus`` authored, never a restatement of them."""
    return tuple(PERSONA_GROUP_IDS[persona] for persona in document.acl.allowed_personas)


def _bindings() -> tuple[OperationalGraphItemBinding, ...]:
    return tuple(
        OperationalGraphItemBinding(
            record_id=document.record_id,
            entity_class=document.entity_class,
            item_id=_item_id(document),
            path=document.filename,
            acl=_granted_group_ids(document),
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
                # THE LIVE SHAPE: the tenant hands back a claims-encoded siteGroup
                # for these invites, not a direct grantedToV2.group.id.
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
                                        "loginName": claims_login(group_id),
                                    }
                                },
                            }
                            for index, group_id in enumerate(_granted_group_ids(document), start=1)
                        ]
                    },
                )
            if suffix == "content":
                return httpx.Response(200, content=document.content)
        return httpx.Response(404, json={"error": {"code": "fixtureMiss"}})

    return httpx.MockTransport(handler)


def _populate() -> tuple[Any, InMemoryGraphStore]:
    store = InMemoryGraphStore()
    state = ConnectorStateStore(":memory:")
    host = M365GraphSyncHost(
        store=store,
        ledger=GraphSyncLedger(make_ledger_engine()),
        mappings=(
            meridian_m365_graph_mapping(
                source_system=_SOURCE,
                drive_id=_DRIVE,
                bindings=_bindings(),
            ),
        ),
        evidence_recorder=_EvidenceRecorder(),
        source_authority_provider=_SourceAuthority(),
        audit_sink=_AuditSink(),
        config_hash="b" * 64,
    )
    try:
        report = asyncio.run(
            host.run(
                _settings(),
                state_store=state,
                actor="meridian-claims-acceptance",
                transport=_transport(),
            )
        )
    finally:
        state.close()
    return report, store


@pytest.fixture(scope="module")
def population() -> tuple[Any, InMemoryGraphStore]:
    """One crawl for the whole file — the 45-point sync is the expensive part."""
    return _populate()


def test_claims_encoded_invites_populate_the_exact_45_node_graph(
    population: tuple[Any, InMemoryGraphStore],
) -> None:
    """The claims path resolves, so nothing quarantines and the graph is whole."""
    report, store = population
    assert (report.seen, report.upserted, report.quarantined, report.unmapped) == (45, 45, 0, 0)
    nodes = asyncio.run(store.scan_nodes())
    assert len(nodes) == 45
    assert len(asyncio.run(store.scan_edges())) == 40


def test_claims_encoded_grants_resolve_to_the_invited_object_ids(
    population: tuple[Any, InMemoryGraphStore],
) -> None:
    """Every vertex ACL is exactly the object ids the tenant's claims logins name.

    Pinning the WHOLE set (not just "frontline is absent") is what stops this from
    passing vacuously: a broken claims resolution yields an EMPTY ACL, and an
    empty ACL trivially satisfies a bare absence check.
    """
    _report, store = population
    nodes = asyncio.run(store.scan_nodes())
    expected = {
        _item_id(document): tuple(sorted(_granted_group_ids(document))) for document in _documents()
    }
    observed = {
        str(node["source_ref"]).rsplit(":", 1)[-1]: tuple(sorted(node["acl"])) for node in nodes
    }
    assert observed == expected
    assert all(acl for acl in observed.values())  # never an empty (vacuous) ACL


def test_frontline_is_refused_every_account_record_on_the_claims_path(
    population: tuple[Any, InMemoryGraphStore],
) -> None:
    """The frontline guarantee, asserted against the payload shape the tenant emits.

    30 of the 45 vertices are account-level classes. Each carries a NON-EMPTY,
    exactly-pinned ACL resolved out of a claims login, and none of them names the
    frontline group — so no retrieval path can serve a frontline principal one.

    ⭐ **The refusal is a DISCRIMINATION, not an inability.** The other 15
    vertices (``Customer``) are granted to frontline, and that grant arrives on
    the SAME claims-encoded branch — so the mapper demonstrably CAN resolve
    frontline's own object id here, and still does not put it on any of the 30.
    Without that half the absence could just as well mean "the claims path never
    resolves frontline at all", which proves nothing about the withholding.
    """
    _report, store = population
    nodes = asyncio.run(store.scan_nodes())
    withheld = [
        node for node in nodes if node["entity_class"] in MERIDIAN_FRONTLINE_WITHHELD_CLASSES
    ]
    assert len(withheld) == 30
    granted = {tuple(sorted(node["acl"])) for node in withheld}
    assert granted == {tuple(sorted((PERSONA_GROUP_IDS["approver"], PERSONA_GROUP_IDS["ops"])))}
    assert all(FRONTLINE_GROUP_ID not in node["acl"] for node in withheld)

    served = [
        node for node in nodes if node["entity_class"] not in MERIDIAN_FRONTLINE_WITHHELD_CLASSES
    ]
    assert len(served) == 15
    assert all(FRONTLINE_GROUP_ID in node["acl"] for node in served)
