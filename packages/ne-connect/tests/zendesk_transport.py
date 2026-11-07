# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Recorded-fixture Zendesk REST transport for the connector suites (NE-093).

``build_zendesk_transport`` returns an :class:`httpx.MockTransport` that replays
the hand-recorded (sanitized-fake) Zendesk JSON under ``fixtures/zendesk`` with
ZERO network sockets — the CI deliverable (no live instances; live seeding needs
owner dev instances per NE-000, out of scope). It asserts every request carries a
Basic ``Authorization`` header, optionally records requests, exposes fault knobs
(fail-after-N; a one-shot 429 + ``Retry-After``; a one-shot 401), and ASSERTS
that no comments are fetched for a quarantined/skipped ticket.

The tickets page-1 fixture embeds a HOSTILE absolute ``after_url`` / ``next_page``
that a recording transport proves is NEVER requested — the connector re-issues
against the pinned base with the opaque ``after_cursor`` only.

Incremental content depends on ``incremental``: ``incremental=False`` (the
conformance default, requiring a STRICT SUBSET of the backfill) serves
``tickets_cursor_incremental_subset`` (only ticket 1001); ``incremental=True``
(the e2e "genuine second run") serves ``tickets_cursor_incremental`` (new ticket
1005). Articles resume (any ``start_time > 1``) serves the empty page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from ne.connect.zendesk.config import ZendeskConnectorSettings

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "zendesk"

BASE_URL = "https://acme.zendesk.com"

#: Tickets whose comments must NEVER be fetched (1003 principal-less -> quarantine,
#: 1004 deleted -> skipped).
_NO_COMMENT_TICKETS = frozenset({"1003", "1004"})

_COMMENTS_RE = re.compile(r"^/api/v2/tickets/(\d+)/comments\.json$")


def _load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def zendesk_settings(**overrides: object) -> ZendeskConnectorSettings:
    """A valid settings object pointed at the fixture instance (fast throttle)."""
    kwargs: dict[str, object] = {
        "base_url": BASE_URL,
        "email": "svc@acme.test",
        "api_token": "fake-zendesk-api-token-not-real",
        "owner": "support-eng@acme.test",
        "requests_per_second": 6000.0,
    }
    kwargs.update(overrides)
    return ZendeskConnectorSettings(**kwargs)  # type: ignore[arg-type]


def build_zendesk_transport(
    *,
    recorder: list[httpx.Request] | None = None,
    fail_after: int | None = None,
    one_shot_429: bool = False,
    respond_401_once: bool = False,
    incremental: bool = False,
    incremental_fixture: str | None = None,
) -> httpx.MockTransport:
    """Build the fixture-replay transport (see module docstring for the knobs).

    ``incremental_fixture``, when set, overrides which JSON file is served for
    the cursor-driven incremental tickets fetch (e.g. to serve a batch containing
    a write-quarantine-triggering blank ticket); the default (``None``) preserves
    the existing ``incremental``-flag-driven selection.
    """
    state = {"calls": 0, "ratelimited": False, "unauthorized": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)

        auth = request.headers.get("Authorization", "")
        assert auth.startswith("Basic "), f"missing Basic auth on {request.url}"

        state["calls"] += 1
        if fail_after is not None and state["calls"] > fail_after:
            return httpx.Response(500)
        if one_shot_429 and not state["ratelimited"]:
            state["ratelimited"] = True
            return httpx.Response(429, headers={"Retry-After": "1"})
        if respond_401_once and not state["unauthorized"]:
            state["unauthorized"] = True
            return httpx.Response(401)

        path = request.url.path
        params = dict(request.url.params)

        if path == "/api/v2/incremental/tickets/cursor.json":
            cursor = params.get("cursor")
            if cursor is None:
                return httpx.Response(200, json=_load("tickets_cursor_page1.json"))
            if cursor == "curs2":
                return httpx.Response(200, json=_load("tickets_cursor_page2.json"))
            fixture = incremental_fixture or (
                "tickets_cursor_incremental.json"
                if incremental
                else "tickets_cursor_incremental_subset.json"
            )
            return httpx.Response(200, json=_load(fixture))

        comments = _COMMENTS_RE.match(path)
        if comments:
            ticket_id = comments.group(1)
            assert ticket_id not in _NO_COMMENT_TICKETS, (
                f"comments were fetched for quarantined/skipped ticket {ticket_id!r}"
            )
            return httpx.Response(200, json=_load(f"comments_{ticket_id}.json"))

        if path == "/api/v2/help_center/incremental/articles.json":
            start = int(params.get("start_time", "0"))
            fixture = "articles_empty.json" if start > 1 else "articles_page1.json"
            return httpx.Response(200, json=_load(fixture))

        return httpx.Response(404)

    return httpx.MockTransport(handler)


def recording_transport(recorder: list[httpx.Request]):
    """A bare handler that records and 200s — proves zero requests on rejection."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={})

    return handler
