# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reusable, sanitized test fixtures (the ``ne.connect.testing`` convention).

A recorded, deterministic ``Connector`` (no I/O, no secrets), the canonical
Salesforce-shaped source mapping, and direct node/edge builders for the k-hop and
C7 batteries. Shipped in the ``testing`` subpackage so every test module can
import them under the repo's ``--import-mode=importlib`` (sibling-test imports do
not resolve otherwise).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from ne.brain_graph.config import GraphEdgeMapping, GraphEntityMapping, GraphSourceMapping
from ne.brain_graph.model import edge_key_for, entity_key_for
from ne.connect.contracts import OdcsDataContract, PermissionMode, PermissionModelSpec, PiiClass
from ne.contracts.integration import AclEntry, Record, SourceObject

__all__ = [
    "DEFAULT_RECORDS",
    "SALESFORCE",
    "FixtureConnector",
    "expected_key",
    "make_edge",
    "make_ledger_engine",
    "make_node",
    "salesforce_contract",
    "salesforce_mapping",
]

SALESFORCE = "salesforce"


def make_ledger_engine() -> Engine:
    """A shared in-memory sqlite engine (StaticPool) for the sync ledger in tests."""
    return sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


_ACCOUNT = Record(
    record_id="A1",
    source_id="Account",
    payload={"Id": "A1", "Name": "Acme", "Status": "active", "Domain": "acme.com"},
)
_CONTACT = Record(
    record_id="C1",
    source_id="Contact",
    payload={
        "Id": "C1",
        "FirstName": "Alice",
        "LastName": "Ng",
        "Email": "alice@acme.com",
        "AccountId": "A1",
    },
)
_CASE = Record(
    record_id="K1",
    source_id="Case",
    payload={
        "Id": "K1",
        "Subject": "Login issue",
        "Status": "open",
        "OpenedAt": "2026-01-01T00:00:00Z",
        "AccountId": "A1",
        "ContactId": "C1",
    },
)
_ORDER = Record(
    record_id="O1",
    source_id="Order",
    payload={
        "Id": "O1",
        "OrderNumber": "ORD-0001",
        "Status": "placed",
        "Currency": "USD",
        "TotalAmount": "100.50",
        "PlacedAt": "2026-01-02T00:00:00Z",
        "AccountId": "A1",
        "ContactId": "C1",
    },
)

DEFAULT_RECORDS: tuple[Record, ...] = (_ACCOUNT, _CONTACT, _CASE, _ORDER)


class FixtureConnector:
    """A recorded, deterministic ``Connector`` (no I/O, no secrets)."""

    def __init__(
        self,
        records: Sequence[Record] = DEFAULT_RECORDS,
        permissions: Sequence[AclEntry] = (),
    ) -> None:
        self._records = tuple(records)
        self._permissions = tuple(permissions)
        self.backfill_deliveries = 0
        self.incremental_deliveries = 0

    async def discover(self) -> Sequence[SourceObject]:
        kinds = sorted({r.source_id for r in self._records})
        return [SourceObject(source_id=k, kind=k, name=k) for k in kinds]

    async def backfill(self, state: Mapping[str, Any] | None = None) -> AsyncIterator[Record]:
        for record in self._records:
            self.backfill_deliveries += 1
            yield record

    async def incremental(self, state: Mapping[str, Any]) -> AsyncIterator[Record]:
        for record in self._records:
            self.incremental_deliveries += 1
            yield record

    async def permissions(self) -> AsyncIterator[AclEntry]:
        for entry in self._permissions:
            yield entry


def salesforce_contract(default_acl: tuple[str, ...] = ("tenant",)) -> OdcsDataContract:
    return OdcsDataContract(
        source=SALESFORCE,
        owner="ops@acme.test",
        schema_def={"type": "object"},
        freshness_sla="PT1H",
        pii=PiiClass.PERSONAL,
        permissions=PermissionModelSpec(
            mode=PermissionMode.PUBLIC_WITHIN_TENANT, default_acl=default_acl
        ),
    )


def salesforce_mapping() -> GraphSourceMapping:
    return GraphSourceMapping(
        source_system=SALESFORCE,
        entities=(
            GraphEntityMapping(
                source_id_prefix="Account",
                entity_class="Account",
                fields={"id": "Id", "name": "Name", "status": "Status", "domain": "Domain"},
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
                source_id_prefix="Case",
                entity_class="Ticket",
                fields={
                    "id": "Id",
                    "subject": "Subject",
                    "status": "Status",
                    "opened_at": "OpenedAt",
                },
                edges=(
                    GraphEdgeMapping(
                        payload_key="AccountId",
                        edge_kind="BELONGS_TO",
                        target_class="Account",
                        target_source_id_template="Account",
                    ),
                    GraphEdgeMapping(
                        payload_key="ContactId",
                        edge_kind="REQUESTER",
                        target_class="Person",
                        target_source_id_template="Contact",
                    ),
                ),
            ),
            GraphEntityMapping(
                source_id_prefix="Order",
                entity_class="Order",
                fields={
                    "id": "Id",
                    "order_number": "OrderNumber",
                    "status": "Status",
                    "currency": "Currency",
                    "total_amount": "TotalAmount",
                    "placed_at": "PlacedAt",
                },
                edges=(
                    GraphEdgeMapping(
                        payload_key="AccountId",
                        edge_kind="ACCOUNT",
                        target_class="Account",
                        target_source_id_template="Account",
                    ),
                    GraphEdgeMapping(
                        payload_key="ContactId",
                        edge_kind="PLACED_BY",
                        target_class="Person",
                        target_source_id_template="Contact",
                    ),
                ),
            ),
        ),
    )


def expected_key(entity_class: str, source_id: str, record_id: str) -> str:
    return entity_key_for(SALESFORCE, entity_class, source_id, record_id)


def make_node(key: str, entity_class: str, acl: tuple[str, ...], **props: Any) -> dict[str, Any]:
    base = {
        "key": key,
        "entity_class": entity_class,
        "display_name": props.get("display_name", key),
        "source_system": "fixture",
        "source_ref": f"{key}:{key}",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "1.0.0",
        "synced_at": "2026-07-09T00:00:00+00:00",
    }
    base.update(props)
    return base


def make_edge(
    edge_kind: str, frm: str, to: str, acl: tuple[str, ...], key: str | None = None
) -> dict[str, Any]:
    ekey = key or edge_key_for(edge_kind, frm, to, f"{frm}:{to}")
    return {
        "key": ekey,
        "edge_kind": edge_kind,
        "from_key": frm,
        "to_key": to,
        "source_system": "fixture",
        "source_ref": f"{frm}:{to}",
        "acl": acl,
        "config_hash": "c",
        "synced_at": "2026-07-09T00:00:00+00:00",
    }
