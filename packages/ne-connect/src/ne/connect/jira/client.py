# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin Jira Cloud REST v3 / Agile 1.0 HTTP client (NE-093) — a C2-guarded httpx site.

:class:`JiraClient` is a deliberately small Basic-auth client (NO
``atlassian-python-api`` — see the build-vs-adopt rationale in the unit's commit
message / package NOTICE: it is requests-based/synchronous and would bypass both
the C2 egress guardian and the mandated ``httpx.MockTransport`` fixture-first CI,
the same call NE-045 made for ``slack_sdk`` and NE-047 for ``simple-salesforce``).

C2 (the security spine): the constructor validates the configured base URL and
refuses to build under ``AIR_GAP`` BEFORE any I/O. The validated base is pinned
to the configured ``allowed_hosts`` (dot-suffix patterns for
``*.atlassian.net``-shaped entries, exact match otherwise). This client NEVER
follows a response-supplied URL: the Jira ``nextPageToken`` is an opaque QUERY
PARAM we attach to our OWN pinned base, and :meth:`_guard_path` refuses any path
not starting with ``/rest/`` BEFORE the request is sent — so there is no
response-injected-link SSRF surface.

C12: the API token never leaves a ``SecretStr``; the Basic ``Authorization``
header is computed per-request; failures raise :class:`JiraSyncError` with the
STATUS / host ONLY — never a token, the JQL text, a response body, or a full URL
(which may embed a ``nextPageToken`` / JQL).
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
from ne.connect.errors import JiraSyncError
from ne.connect.jira.config import JiraConnectorSettings
from ne.connect.throttle import AdaptiveThrottle
from ne.net.airgap import air_gap_engaged
from ne.net.ssrf import normalize_host

#: Re-exported transport alias so the connector/sync modules can type their
#: injectable transport WITHOUT importing httpx — keeping client.py one of the
#: locked httpx import sites under ``ne/connect`` (C2 egress guardian test).
type Transport = httpx.AsyncBaseTransport
#: The concrete client type :meth:`JiraClient.session` yields.
type Client = httpx.AsyncClient

__all__ = ["Client", "JiraClient", "Transport"]

# Transient statuses that warrant a retry: 429 rate-limit + the common transient
# 5xx (500 / 502 / 503 / 504).
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
#: A Jira issue key, e.g. ``ACME-1`` — validated before path interpolation.
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    """True iff ``host`` matches one of ``allowed_hosts`` (dot-suffix or exact)."""
    for entry in allowed_hosts:
        if entry.startswith("."):
            if host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


class JiraClient:
    """Jira Cloud REST v3 / Agile 1.0 client: SSRF-guarded, throttled, retried."""

    def __init__(
        self,
        settings: JiraConnectorSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # (a) Fail-closed BEFORE any I/O — Jira Cloud is egress.
        if air_gap_engaged():
            raise AirGapViolationError(
                "AIR_GAP engaged: the Jira connector is egress and is not permitted"
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
        """Refuse anything that is not a relative ``/rest/...`` path.

        Runs BEFORE every call. An absolute URL or an off-prefix path fails this
        check outright — closing any response-injected-link SSRF surface.
        """
        if not path.startswith("/rest/"):
            raise JiraSyncError("refusing to call a Jira path outside '/rest/'")

    def _auth_header(self) -> str:
        creds = f"{self._settings.email}:{self._settings.api_token.get_secret_value()}"
        return "Basic " + base64.b64encode(creds.encode("utf-8")).decode("ascii")

    # -- HTTP plumbing -------------------------------------------------------
    async def _call(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
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
                    raise JiraSyncError(
                        f"Jira API returned HTTP {status} after {attempt} attempts",
                        status=status,
                    )
                await self._throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                continue
            if status >= 400:
                # STATUS ONLY — never the body/token/URL (C12).
                raise JiraSyncError(f"Jira API returned HTTP {status}", status=status)
            return resp
        raise JiraSyncError("exhausted Jira retries")  # pragma: no cover

    async def get_json(
        self, client: httpx.AsyncClient, path: str, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        resp = await self._call(client, path, params)
        return resp.json()

    # -- issue search (enhanced JQL, nextPageToken pagination) ---------------
    async def search_jql(
        self, client: httpx.AsyncClient, jql: str, fields: list[str]
    ) -> list[dict[str, Any]]:
        """GET ``/rest/api/3/search/jql`` following ``nextPageToken`` to exhaustion.

        The legacy offset-paginated ``/rest/api/3/search`` has been
        deprecated/removed by Atlassian; this uses the enhanced endpoint. The
        ``nextPageToken`` is an OPAQUE query param on our OWN pinned base — never
        a followed URL. Pagination stops when the response reports ``isLast`` or
        omits ``nextPageToken``.
        """
        path = "/rest/api/3/search/jql"
        issues: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            params: dict[str, str] = {
                "jql": jql,
                "maxResults": str(self._settings.page_size),
                "fields": ",".join(fields),
            }
            if next_token:
                params["nextPageToken"] = next_token
            payload = await self.get_json(client, path, params)
            page = payload.get("issues")
            if isinstance(page, list):
                issues.extend(page)
            if payload.get("isLast"):
                break
            token = payload.get("nextPageToken")
            if not isinstance(token, str) or not token:
                break
            next_token = token
        return issues

    async def issue_comments(
        self, client: httpx.AsyncClient, issue_key: str
    ) -> list[dict[str, Any]]:
        """GET ``/rest/api/3/issue/{key}/comment`` (startAt-paginated)."""
        if not _ISSUE_KEY_RE.match(issue_key):
            raise JiraSyncError("refusing to use a malformed Jira issue key")
        path = f"/rest/api/3/issue/{issue_key}/comment"
        comments: list[dict[str, Any]] = []
        start_at = 0
        while True:
            payload = await self.get_json(
                client,
                path,
                {"startAt": str(start_at), "maxResults": str(self._settings.page_size)},
            )
            page = payload.get("comments")
            if isinstance(page, list):
                comments.extend(page)
            total = payload.get("total")
            got = len(comments)
            if not isinstance(page, list) or not page:
                break
            if isinstance(total, int) and got >= total:
                break
            start_at = got
        return comments

    async def project(self, client: httpx.AsyncClient, key: str) -> dict[str, Any]:
        return await self.get_json(client, f"/rest/api/3/project/{key}")

    async def project_permission_scheme(
        self, client: httpx.AsyncClient, key: str
    ) -> dict[str, Any]:
        return await self.get_json(
            client, f"/rest/api/3/project/{key}/permissionscheme", {"expand": "permissions"}
        )

    async def boards(self, client: httpx.AsyncClient, project_key: str) -> list[dict[str, Any]]:
        """GET ``/rest/agile/1.0/board`` (startAt-paginated) for a project."""
        path = "/rest/agile/1.0/board"
        result: list[dict[str, Any]] = []
        start_at = 0
        while True:
            payload = await self.get_json(
                client,
                path,
                {
                    "projectKeyOrId": project_key,
                    "startAt": str(start_at),
                    "maxResults": str(self._settings.page_size),
                },
            )
            page = payload.get("values")
            if isinstance(page, list):
                result.extend(page)
            if payload.get("isLast") or not isinstance(page, list) or not page:
                break
            start_at = len(result)
        return result

    async def sprints(self, client: httpx.AsyncClient, board_id: int) -> list[dict[str, Any]]:
        """GET ``/rest/agile/1.0/board/{board_id}/sprint`` (startAt-paginated).

        ``board_id`` comes from our own :meth:`boards` response and is
        int-validated before path interpolation.
        """
        board = int(board_id)
        path = f"/rest/agile/1.0/board/{board}/sprint"
        result: list[dict[str, Any]] = []
        start_at = 0
        while True:
            payload = await self.get_json(
                client,
                path,
                {"startAt": str(start_at), "maxResults": str(self._settings.page_size)},
            )
            page = payload.get("values")
            if isinstance(page, list):
                result.extend(page)
            if payload.get("isLast") or not isinstance(page, list) or not page:
                break
            start_at = len(result)
        return result
