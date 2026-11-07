# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin Microsoft Graph HTTP client for Teams (NE-080) — a C2-guarded httpx site.

:class:`TeamsGraphClient` is a deliberately small app-only Graph client (NO
msgraph-sdk — the NE-042/044 precedent), a sibling-parity copy of
``ne.connect.m365.graph.GraphClient`` typed to :class:`TeamsConnectorSettings`
(each connector owns its client, the repo convention). It owns the
judgment-dense egress surface: fail-closed endpoint validation, the
client-credentials token flow (certificate-signed assertion OR shared secret),
per-request SSRF re-validation of every followed ``@odata.nextLink`` /
``deltaLink`` and every persisted delta cursor, adaptive throttling, and Graph's
retry / delta-expiry status semantics.

C2 (the security spine): the constructor validates BOTH config base URLs and
refuses to build under ``AIR_GAP`` BEFORE any I/O. The multi-cloud
``graph_allowed_hosts`` / ``login_allowed_hosts`` lists are consulted ONLY at
construction to decide which Microsoft sovereign cloud may be configured. Every
response-supplied link (and every persisted delta cursor, which re-enters the
same GET path) is re-validated against the FULL external policy and then pinned
to the SINGLE host of the configured ``graph_base_url`` — so a hostile Graph
response can neither downgrade to cleartext, smuggle credentials, nor redirect
the cached bearer token off-host, not even to a DIFFERENT valid Microsoft
sovereign cloud. This module is one of the ONLY files under ``ne/connect``
permitted to import httpx (locked by the C2 egress guardian test).

C12: the client secret / private key appear only in the token POST body or local
signing; token/API failures raise :class:`GraphSyncError` with the STATUS (and
host) only — never a body, secret, or URL.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ne.connect.endpoints import AirGapViolationError, UnsafeEndpointError, validate_source_url
from ne.connect.errors import DeltaTokenExpiredError, GraphSyncError
from ne.connect.m365.auth import build_client_assertion
from ne.connect.teams.config import TeamsConnectorSettings
from ne.connect.throttle import AdaptiveThrottle
from ne.net.airgap import air_gap_engaged
from ne.net.ssrf import normalize_host

#: Re-exported transport alias so the connector/sync modules can type their
#: injectable transport WITHOUT importing httpx — keeping teams/graph.py a locked
#: httpx import site under ``ne/connect`` (C2 egress guardian test).
type Transport = httpx.AsyncBaseTransport
#: The concrete client type :meth:`TeamsGraphClient.session` yields.
type Client = httpx.AsyncClient

__all__ = ["Client", "TeamsGraphClient", "Transport"]

_RETRYABLE = frozenset({429, 503, 504})
_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class TeamsGraphClient:
    """App-only Graph client for Teams: token flow, SSRF-guarded paginated GET, retries."""

    def __init__(
        self,
        settings: TeamsConnectorSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # (a) Fail-closed BEFORE any I/O — Graph is egress.
        if air_gap_engaged():
            raise AirGapViolationError(
                "AIR_GAP engaged: the Teams connector is egress and is not permitted"
            )
        # (b) Validate both base URLs (https / no IP / no packed-IPv4 / no metadata).
        graph_base = validate_source_url(settings.graph_base_url, allow_external=True)
        login_base = validate_source_url(settings.login_base_url, allow_external=True)
        # (c) Close the external allowlist gap (validate_source_url has none).
        self._graph_hosts = frozenset(settings.graph_allowed_hosts)
        self._login_hosts = frozenset(settings.login_allowed_hosts)
        graph_host = self._require_host(graph_base, self._graph_hosts, "graph_base_url")
        self._require_host(login_base, self._login_hosts, "login_base_url")

        self._graph = graph_base
        self._settings = settings
        self._transport = transport
        # Pin every FOLLOWED Graph link to the configured cloud's single host.
        self._graph_host = graph_host
        self._scope = f"https://{graph_host}/.default"
        self._token_url = f"{login_base}/{settings.tenant_id}/oauth2/v2.0/token"
        self._throttle = AdaptiveThrottle(settings.requests_per_second, settings.burst)
        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def graph_base(self) -> str:
        return self._graph

    @asynccontextmanager
    async def session(self) -> AsyncIterator[httpx.AsyncClient]:
        """Open ONE httpx client for a crawl (timeout + injected transport)."""
        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds, transport=self._transport
        ) as client:
            yield client

    @staticmethod
    def _require_host(url: str, allowed: frozenset[str], field: str) -> str:
        host = normalize_host(urlparse(url).hostname or "")
        if host not in allowed:
            raise UnsafeEndpointError(f"{field} host {host!r} is not in the configured allowlist")
        return host

    def _guard_graph_url(self, url: str) -> None:
        """Full-policy SSRF re-guard for EVERY Graph URL we are about to fetch.

        Runs on every response-supplied ``@odata.nextLink`` / ``deltaLink`` and
        on every persisted delta cursor (which re-enters via ``get_json``). Only
        the reconstructed ``scheme://netloc`` is validated through the FULL
        external policy — https-only, no userinfo, no IP literal / packed-IPv4 /
        cloud-metadata host — then the host is pinned to the SINGLE host of the
        configured ``graph_base_url`` (the active cloud). A link to a DIFFERENT
        valid Microsoft sovereign cloud is refused here before any token-bearing
        request.
        """
        parsed = urlparse(url)
        try:
            validate_source_url(f"{parsed.scheme}://{parsed.netloc}", allow_external=True)
        except UnsafeEndpointError as exc:
            # validate_source_url names only the host (C12-safe) — NEVER the url.
            raise GraphSyncError(f"refusing to follow unsafe Graph link: {exc}") from exc
        host = normalize_host(parsed.hostname or "")
        if host != self._graph_host:
            raise GraphSyncError(
                f"refusing to follow Graph link to host {host!r} outside the configured Graph cloud"
            )

    # -- token flow ----------------------------------------------------------
    async def _get_token(self, client: httpx.AsyncClient) -> str:
        now = time.monotonic()
        if self._token is not None and now < self._token_expires_at:
            return self._token
        settings = self._settings
        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": settings.client_id,
            "scope": self._scope,
        }
        if settings.auth_method == "certificate":
            assert settings.certificate_pem_path is not None  # validated at construction
            assert settings.private_key_pem_path is not None
            assertion = build_client_assertion(
                settings.client_id,
                self._token_url,
                Path(settings.certificate_pem_path).read_bytes(),
                Path(settings.private_key_pem_path).read_bytes(),
            )
            data["client_assertion_type"] = _ASSERTION_TYPE
            data["client_assertion"] = assertion
        else:
            data["client_secret"] = settings.client_secret.get_secret_value()

        max_attempts = self._settings.max_attempts
        cap = self._settings.retry_after_cap_seconds
        for attempt in range(1, max_attempts + 1):
            resp = await client.post(self._token_url, data=data)
            status = resp.status_code
            if status in _RETRYABLE:
                if attempt == max_attempts:
                    raise GraphSyncError(
                        f"Graph token endpoint returned HTTP {status} after {attempt} attempts",
                        status=status,
                    )
                await self._throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                continue
            if status >= 400:
                # STATUS ONLY — never the body/secret/URL (C12).
                raise GraphSyncError(f"Graph token endpoint returned HTTP {status}", status=status)
            payload = resp.json()
            self._token = str(payload["access_token"])
            self._token_expires_at = now + max(int(payload.get("expires_in", 3600)) - 60, 0)
            return self._token
        raise GraphSyncError("exhausted token retries")  # pragma: no cover

    # -- HTTP plumbing -------------------------------------------------------
    async def get_json(self, client: httpx.AsyncClient, url: str) -> dict[str, Any]:
        """GET a Graph URL: SSRF-guarded, throttled, retried, delta-aware."""
        self._guard_graph_url(url)  # MUST stay first: a hostile link never triggers a token POST.
        token = await self._get_token(client)
        max_attempts = self._settings.max_attempts
        cap = self._settings.retry_after_cap_seconds
        for attempt in range(1, max_attempts + 1):
            await self._throttle.acquire()
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            status = resp.status_code
            if status in _RETRYABLE:
                if attempt == max_attempts:
                    raise GraphSyncError(
                        f"Graph returned HTTP {status} after {attempt} attempts "
                        f"(host {normalize_host(urlparse(url).hostname or '')!r})",
                        status=status,
                    )
                await self._throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                continue
            if status == 410:
                # Delta token expired — do NOT follow Location; re-derive full crawl.
                raise DeltaTokenExpiredError(
                    f"Graph deltaLink expired (HTTP 410) for host "
                    f"{normalize_host(urlparse(url).hostname or '')!r}"
                )
            if status >= 400:
                raise GraphSyncError(
                    f"Graph returned HTTP {status} "
                    f"(host {normalize_host(urlparse(url).hostname or '')!r})",
                    status=status,
                )
            return resp.json()
        raise GraphSyncError("exhausted Graph retries")  # pragma: no cover

    async def get_paginated(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Follow ``@odata.nextLink`` pages; return ``(items, deltaLink | None)``.

        Every followed link is re-guarded by :meth:`get_json`, so a hostile
        ``nextLink`` / ``deltaLink`` pointing off-host aborts the crawl instead of
        redirecting it (response-injected-link SSRF closed).
        """
        items: list[dict[str, Any]] = []
        delta_link: str | None = None
        next_url: str | None = url
        while next_url:
            data = await self.get_json(client, next_url)
            items.extend(data.get("value", []))
            delta = data.get("@odata.deltaLink")
            if delta:
                delta_link = delta
            next_url = data.get("@odata.nextLink")
        return items, delta_link

    # -- Teams endpoints -----------------------------------------------------
    async def list_channels(self, client: httpx.AsyncClient, team_id: str) -> list[dict[str, Any]]:
        """All channels of a team (``GET /teams/{id}/channels``, paginated)."""
        channels, _ = await self.get_paginated(client, f"{self._graph}/teams/{team_id}/channels")
        return channels

    async def channel_messages_delta(
        self,
        client: httpx.AsyncClient,
        team_id: str,
        channel_id: str,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One delta crawl of a channel's messages; return ``(messages, deltaLink)``.

        ``cursor`` None starts a full delta at
        ``/teams/{tid}/channels/{cid}/messages/delta``; otherwise the persisted
        (re-validated + host-pinned by :meth:`get_json`) delta cursor URL is GET.
        """
        if cursor is None:
            url = f"{self._graph}/teams/{team_id}/channels/{channel_id}/messages/delta"
        else:
            url = cursor
        return await self.get_paginated(client, url)

    async def message_replies(
        self,
        client: httpx.AsyncClient,
        team_id: str,
        channel_id: str,
        message_id: str,
    ) -> list[dict[str, Any]]:
        """All replies to a channel message (paginated)."""
        replies, _ = await self.get_paginated(
            client,
            f"{self._graph}/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies",
        )
        return replies
