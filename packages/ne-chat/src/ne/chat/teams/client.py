# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin Bot Framework Connector-API + token client — the ONLY new httpx site in ne.chat.

:class:`TeamsChatClient` is a deliberately small app-secret client (NO botbuilder
/ Microsoft 365 Agents SDK — the Bot Framework SDK was archived Dec 2025 and the
Agents SDK migration guidance itself delegates inbound auth to plain JWT
validation; a thin httpx client implements the documented REST wire protocol, the
NE-045 build-vs-adopt precedent). It owns the judgment-dense egress surface: the
client-credentials token flow, outbound Connector-API replies, and the LIVE JWKS
fetch (making the client the production :class:`~ne.chat.teams.auth.KeySource`).

C2 (the security spine): the constructor refuses to build under ``AIR_GAP`` BEFORE
any I/O and validates + pins both the login and OpenID-metadata base URLs to their
configured allowlists. Outbound replies go to a per-conversation ``serviceUrl``
that was VALIDATED + allowlisted and cached by the ingress at inbound time and is
RE-VALIDATED here before every send — a hostile serviceUrl can never redirect the
bearer token. The JWKS discovery re-pins ``jwks_uri`` to the metadata allowlist
(discovery-substitution guard).

C12: the app secret / bearer token appear only in the token POST body / the
Authorization header; failures raise :class:`~ne.chat.errors.TeamsApiError`
(token/reply) or :class:`~ne.chat.errors.ActivityAuthError` (JWKS) with the HTTP
status / host only — never a secret, params, or a response body.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt

from ne.chat.errors import ActivityAuthError, TeamsApiError
from ne.chat.teams.config import TeamsChatSettings
from ne.chat.teams.refs import ConversationRefs, validate_service_url

# The AIR_GAP guard is the ONE shared read (NE-CKC10 / P2-23); aliased to keep the
# module-local name this client's callers/tests reference.
from ne.net.airgap import air_gap_engaged as _air_gap_engaged
from ne.net.ssrf import normalize_host, validate_endpoint_url

#: Re-exported transport alias so callers can type an injectable transport WITHOUT
#: importing httpx — keeping client.py the only NEW httpx import site under ne.chat
#: (locked by the C2 egress guardian test).
type Transport = httpx.AsyncBaseTransport

__all__ = ["TeamsChatClient", "Transport"]

_RETRYABLE = frozenset({429, 503, 504})


class TeamsChatClient:
    """Bot Framework Connector-API WRITE client + token + JWKS: SSRF-guarded, C12."""

    def __init__(
        self,
        settings: TeamsChatSettings,
        refs: ConversationRefs,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # (a) Fail-closed BEFORE any I/O — Teams is egress.
        if _air_gap_engaged():
            raise TeamsApiError("AIR_GAP engaged: the Teams chat client is egress and is refused")
        # (b) Validate + pin the login and metadata base URLs.
        login_base = validate_endpoint_url(
            settings.login_base_url,
            require_https=True,
            allow_private=False,
            error_cls=TeamsApiError,
        )
        self._require_host(login_base, settings.login_allowed_hosts, "login_base_url")
        metadata_url = validate_endpoint_url(
            settings.openid_metadata_url,
            require_https=True,
            allow_private=False,
            error_cls=TeamsApiError,
        )
        self._metadata_url = metadata_url
        self._metadata_hosts = {normalize_host(h) for h in settings.metadata_allowed_hosts}
        self._require_host(metadata_url, settings.metadata_allowed_hosts, "openid_metadata_url")

        self._settings = settings
        self._refs = refs
        self._transport = transport
        self._token_url = f"{login_base}/{settings.tenant_id}/oauth2/v2.0/token"
        self._min_interval = 60.0 / settings.requests_per_minute
        self._last_call = 0.0
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._jwks: jwt.PyJWKSet | None = None
        self._jwks_at = 0.0
        self._jwks_lock = asyncio.Lock()

    @staticmethod
    def _require_host(url: str, allowed: tuple[str, ...], field: str) -> None:
        host = normalize_host(urlparse(url).hostname or "")
        if host not in {normalize_host(h) for h in allowed}:
            raise TeamsApiError(f"{field} host {host!r} is not in the configured allowlist")

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._settings.timeout_seconds, transport=self._transport)

    # -- token flow ----------------------------------------------------------
    async def _get_token(self, client: httpx.AsyncClient) -> str:
        now = time.monotonic()
        if self._token is not None and now < self._token_expires_at:
            return self._token
        settings = self._settings
        data = {
            "grant_type": "client_credentials",
            "client_id": settings.app_id,
            "client_secret": settings.app_secret.get_secret_value(),
            "scope": settings.token_scope,
        }
        host = normalize_host(urlparse(self._token_url).hostname or "")
        resp = await client.post(self._token_url, data=data)
        status = resp.status_code
        if status >= 400:
            raise TeamsApiError(
                f"Teams token endpoint returned HTTP {status}", status=status, host=host
            )
        payload = resp.json()
        self._token = str(payload["access_token"])
        self._token_expires_at = now + max(int(payload.get("expires_in", 3600)) - 60, 0)
        return self._token

    # -- outbound Connector API ---------------------------------------------
    async def post_activity(self, conversation_id: str, payload: Mapping[str, Any]) -> str:
        """POST a new activity to a conversation; return the created activity id."""
        service_url = self._resolve_service_url(conversation_id)
        url = f"{service_url}/v3/conversations/{conversation_id}/activities"
        body = await self._send("POST", url, payload)
        return str(body.get("id", ""))

    async def update_activity(
        self, conversation_id: str, activity_id: str, payload: Mapping[str, Any]
    ) -> None:
        """PUT (replace) an existing activity in a conversation."""
        service_url = self._resolve_service_url(conversation_id)
        url = f"{service_url}/v3/conversations/{conversation_id}/activities/{activity_id}"
        await self._send("PUT", url, payload)

    def _resolve_service_url(self, conversation_id: str) -> str:
        service_url = self._refs.lookup(conversation_id)
        if service_url is None:
            # Proactive messaging to an unseen conversation is a documented v1 limit.
            raise TeamsApiError("no serviceUrl recorded for this conversation")
        return validate_service_url(self._settings, service_url)

    async def _send(self, method: str, url: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        settings = self._settings
        host = normalize_host(urlparse(url).hostname or "")
        async with self._client() as client:
            token = await self._get_token(client)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            for attempt in range(1, settings.max_attempts + 1):
                await self._throttle()
                resp = await client.request(method, url, json=dict(payload), headers=headers)
                status = resp.status_code
                if status in _RETRYABLE:
                    if attempt == settings.max_attempts:
                        raise TeamsApiError(
                            f"Teams Connector API returned HTTP {status} after {attempt} attempts",
                            status=status,
                            host=host,
                        )
                    await self._sleep_retry(resp.headers.get("Retry-After"))
                    continue
                if status >= 400:
                    raise TeamsApiError(
                        f"Teams Connector API returned HTTP {status}", status=status, host=host
                    )
                if resp.content:
                    parsed = resp.json()
                    return parsed if isinstance(parsed, dict) else {}
                return {}
        raise TeamsApiError("exhausted Teams retries", host=host)  # pragma: no cover

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

    # -- live KeySource (inbound JWT validation) -----------------------------
    async def get_signing_key(self, kid: str) -> jwt.PyJWK:
        """Resolve ``kid`` to a signing key from the pinned Bot Framework JWKS."""
        async with self._jwks_lock:
            cached = self._jwks
            jwk_set = (
                cached
                if (self._jwks_fresh() and cached is not None)
                else (await self._refresh_jwks())
            )
            key = self._find_key(jwk_set, kid)
            if key is not None:
                return key
            # Unknown kid: force AT MOST ONE refresh (keys may have rotated).
            jwk_set = await self._refresh_jwks()
            key = self._find_key(jwk_set, kid)
            if key is None:
                raise ActivityAuthError("no signing key for kid")
            return key

    @staticmethod
    def _find_key(jwk_set: jwt.PyJWKSet, kid: str) -> jwt.PyJWK | None:
        for key in jwk_set.keys:
            if key.key_id == kid:
                return key
        return None

    def _jwks_fresh(self) -> bool:
        return (
            self._jwks is not None
            and (time.monotonic() - self._jwks_at) < self._settings.jwks_cache_ttl_s
        )

    async def _refresh_jwks(self) -> jwt.PyJWKSet:
        try:
            async with self._client() as client:
                metadata = await self._get_json(client, self._metadata_url)
                jwks_uri = metadata.get("jwks_uri")
                if not isinstance(jwks_uri, str) or not jwks_uri:
                    raise ActivityAuthError("OpenID metadata did not provide a jwks_uri")
                # Discovery-substitution guard: pin jwks_uri to https + the
                # metadata allowlist BEFORE fetching keys from it.
                validate_endpoint_url(
                    jwks_uri, require_https=True, allow_private=False, error_cls=ActivityAuthError
                )
                if normalize_host(urlparse(jwks_uri).hostname or "") not in self._metadata_hosts:
                    raise ActivityAuthError("jwks_uri host is not in the metadata allowlist")
                payload = await self._get_json(client, jwks_uri)
            jwk_set = jwt.PyJWKSet.from_dict(payload)
        except ActivityAuthError:
            raise
        except (
            httpx.HTTPError,
            jwt.PyJWTError,
            jwt.InvalidKeyError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ActivityAuthError("JWKS fetch failed") from exc
        self._jwks = jwk_set
        self._jwks_at = time.monotonic()
        return jwk_set

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> dict[str, Any]:
        resp = await client.get(url)
        if resp.status_code >= 400:
            raise ActivityAuthError("JWKS/metadata fetch returned an error status")
        parsed = resp.json()
        if not isinstance(parsed, dict):
            raise ActivityAuthError("JWKS/metadata response was not a JSON object")
        return parsed
