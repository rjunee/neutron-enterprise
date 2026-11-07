# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ConnectorConformanceKit — the frozen ``Connector`` Protocol conformance kit.

Subclass and override the factories to prove a connector honors the framework
contract: schema-valid records, unique record ids per source, cursor-respecting
incremental resume, well-formed ACL entries, and — the recorded-fixture CI
deliverable — a zero-network replay. This kit SHIPS with the framework so
NE-044/045 suites import it (conformance-kit-per-Protocol rule).

Async contract methods are exercised via ``asyncio.run`` (pytest-asyncio is NOT
a dependency and must not be added — the repo kit law). ``import pytest`` is
fine here: this module is only ever imported by test suites.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Mapping

import pytest

from ne.connect.contracts import OdcsDataContract, PermissionMode
from ne.contracts.integration import Connector, Record


class ConnectorConformanceKit:
    """Subclass and override the factories to prove Connector conformance."""

    def create_connector(self) -> Connector:
        """Return a fresh connector under test."""
        raise NotImplementedError

    def create_contract(self) -> OdcsDataContract:
        """Return the ODCS contract describing the connector's records."""
        raise NotImplementedError

    def expected_min_records(self) -> int:
        """Minimum number of records a full backfill must yield."""
        return 1

    def incremental_state(self) -> Mapping[str, str]:
        """A cursor state that EXCLUDES at least one record from an incremental run."""
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------
    @staticmethod
    async def _drain_backfill(connector: Connector) -> list[Record]:
        return [record async for record in connector.backfill()]

    # -- kit tests ----------------------------------------------------------
    def test_discover_returns_source_objects(self) -> None:
        connector = self.create_connector()

        async def scenario() -> None:
            objects = await connector.discover()
            assert objects
            for obj in objects:
                assert obj.source_id
                assert obj.kind
                assert obj.name

        asyncio.run(scenario())

    def test_backfill_yields_schema_valid_records(self) -> None:
        connector = self.create_connector()
        contract = self.create_contract()

        async def scenario() -> None:
            records = await self._drain_backfill(connector)
            assert len(records) >= self.expected_min_records()
            for record in records:
                assert contract.validate(record.payload) == []

        asyncio.run(scenario())

    def test_backfill_record_ids_unique_per_source(self) -> None:
        connector = self.create_connector()

        async def scenario() -> None:
            records = await self._drain_backfill(connector)
            seen: set[tuple[str, str]] = set()
            for record in records:
                key = (record.source_id, record.record_id)
                assert key not in seen, f"duplicate record id {key!r}"
                seen.add(key)

        asyncio.run(scenario())

    def test_incremental_respects_cursor_state(self) -> None:
        connector = self.create_connector()
        state = self.incremental_state()

        async def scenario() -> None:
            full = await self._drain_backfill(connector)
            incremental = [record async for record in connector.incremental(state)]
            full_ids = {(r.source_id, r.record_id) for r in full}
            incremental_ids = {(r.source_id, r.record_id) for r in incremental}
            # A strict subset — at least one record excluded by the cursor state.
            assert incremental_ids < full_ids

        asyncio.run(scenario())

    def test_permissions_yield_wellformed_acl_entries(self) -> None:
        connector = self.create_connector()
        contract = self.create_contract()

        async def scenario() -> None:
            entries = [entry async for entry in connector.permissions()]
            public = contract.permissions.mode is PermissionMode.PUBLIC_WITHIN_TENANT
            for entry in entries:
                assert isinstance(entry.resource_id, str) and entry.resource_id
                if not public:
                    assert entry.principals, "source-acl entries must carry non-empty principals"

        asyncio.run(scenario())

    def test_zero_network_replay(self) -> None:
        connector = self.create_connector()

        async def scenario() -> None:
            await connector.discover()
            _ = await self._drain_backfill(connector)
            _ = [entry async for entry in connector.permissions()]

        # Build the event loop BEFORE disabling ``socket.socket`` — the loop's
        # self-pipe is created at construction, so patching afterwards blocks
        # only NEW socket creation (i.e. any network I/O the replay would do),
        # not the loop machinery itself.
        loop = asyncio.new_event_loop()
        try:
            with pytest.MonkeyPatch.context() as mp:

                def _no_network(*args: object, **kwargs: object) -> object:
                    raise AssertionError("network I/O attempted during fixture replay")

                mp.setattr(socket, "socket", _no_network)
                loop.run_until_complete(scenario())
        finally:
            loop.close()
