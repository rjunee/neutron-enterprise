# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SlackConnector passes the frozen ConnectorConformanceKit (NE-045).

The kit itself is UNMODIFIED (contract-law) — only subclassed.
``incremental_state`` supplies a differing cursor basis so
``test_incremental_respects_cursor_state`` exercises real cursor semantics (a
strict subset), and ``test_zero_network_replay`` proves the MockTransport
replay opens no sockets.
"""

from __future__ import annotations

from collections.abc import Mapping

from ne.connect.slack.connector import SlackConnector
from ne.connect.slack.contract import slack_data_contract
from ne.connect.testing.kits.connector import ConnectorConformanceKit
from ne.contracts.integration import Connector

from .slack_transport import C0000PUB, build_slack_transport, slack_settings


class TestSlackConformance(ConnectorConformanceKit):
    def create_connector(self) -> Connector:
        return SlackConnector(slack_settings(), transport=build_slack_transport())

    def create_contract(self):
        return slack_data_contract(slack_settings())

    def expected_min_records(self) -> int:
        return 7

    def incremental_state(self) -> Mapping[str, str]:
        # A differing basis -> the connector re-crawls; the fixture transport
        # answers C0000PUB's "oldest"-filtered history call with a strict
        # subset (the later page), leaving the other channels unchanged.
        return {f"ts:{C0000PUB}": "1720000010.000200"}
