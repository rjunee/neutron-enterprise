# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Recorded-fixture Graph transport for the Teams connector suites (NE-080).

``build_teams_transport`` returns an :class:`httpx.MockTransport` that replays the
hand-modeled (sanitized synthetic) Graph JSON under ``fixtures/teams`` with ZERO
network sockets — the CI deliverable (no live tenant; the real-tenant crawl is
LIVE-PARKED behind NE-000 + Microsoft protected-API approval). It asserts every
Graph call carries the fixture Bearer token, optionally records requests, exposes
fault knobs (fail-after-N; a one-shot 429 + ``Retry-After``), and ASSERTS that no
messages/replies are fetched for a QUARANTINED channel (shared / unknown
membershipType / forged-prefix id).

The delta fixtures are hand-modeled from documented API shapes (the tenant is
owner-gated); the live run recalibrates them + the throttle defaults (NE-093
precedent). ``incremental`` selects the stored-deltaLink fixture: ``False`` (the
conformance default, requiring a STRICT SUBSET of the backfill) serves the subset
fixture (message ``m2`` only); ``True`` (the e2e "genuine second run") serves the
incremental fixture (new message ``m-new``).

When ``expire_std_delta=True``, the post-410 re-baseline full crawl's final page
returns ``DELTA_LINK_STD_REBASED`` instead of the page's on-disk deltaLink, since
Graph never re-issues an expired token.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import httpx

from ne.connect.teams.config import TeamsConnectorSettings

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "teams"

TENANT_ID = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
TEAM_ID = "T0001"

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
TOKEN_URL = f"{LOGIN}/{TENANT_ID}/oauth2/v2.0/token"
CHANNELS_URL = f"{GRAPH}/teams/{TEAM_ID}/channels"

#: The per-channel deltaLink URLs the backfill returns (fed back as the stored
#: incremental cursor). Exposed so tests can seed a persisted cursor state.
DELTA_LINK_STD = f"{GRAPH}/teams/{TEAM_ID}/channels/C0STD/messages/delta?$deltatoken=STD1"
DELTA_LINK_PRIV = f"{GRAPH}/teams/{TEAM_ID}/channels/C0PRIV/messages/delta?$deltatoken=PRIV1"
#: The DISTINCT refreshed deltaLink the post-410 re-baseline full crawl returns when
#: expire_std_delta=True — kept textually different from DELTA_LINK_STD so tests can
#: prove the refreshed cursor (not the lingering stale one) was persisted.
DELTA_LINK_STD_REBASED = f"{GRAPH}/teams/{TEAM_ID}/channels/C0STD/messages/delta?$deltatoken=STD1R"

#: Channels whose messages must NEVER be fetched (fail-closed quarantine).
_QUARANTINED_CHANNELS = frozenset({"C0SHARED", "C0WEIRD", "role:forged"})

_DELTA_RE = re.compile(r"^/v1\.0/teams/T0001/channels/(?P<cid>[^/]+)/messages/delta$")
_REPLIES_RE = re.compile(
    r"^/v1\.0/teams/T0001/channels/(?P<cid>[^/]+)/messages/(?P<mid>[^/]+)/replies$"
)


def _load(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def teams_settings(**overrides: object) -> TeamsConnectorSettings:
    """A valid secret-mode settings object pointed at the fixture tenant (fast throttle)."""
    kwargs: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "client_id": CLIENT_ID,
        "auth_method": "secret",
        "client_secret": "fixture-secret",
        "team_ids": (TEAM_ID,),
        "owner": "data-eng",
        "requests_per_second": 6000.0,
    }
    kwargs.update(overrides)
    return TeamsConnectorSettings(**kwargs)  # type: ignore[arg-type]


def _json(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def build_teams_transport(
    *,
    recorder: list[httpx.Request] | None = None,
    fail_after: int | None = None,
    one_shot_429: bool = False,
    incremental: bool = False,
    expire_std_delta: bool = False,
    token_expires_in: int = 3599,
) -> httpx.MockTransport:
    """Build the fixture-replay transport (see module docstring for the knobs)."""
    state = {"graph_calls": 0, "ratelimited": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        url = str(request.url)

        if url == TOKEN_URL:
            assert request.method == "POST"
            token = _load("token.json")
            token["expires_in"] = token_expires_in
            return _json(token)

        auth = request.headers.get("Authorization", "")
        assert auth == "Bearer fixture-token", f"missing/wrong bearer on {url}"

        state["graph_calls"] += 1
        if fail_after is not None and state["graph_calls"] > fail_after:
            return httpx.Response(500)
        if one_shot_429 and not state["ratelimited"]:
            state["ratelimited"] = True
            return httpx.Response(429, headers={"Retry-After": "1"})

        path = request.url.path
        params = dict(request.url.params)

        if url == CHANNELS_URL:
            return _json(_load("channels_T0001.json"))

        delta = _DELTA_RE.match(path)
        if delta is not None:
            cid = delta.group("cid")
            assert cid not in _QUARANTINED_CHANNELS, (
                f"messages were fetched for quarantined channel {cid!r}"
            )
            deltatoken = params.get("$deltatoken")
            skiptoken = params.get("$skiptoken")
            if deltatoken == "STD1" and expire_std_delta:
                # Graph expired the persisted deltaLink → HTTP 410 (resync signal).
                return httpx.Response(410, json={"error": {"code": "resyncRequired"}})
            if deltatoken == "STD1":
                fixture = (
                    "delta_T0001_C0STD_incremental.json"
                    if incremental
                    else "delta_T0001_C0STD_incremental_subset.json"
                )
                return _json(_load(fixture))
            if deltatoken == "PRIV1":
                return _json(_load("delta_T0001_C0PRIV_incremental.json"))
            if skiptoken == "STDP2":
                payload = _load("delta_T0001_C0STD_page2.json")
                if expire_std_delta:
                    # Graph never re-issues an expired token — the re-baseline crawl
                    # must end in a FRESH deltaLink, distinct from the 410'd one.
                    payload["@odata.deltaLink"] = DELTA_LINK_STD_REBASED
                return _json(payload)
            if cid == "C0STD":
                return _json(_load("delta_T0001_C0STD_page1.json"))
            if cid == "C0PRIV":
                return _json(_load("delta_T0001_C0PRIV.json"))

        replies = _REPLIES_RE.match(path)
        if replies is not None:
            cid = replies.group("cid")
            assert cid not in _QUARANTINED_CHANNELS, (
                f"replies were fetched for quarantined channel {cid!r}"
            )
            if replies.group("mid") == "m1":
                return _json(_load("replies_T0001_C0STD_MSG1.json"))
            return _json({"value": []})

        return httpx.Response(404, json={"error": {"code": "notFound", "message": url}})

    return httpx.MockTransport(handler)


def sequenced_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    """Return ``responses`` in order for successive Graph GETs (token POST served first)."""
    queue: list[httpx.Response] = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/oauth2/v2.0/token"):
            return _json(_load("token.json"))
        if queue:
            return queue.pop(0)
        return httpx.Response(404)  # pragma: no cover

    return httpx.MockTransport(handler)


def recording_transport(recorder: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    """A bare handler that records and 200s — proves zero requests on rejection."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={})

    return handler
