# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ZendeskConnector: crawl-once caching, fail-closed ticket/article quarantine (NE-093)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ne.connect.errors import InvalidRecordError
from ne.connect.zendesk.connector import ZendeskConnector

from .zendesk_transport import build_zendesk_transport, zendesk_settings


async def _drain(connector: ZendeskConnector) -> list:
    return [record async for record in connector.backfill()]


def test_discover_is_config_driven_with_zero_requests() -> None:
    recorder: list[httpx.Request] = []
    connector = ZendeskConnector(
        zendesk_settings(), transport=build_zendesk_transport(recorder=recorder)
    )
    objects = asyncio.run(connector.discover())
    assert {(o.source_id, o.kind) for o in objects} == {
        ("tickets", "stream"),
        ("articles", "stream"),
    }
    assert recorder == []


def test_backfill_record_shape() -> None:
    connector = ZendeskConnector(zendesk_settings(), transport=build_zendesk_transport())
    records = asyncio.run(_drain(connector))
    by_source: dict[str, set[str]] = {}
    for r in records:
        by_source.setdefault(r.source_id, set()).add(r.record_id)
    assert by_source["ticket/1001"] == {"1001", "comment-9001"}
    assert by_source["ticket/1001/internal"] == {"comment-9002"}
    assert by_source["ticket/1002"] == {"1002"}
    assert by_source["article/2001"] == {"2001"}
    assert by_source["article/2002"] == {"2002"}


def test_principal_less_ticket_quarantined_with_its_comments() -> None:
    # The transport asserts comments are NEVER fetched for ticket 1003.
    connector = ZendeskConnector(zendesk_settings(), transport=build_zendesk_transport())
    records = asyncio.run(_drain(connector))
    assert not any(r.source_id.startswith("ticket/1003") for r in records)
    assert "ticket/1003" in connector.acl_quarantined_ids


def test_deleted_ticket_is_skipped_not_quarantined() -> None:
    connector = ZendeskConnector(zendesk_settings(), transport=build_zendesk_transport())
    records = asyncio.run(_drain(connector))
    assert not any(r.source_id.startswith("ticket/1004") for r in records)
    assert "ticket/1004" not in connector.acl_quarantined_ids


def test_empty_staff_subset_private_comment_is_quarantined_per_item() -> None:
    connector = ZendeskConnector(zendesk_settings(), transport=build_zendesk_transport())
    records = asyncio.run(_drain(connector))
    # ticket 1002 has only a private comment (9101) with no staff group/agent.
    assert not any(r.record_id == "comment-9101" for r in records)
    assert "comment/9101" in connector.items_quarantined_ids


def test_draft_article_quarantined_and_null_segment_is_tenant_all() -> None:
    connector = ZendeskConnector(zendesk_settings(), transport=build_zendesk_transport())

    async def scenario() -> dict[str, tuple[str, ...]]:
        _ = await _drain(connector)
        return {e.resource_id: e.principals async for e in connector.permissions()}

    acl = asyncio.run(scenario())
    assert acl["article/2001"] == ("acl:tenant-all",)
    assert acl["article/2002"] == ("zd-segment:3001",)
    assert "article/2003" not in acl  # draft quarantined
    assert "article/2003" in connector.acl_quarantined_ids


def test_private_comment_internal_acl_is_staff_subset() -> None:
    connector = ZendeskConnector(zendesk_settings(), transport=build_zendesk_transport())

    async def scenario() -> dict[str, tuple[str, ...]]:
        _ = await _drain(connector)
        return {e.resource_id: e.principals async for e in connector.permissions()}

    acl = asyncio.run(scenario())
    assert set(acl["ticket/1001"]) == {
        "zd-user:5001",
        "zd-agent:6001",
        "zd-group:7001",
        "zd-org:8001",
    }
    assert set(acl["ticket/1001/internal"]) == {"zd-agent:6001", "zd-group:7001"}


def test_cursor_capture_for_both_streams() -> None:
    connector = ZendeskConnector(zendesk_settings(), transport=build_zendesk_transport())
    asyncio.run(_drain(connector))
    assert connector.pending_cursors == {"tickets": "curs3", "articles": "1704067200"}


def test_crawl_once_cached_second_backfill_issues_no_new_requests() -> None:
    recorder: list[httpx.Request] = []
    connector = ZendeskConnector(
        zendesk_settings(), transport=build_zendesk_transport(recorder=recorder)
    )
    first = asyncio.run(_drain(connector))
    count = len(recorder)
    second = asyncio.run(_drain(connector))
    assert len(recorder) == count
    assert len(first) == len(second) == 6


def test_incremental_with_differing_basis_recrawls() -> None:
    connector = ZendeskConnector(
        zendesk_settings(), transport=build_zendesk_transport(incremental=True)
    )

    async def scenario() -> list:
        return [
            record
            async for record in connector.incremental(
                {"cursor:tickets": "curs-resume", "cursor:articles": "1704067200"}
            )
        ]

    records = asyncio.run(scenario())
    # e2e-flavoured incremental returns the new ticket 1005; articles resume empty.
    assert {r.record_id for r in records if r.source_id.startswith("ticket/")} == {"1005"}
    assert not any(r.source_id.startswith("article/") for r in records)


def test_missing_ticket_id_raises_invalid_record_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/incremental/tickets/cursor.json":
            return httpx.Response(
                200,
                json={
                    "tickets": [{"requester_id": 1, "subject": "no id"}],
                    "after_cursor": "c",
                    "end_of_stream": True,
                },
            )
        return httpx.Response(404)

    connector = ZendeskConnector(
        zendesk_settings(sync_articles=False), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(InvalidRecordError):
        asyncio.run(_drain(connector))
