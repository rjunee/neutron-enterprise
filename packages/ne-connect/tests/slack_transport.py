# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Recorded-fixture Slack Web API transport for the connector suites (NE-045).

``build_slack_transport`` returns an :class:`httpx.MockTransport` that replays
the hand-recorded (sanitized-fake) Slack JSON under ``fixtures/slack`` with ZERO
network sockets — the CI deliverable per NE-000. It asserts every call carries a
Bearer ``Authorization`` header, optionally records requests, and exposes
fault-injection knobs (fail-after-N calls; a one-shot 429+``Retry-After``;
per-channel ``conversations.members``/``conversations.history`` status
overrides; an incremental-history routing mode) used by the durability /
rate-limit / quarantine tests.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import httpx

from ne.connect.slack.config import SlackConnectorSettings

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "slack"

API = "https://slack.com/api"

C0000PUB = "C0000PUB"
C0000PRIV = "C0000PRIV"
G0000MPIM = "G0000MPIM"
C0000QTYPE = "C0000QTYPE"
C0000QMEM = "C0000QMEM"
C0000FORGED = "C0000FORGED"
C0000EXT = "C0000EXT"

#: The default per-channel HTTP status served for ``conversations.members`` when
#: no ``member_status_overrides`` entry is supplied — C0000QMEM is a fixture
#: channel dedicated to the fetch-failure quarantine path.
_DEFAULT_MEMBER_STATUS = {C0000QMEM: 403}

_HISTORY_FIXTURE = {
    C0000PUB: "history_C0000PUB_page1.json",
    C0000PRIV: "history_C0000PRIV.json",
    G0000MPIM: "history_G0000MPIM.json",
}
_HISTORY_LATER_FIXTURE = {
    C0000PUB: "history_C0000PUB_page2.json",
    C0000PRIV: "history_C0000PRIV.json",
    G0000MPIM: "history_G0000MPIM.json",
}
_MEMBERS_FIXTURE = {
    C0000PRIV: "members_C0000PRIV.json",
    G0000MPIM: "members_G0000MPIM.json",
    C0000FORGED: "members_C0000FORGED.json",
}


def _load(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def slack_settings(**overrides: object) -> SlackConnectorSettings:
    """A valid settings object pointed at the fixture workspace.

    The throttle rates are overridden to a fast-but-still-real token bucket
    (production defaults are the conservative 30/15 rpm from
    :class:`SlackConnectorSettings` — see ``test_slack_config.py`` for those)
    so the fixture-replay suites exercise real ``AdaptiveThrottle`` pacing
    without real wall-clock waits blowing up CI run time.
    """
    kwargs: dict[str, object] = {
        "bot_token": "xoxb-fake-not-real",
        "owner": "data-eng@acme.test",
        "history_requests_per_minute": 6000.0,
        "requests_per_minute": 6000.0,
    }
    kwargs.update(overrides)
    return SlackConnectorSettings(**kwargs)  # type: ignore[arg-type]


def _json(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def build_slack_transport(
    *,
    recorder: list[httpx.Request] | None = None,
    fail_after: int | None = None,
    ratelimit_once: bool = False,
    member_status_overrides: Mapping[str, int] | None = None,
    member_ok_false_ratelimited: str | None = None,
    history_status_overrides: Mapping[str, int] | None = None,
    incremental: bool = False,
) -> httpx.MockTransport:
    """Build the fixture-replay transport.

    ``recorder`` collects every request. ``fail_after`` returns HTTP 500 beyond
    the Nth API call. ``ratelimit_once`` serves ONE HTTP 429 with
    ``Retry-After: 1`` on the first ``conversations.history`` call, then the
    normal fixture. ``member_status_overrides`` maps a channel id to an HTTP
    status served for that channel's ``conversations.members`` call instead of
    the fixture (drives the per-channel members-fetch-failure tests).
    ``member_ok_false_ratelimited`` serves Slack's ``ok:false error:'ratelimited'``
    envelope on HTTP 200 for that one channel's ``conversations.members`` call on
    EVERY attempt (never a fixture, never a plain 429) — driving the client's
    retry loop to exhaustion so the connector must classify the resulting
    ``SlackRateLimitedError(status=200, error_code='ratelimited')`` as a
    per-channel retryable skip, not a permanent ACL quarantine.
    ``history_status_overrides`` maps a channel id to an HTTP status served for
    that channel's ``conversations.history`` call on EVERY attempt (mirroring
    ``member_status_overrides`` but for the history fetch) — used to drive the
    history-side rate-limit exhaustion / partial-channel-rollback tests.
    ``incremental=True`` makes a ``conversations.history`` call carrying an
    ``oldest`` param serve the ``history_incremental_*`` / empty fixtures
    (simulating a genuine second run); with ``incremental=False`` (the
    conformance-kit / first-run-resume default), an ``oldest`` param instead
    serves the channel's "later page" fixture — still a real subset of the
    full history, never new content.
    """
    state = {"calls": 0, "ratelimited": False}
    overrides = dict(member_status_overrides or {})
    history_overrides = dict(history_status_overrides or {})

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        auth = request.headers.get("Authorization", "")
        assert auth.startswith("Bearer "), f"missing bearer on {request.url}"

        method = request.url.path.removeprefix("/api/")
        params = dict(request.url.params)

        state["calls"] += 1
        if fail_after is not None and state["calls"] > fail_after:
            return httpx.Response(500)

        if method == "conversations.list":
            if params.get("cursor") == "CURSPAGE2":
                return _json(_load("conversations_page2.json"))
            return _json(_load("conversations_page1.json"))

        if method == "users.list":
            return _json(_load("users.json"))

        if method == "conversations.members":
            channel = params.get("channel", "")
            if channel == member_ok_false_ratelimited:
                return _json({"ok": False, "error": "ratelimited"})
            status = overrides.get(channel, _DEFAULT_MEMBER_STATUS.get(channel))
            if status is not None:
                if status == 200:
                    return _json({"ok": False, "error": "channel_not_found"})
                return httpx.Response(status)
            fixture = _MEMBERS_FIXTURE.get(channel)
            if fixture is not None:
                return _json(_load(fixture))
            return httpx.Response(404)  # pragma: no cover — no test exercises this path

        if method == "conversations.history":
            channel = params.get("channel", "")
            oldest = params.get("oldest")
            history_status = history_overrides.get(channel)
            if history_status is not None:
                if history_status == 200:
                    return _json({"ok": False, "error": "ratelimited"})
                return httpx.Response(history_status)
            if ratelimit_once and not state["ratelimited"]:
                state["ratelimited"] = True
                return httpx.Response(429, headers={"Retry-After": "1"})
            if oldest:
                if incremental:
                    if channel == C0000PUB:
                        return _json(_load("history_incremental_C0000PUB.json"))
                    return _json(_load("history_empty.json"))
                fixture = _HISTORY_LATER_FIXTURE.get(channel, "history_empty.json")
                return _json(_load(fixture))
            if channel == C0000PUB and params.get("cursor") == "HCUR2":
                return _json(_load("history_C0000PUB_page2.json"))
            fixture = _HISTORY_FIXTURE.get(channel, "history_empty.json")
            return _json(_load(fixture))

        if method == "conversations.replies":
            channel = params.get("channel", "")
            ts = params.get("ts", "")
            if channel == C0000PUB and ts == "1720000000.000100":
                return _json(_load("replies_C0000PUB_1720000000.json"))
            return _json(_load("history_empty.json"))  # pragma: no cover

        return httpx.Response(404, json={"ok": False, "error": "unknown_method"})

    return httpx.MockTransport(handler)


def recording_transport(recorder: list[httpx.Request]):
    """A bare handler that records and 200s — proves zero requests on rejection."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={"ok": True})

    return handler
