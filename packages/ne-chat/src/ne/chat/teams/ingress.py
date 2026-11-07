# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FastAPI ingress for Bot Framework activities (security-critical, NE-080).

Every inbound activity's ``Authorization`` bearer JWT is validated BEFORE any
agent activity. Because the fail-closed serviceUrl cross-check needs the parsed
``serviceUrl``, the DECIDED order is: parse the JSON body (an undecodable /
non-object body acks 200 and trusts NOTHING), extract ``serviceUrl`` as a plain
string, then call ``authenticate(auth_header, service_url)``. An
``ActivityAuthError`` yields a status-only HTTP 401 whose body echoes NOTHING and
is DELIBERATELY NOT audited — an unauthenticated forgery must not flood the
tamper-evident audit log (the Slack forged-signature precedent).

A VERIFIED activity whose ``serviceUrl`` host is off the Bot Connector allowlist
is ACKed 200 and DROPPED (no refs entry, no dispatch) — preventing egress + retry
storms. Only a verified + allowlisted activity records its serviceUrl into the
shared :class:`~ne.chat.teams.refs.ConversationRefs` (cache-poisoning defense).
The route acks 200 within Bot Framework's ~15s window and runs the governed turn
in the background via the injected ``spawn`` seam.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

import fastapi
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from ne.chat.errors import ActivityAuthError, TeamsApiError
from ne.chat.teams.adapter import TeamsChatAdapter
from ne.chat.teams.auth import TeamsActivityAuthenticator
from ne.chat.teams.config import TeamsChatSettings
from ne.chat.teams.refs import ConversationRefs, validate_service_url
from ne.contracts.runtime import InboundMessage

__all__ = ["create_teams_ingress"]

_logger = logging.getLogger(__name__)
_MENTION_RE = re.compile(r"<at>.*?</at>")


class _SeenEvents:
    """A bounded, TTL-expiring set of activity keys (in-process dedup)."""

    def __init__(self, *, ttl_s: float, max_size: int, clock: Callable[[], float]) -> None:
        self._ttl = ttl_s
        self._max = max_size
        self._clock = clock
        self._seen: OrderedDict[str, float] = OrderedDict()

    def seen_before(self, event_id: str) -> bool:
        now = self._clock()
        self._evict(now)
        if event_id in self._seen:
            self._seen.move_to_end(event_id)
            return True
        self._seen[event_id] = now + self._ttl
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return False

    def _evict(self, now: float) -> None:
        expired = [k for k, deadline in self._seen.items() if deadline <= now]
        for k in expired:
            del self._seen[k]


def _default_spawn(coro: Coroutine[Any, Any, Any]) -> object:
    import asyncio

    return asyncio.get_running_loop().create_task(coro)


async def _log_failures(coro: Coroutine[Any, Any, Any], *, task_name: str, **context: str) -> None:
    """Await ``coro``; on an unhandled exception, log id-only context and re-raise (C12)."""
    try:
        await coro
    except Exception:
        ctx = " ".join(f"{k}={v}" for k, v in sorted(context.items()))
        _logger.exception("teams background task %s failed (%s)", task_name, ctx)
        raise


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _is_user_message(activity: Mapping[str, Any]) -> bool:
    """True for a real user message (not the bot's own echo / non-message activity)."""
    if activity.get("type") != "message":
        return False
    from_id = _mapping(activity.get("from")).get("id")
    recipient_id = _mapping(activity.get("recipient")).get("id")
    return bool(from_id) and from_id != recipient_id


def create_teams_ingress(
    *,
    settings: TeamsChatSettings,
    adapter: TeamsChatAdapter,
    authenticator: TeamsActivityAuthenticator,
    refs: ConversationRefs,
    clock: Callable[[], float] = time.time,
    spawn: Callable[[Coroutine[Any, Any, Any]], object] | None = None,
) -> fastapi.FastAPI:
    """Build the Teams ingress FastAPI app (single Bot Framework messages route)."""
    app = fastapi.FastAPI()
    dispatch = spawn or _default_spawn
    seen = _SeenEvents(
        ttl_s=settings.event_dedup_ttl_s, max_size=settings.event_dedup_max, clock=clock
    )

    @app.post("/teams/messages")
    async def teams_messages(request: Request) -> Response:
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(content={})  # undecodable body: ack, trust nothing
        if not isinstance(payload, dict):
            return JSONResponse(content={})  # non-object body: ack, trust nothing

        service_url = str(payload.get("serviceUrl") or "")
        try:
            await authenticator.authenticate(request.headers.get("Authorization"), service_url)
        except ActivityAuthError:
            # Status-only, no echo, DELIBERATELY no audit (see module docstring).
            return JSONResponse(
                status_code=401, content={"detail": "invalid activity authorization"}
            )

        # Post-auth: a verified activity with an off-allowlist serviceUrl is
        # ACKed and DROPPED (no refs entry, no dispatch).
        try:
            validated_service_url = validate_service_url(settings, service_url)
        except TeamsApiError:
            return JSONResponse(content={})

        conversation_id = str(_mapping(payload.get("conversation")).get("id") or "")
        if not conversation_id:
            return JSONResponse(content={})
        # Only a verified + allowlisted activity may set refs (poisoning defense).
        refs.record(conversation_id, validated_service_url)

        if _is_user_message(payload):
            activity_id = str(payload.get("id") or "")
            event_key = f"{conversation_id}:{activity_id}"
            if activity_id and seen.seen_before(event_key):
                return JSONResponse(content={})  # duplicate delivery: ack, no re-run
            frm = _mapping(payload.get("from"))
            sender = str(frm.get("aadObjectId") or frm.get("id") or "")
            text = _MENTION_RE.sub("", str(payload.get("text") or "")).strip()
            msg = InboundMessage(channel=conversation_id, sender=sender, text=text)
            dispatch(
                _log_failures(
                    adapter.dispatch_inbound(msg),
                    task_name="teams.dispatch_inbound",
                    channel=conversation_id,
                    sender=sender,
                )
            )
        return JSONResponse(content={})

    return app
