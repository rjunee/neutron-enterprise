# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""M365GraphConnector passes the frozen ConnectorConformanceKit (NE-044).

The kit itself is UNMODIFIED (contract-law) — only subclassed. ``incremental_state``
supplies a differing delta basis so ``test_incremental_respects_cursor_state``
exercises real delta semantics (a strict subset), and ``test_zero_network_replay``
proves the MockTransport replay opens no sockets.
"""

from __future__ import annotations

from collections.abc import Mapping

from ne.connect.m365.connector import M365GraphConnector
from ne.connect.m365.contract import m365_data_contract
from ne.connect.testing.kits.connector import ConnectorConformanceKit
from ne.contracts.integration import Connector

from .m365_transport import (
    DELTA_LINK_1,
    DRIVE_ID,
    RecordedBodyParser,
    build_m365_transport,
    m365_settings,
)


class TestM365Conformance(ConnectorConformanceKit):
    def create_connector(self) -> Connector:
        return M365GraphConnector(
            m365_settings(), transport=build_m365_transport(), parser=RecordedBodyParser()
        )

    def create_contract(self):
        return m365_data_contract(m365_settings())

    def expected_min_records(self) -> int:
        return 5

    def incremental_state(self) -> Mapping[str, str]:
        # A differing basis -> the connector re-crawls from the stored deltaLink,
        # which the fixture answers with a strict subset of the full backfill.
        return {f"delta:{DRIVE_ID}": DELTA_LINK_1}
