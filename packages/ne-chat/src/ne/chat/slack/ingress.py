# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FastAPI ingress for Slack Events API + interactivity (security-critical).

Every inbound request has its Slack v0 signature verified on the RAW BYTES FIRST
— before any JSON parsing or agent activity. A missing / forged / stale signature
is rejected with a status-only HTTP 401 whose body echoes NOTHING (no secret, no
config, no payload). That rejection is DELIBERATELY NOT audited: an unauthenticated
forgery must not be able to flood the tamper-evident audit log. Authenticated-but-
unauthorized actions (e.g. a mapped non-approver clicking Approve) ARE audited —
that asymmetry is intentional.

Both routes ack within Slack's ~3s window and run the real work (a governed agent
turn; an approval submission onto the durable gate) in the background via the
injected ``spawn`` seam (default ``create_task``). Tests pass a ``spawn`` that
captures the coroutine so they can await it inline — deterministic, no sleeps.
Every background coroutine is wrapped by :func:`_log_failures`, which logs an
unhandled exception (task name + id-only context, via the package logger)
before re-raising — no swallow, no control-flow change, just visibility
(NE-CLEAN3 item 1; previously an unhandled background-task exception was only
visible as asyncio's GC-time "Task exception was never retrieved" noise).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import parse_qsl

import fastapi
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from ne.chat.approvals import ApprovalActionService
from ne.chat.errors import IngressRejectedError
from ne.chat.slack.adapter import SlackChatAdapter
from ne.chat.slack.blocks import APPROVE_ACTION_ID
from ne.chat.slack.config import SlackChatSettings
from ne.chat.slack.interactions import SlackBlockAction, parse_block_action
from ne.chat.slack.mirror import SlackApprovalMirror
from ne.chat.slack.signature import verify_slack_signature
from ne.contracts.runtime import InboundMessage

__all__ = ["create_slack_ingress"]

_logger = logging.getLogger(__name__)

_TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
_SIGNATURE_HEADER = "X-Slack-Signature"
_DECIDED_STATUSES = frozenset({"approved", "denied"})


class _SeenEvents:
    """A bounded, TTL-expiring set of event ids (in-process dedup, monotonic clock).

    Slack re-delivers events (``X-Slack-Retry-Num``) when an ack is slow; a
    duplicate ``event_id`` must ack 200 without re-processing. This is in-process
    only — a restart may re-process a retried event (agent turns are audited and
    the gate is idempotent per request; durable dedup is a v1.1 hardening item).
    """

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
    """Await ``coro``; on an unhandled exception, log it (with context) and re-raise.

    Every ``dispatch(...)``-ed background coroutine is wrapped in this so an
    unhandled exception is no longer silent — it is logged at ERROR via the
    package logger, with the task name plus id-only context (channel/sender or
    user/request-id; NEVER message text, tokens, or secrets — C12), then
    re-raised unchanged. Control flow is identical to before this wrapper
    existed: the exception still propagates to whatever awaits the task (or,
    for the default ``create_task`` spawn, becomes asyncio's usual unretrieved-
    task-exception noise at GC time) — this only adds visibility, it never
    swallows-then-continues.
    """
    try:
        await coro
    except Exception:
        ctx = " ".join(f"{k}={v}" for k, v in sorted(context.items()))
        _logger.exception("slack background task %s failed (%s)", task_name, ctx)
        raise


def create_slack_ingress(
    *,
    settings: SlackChatSettings,
    adapter: SlackChatAdapter,
    approval_service: ApprovalActionService,
    mirror: SlackApprovalMirror,
    clock: Callable[[], float] = time.time,
    spawn: Callable[[Coroutine[Any, Any, Any]], object] | None = None,
) -> fastapi.FastAPI:
    """Build the Slack ingress FastAPI app (events + interactivity routes)."""
    app = fastapi.FastAPI()
    dispatch = spawn or _default_spawn
    seen = _SeenEvents(
        ttl_s=settings.event_dedup_ttl_s,
        max_size=settings.event_dedup_max,
        clock=time.monotonic,
    )

    def _verify(raw: bytes, request: Request) -> None:
        verify_slack_signature(
            raw_body=raw,
            timestamp_header=request.headers.get(_TIMESTAMP_HEADER),
            signature_header=request.headers.get(_SIGNATURE_HEADER),
            signing_secret=settings.signing_secret.get_secret_value(),
            now=clock(),
            tolerance_s=settings.signature_tolerance_s,
        )

    @app.post("/slack/events")
    async def slack_events(request: Request) -> Response:
        raw = await request.body()
        try:
            _verify(raw, request)
        except IngressRejectedError:
            # Status-only, no echo, and DELIBERATELY no audit (see module docstring).
            return JSONResponse(status_code=401, content={"detail": "invalid request signature"})

        payload = await request.json()
        kind = payload.get("type")
        if kind == "url_verification":
            return JSONResponse(content={"challenge": payload.get("challenge", "")})
        if kind == "event_callback":
            event = payload.get("event") or {}
            if _is_user_message(event):
                event_id = str(payload.get("event_id") or "")
                if event_id and seen.seen_before(event_id):
                    return JSONResponse(content={})  # duplicate delivery: ack, no re-run
                msg = InboundMessage(
                    channel=str(event.get("channel", "")),
                    sender=str(event.get("user", "")),
                    text=str(event.get("text", "")),
                )
                dispatch(
                    _log_failures(
                        adapter.dispatch_inbound(msg),
                        task_name="slack.dispatch_inbound",
                        channel=msg.channel,
                        sender=msg.sender,
                    )
                )
        return JSONResponse(content={})

    @app.post("/slack/interactions")
    async def slack_interactions(request: Request) -> Response:
        raw = await request.body()
        try:
            _verify(raw, request)
        except IngressRejectedError:
            return JSONResponse(status_code=401, content={"detail": "invalid request signature"})

        # Parse the urlencoded body from the RAW BYTES already read for signature
        # verification, using only the stdlib. Starlette's request-form helper would
        # require the ``python-multipart`` package (its ``_get_form`` asserts on it
        # even for application/x-www-form-urlencoded), which ne-chat deliberately
        # does not declare — never reintroduce it here (regression-locked in tests).
        try:
            fields = dict(parse_qsl(raw.decode("utf-8")))
        except UnicodeDecodeError:
            return JSONResponse(content={})  # undecodable body: ack and ignore
        action = parse_block_action(fields)
        if action is None:
            return JSONResponse(content={})  # malformed / unknown shape: ack and ignore
        dispatch(
            _log_failures(
                _action_followup(approval_service, mirror, action),
                task_name="slack.approval_action",
                user=action.user_id,
                request_id=action.value,
            )
        )
        return JSONResponse(content={})

    return app


def _is_user_message(event: dict[str, Any]) -> bool:
    """True for a real user message / app-mention (skip bot + edited-message subtypes)."""
    if event.get("bot_id"):
        return False
    etype = event.get("type")
    if etype == "app_mention":
        return True
    return etype == "message" and event.get("subtype") is None


async def _action_followup(
    approval_service: ApprovalActionService,
    mirror: SlackApprovalMirror,
    action: SlackBlockAction,
) -> None:
    """Submit the click onto the durable gate, then update Slack (buttons removed if decided)."""
    decision = "approve" if action.action_id == APPROVE_ACTION_ID else "deny"
    result = await approval_service.act(
        external_user_id=action.user_id,
        request_id=action.value,
        decision=decision,  # type: ignore[arg-type]
    )
    if result.status in _DECIDED_STATUSES:
        # The approval is decided: replace the mirror with a button-less outcome block.
        await mirror.replace_with_outcome(
            channel=action.channel_id,
            ts=action.message_ts,
            request_id=result.request_id,
            status=result.status,
            approver_id=result.approver_id,
            note=result.note,
        )
    else:
        # Not decided (unauthorized / stale / awaiting a second approver): keep the
        # buttons for a legitimate approver; notify only the clicker, ephemerally.
        await mirror.ephemeral(
            channel=action.channel_id,
            user=action.user_id,
            text=result.note or "This approval could not be actioned.",
        )
