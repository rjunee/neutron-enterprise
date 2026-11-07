# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin Salesforce REST/Bulk API 2.0 HTTP client (NE-047) — the THIRD httpx site in ne.connect.

:class:`SalesforceClient` is a deliberately small connected-app OAuth client (NO
``simple-salesforce`` — see the build-vs-adopt rationale in the unit's commit
message: it is requests-based/synchronous and would bypass both the C2 egress
guardian and the mandated ``httpx.MockTransport`` fixture-first CI, the same
call NE-045 made for ``slack_sdk``). It owns the judgment-dense surface:
fail-closed endpoint validation of BOTH the instance and login hosts, two OAuth
flows (JWT-bearer / client-credentials), a path-prefix guard that closes the
Salesforce ``nextRecordsUrl``-is-a-response-supplied-link SSRF surface, adaptive
throttling, and Salesforce's retry / 401-refresh semantics.

C2 (the security spine): the constructor validates BOTH configured base URLs
and refuses to build under ``AIR_GAP`` BEFORE any I/O. ``instance_url`` is
pinned to the configured ``instance_allowed_hosts`` (dot-suffix patterns for
``*.my.salesforce.com``-shaped entries, exact match otherwise);
``login_base_url`` is pinned to ``login_allowed_hosts`` exactly. The ONE
response-supplied URL material this client ever follows — the REST
``nextRecordsUrl`` / the Bulk ``Sforce-Locator`` cursor — is never itself a full
URL: ``nextRecordsUrl`` is re-guarded by :meth:`_guard_path` (must start with
``/services/`` — an absolute URL or an off-prefix path is refused BEFORE any
token is even acquired) and resolved against OUR OWN pinned ``instance_base``;
the Bulk locator is an opaque query parameter on our own base (the same
pattern ``slack/client.py`` uses for Slack's pagination cursor). The
token-response ``instance_url`` field is deliberately never read — the
configured instance stays pinned for the client's whole lifetime.

C12: the client secret / private key never leave a ``SecretStr`` / on-disk PEM
path; failures raise :class:`SalesforceSyncError` with the STATUS / short
Salesforce error CODE / host ONLY — never a token, a SOQL query, a response
body, or a full URL (which may embed a query-locator).
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt

from ne.connect.endpoints import AirGapViolationError, UnsafeEndpointError, validate_source_url
from ne.connect.errors import SalesforceSyncError
from ne.connect.salesforce.config import SalesforceConnectorSettings
from ne.connect.salesforce.identity import (
    canonical_salesforce_organization_id,
    organization_id_from_identity_url,
)
from ne.connect.throttle import AdaptiveThrottle
from ne.net.airgap import air_gap_engaged
from ne.net.ssrf import normalize_host

#: Re-exported transport alias so the connector/sync modules can type their
#: injectable transport WITHOUT importing httpx — keeping client.py (alongside
#: m365/graph.py and slack/client.py) one of exactly three httpx import sites
#: under ``ne/connect`` (locked by the C2 egress guardian test).
type Transport = httpx.AsyncBaseTransport
#: The concrete client type :meth:`SalesforceClient.session` yields.
type Client = httpx.AsyncClient

__all__ = ["Client", "SalesforceClient", "Transport"]

# Transient statuses that warrant a retry (Codex P2): 429 rate-limit + the common
# transient 5xx (500 internal / 502 bad-gateway / 503 unavailable / 504 timeout).
# Salesforce/proxies emit these on transient faults — raising immediately would
# abort an otherwise-recoverable sync (fail-safe abort, but avoidable).
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
#: A Salesforce Bulk API 2.0 job id is a bare alphanumeric Salesforce record id
#: — validated before path interpolation (defense in depth; job ids never come
#: from free-text config, only from our own ``create_query_job`` response).
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9]+$")

_GRANT_TYPE_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def _instance_host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    """True iff ``host`` matches one of ``allowed_hosts`` (dot-suffix or exact).

    An entry starting with ``'.'`` is a dot-suffix pattern (``normalize_host(host)
    .endswith(entry)``) — e.g. ``.my.salesforce.com`` matches every org's My
    Domain host. Any other entry must match the host EXACTLY.
    """
    for entry in allowed_hosts:
        if entry.startswith("."):
            if host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


#: Salesforce reports BOTH "your daily API allowance is spent" and "you may not
#: do that" as HTTP 403, and this client deliberately discarded the body — so an
#: operator saw the identical string for a condition that clears by WAITING and
#: one that needs an access grant. Measured 2026-08-31 on the demo org: every
#: call returned `403` while the body said `REQUEST_LIMIT_EXCEEDED`, and nothing
#: in our own logs could have told anyone that.
#:
#: The body still does not leave this frame. What is appended is ONE token from
#: a CLOSED VOCABULARY — Salesforce's machine-readable `errorCode`, shape-checked
#: to a short SCREAMING_SNAKE identifier and length-capped. Provider PROSE
#: (`message`) is never surfaced: it is provider-authored text on an egress
#: boundary, which is exactly what the stage-name guard refuses elsewhere. An
#: unrecognised shape yields nothing rather than a sanitised guess.
_MAX_ERROR_CODE = 48


def _error_code_suffix(resp: httpx.Response) -> str:
    """`" (REQUEST_LIMIT_EXCEEDED)"` when the body carries a well-shaped code."""
    try:
        body = resp.json()
    except Exception:  # a non-JSON error body is not itself an error here
        return ""
    if isinstance(body, list) and body:
        body = body[0]
    if not isinstance(body, dict):
        return ""
    code = body.get("errorCode")
    if not isinstance(code, str):
        return ""
    if not 0 < len(code) <= _MAX_ERROR_CODE:
        return ""
    if not all(ch.isascii() and (ch.isupper() or ch.isdigit() or ch == "_") for ch in code):
        return ""
    return f" ({code})"


class SalesforceClient:
    """Salesforce REST/Bulk API 2.0 client: SSRF-guarded, throttled, retried."""

    def __init__(
        self,
        settings: SalesforceConnectorSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # (a) Fail-closed BEFORE any I/O — Salesforce is egress.
        if air_gap_engaged():
            raise AirGapViolationError(
                "AIR_GAP engaged: the Salesforce connector is egress and is not permitted"
            )
        # (b) Validate BOTH base URLs (https / no IP / no packed-IPv4 / no metadata).
        instance_base = validate_source_url(settings.instance_url, allow_external=True)
        login_base = validate_source_url(settings.login_base_url, allow_external=True)

        # (c) Close the external allowlist gap (validate_source_url has none).
        instance_host = normalize_host(urlparse(instance_base).hostname or "")
        if not _instance_host_allowed(instance_host, settings.instance_allowed_hosts):
            raise UnsafeEndpointError(
                f"instance_url host {instance_host!r} is not in the configured allowlist"
            )
        login_host = normalize_host(urlparse(login_base).hostname or "")
        if login_host not in frozenset(settings.login_allowed_hosts):
            raise UnsafeEndpointError(
                f"login_base_url host {login_host!r} is not in the configured allowlist"
            )

        self._instance_base = instance_base
        self._login_base = login_base
        self._instance_host = instance_host
        self._settings = settings
        self._transport = transport
        self._throttle = AdaptiveThrottle(settings.requests_per_second, settings.burst)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._organization_id: str | None = None
        self.last_rest_pages_read = 0

    @property
    def organization_id(self) -> str:
        """The provider-issued organization identity, once OAuth established it."""
        if self._organization_id is None:
            raise SalesforceSyncError("Salesforce organization identity has not been established")
        return self._organization_id

    async def authenticate(self, client: httpx.AsyncClient) -> str:
        """Acquire OAuth and return the validated immutable organization id."""
        await self._get_token(client)
        if self._organization_id is None:
            raise SalesforceSyncError(
                "Salesforce token omitted its provider-issued organization identity"
            )
        return self._organization_id

    @asynccontextmanager
    async def session(self) -> AsyncIterator[httpx.AsyncClient]:
        """Open ONE httpx client for a crawl (timeout + injected transport)."""
        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds, transport=self._transport
        ) as client:
            yield client

    # -- path guard ------------------------------------------------------------
    def _guard_path(self, path: str) -> None:
        """Refuse anything that is not a relative ``/services/...`` path.

        Runs BEFORE token acquisition on every call. The Bulk/REST APIs only ever
        hand us relative continuation paths (``nextRecordsUrl``); an absolute URL
        (``https://evil.example.com/...``) or an off-prefix path (``/evil/path``)
        fails this check outright — closing the response-injected-link SSRF
        surface without ever spending a token on the hostile request.
        """
        if not path.startswith("/services/"):
            raise SalesforceSyncError("refusing to call a Salesforce path outside '/services/'")

    # -- token flow --------------------------------------------------------------
    async def _get_token(self, client: httpx.AsyncClient) -> str:
        monotonic_now = time.monotonic()
        if self._token is not None and monotonic_now < self._token_expires_at:
            return self._token

        settings = self._settings
        if settings.auth_method == "jwt_bearer":
            assert settings.private_key_pem_path is not None  # validated at construction
            assertion = jwt.encode(
                {
                    "iss": settings.client_id,
                    "sub": settings.username,
                    "aud": settings.login_base_url,
                    "exp": int(time.time()) + 300,
                },
                Path(settings.private_key_pem_path).read_bytes(),
                algorithm="RS256",
            )
            data: dict[str, str] = {
                "grant_type": _GRANT_TYPE_JWT_BEARER,
                "assertion": assertion,
            }
            token_url = f"{self._login_base}/services/oauth2/token"
        else:
            data = {
                "grant_type": "client_credentials",
                "client_id": settings.client_id,
                "client_secret": settings.client_secret.get_secret_value(),
            }
            # client_credentials REQUIRES the org's own My Domain token endpoint —
            # login.salesforce.com does not serve this grant type.
            token_url = f"{self._instance_base}/services/oauth2/token"

        max_attempts = settings.max_attempts
        cap = settings.retry_after_cap_seconds
        for attempt in range(1, max_attempts + 1):
            resp = await client.post(token_url, data=data)
            status = resp.status_code
            if status in _RETRYABLE:
                if attempt == max_attempts:
                    raise SalesforceSyncError(
                        f"Salesforce token endpoint returned HTTP {status} after "
                        f"{attempt} attempts",
                        status=status,
                    )
                await self._throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                continue
            if status >= 400:
                # STATUS ONLY — never the body/secret/URL (C12).
                raise SalesforceSyncError(
                    f"Salesforce token endpoint returned HTTP {status}", status=status
                )
            payload = resp.json()
            # The response's own `instance_url` field is deliberately IGNORED — we
            # never re-point; the configured instance stays pinned for our lifetime.
            identity_org: str | None = None
            response_org = payload.get("organization_id")
            if isinstance(response_org, str):
                identity_org = canonical_salesforce_organization_id(response_org)
            response_identity = payload.get("id")
            if isinstance(response_identity, str):
                url_org = organization_id_from_identity_url(
                    response_identity, allowed_hosts=settings.identity_allowed_hosts
                )
                if identity_org is not None and url_org != identity_org:
                    raise SalesforceSyncError(
                        "Salesforce token returned conflicting organization identity"
                    )
                identity_org = url_org
            if (
                identity_org is not None
                and self._organization_id is not None
                and identity_org != self._organization_id
            ):
                raise SalesforceSyncError("Salesforce token changed organization identity")
            if identity_org is not None:
                self._organization_id = identity_org
            self._token = str(payload["access_token"])
            self._token_expires_at = monotonic_now + settings.token_ttl_seconds
            return self._token
        raise SalesforceSyncError("exhausted Salesforce token retries")  # pragma: no cover

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0

    # -- HTTP plumbing -----------------------------------------------------------
    async def _send_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None,
        json_body: Mapping[str, Any] | None,
    ) -> httpx.Response:
        settings = self._settings
        max_attempts = settings.max_attempts
        cap = settings.retry_after_cap_seconds
        url = f"{self._instance_base}{path}"
        for attempt in range(1, max_attempts + 1):
            await self._throttle.acquire()
            token = await self._get_token(client)
            resp = await client.request(
                method,
                url,
                params=params,
                json=dict(json_body) if json_body is not None else None,
                headers={"Authorization": f"Bearer {token}"},
            )
            status = resp.status_code
            if status in _RETRYABLE:
                if attempt == max_attempts:
                    raise SalesforceSyncError(
                        f"Salesforce API returned HTTP {status} after {attempt} attempts",
                        status=status,
                    )
                await self._throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                continue
            return resp
        raise SalesforceSyncError("exhausted Salesforce retries")  # pragma: no cover

    async def _call(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        self._guard_path(path)  # BEFORE any token acquisition.
        resp = await self._send_with_retry(client, method, path, params=params, json_body=json_body)
        if resp.status_code == 401:
            # Re-acquire ONCE then retry; a second 401 is a status-only error.
            self._invalidate_token()
            resp = await self._send_with_retry(
                client, method, path, params=params, json_body=json_body
            )
        if resp.status_code >= 400:
            raise SalesforceSyncError(
                f"Salesforce API {method} returned HTTP {resp.status_code}"
                f"{_error_code_suffix(resp)}",
                status=resp.status_code,
            )
        return resp

    async def get_json(
        self, client: httpx.AsyncClient, path: str, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        resp = await self._call(client, "GET", path, params=params)
        return resp.json()

    async def post_json(
        self, client: httpx.AsyncClient, path: str, json_body: Mapping[str, Any]
    ) -> dict[str, Any]:
        resp = await self._call(client, "POST", path, json_body=json_body)
        return resp.json()

    async def get_csv(
        self, client: httpx.AsyncClient, path: str, params: Mapping[str, str] | None = None
    ) -> tuple[str, Mapping[str, str]]:
        resp = await self._call(client, "GET", path, params=params)
        return resp.text, resp.headers

    # -- REST query (used for the ObjectPermissions crawl) ----------------------
    async def rest_query(self, client: httpx.AsyncClient, soql: str) -> list[dict[str, Any]]:
        """Run a REST ``/query`` and follow ``nextRecordsUrl`` (a RELATIVE path).

        Every followed link is re-guarded by :meth:`_guard_path` (via
        :meth:`get_json`) — a hostile/off-prefix ``nextRecordsUrl`` aborts the
        crawl rather than being followed.
        """
        path = f"/services/data/{self._settings.api_version}/query"
        payload = await self.get_json(client, path, params={"q": soql})
        records: list[dict[str, Any]] = []
        total_size: int | None = None
        seen_next_urls: set[str] = set()
        page_count = 0
        self.last_rest_pages_read = 0
        while True:
            page_count += 1
            page_records = payload.get("records")
            page_total = payload.get("totalSize")
            done = payload.get("done")
            if (
                not isinstance(page_records, list)
                or any(not isinstance(record, dict) for record in page_records)
                or not isinstance(page_total, int)
                or isinstance(page_total, bool)
                or page_total < 0
                or not isinstance(done, bool)
            ):
                raise SalesforceSyncError("Salesforce REST query returned malformed page metadata")
            if total_size is None:
                total_size = page_total
            elif page_total != total_size:
                raise SalesforceSyncError("Salesforce REST query totalSize changed between pages")
            records.extend(page_records)
            if len(records) > total_size:
                raise SalesforceSyncError("Salesforce REST query returned more rows than totalSize")

            if done:
                if len(records) != total_size:
                    raise SalesforceSyncError(
                        "Salesforce REST query row count does not match totalSize"
                    )
                self.last_rest_pages_read = page_count
                return records

            next_url = payload.get("nextRecordsUrl")
            if not isinstance(next_url, str) or not next_url:
                raise SalesforceSyncError(
                    "Salesforce REST query is incomplete without nextRecordsUrl"
                )
            if next_url in seen_next_urls:
                raise SalesforceSyncError("Salesforce REST query repeated nextRecordsUrl")
            if page_count >= self._settings.rest_max_result_pages:
                raise SalesforceSyncError("Salesforce REST query exceeded its result-page bound")
            seen_next_urls.add(next_url)
            payload = await self.get_json(client, next_url)

    # -- Bulk API 2.0 -------------------------------------------------------------
    async def create_query_job(self, client: httpx.AsyncClient, soql: str) -> str:
        path = f"/services/data/{self._settings.api_version}/jobs/query"
        payload = await self.post_json(client, path, {"operation": "query", "query": soql})
        return str(payload["id"])

    async def get_query_job(self, client: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
        if not _JOB_ID_RE.match(job_id):
            raise SalesforceSyncError("refusing to use a malformed Salesforce job id")
        path = f"/services/data/{self._settings.api_version}/jobs/query/{job_id}"
        return await self.get_json(client, path)

    async def get_query_results_page(
        self, client: httpx.AsyncClient, job_id: str, locator: str | None = None
    ) -> tuple[str, str | None]:
        if not _JOB_ID_RE.match(job_id):
            raise SalesforceSyncError("refusing to use a malformed Salesforce job id")
        path = f"/services/data/{self._settings.api_version}/jobs/query/{job_id}/results"
        params: dict[str, str] = {"maxRecords": str(self._settings.bulk_max_records_per_page)}
        if locator:
            params["locator"] = locator
        text, headers = await self.get_csv(client, path, params)
        next_locator = headers.get("Sforce-Locator")
        if next_locator in (None, "", "null"):
            next_locator = None
        return text, next_locator
