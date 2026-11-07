# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin Slack Web API WRITE client — the ONLY httpx import site in ne.chat.

:class:`SlackChatClient` is a deliberately small bot-token client (NO
``slack_sdk`` — same build-vs-adopt call as the merged NE-045 connector: its own
urllib/aiohttp transport stack would bypass both the C2 egress import allowlist
and the mandated ``httpx.MockTransport`` fixture-first CI). It owns the
judgment-dense egress surface for the chat adapter's WRITE side.

C2 (the security spine): the constructor validates the configured base URL and
refuses to build under ``AIR_GAP`` BEFORE any I/O, then pins the validated base
to the configured ``api_allowed_hosts``. A method allowlist restricts calls to
the three WRITE endpoints the chat adapter needs — this client never reads
channel history (that is the connector's job) and never follows a
response-supplied URL, so there is no off-host redirect / SSRF re-guard surface.

C12: the bot token appears only in the ``Authorization`` header; failures raise
:class:`~ne.chat.errors.SlackApiError` with the HTTP status / short Slack error
CODE / host only — never a token, request params, or a response body.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from urllib.parse import urlparse

import httpx

from ne.chat.errors import SlackApiError
from ne.chat.slack.config import SlackChatSettings

# The AIR_GAP guard is the ONE shared read (NE-CKC10 / P2-23); aliased to keep the
# module-local name this client's callers/tests reference.
from ne.net.airgap import air_gap_engaged as _air_gap_engaged
from ne.net.ssrf import normalize_host, validate_endpoint_url

#: Re-exported transport alias so callers can type an injectable transport WITHOUT
#: importing httpx — keeping client.py the only httpx import site under ne.chat
#: (locked by the C2 egress guardian test).
type Transport = httpx.AsyncBaseTransport

__all__ = ["SlackChatClient", "Transport"]

_RETRYABLE = frozenset({429, 503, 504})
_ALLOWED_METHODS = frozenset({"chat.postMessage", "chat.update", "chat.postEphemeral"})


class SlackChatClient:
    """Slack Web API WRITE client: SSRF-guarded, throttled, retried, token-auth."""

    def __init__(
        self,
        settings: SlackChatSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # (a) Fail-closed BEFORE any I/O — Slack is egress.
        if _air_gap_engaged():
            raise SlackApiError("AIR_GAP engaged: the Slack chat client is egress and is refused")
        # (b) Validate the base URL (https / no IP / no packed-IPv4 / no metadata).
        api_base = validate_endpoint_url(
            settings.api_base_url,
            require_https=True,
            allow_private=False,
            error_cls=SlackApiError,
        )
        # (c) Pin the validated host to the configured allowlist.
        host = normalize_host(urlparse(api_base).hostname or "")
        allowed = {normalize_host(h) for h in settings.api_allowed_hosts}
        if host not in allowed:
            raise SlackApiError(f"api_base_url host {host!r} is not in the configured allowlist")
        self._base = api_base
        self._host = host
        self._settings = settings
        self._transport = transport
        self._min_interval = 60.0 / settings.requests_per_minute
        self._last_call = 0.0

    async def call(self, method: str, payload: Mapping[str, object]) -> dict[str, object]:
        """POST ``method`` with a JSON ``payload``; throttled, retried, ok-aware.

        The method allowlist pins the path segment (defense-in-depth): the request
        URL is built ONLY from the validated ``self._base`` and a known-good method
        name, never from a response-supplied URL.
        """
        if method not in _ALLOWED_METHODS:
            raise SlackApiError(f"refusing to call disallowed Slack method {method!r}")
        settings = self._settings
        token = settings.bot_token.get_secret_value()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            timeout=settings.timeout_seconds, transport=self._transport
        ) as client:
            for attempt in range(1, settings.max_attempts + 1):
                await self._throttle()
                resp = await client.post(
                    f"{self._base}/{method}", json=dict(payload), headers=headers
                )
                status = resp.status_code
                if status in _RETRYABLE:
                    if attempt == settings.max_attempts:
                        raise SlackApiError(
                            f"Slack API {method} returned HTTP {status} after {attempt} attempts",
                            status=status,
                            host=self._host,
                        )
                    await self._sleep_retry(resp.headers.get("Retry-After"))
                    continue
                if status >= 400:
                    raise SlackApiError(
                        f"Slack API {method} returned HTTP {status}",
                        status=status,
                        host=self._host,
                    )
                body = resp.json()
                if body.get("ok") is not True:
                    code = str(body.get("error") or "unknown_error")
                    raise SlackApiError(
                        f"Slack API {method} returned error {code!r}",
                        status=200,
                        error_code=code,
                        host=self._host,
                    )
                return body
        raise SlackApiError("exhausted Slack retries", host=self._host)  # pragma: no cover

    async def _throttle(self) -> None:
        loop = asyncio.get_running_loop()
        wait = self._min_interval - (loop.time() - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = loop.time()

    async def _sleep_retry(self, retry_after: str | None) -> None:
        delay = self._min_interval
        if retry_after:
            try:
                delay = min(float(retry_after), float(self._settings.retry_after_cap_seconds))
            except (TypeError, ValueError):
                delay = self._min_interval
        await asyncio.sleep(delay)
