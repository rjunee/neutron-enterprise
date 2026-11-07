# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin Microsoft Graph HTTP client (NE-044) — the SOLE httpx site in ne.connect.

:class:`GraphClient` is a deliberately small app-only Graph client (NO
msgraph-sdk — NE-042 ``entra.py`` precedent). It owns exactly the judgment-dense
surface: fail-closed endpoint validation, the client-credentials token flow
(certificate-signed assertion OR shared secret), per-request SSRF re-validation
of every followed ``@odata.nextLink`` / ``deltaLink`` / persisted delta cursor,
adaptive throttling, and Graph's retry / delta-expiry status semantics.

C2 (the security spine): the constructor validates BOTH config base URLs and
refuses to build under ``AIR_GAP`` BEFORE any I/O. The multi-cloud
``graph_allowed_hosts`` / ``login_allowed_hosts`` lists are consulted ONLY at
construction time to decide which Microsoft sovereign cloud may be configured
(national-cloud support). Every response-supplied link (and every persisted delta
cursor, which re-enters through the same GET path) is re-validated against the
FULL external policy — https-only scheme, no embedded userinfo, no IP literal /
packed-IPv4 / cloud-metadata host — reusing ``validate_source_url`` on the
reconstructed ``scheme://netloc`` (the token-bearing query is stripped first),
and is then pinned to the SINGLE host of the configured ``graph_base_url`` (the
active cloud). So a hostile Graph response can neither downgrade the crawl to
cleartext, smuggle credentials, nor redirect the cached bearer token off-host —
not even to a DIFFERENT valid Microsoft sovereign cloud. This module is the ONLY
file under ``ne/connect`` permitted to import httpx (locked by the C2 egress
guardian test).

File content is fetched from the canonical, locally-derived Graph
``/drives/{drive}/items/{item}/content`` endpoint. Graph normally answers with a
short-lived pre-authenticated redirect. That URL is treated as hostile input: its
origin is revalidated against the full SSRF policy and an exact operator
allowlist, the Graph bearer is stripped before following it, redirect count and
response bytes are bounded, and neither the URL nor response body appears in an
exception. The response's ``@microsoft.graph.downloadUrl`` field is never used.

C12: the client secret / private key appear only in the token POST body or local
signing; token failures raise :class:`GraphSyncError` with the STATUS (and host)
only — never a body, secret, or URL.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from ne.connect.endpoints import AirGapViolationError, UnsafeEndpointError, validate_source_url
from ne.connect.errors import (
    DeltaTokenExpiredError,
    GraphContentError,
    GraphForbiddenError,
    GraphRateLimitedError,
    GraphRedirectError,
    GraphResponseParseError,
    GraphSyncError,
    GraphTransportError,
    GraphUnauthorizedError,
    GraphUpstreamError,
)
from ne.connect.m365.auth import build_client_assertion
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.throttle import AdaptiveThrottle
from ne.net.airgap import air_gap_engaged
from ne.net.ssrf import normalize_host

#: Re-exported transport alias so the connector/sync modules can type their
#: injectable transport WITHOUT importing httpx — keeping graph.py the sole httpx
#: import site under ``ne/connect`` (locked by the C2 egress guardian test).
type Transport = httpx.AsyncBaseTransport
#: The concrete client type :meth:`GraphClient.session` yields — re-exported so
#: callers can type it without importing httpx themselves.
type Client = httpx.AsyncClient

__all__ = ["Client", "GraphClient", "GraphPage", "Transport"]


@dataclass(frozen=True)
class GraphPage:
    """Exactly one guarded Graph collection page and its continuation links.

    ``next_link`` is the resumable page cursor a bounded permission sweep
    persists: restarting mid-rotation re-requests that exact page instead of
    starting the collection again, which is what stops a rotating sweep from
    starving its tail. ``next_link`` and ``delta_link`` are mutually exclusive
    by Graph's own contract, and both are re-guarded before they are returned.
    """

    items: tuple[dict[str, Any], ...]
    next_link: str | None
    delta_link: str | None


_RETRYABLE = frozenset({429, 503, 504})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
_MAX_TOKEN_TTL_SECONDS = 86_400


def _http_error(message: str, *, status: int) -> GraphSyncError:
    """Return the closed, nonsecret diagnostic class for one HTTP failure.

    The exception message remains the existing status/host-only text. Only the
    class name is projected into connector/tool audit, so the diagnostic surface
    cannot expose a URL, response body, provider message, header, or credential.
    """
    error_type: type[GraphSyncError]
    if status == 401:
        error_type = GraphUnauthorizedError
    elif status == 403:
        error_type = GraphForbiddenError
    elif status == 429:
        error_type = GraphRateLimitedError
    elif 500 <= status < 600:
        error_type = GraphUpstreamError
    else:
        error_type = GraphSyncError
    return error_type(message, status=status)


def _redirect_error(context: str, *, status: int) -> GraphRedirectError:
    """Return a status-only refusal for an unexpected HTTP redirect."""
    return GraphRedirectError(f"{context} returned HTTP redirect {status}", status=status)


class GraphClient:
    """App-only Graph client: token flow, SSRF-guarded paginated GET, retries."""

    def __init__(
        self,
        settings: M365ConnectorSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # (a) Fail-closed BEFORE any I/O — Graph is egress.
        if air_gap_engaged():
            raise AirGapViolationError(
                "AIR_GAP engaged: the M365/SharePoint connector is egress and is not permitted"
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
        # Pin every FOLLOWED Graph link to the configured cloud's single host: the
        # multi-cloud allowlist above is construction-time-only; response-supplied
        # links must match this exact host so the token stays in the active cloud.
        self._graph_host = graph_host
        self._content_hosts = frozenset(
            {graph_host, *self._validate_content_hosts(settings.content_allowed_hosts)}
        )
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
        """Open ONE httpx client for a crawl (timeout + injected transport).

        Centralizing client construction here keeps graph.py the sole httpx site;
        callers pass the yielded client back into :meth:`get_paginated`.
        """
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

    @staticmethod
    def _validate_content_hosts(hosts: tuple[str, ...]) -> frozenset[str]:
        """Validate the exact cross-host redirect allowlist at construction.

        Values are hostnames, not URLs. Building a query-free HTTPS origin lets
        the shared external validator reject IP literals, packed IPv4, metadata
        hosts, embedded userinfo, and malformed ports before any crawl starts.
        """
        validated: set[str] = set()
        for raw in hosts:
            if not raw or any(char in raw for char in "/?#@"):
                raise UnsafeEndpointError(
                    "content_allowed_hosts entries must be bare DNS hostnames"
                )
            origin = validate_source_url(f"https://{raw}", allow_external=True)
            parsed = urlparse(origin)
            try:
                port = parsed.port
            except ValueError:
                raise UnsafeEndpointError(
                    "content_allowed_hosts entries must be bare DNS hostnames"
                ) from None
            if port is not None:
                raise UnsafeEndpointError(
                    "content_allowed_hosts entries must be bare DNS hostnames"
                )
            validated.add(normalize_host(parsed.hostname or ""))
        return frozenset(validated)

    def _guard_graph_url(self, url: str) -> None:
        """Full-policy SSRF re-guard for EVERY Graph URL we are about to fetch.

        Runs on every response-supplied ``@odata.nextLink`` / ``deltaLink`` and on
        every persisted delta cursor (which re-enters via ``get_json``). The delta
        token legitimately lives in the query string, so we re-validate only the
        reconstructed ``scheme://netloc`` (netloc retains userinfo + port) through
        the FULL external policy — https-only, no userinfo, malformed port, no IP
        literal / packed-IPv4 / cloud-metadata host — then pin the host to the
        SINGLE host of the configured ``graph_base_url`` (the active cloud). The
        multi-cloud ``graph_allowed_hosts`` list is construction-time-only, so a
        link to a DIFFERENT valid Microsoft sovereign cloud is refused here before
        any token-bearing request. A hostile link can neither downgrade to
        cleartext, smuggle credentials, nor redirect the token off the configured
        cloud's host.
        """
        parsed = urlparse(url)
        try:
            validate_source_url(f"{parsed.scheme}://{parsed.netloc}", allow_external=True)
        except UnsafeEndpointError as exc:
            # validate_source_url names only the host (C12-safe) — NEVER the url.
            raise GraphRedirectError(f"refusing to follow unsafe Graph link: {exc}") from exc
        host = normalize_host(parsed.hostname or "")
        if host != self._graph_host:
            raise GraphRedirectError(
                f"refusing to follow Graph link to host {host!r} outside the configured Graph cloud"
            )

    def _guard_content_redirect(self, url: str) -> None:
        """Revalidate one response-supplied content redirect without exposing it."""
        parsed = urlparse(url)
        try:
            validate_source_url(f"{parsed.scheme}://{parsed.netloc}", allow_external=True)
            port = parsed.port
        except (UnsafeEndpointError, ValueError) as exc:
            raise GraphRedirectError("refusing an unsafe Graph content redirect") from exc
        host = normalize_host(parsed.hostname or "")
        if port not in {None, 443} or host not in self._content_hosts:
            raise GraphRedirectError(
                f"refusing Graph content redirect to host {host!r} outside the configured allowlist"
            )

    @staticmethod
    def _json_object(response: httpx.Response, *, context: str) -> dict[str, Any]:
        """Parse a bounded Graph JSON envelope without projecting provider text."""
        try:
            payload = response.json()
        except (RecursionError, TypeError, ValueError):
            raise GraphResponseParseError(f"{context} returned an invalid JSON object") from None
        if not isinstance(payload, dict):
            raise GraphResponseParseError(f"{context} returned an invalid JSON object")
        return payload

    # -- token flow ----------------------------------------------------------
    async def _get_token(self, client: httpx.AsyncClient) -> str:
        now = time.monotonic()
        if self._token is not None and now < self._token_expires_at:
            return self._token
        settings = self._settings
        # The client assertion is rebuilt fresh per refresh (fresh jti/exp), never
        # cached — kept inside the refresh path.
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
            try:
                resp = await client.post(self._token_url, data=data)
            except httpx.HTTPError:
                raise GraphTransportError("Graph token transport failed") from None
            status = resp.status_code
            if 300 <= status < 400:
                # Authentication redirects are never followed and their Location
                # is never inspected: neither credentials nor provider input may
                # redirect the token exchange.
                raise _redirect_error("Graph token endpoint", status=status)
            if status in _RETRYABLE:
                if attempt == max_attempts:
                    raise _http_error(
                        f"Graph token endpoint returned HTTP {status} after {attempt} attempts",
                        status=status,
                    )
                await self._throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                continue
            if status >= 400:
                # STATUS ONLY — never the body/secret/URL (C12).
                raise _http_error(f"Graph token endpoint returned HTTP {status}", status=status)
            payload = self._json_object(resp, context="Graph token endpoint")
            access_token = payload.get("access_token")
            if (
                not isinstance(access_token, str)
                or not access_token.strip()
                or access_token != access_token.strip()
            ):
                raise GraphResponseParseError(
                    "Graph token endpoint returned an invalid JSON object"
                )
            expires_in = payload.get("expires_in", 3600)
            if (
                not isinstance(expires_in, int)
                or isinstance(expires_in, bool)
                or not 1 <= expires_in <= _MAX_TOKEN_TTL_SECONDS
            ):
                raise GraphResponseParseError(
                    "Graph token endpoint returned an invalid JSON object"
                )
            self._token = access_token
            self._token_expires_at = now + max(expires_in - 60, 0)
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
            try:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError:
                raise GraphTransportError("Graph request transport failed") from None
            status = resp.status_code
            if 300 <= status < 400:
                # Graph collection/control requests do not have an admitted
                # redirect protocol. Refuse before parsing any response JSON so
                # a redirect-shaped payload cannot supply a cursor.
                raise _redirect_error("Graph endpoint", status=status)
            if status in _RETRYABLE:
                if attempt == max_attempts:
                    raise _http_error(
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
                raise _http_error(
                    f"Graph returned HTTP {status} "
                    f"(host {normalize_host(urlparse(url).hostname or '')!r})",
                    status=status,
                )
            return self._json_object(resp, context="Graph endpoint")
        raise GraphSyncError("exhausted Graph retries")  # pragma: no cover

    async def post_accepted(
        self, client: httpx.AsyncClient, url: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        """POST to a Graph action endpoint whose success is an EMPTY 2xx (e.g. 202).

        An exact structural mirror of :meth:`post_json` — SSRF-re-guard FIRST,
        then token, throttle, the same ``_RETRYABLE`` retry loop, the same
        redirect refusal, the same STATUS-ONLY errors (C12) — differing only in
        the success shape: Graph *action* endpoints (``.../messages/{id}/send``)
        attest success with ``202 Accepted`` and NO body, so any 2xx returns
        ``None`` and the body is never parsed.
        """
        self._guard_graph_url(url)  # MUST stay first: a hostile link never triggers a token POST.
        token = await self._get_token(client)
        max_attempts = self._settings.max_attempts
        cap = self._settings.retry_after_cap_seconds
        for attempt in range(1, max_attempts + 1):
            await self._throttle.acquire()
            try:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json=dict(payload) if payload is not None else {},
                )
            except httpx.HTTPError:
                raise GraphTransportError("Graph request transport failed") from None
            status = resp.status_code
            if 300 <= status < 400:
                raise _redirect_error("Graph endpoint", status=status)
            if status in _RETRYABLE:
                if attempt == max_attempts:
                    raise _http_error(
                        f"Graph returned HTTP {status} after {attempt} attempts "
                        f"(host {normalize_host(urlparse(url).hostname or '')!r})",
                        status=status,
                    )
                await self._throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                continue
            if status >= 400:
                raise _http_error(
                    f"Graph returned HTTP {status} "
                    f"(host {normalize_host(urlparse(url).hostname or '')!r})",
                    status=status,
                )
            return None
        raise GraphSyncError("exhausted Graph retries")  # pragma: no cover

    async def get_content(self, client: httpx.AsyncClient, url: str, *, max_bytes: int) -> bytes:
        """Fetch bounded bytes from a canonical Graph ``/content`` endpoint.

        ``url`` must be locally derived from :attr:`graph_base`; it is guarded
        before token acquisition. Redirects are followed manually so every
        response-supplied origin is SSRF-revalidated and the Graph bearer is
        never forwarded off the configured Graph host. Error messages carry at
        most a status/host, never a response body or credential-bearing URL.
        """
        self._guard_graph_url(url)  # MUST remain before token acquisition.
        token = await self._get_token(client)
        current_url = url
        graph_request = True
        redirects = 0
        attempt = 1

        while True:
            headers = {"Authorization": f"Bearer {token}"} if graph_request else {}
            try:
                await self._throttle.acquire()
                request = client.build_request("GET", current_url, headers=headers)
                response = await client.send(request, stream=True)
            except httpx.HTTPError:
                raise GraphTransportError("Graph content transport failed") from None

            status = response.status_code
            if status in _RETRYABLE:
                retry_after = response.headers.get("Retry-After")
                await response.aclose()
                if attempt == self._settings.max_attempts:
                    raise _http_error(
                        f"Graph content endpoint returned HTTP {status} after {attempt} attempts",
                        status=status,
                    )
                await self._throttle.wait_retry(
                    attempt, retry_after, self._settings.retry_after_cap_seconds
                )
                attempt += 1
                continue

            if status in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                await response.aclose()
                if location is None:
                    raise GraphRedirectError(
                        f"Graph content endpoint returned HTTP {status} without a redirect target",
                        status=status,
                    )
                redirects += 1
                if redirects > self._settings.max_content_redirects:
                    raise GraphRedirectError("Graph content redirect limit exceeded")
                target = urljoin(current_url, location)
                self._guard_content_redirect(target)
                current_url = target
                graph_request = False  # never forward the Graph bearer to a redirect host
                attempt = 1
                continue

            if 300 <= status < 400:
                await response.aclose()
                raise GraphRedirectError(
                    f"Graph content endpoint returned unsupported HTTP redirect {status}",
                    status=status,
                )

            if status >= 400:
                host = normalize_host(urlparse(current_url).hostname or "")
                await response.aclose()
                raise _http_error(
                    f"Graph content endpoint returned HTTP {status} (host {host!r})",
                    status=status,
                )

            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError:
                    await response.aclose()
                    raise GraphContentError(
                        "Graph content endpoint returned an invalid Content-Length"
                    ) from None
                if declared_size < 0 or declared_size > max_bytes:
                    await response.aclose()
                    raise GraphContentError("Graph content exceeded the configured byte limit")

            chunks: list[bytes] = []
            received = 0
            try:
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise GraphContentError("Graph content exceeded the configured byte limit")
                    chunks.append(chunk)
            except httpx.HTTPError:
                raise GraphTransportError("Graph content transport failed") from None
            finally:
                await response.aclose()
            return b"".join(chunks)

    async def post_json(
        self, client: httpx.AsyncClient, url: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """POST JSON to a Graph URL: SSRF-guarded, throttled, retried (NE-050 write seam).

        An exact structural mirror of :meth:`get_json` for the write path: the URL is
        SSRF-re-guarded FIRST (a hostile URL never triggers a token POST), then the
        token flow, throttle, and the same ``_RETRYABLE`` retry loop with the
        Retry-After cap run. Any status >= 400 raises a STATUS-ONLY
        :class:`GraphSyncError` (never a body / secret / URL — C12). There is no
        410-delta branch: a create POST carries no delta cursor. A 2xx (incl. 201
        Created) returns the parsed JSON body.
        """
        self._guard_graph_url(url)  # MUST stay first: a hostile link never triggers a token POST.
        token = await self._get_token(client)
        max_attempts = self._settings.max_attempts
        cap = self._settings.retry_after_cap_seconds
        for attempt in range(1, max_attempts + 1):
            await self._throttle.acquire()
            try:
                resp = await client.post(
                    url, headers={"Authorization": f"Bearer {token}"}, json=dict(payload)
                )
            except httpx.HTTPError:
                raise GraphTransportError("Graph request transport failed") from None
            status = resp.status_code
            if 300 <= status < 400:
                # Write redirects are not an admitted Graph operation and could
                # transfer the bearer or payload to a response-selected target.
                raise _redirect_error("Graph endpoint", status=status)
            if status in _RETRYABLE:
                if attempt == max_attempts:
                    raise _http_error(
                        f"Graph returned HTTP {status} after {attempt} attempts "
                        f"(host {normalize_host(urlparse(url).hostname or '')!r})",
                        status=status,
                    )
                await self._throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                continue
            if status >= 400:
                raise _http_error(
                    f"Graph returned HTTP {status} "
                    f"(host {normalize_host(urlparse(url).hostname or '')!r})",
                    status=status,
                )
            return self._json_object(resp, context="Graph endpoint")
        raise GraphSyncError("exhausted Graph retries")  # pragma: no cover

    async def get_paginated(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Follow ``@odata.nextLink`` pages; return ``(items, deltaLink | None)``.

        Every response-supplied link is re-guarded immediately, before it can be
        followed or returned for cursor persistence. :meth:`get_json` guards a
        ``nextLink`` again at request time, so both admission and use fail closed.
        """
        items: list[dict[str, Any]] = []
        delta_link: str | None = None
        next_url: str | None = url
        while next_url:
            page = await self.get_page(client, next_url)
            items.extend(page.items)
            if page.delta_link is not None:
                delta_link = page.delta_link
            next_url = page.next_link
        return items, delta_link

    async def get_page(self, client: httpx.AsyncClient, url: str) -> GraphPage:
        """Fetch ONE guarded Graph collection page and its continuation links.

        This is the single place a page is parsed and its links are guarded;
        :meth:`get_paginated` is a loop over it. A bounded sweep that must stop
        after N pages and resume at the exact next page therefore shares the same
        guarded parse as the unbounded crawl rather than reimplementing it.
        """
        data = await self.get_json(client, url)
        page = data.get("value")
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise GraphResponseParseError("Graph page returned an invalid value collection")

        next_link = data.get("@odata.nextLink")
        delta = data.get("@odata.deltaLink")
        for key, link in (
            ("@odata.nextLink", next_link),
            ("@odata.deltaLink", delta),
        ):
            if key in data and (not isinstance(link, str) or not link.strip()):
                raise GraphResponseParseError(f"Graph page returned an invalid {key}")
        if next_link is not None and delta is not None:
            raise GraphResponseParseError("Graph page returned ambiguous continuation links")
        if next_link is not None:
            self._guard_graph_url(next_link)
        if delta is not None:
            self._guard_graph_url(delta)
        return GraphPage(items=tuple(page), next_link=next_link, delta_link=delta)
