# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin Zendesk REST API HTTP client (NE-093) — a C2-guarded httpx site.

:class:`ZendeskClient` is a deliberately small Basic-auth (``email/token``)
client (NO ``zenpy`` — see the build-vs-adopt rationale in the package NOTICE:
it is requests-based/synchronous AND GPL-family, so it would bypass both the C2
egress guardian and the mandated ``httpx.MockTransport`` fixture-first CI, the
same call NE-045 made for ``slack_sdk`` and NE-047 for ``simple-salesforce``).

C2 (the security spine): the constructor validates the configured base URL and
refuses to build under ``AIR_GAP`` BEFORE any I/O. The validated base is pinned
to the configured ``allowed_hosts`` (dot-suffix for ``*.zendesk.com``, exact
otherwise). This client NEVER follows a response-supplied URL: Zendesk's
incremental exports return ``after_url`` / ``next_page`` FULL URLs which are
deliberately IGNORED — we extract the opaque ``after_cursor`` (validated) / the
numeric ``end_time`` and issue our OWN request against the pinned base (the same
pinned-base discipline Salesforce uses for its ``Sforce-Locator``).
:meth:`_guard_path` refuses any path not starting with ``/api/v2/`` before send.

C12: the API token never leaves a ``SecretStr``; the Basic ``Authorization``
header is computed per-request; failures raise :class:`ZendeskSyncError` with
the STATUS / host ONLY — never a token, a response body, or a full URL.
"""

from __future__ import annotations

import base64
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import httpx

from ne.connect.endpoints import AirGapViolationError, UnsafeEndpointError, validate_source_url
from ne.connect.errors import ZendeskSyncError
from ne.connect.throttle import AdaptiveThrottle
from ne.connect.zendesk.config import ZendeskConnectorSettings
from ne.net.airgap import air_gap_engaged
from ne.net.ssrf import normalize_host

#: Re-exported transport alias so the connector/sync modules can type their
#: injectable transport WITHOUT importing httpx — keeping client.py one of the
#: locked httpx import sites under ``ne/connect`` (C2 egress guardian test).
type Transport = httpx.AsyncBaseTransport
#: The concrete client type :meth:`ZendeskClient.session` yields.
type Client = httpx.AsyncClient

__all__ = ["Client", "Transport", "ZendeskClient"]

_RETRYABLE = frozenset({429, 500, 502, 503, 504})
#: Zendesk cursor tokens are URL-safe base64-ish opaque strings — validated
#: before we re-issue a request with them (defense in depth; never interpolated
#: into a path, always a query param on our own base).
_CURSOR_RE = re.compile(r"^[A-Za-z0-9=+/_-]+$")


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    """True iff ``host`` matches one of ``allowed_hosts`` (dot-suffix or exact)."""
    for entry in allowed_hosts:
        if entry.startswith("."):
            if host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


class ZendeskClient:
    """Zendesk REST client: SSRF-guarded, throttled, retried, cursor-safe."""

    def __init__(
        self,
        settings: ZendeskConnectorSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # (a) Fail-closed BEFORE any I/O — Zendesk is egress.
        if air_gap_engaged():
            raise AirGapViolationError(
                "AIR_GAP engaged: the Zendesk connector is egress and is not permitted"
            )
        # (b) Validate the base URL (https / no IP / no packed-IPv4 / no metadata).
        base = validate_source_url(settings.base_url, allow_external=True)
        # (c) Close the external allowlist gap (validate_source_url has none).
        host = normalize_host(urlparse(base).hostname or "")
        if not _host_allowed(host, settings.allowed_hosts):
            raise UnsafeEndpointError(f"base_url host {host!r} is not in the configured allowlist")
        self._base = base
        self._host = host
        self._settings = settings
        self._transport = transport
        self._throttle = AdaptiveThrottle(settings.requests_per_second, settings.burst)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[httpx.AsyncClient]:
        """Open ONE httpx client for a crawl (timeout + injected transport)."""
        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds, transport=self._transport
        ) as client:
            yield client

    # -- path guard + auth ---------------------------------------------------
    def _guard_path(self, path: str) -> None:
        """Refuse anything that is not a relative ``/api/v2/...`` path."""
        if not path.startswith("/api/v2/"):
            raise ZendeskSyncError("refusing to call a Zendesk path outside '/api/v2/'")

    def _auth_header(self) -> str:
        creds = f"{self._settings.email}/token:{self._settings.api_token.get_secret_value()}"
        return "Basic " + base64.b64encode(creds.encode("utf-8")).decode("ascii")

    def _validate_cursor(self, cursor: str) -> str:
        if not _CURSOR_RE.match(cursor):
            raise ZendeskSyncError("refusing to reuse a malformed Zendesk cursor")
        return cursor

    async def _get_json(
        self, client: httpx.AsyncClient, path: str, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        self._guard_path(path)  # BEFORE any request.
        settings = self._settings
        max_attempts = settings.max_attempts
        cap = settings.retry_after_cap_seconds
        url = f"{self._base}{path}"
        for attempt in range(1, max_attempts + 1):
            await self._throttle.acquire()
            resp = await client.get(
                url, params=params, headers={"Authorization": self._auth_header()}
            )
            status = resp.status_code
            if status in _RETRYABLE:
                if attempt == max_attempts:
                    raise ZendeskSyncError(
                        f"Zendesk API returned HTTP {status} after {attempt} attempts",
                        status=status,
                    )
                await self._throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                continue
            if status >= 400:
                raise ZendeskSyncError(f"Zendesk API returned HTTP {status}", status=status)
            return resp.json()
        raise ZendeskSyncError("exhausted Zendesk retries")  # pragma: no cover

    # -- incremental ticket export (cursor-based) ----------------------------
    async def incremental_tickets(
        self, client: httpx.AsyncClient, *, start_time: int | None, cursor: str | None
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """One page of ``/api/v2/incremental/tickets/cursor.json``.

        Uses ``cursor`` when resuming, else ``start_time`` for the first page.
        Returns ``(tickets, after_cursor, end_of_stream)``. The response's
        ``after_url`` / ``next_page`` FULL URLs are deliberately NEVER followed —
        only the opaque ``after_cursor`` (validated) is reused on our pinned base.
        """
        params: dict[str, str] = {}
        if cursor is not None:
            params["cursor"] = self._validate_cursor(cursor)
        else:
            params["start_time"] = str(int(start_time or 0))
        payload = await self._get_json(client, "/api/v2/incremental/tickets/cursor.json", params)
        tickets = payload.get("tickets")
        tickets = list(tickets) if isinstance(tickets, list) else []
        raw_after = payload.get("after_cursor")
        after_cursor = raw_after if isinstance(raw_after, str) and raw_after else None
        end_of_stream = bool(payload.get("end_of_stream"))
        return tickets, after_cursor, end_of_stream

    async def ticket_comments(
        self, client: httpx.AsyncClient, ticket_id: int
    ) -> list[dict[str, Any]]:
        """All comments for a ticket, cursor-paginated via ``meta.after_cursor``.

        The ``links.next`` FULL URL is never followed — only ``meta.after_cursor``
        (validated) is reused as a query param on our pinned base.
        """
        path = f"/api/v2/tickets/{int(ticket_id)}/comments.json"
        comments: list[dict[str, Any]] = []
        after_cursor: str | None = None
        while True:
            params: dict[str, str] = {"page[size]": str(self._settings.page_size)}
            if after_cursor is not None:
                params["page[after]"] = self._validate_cursor(after_cursor)
            payload = await self._get_json(client, path, params)
            page = payload.get("comments")
            if isinstance(page, list):
                comments.extend(page)
            meta = payload.get("meta")
            has_more = bool(meta.get("has_more")) if isinstance(meta, Mapping) else False
            raw_after = meta.get("after_cursor") if isinstance(meta, Mapping) else None
            if not has_more or not isinstance(raw_after, str) or not raw_after:
                break
            after_cursor = raw_after
        return comments

    async def incremental_articles(
        self, client: httpx.AsyncClient, start_time: int
    ) -> tuple[list[dict[str, Any]], int | None, bool]:
        """One page of ``/api/v2/help_center/incremental/articles.json``.

        Returns ``(articles, end_time, has_next)``. The connector loops feeding
        ``end_time`` back as ``start_time``; the ``next_page`` FULL URL is never
        followed.
        """
        payload = await self._get_json(
            client,
            "/api/v2/help_center/incremental/articles.json",
            {"start_time": str(int(start_time))},
        )
        articles = payload.get("articles")
        articles = list(articles) if isinstance(articles, list) else []
        raw_end = payload.get("end_time")
        end_time = int(raw_end) if isinstance(raw_end, (int, float)) else None
        has_next = payload.get("next_page") not in (None, "")
        return articles, end_time, has_next
