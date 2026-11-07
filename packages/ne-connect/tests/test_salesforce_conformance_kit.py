# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ConnectorConformanceKit conformance proof for SalesforceConnector (NE-047, NE-000).

Subclasses the FROZEN ``ConnectorConformanceKit`` unmodified — this is the
contract-conformance law every NE-044/045/046 connector follows. The
recorded-fixture transport proves ``test_zero_network_replay`` (the whole
crawl — token, ObjectPermissions, Bulk job, CSV pagination — opens zero
sockets), the NE-000 CI deliverable.
"""

from __future__ import annotations

from collections.abc import Mapping

from ne.connect.contracts import OdcsDataContract
from ne.connect.salesforce.connector import SalesforceConnector
from ne.connect.salesforce.contract import salesforce_data_contract
from ne.connect.testing.kits.connector import ConnectorConformanceKit
from ne.contracts.integration import Connector

from .salesforce_transport import build_salesforce_transport, salesforce_settings


class TestSalesforceConformance(ConnectorConformanceKit):
    def create_connector(self) -> Connector:
        settings = salesforce_settings()
        transport = build_salesforce_transport()
        return SalesforceConnector(settings, transport=transport)

    def create_contract(self) -> OdcsDataContract:
        return salesforce_data_contract(salesforce_settings())

    def expected_min_records(self) -> int:
        return 5

    def incremental_state(self) -> Mapping[str, str]:
        # Between the first and second backfilled Account rows — excludes
        # 001000000000001AAA from the incremental result (a strict subset).
        return {"modstamp:Account": "2026-01-01T12:00:00.000Z"}


def test_contract_requires_exact_record_identity_and_version_fields() -> None:
    contract = salesforce_data_contract(salesforce_settings())
    valid = {
        "Id": "001000000000001AAA",
        "SystemModstamp": "2026-01-01T00:00:00.000Z",
        "Name": "Acme Corp",
        "Description": "Leading widget maker",
        "Industry": "Manufacturing",
    }
    assert contract.validate(valid) == []

    for field in ("Id", "SystemModstamp"):
        malformed = {key: value for key, value in valid.items() if key != field}
        assert contract.validate(malformed)

    malformed_id = {**valid, "Id": "not-a-salesforce-id"}
    malformed_version = {**valid, "SystemModstamp": "01/01/2026"}
    assert contract.validate(malformed_id)
    assert contract.validate(malformed_version)


def test_contract_is_closed_and_refuses_cross_object_payload_collision() -> None:
    contract = salesforce_data_contract(salesforce_settings())
    account = {
        "Id": "001000000000001AAA",
        "SystemModstamp": "2026-01-01T00:00:00.000Z",
        "Name": "Acme Corp",
        "Description": "Leading widget maker",
        "Industry": "Manufacturing",
    }
    assert contract.validate(account) == []
    assert contract.validate({**account, "StageName": "Prospecting", "Amount": "50000"})
