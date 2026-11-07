# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FixtureConnector proves the ConnectorConformanceKit — zero-network CI mode.

This is the NE-040 deliverable proving NE-044/045 are CI-testable without live
tenants: a fixture-backed connector passing every kit test, including the
socket-disabled ``test_zero_network_replay``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ne.connect import OdcsDataContract, PermissionMode, PermissionModelSpec, PiiClass
from ne.connect.testing.fixture_connector import FixtureConnector
from ne.connect.testing.kits.connector import ConnectorConformanceKit
from ne.contracts.integration import Connector

_FIXTURE = Path(__file__).parent / "fixtures" / "fixture_source.json"

_KIT_SCHEMA = {
    "type": "object",
    "required": ["name"],
    "properties": {"name": {"type": "string", "minLength": 1}},
}


class TestFixtureConnectorConformance(ConnectorConformanceKit):
    def create_connector(self) -> Connector:
        return FixtureConnector.from_path(_FIXTURE)

    def create_contract(self) -> OdcsDataContract:
        return OdcsDataContract(
            source="acme-crm",
            owner="data-eng@acme.test",
            schema_def=_KIT_SCHEMA,
            freshness_sla="PT30M",
            pii=PiiClass.PERSONAL,
            permissions=PermissionModelSpec(mode=PermissionMode.SOURCE_ACL),
        )

    def expected_min_records(self) -> int:
        return 3

    def incremental_state(self) -> Mapping[str, str]:
        return {"acme-crm/contacts": "2026-01-01T00:00:00Z"}
