# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ConnectorConformanceKit conformance proof for TeamsConnector (NE-080, NE-000).

Subclasses the FROZEN ``ConnectorConformanceKit`` unmodified. The recorded-fixture
transport proves ``test_zero_network_replay`` (the whole crawl — channel
enumeration, per-channel messages delta, replies — opens zero sockets).
"""

from __future__ import annotations

from collections.abc import Mapping

from ne.connect.contracts import OdcsDataContract
from ne.connect.teams.connector import TeamsConnector
from ne.connect.teams.contract import teams_data_contract
from ne.connect.testing.kits.connector import ConnectorConformanceKit
from ne.contracts.integration import Connector

from .teams_transport import DELTA_LINK_PRIV, DELTA_LINK_STD, build_teams_transport, teams_settings


class TestTeamsConformance(ConnectorConformanceKit):
    def create_connector(self) -> Connector:
        return TeamsConnector(teams_settings(), transport=build_teams_transport())

    def create_contract(self) -> OdcsDataContract:
        return teams_data_contract(teams_settings())

    def expected_min_records(self) -> int:
        return 5

    def incremental_state(self) -> Mapping[str, str]:
        # The stored per-channel deltaLinks replay the subset fixture (message m2
        # only for C0STD; empty for C0PRIV) — a strict subset of the backfill.
        return {
            "cursor:delta:T0001/C0STD": DELTA_LINK_STD,
            "cursor:delta:T0001/C0PRIV": DELTA_LINK_PRIV,
        }
