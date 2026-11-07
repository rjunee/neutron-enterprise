# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ConnectorConformanceKit conformance proof for JiraConnector (NE-093, NE-000).

Subclasses the FROZEN ``ConnectorConformanceKit`` unmodified. The recorded-fixture
transport proves ``test_zero_network_replay`` (the whole crawl — project,
permission scheme, JQL search, comments, boards/sprints — opens zero sockets).
"""

from __future__ import annotations

from collections.abc import Mapping

from ne.connect.contracts import OdcsDataContract
from ne.connect.jira.connector import JiraConnector
from ne.connect.jira.contract import jira_data_contract
from ne.connect.testing.kits.connector import ConnectorConformanceKit
from ne.contracts.integration import Connector

from .jira_transport import build_jira_transport, jira_settings


class TestJiraConformance(ConnectorConformanceKit):
    def create_connector(self) -> Connector:
        return JiraConnector(jira_settings(), transport=build_jira_transport())

    def create_contract(self) -> OdcsDataContract:
        return jira_data_contract(jira_settings())

    def expected_min_records(self) -> int:
        return 6

    def incremental_state(self) -> Mapping[str, str]:
        # Routes ACME to the incremental subset fixture (only ACME-3), excluding
        # ACME-1 + its comment + no new content — a strict subset of the backfill.
        return {"updated:ACME": "2026-01-02 00:00"}
