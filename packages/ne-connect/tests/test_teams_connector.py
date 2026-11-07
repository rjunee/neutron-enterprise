# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TeamsConnector fixture crawl (NE-080): records, ACLs, quarantine, incremental."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ne.connect.errors import InvalidRecordError
from ne.connect.teams.connector import TeamsConnector
from ne.contracts.integration import AclEntry, Record

from .teams_transport import (
    DELTA_LINK_PRIV,
    DELTA_LINK_STD,
    DELTA_LINK_STD_REBASED,
    TOKEN_URL,
    build_teams_transport,
    teams_settings,
)


async def _drain(connector: TeamsConnector) -> list[Record]:
    return [record async for record in connector.backfill()]


async def _acls(connector: TeamsConnector) -> list[AclEntry]:
    return [entry async for entry in connector.permissions()]


def test_full_backfill_records_and_acls() -> None:
    connector = TeamsConnector(teams_settings(), transport=build_teams_transport())

    async def scenario() -> None:
        records = await _drain(connector)
        keys = {(r.source_id, r.record_id) for r in records}
        assert keys == {
            ("team/T0001/channel/C0STD", "m1"),
            ("team/T0001/channel/C0STD", "reply-r1"),
            ("team/T0001/channel/C0STD", "m2"),
            ("team/T0001/channel/C0STD", "m3"),
            ("team/T0001/channel/C0PRIV", "mp1"),
        }
        # deleted (m-del) + system (m-sys) messages are skipped — and so are
        # deleted (r-del) + system (r-sys) REPLIES (the same filters apply).
        assert not any(
            r.record_id in {"m-del", "m-sys", "reply-r-del", "reply-r-sys"} for r in records
        )

        # The reply record carries reply_to_id + the same channel source.
        reply = next(r for r in records if r.record_id == "reply-r1")
        assert reply.payload["reply_to_id"] == "m1"
        assert reply.source_id == "team/T0001/channel/C0STD"

        acls = await _acls(connector)
        by_resource = {e.resource_id: e.principals for e in acls}
        assert by_resource == {
            "team/T0001/channel/C0STD": ("teams-team:T0001",),
            "team/T0001/channel/C0PRIV": ("teams-channel:T0001/C0PRIV",),
        }

    asyncio.run(scenario())


def test_quarantine_counters_and_ids() -> None:
    connector = TeamsConnector(teams_settings(), transport=build_teams_transport())

    async def scenario() -> None:
        await _drain(connector)
        assert connector.acl_quarantined == 3
        assert set(connector.acl_quarantined_ids) == {
            "team/T0001/channel/C0SHARED",
            "team/T0001/channel/C0WEIRD",
            "team/T0001/channel/role:forged",
        }

    asyncio.run(scenario())


def test_pending_cursors_carry_both_delta_links() -> None:
    connector = TeamsConnector(teams_settings(), transport=build_teams_transport())

    async def scenario() -> None:
        await _drain(connector)
        assert connector.pending_cursors == {
            "delta:T0001/C0STD": DELTA_LINK_STD,
            "delta:T0001/C0PRIV": DELTA_LINK_PRIV,
        }

    asyncio.run(scenario())


def test_replies_only_fetched_for_channels_with_reply_count() -> None:
    recorder: list[httpx.Request] = []
    connector = TeamsConnector(teams_settings(), transport=build_teams_transport(recorder=recorder))

    async def scenario() -> None:
        await _drain(connector)

    asyncio.run(scenario())
    replies_calls = [r for r in recorder if str(r.url).endswith("/replies")]
    assert len(replies_calls) == 1  # only m1 (replyCount=1) triggered a replies fetch
    assert "/messages/m1/replies" in str(replies_calls[0].url)


def test_replies_disabled_by_config() -> None:
    recorder: list[httpx.Request] = []
    connector = TeamsConnector(
        teams_settings(include_replies=False),
        transport=build_teams_transport(recorder=recorder),
    )

    async def scenario() -> list[Record]:
        return await _drain(connector)

    records = asyncio.run(scenario())
    assert not any(r.record_id.startswith("reply-") for r in records)
    assert not any(str(r.url).endswith("/replies") for r in recorder)


def test_missing_message_id_raises_loud() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "fixture-token", "expires_in": 3599})
        assert request.headers.get("Authorization") == "Bearer fixture-token"
        if url.endswith("/teams/T0001/channels"):
            return httpx.Response(
                200, json={"value": [{"id": "C0STD", "membershipType": "standard"}]}
            )
        if "/messages/delta" in url:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "messageType": "message",
                            "body": {"contentType": "text", "content": "no id here"},
                        }
                    ]
                },
            )
        return httpx.Response(404)  # pragma: no cover

    connector = TeamsConnector(teams_settings(), transport=httpx.MockTransport(handler))

    async def scenario() -> None:
        with pytest.raises(InvalidRecordError):
            await _drain(connector)

    asyncio.run(scenario())


def test_expired_delta_cursor_rebaselines_with_full_crawl() -> None:
    """A Graph 410 on the persisted deltaLink re-baselines the channel (m365 parity)."""
    connector = TeamsConnector(
        teams_settings(),
        transport=build_teams_transport(expire_std_delta=True),
        cursor_state={"delta:T0001/C0STD": DELTA_LINK_STD},
    )

    async def scenario() -> None:
        records = await _drain(connector)
        keys = {(r.source_id, r.record_id) for r in records}
        # The stale cursor 410s → full delta crawl replays the complete backfill set.
        assert keys == {
            ("team/T0001/channel/C0STD", "m1"),
            ("team/T0001/channel/C0STD", "reply-r1"),
            ("team/T0001/channel/C0STD", "m2"),
            ("team/T0001/channel/C0STD", "m3"),
            ("team/T0001/channel/C0PRIV", "mp1"),
        }
        # A fresh deltaLink, DISTINCT from the expired one, is captured for the
        # re-baselined channel.
        assert connector.pending_cursors["delta:T0001/C0STD"] == DELTA_LINK_STD_REBASED

    asyncio.run(scenario())


def test_deleted_and_system_replies_are_not_ingested() -> None:
    """FIX-2: the top-level message filter (skip deletedDateTime-set; require
    messageType == 'message') applies to the replies path too — a deleted reply and a
    system-message reply are NOT ingested, while a normal user reply IS (parity; the
    fail-closed content posture: a deleted source message must not linger as knowledge).
    """
    connector = TeamsConnector(teams_settings(), transport=build_teams_transport())

    async def scenario() -> None:
        records = await _drain(connector)
        ids = {r.record_id for r in records}
        # From replies_T0001_C0STD_MSG1.json: r1 (normal) / r-del (deleted) / r-sys (system).
        assert "reply-r1" in ids  # a normal user reply IS ingested
        assert "reply-r-del" not in ids  # a deleted reply is NOT ingested
        assert "reply-r-sys" not in ids  # a system-message reply is NOT ingested
        reply = next(r for r in records if r.record_id == "reply-r1")
        assert reply.payload["reply_to_id"] == "m1"

    asyncio.run(scenario())


def test_incremental_with_stored_cursor_yields_strict_subset() -> None:
    connector = TeamsConnector(
        teams_settings(),
        transport=build_teams_transport(),
        cursor_state={
            "delta:T0001/C0STD": DELTA_LINK_STD,
            "delta:T0001/C0PRIV": DELTA_LINK_PRIV,
        },
    )

    async def scenario() -> None:
        records = await _drain(connector)
        keys = {(r.source_id, r.record_id) for r in records}
        # The stored deltaLinks replay the subset fixture (m2 only) + empty private.
        assert keys == {("team/T0001/channel/C0STD", "m2")}
        # Quarantines are re-counted on the recrawl.
        assert connector.acl_quarantined == 3

    asyncio.run(scenario())
