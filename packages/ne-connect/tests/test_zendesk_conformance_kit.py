# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ConnectorConformanceKit conformance proof for ZendeskConnector (NE-093, NE-000).

Subclasses the FROZEN ``ConnectorConformanceKit`` unmodified. The recorded-fixture
transport proves ``test_zero_network_replay`` (the whole crawl — cursor ticket
export, comments, incremental article export — opens zero sockets).
"""

from __future__ import annotations

from collections.abc import Mapping

from ne.connect.contracts import OdcsDataContract
from ne.connect.testing.kits.connector import ConnectorConformanceKit
from ne.connect.zendesk.connector import ZendeskConnector
from ne.connect.zendesk.contract import zendesk_data_contract
from ne.contracts.integration import Connector

from .zendesk_transport import build_zendesk_transport, zendesk_settings


class TestZendeskConformance(ConnectorConformanceKit):
    def create_connector(self) -> Connector:
        return ZendeskConnector(zendesk_settings(), transport=build_zendesk_transport())

    def create_contract(self) -> OdcsDataContract:
        return zendesk_data_contract(zendesk_settings())

    def expected_min_records(self) -> int:
        return 6

    def incremental_state(self) -> Mapping[str, str]:
        # tickets cursor routes to the incremental subset (only ticket 1001);
        # articles start_time excludes all backfill articles — a strict subset.
        return {"cursor:tickets": "inccursor", "cursor:articles": "1900000000"}
