# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""DltResourceConnector over a REAL dlt.resource factory (zero network)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from typing import Any

import pytest

from ne.connect import DltResourceConnector, InvalidRecordError, ResourceSpec
from ne.connect._dlt import dlt

_ROWS = [
    {"id": "c1", "name": "Ada", "updated_at": "2026-01-01T00:00:00Z"},
    {"id": "c2", "name": "Grace", "updated_at": "2026-01-02T00:00:00Z"},
]


@dlt.resource
def _contacts(cursor: str | None) -> Iterable[Mapping[str, Any]]:
    for row in _ROWS:
        if cursor is None or row["updated_at"] > cursor:
            yield row


def _factory(received: list[str | None]):
    def factory(cursor: str | None) -> Iterable[Mapping[str, Any]]:
        received.append(cursor)
        return _contacts(cursor)

    return factory


def _connector(received: list[str | None], **kwargs: Any) -> DltResourceConnector:
    spec = ResourceSpec(name="contacts", factory=_factory(received), record_id_key="id")
    return DltResourceConnector("testsrc", [spec], **kwargs)


def test_discover_lists_resource_ids() -> None:
    connector = _connector([])

    async def scenario() -> None:
        objects = await connector.discover()
        assert [o.source_id for o in objects] == ["testsrc/contacts"]
        assert objects[0].kind == "resource"

    asyncio.run(scenario())


def test_backfill_yields_records_with_payloads() -> None:
    received: list[str | None] = []
    connector = _connector(received)

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        assert [r.record_id for r in records] == ["c1", "c2"]
        assert records[0].payload["name"] == "Ada"
        assert records[0].source_id == "testsrc/contacts"

    asyncio.run(scenario())
    assert received == [None]


def test_incremental_passes_stored_cursor_into_factory() -> None:
    received: list[str | None] = []
    connector = _connector(received)

    async def scenario() -> None:
        records = [
            r async for r in connector.incremental({"testsrc/contacts": "2026-01-01T00:00:00Z"})
        ]
        assert [r.record_id for r in records] == ["c2"]

    asyncio.run(scenario())
    assert received == ["2026-01-01T00:00:00Z"]


def test_missing_record_id_key_raises() -> None:
    spec = ResourceSpec(
        name="broken",
        factory=lambda _cursor: [{"name": "no-id-here"}],
        record_id_key="id",
    )
    connector = DltResourceConnector("testsrc", [spec])

    async def scenario() -> None:
        with pytest.raises(InvalidRecordError):
            _ = [r async for r in connector.backfill()]

    asyncio.run(scenario())


def test_permissions_empty_without_acl_factory() -> None:
    connector = _connector([])

    async def scenario() -> None:
        assert [e async for e in connector.permissions()] == []

    asyncio.run(scenario())


def test_permissions_yields_entries_with_acl_factory() -> None:
    def acl_factory() -> Iterable[Mapping[str, Any]]:
        return [{"resource_id": "testsrc/contacts", "principals": ["oidc:sales"]}]

    spec = ResourceSpec(name="contacts", factory=_factory([]), record_id_key="id")
    connector = DltResourceConnector("testsrc", [spec], acl_factory=acl_factory)

    async def scenario() -> None:
        entries = [e async for e in connector.permissions()]
        assert len(entries) == 1
        assert entries[0].resource_id == "testsrc/contacts"
        assert entries[0].principals == ("oidc:sales",)

    asyncio.run(scenario())
