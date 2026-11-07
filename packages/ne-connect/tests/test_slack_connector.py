# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SlackConnector fixture crawl: discovery, backfill records, ACL, quarantine."""

from __future__ import annotations

import asyncio

import pytest

from ne.connect.errors import SlackSyncError
from ne.connect.slack.acl import TENANT_ALL
from ne.connect.slack.connector import SlackConnector

from .slack_transport import (
    C0000EXT,
    C0000FORGED,
    C0000PRIV,
    C0000PUB,
    C0000QMEM,
    C0000QTYPE,
    G0000MPIM,
    build_slack_transport,
    slack_settings,
)

_RESOLVED_ACL = {
    C0000PUB: (TENANT_ALL,),
    C0000PRIV: ("U00000ADA", "U00000BOB"),
    G0000MPIM: ("U00000ADA", "U00000EVE"),
}


def _connector(**kw: object) -> SlackConnector:
    return SlackConnector(slack_settings(), transport=build_slack_transport(), **kw)  # type: ignore[arg-type]


def test_discover_lists_all_seven_channels() -> None:
    async def scenario() -> None:
        objects = await _connector().discover()
        assert {o.source_id for o in objects} == {
            C0000PUB,
            C0000PRIV,
            G0000MPIM,
            C0000QTYPE,
            C0000QMEM,
            C0000FORGED,
            C0000EXT,
        }
        assert all(o.kind == "channel" for o in objects)

    asyncio.run(scenario())


def test_backfill_yields_exactly_seven_records() -> None:
    connector = _connector()

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        assert len(records) == 7
        keys = {(r.source_id, r.record_id) for r in records}
        assert (C0000PUB, "1720000000.000100") in keys  # thread parent
        assert (C0000PUB, "1720000005.000150") in keys  # thread reply
        assert (C0000PUB, "1720000010.000200") in keys  # plain message
        assert (C0000PUB, "1720000020.000300") in keys  # page-2 message
        assert (C0000PUB, "1720000002.000110") not in keys  # channel_join skipped
        assert (C0000PRIV, "1720000100.000100") in keys
        assert (C0000PRIV, "1720000110.000200") in keys
        assert (G0000MPIM, "1720000200.000100") in keys

    asyncio.run(scenario())


def test_payload_carries_user_name_enrichment_and_thread_ts() -> None:
    connector = _connector()

    async def scenario() -> None:
        records = {r.record_id: r.payload for r in [r async for r in connector.backfill()]}
        parent = records["1720000000.000100"]
        assert parent["user"] == "U00000ADA"
        assert parent["user_name"] == "Ada Lovelace"
        assert "thread_ts" not in parent  # parent's own thread_ts == its own ts -> omitted

        reply = records["1720000005.000150"]
        assert reply["user_name"] == "Eve Torres"
        assert reply["thread_ts"] == "1720000000.000100"

        assert records["1720000010.000200"]["user_name"] == "Bob Boyle"

    asyncio.run(scenario())


def test_records_validate_against_the_contract() -> None:
    from ne.connect.slack.contract import slack_data_contract

    connector = _connector()
    contract = slack_data_contract(slack_settings())

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        for record in records:
            assert contract.validate(record.payload) == []

    asyncio.run(scenario())


def test_permissions_yield_acl_only_for_resolved_channels() -> None:
    connector = _connector()

    async def scenario() -> None:
        entries = {e.resource_id: e.principals async for e in connector.permissions()}
        assert entries == _RESOLVED_ACL

    asyncio.run(scenario())


def test_quarantine_counts_and_ids() -> None:
    connector = _connector()

    async def scenario() -> None:
        _ = [e async for e in connector.permissions()]
        assert connector.acl_quarantined == 4
        assert set(connector.acl_quarantined_ids) == {C0000QTYPE, C0000QMEM, C0000FORGED, C0000EXT}

    asyncio.run(scenario())


def test_quarantined_channels_produce_no_records_or_acl() -> None:
    connector = _connector()

    async def scenario() -> None:
        entries = [e async for e in connector.permissions()]
        records = [r async for r in connector.backfill()]
        acl_ids = {e.resource_id for e in entries}
        rec_ids = {r.source_id for r in records}
        for qid in (C0000QTYPE, C0000QMEM, C0000FORGED, C0000EXT):
            assert qid not in acl_ids
            assert qid not in rec_ids

    asyncio.run(scenario())


def test_pending_cursors_are_max_ts_per_live_channel_only() -> None:
    connector = _connector()

    async def scenario() -> None:
        _ = [r async for r in connector.backfill()]
        assert connector.pending_cursors == {
            C0000PUB: "1720000020.000300",
            C0000PRIV: "1720000110.000200",
            G0000MPIM: "1720000200.000100",
        }
        # quarantined channels never reach history -> no pending cursor entry.
        for qid in (C0000QTYPE, C0000QMEM, C0000FORGED, C0000EXT):
            assert qid not in connector.pending_cursors

    asyncio.run(scenario())


def test_members_fetch_403_quarantines_channel_and_continues() -> None:
    connector = SlackConnector(
        slack_settings(), transport=build_slack_transport(member_status_overrides={C0000PRIV: 403})
    )

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        assert C0000PRIV not in {r.source_id for r in records}
        # baseline: QMEM is ALSO always a fetch failure (default 403) -> +1 for PRIV.
        assert connector.acl_fetch_failed == 2
        assert connector.acl_quarantined == 5  # QTYPE, FORGED, EXT (mapping) + QMEM, PRIV (fetch)
        assert C0000PRIV in connector.acl_quarantined_ids

    asyncio.run(scenario())


def test_members_fetch_404_quarantines_channel() -> None:
    connector = SlackConnector(
        slack_settings(), transport=build_slack_transport(member_status_overrides={G0000MPIM: 404})
    )

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        assert G0000MPIM not in {r.source_id for r in records}
        assert connector.acl_fetch_failed == 2  # QMEM baseline + G0000MPIM
        assert connector.acl_quarantined == 5

    asyncio.run(scenario())


def test_members_fetch_429_exhaustion_skips_channel_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NE-CLEAN3 item 2 supersedes NE-CLEAN1's "ratelimited exhaustion aborts
    the run" policy (previously ``test_members_fetch_429_exhaustion_aborts_run``):
    a per-channel members-fetch 429 exhaustion is now a per-channel RETRYABLE
    SKIP — backfill COMPLETES, the rate-limited channel produces no
    record/ACL/cursor and is never an ACL quarantine, and every other channel's
    output is unaffected."""

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    connector = SlackConnector(
        slack_settings(), transport=build_slack_transport(member_status_overrides={C0000PRIV: 429})
    )

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        acl = [e async for e in connector.permissions()]
        assert C0000PRIV not in {r.source_id for r in records}
        assert C0000PRIV not in {e.resource_id for e in acl}
        # other live channels' records/ACL are untouched.
        assert C0000PUB in {r.source_id for r in records}
        assert G0000MPIM in {r.source_id for r in records}
        assert {e.resource_id for e in acl} >= {C0000PUB, G0000MPIM}

    asyncio.run(scenario())
    assert connector.rate_limited == 1
    assert C0000PRIV in connector.rate_limited_ids
    assert C0000PRIV not in connector.acl_quarantined_ids
    assert set(connector.acl_quarantined_ids) == {C0000QTYPE, C0000QMEM, C0000FORGED, C0000EXT}
    assert C0000PRIV not in connector.pending_cursors
    assert connector.pending_cursors[C0000PUB] == "1720000020.000300"
    assert connector.pending_cursors[G0000MPIM] == "1720000200.000100"


def test_members_fetch_ok_false_ratelimited_skips_channel_not_acl_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slack's ok:false error:'ratelimited' envelope (HTTP 200) is RETRYABLE at
    ANY status. NE-CLEAN3 item 2 supersedes NE-CLEAN1's run-abort policy
    (previously
    ``test_members_fetch_ok_false_ratelimited_exhaustion_aborts_run_not_quarantine``):
    exhaustion now skips the channel (retryable, cursor untouched) rather than
    aborting the run — and it must NEVER be an ACL quarantine either way."""

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    connector = SlackConnector(
        slack_settings(),
        transport=build_slack_transport(member_ok_false_ratelimited=C0000PRIV),
    )

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        assert C0000PRIV not in {r.source_id for r in records}
        assert C0000PUB in {r.source_id for r in records}
        assert G0000MPIM in {r.source_id for r in records}

    asyncio.run(scenario())
    assert connector.rate_limited == 1
    assert C0000PRIV in connector.rate_limited_ids
    assert C0000PRIV not in connector.acl_quarantined_ids
    assert C0000PRIV not in connector.pending_cursors


def test_history_ratelimit_rolls_back_partial_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rate-limit exhaustion on the HISTORY fetch (after the ACL entry for
    that channel was already appended) rolls back the partial channel output —
    no record, no AclEntry, no pending cursor for the rate-limited channel —
    while every other channel's output is untouched and the run completes."""

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    connector = SlackConnector(
        slack_settings(),
        transport=build_slack_transport(history_status_overrides={C0000PUB: 429}),
    )

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        acl = [e async for e in connector.permissions()]
        assert C0000PUB not in {r.source_id for r in records}
        assert C0000PUB not in {e.resource_id for e in acl}
        # other channels intact.
        assert C0000PRIV in {r.source_id for r in records}
        assert G0000MPIM in {r.source_id for r in records}
        assert {e.resource_id for e in acl} >= {C0000PRIV, G0000MPIM}

    asyncio.run(scenario())
    assert connector.rate_limited == 1
    assert C0000PUB in connector.rate_limited_ids
    assert C0000PUB not in connector.pending_cursors
    assert connector.pending_cursors[C0000PRIV] == "1720000110.000200"
    assert connector.pending_cursors[G0000MPIM] == "1720000200.000100"


def test_is_permanent_failure_ratelimited_is_retryable_at_any_status() -> None:
    """Full `_is_permanent_failure` truth table: ratelimited is retryable at ANY
    status (incl. 200); every other pre-existing row stays byte-identical."""
    from ne.connect.slack.connector import _is_permanent_failure

    # ratelimited: retryable regardless of status (200, or any other value).
    assert _is_permanent_failure(SlackSyncError("x", status=200, error_code="ratelimited")) is False
    assert _is_permanent_failure(SlackSyncError("x", status=429, error_code="ratelimited")) is False

    # ok:false on HTTP 200 with any OTHER error code: still permanent.
    assert (
        _is_permanent_failure(SlackSyncError("x", status=200, error_code="channel_not_found"))
        is True
    )

    # non-429 4xx: permanent.
    assert _is_permanent_failure(SlackSyncError("x", status=403)) is True
    assert _is_permanent_failure(SlackSyncError("x", status=404)) is True

    # run-level faults: not permanent (re-raise / abort).
    assert _is_permanent_failure(SlackSyncError("x", status=429)) is False
    assert _is_permanent_failure(SlackSyncError("x", status=500)) is False
    assert _is_permanent_failure(SlackSyncError("x", status=None)) is False


def test_members_fetch_500_aborts_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    connector = SlackConnector(
        slack_settings(), transport=build_slack_transport(member_status_overrides={C0000PRIV: 500})
    )

    async def scenario() -> None:
        with pytest.raises(SlackSyncError):
            _ = [r async for r in connector.backfill()]

    asyncio.run(scenario())


def test_users_list_403_degrades_no_user_name_run_continues() -> None:
    # users.list has no override knob on the shared fixture transport (it is
    # NOT security-bearing), so the degrade path is proven directly against a
    # small dedicated transport that 403s only users.list.
    import httpx

    from .slack_transport import _load

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        method = request.url.path.removeprefix("/api/")
        params = dict(request.url.params)
        if method == "users.list":
            return httpx.Response(403)
        if method == "conversations.list":
            if params.get("cursor") == "CURSPAGE2":
                return httpx.Response(200, json=_load("conversations_page2.json"))
            return httpx.Response(200, json=_load("conversations_page1.json"))
        if method == "conversations.history":
            channel = params.get("channel")
            if channel == C0000PUB:
                if params.get("cursor") == "HCUR2":
                    return httpx.Response(200, json=_load("history_C0000PUB_page2.json"))
                return httpx.Response(200, json=_load("history_C0000PUB_page1.json"))
            return httpx.Response(200, json=_load("history_empty.json"))
        if method == "conversations.members":
            return httpx.Response(200, json={"ok": True, "members": []})
        return httpx.Response(200, json={"ok": True, "channels": [], "messages": [], "members": []})

    connector = SlackConnector(slack_settings(), transport=httpx.MockTransport(handler))

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        assert records  # the run continues despite the users.list failure
        for record in records:
            assert "user_name" not in record.payload

    asyncio.run(scenario())


def test_incremental_with_differing_basis_triggers_recrawl() -> None:
    connector = _connector()

    async def scenario() -> None:
        full = [r async for r in connector.backfill()]
        assert len(full) == 7
        incremental = [r async for r in connector.incremental({"ts:C0000PUB": "1720000010.000200"})]
        full_ids = {(r.source_id, r.record_id) for r in full}
        incremental_ids = {(r.source_id, r.record_id) for r in incremental}
        assert incremental_ids < full_ids

    asyncio.run(scenario())


def test_channel_ids_filter_restricts_the_crawl() -> None:
    connector = SlackConnector(
        slack_settings(channel_ids=(C0000PUB,)), transport=build_slack_transport()
    )

    async def scenario() -> None:
        objects = await connector.discover()
        assert {o.source_id for o in objects} == {C0000PUB}
        records = [r async for r in connector.backfill()]
        assert {r.source_id for r in records} == {C0000PUB}

    asyncio.run(scenario())
