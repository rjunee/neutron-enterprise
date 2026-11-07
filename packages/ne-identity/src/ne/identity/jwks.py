# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Signing-key sources for the OIDC verifier.

Two implementations behind the :class:`KeySource` protocol:

* :class:`StaticJwksSource` — an in-memory JWKS, used by the CI security suite
  (locally minted keys, ZERO network) and usable air-gapped.
* :class:`HttpJwksSource` — fetches JWKS/discovery from the pinned local issuer.

HTTP uses stdlib ``urllib`` in ``asyncio.to_thread`` — the SAME deliberate
choice as ``ne.secrets.openbao`` (a fixed, few-call REST client over stdlib
minimizes supply-chain surface). ALL cryptography and JWK parsing is delegated
to PyJWT; this module implements zero cryptography.

Fail-closed behaviors: the discovery document's ``issuer`` MUST echo the pinned
issuer EXACTLY (defeats discovery substitution); ``http://`` URLs are refused
unless ``allow_insecure_http`` (and non-HTTP(S) schemes such as ``file://`` are
refused unconditionally); and every network/JSON error becomes a
:class:`JwksError` (never a pass-through), which is a subclass of
:class:`TokenVerificationError` (fail-closed, no oracle).

Anti-amplification + egress hardening (NE-FIX-IDENTITY):

* **Lock-free fresh-cache reads (P2-6).** A ``kid`` present in the fresh cache is
  served WITHOUT awaiting the network or the ``asyncio.Lock`` — a degraded
  Keycloak or an attacker-driven unknown-``kid`` spray can therefore never stall
  legitimate auth behind an in-flight refresh.
* **Single-flight forced refresh with a cooldown (P2-6).** An unknown ``kid``
  forces AT MOST ONE refresh per ``jwks_refresh_cooldown_seconds`` window (default
  30s); within the window unknown kids fail CLOSED (:class:`JwksError`) instead of
  each triggering a Keycloak round trip, and recently-missed kids are
  negatively-cached (bounded to ``_MAX_KID_MISSES``) so a re-request costs no
  fetch. During a genuine key rotation this can fail-close a new ``kid`` for up to
  the cooldown — a deliberate fail-closed availability tradeoff.
* **Env-proxy-immune, same-origin-only fetch (P2-3a, C2).** The fetch uses a
  dedicated opener built with ``ProxyHandler({})`` so ``HTTP_PROXY`` /
  ``HTTPS_PROXY`` / ``ALL_PROXY`` can never route the JWKS request (or a token on
  redirect) off-box, plus a redirect handler that refuses any cross-origin
  redirect.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import jwt

from ne.identity.config import OidcConfig
from ne.identity.errors import JwksError

# Bound on the negative kid-miss cache so the anti-amplification defense can never
# itself become an unbounded-memory DoS under a distinct-kid spray.
_MAX_KID_MISSES = 1024


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse any JWKS redirect that leaves the original scheme+host (C2).

    A compromised or misconfigured issuer must not be able to bounce the key
    fetch (and any credential carried on it) to another host.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        new = urlparse(urljoin(req.full_url, newurl))
        old = urlparse(req.full_url)
        if (new.scheme, new.netloc) != (old.scheme, old.netloc):
            raise JwksError("refusing cross-origin JWKS redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_no_proxy_opener() -> urllib.request.OpenerDirector:
    """Build an opener that hard-disables env proxies and cross-origin redirects.

    ``ProxyHandler({})`` yields NO proxy handler at all, so a set
    ``HTTP(S)_PROXY`` / ``ALL_PROXY`` is ignored. Kept a public-to-tests factory
    so the structural test can rebuild it under a mutated environment.
    """
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _SameOriginRedirectHandler(),
    )


# Built ONCE at import; env proxies present at import time are already disabled.
_NO_PROXY_OPENER = _build_no_proxy_opener()


class KeySource(Protocol):
    """Resolves a JWT ``kid`` to a verified :class:`jwt.PyJWK` signing key."""

    async def get_signing_key(self, kid: str) -> jwt.PyJWK: ...


def _key_from_set(jwk_set: jwt.PyJWKSet, kid: str) -> jwt.PyJWK:
    for key in jwk_set.keys:
        if key.key_id == kid:
            return key
    raise JwksError(f"no signing key for kid {kid!r}")


class StaticJwksSource:
    """A fixed, in-memory JWKS. No network. Used by CI tests and air-gapped runs."""

    def __init__(self, jwks: dict[str, Any]) -> None:
        try:
            self._set = jwt.PyJWKSet.from_dict(jwks)
        except (jwt.PyJWKError, jwt.InvalidKeyError, KeyError, TypeError, ValueError) as exc:
            raise JwksError(f"invalid JWKS: {exc}") from exc

    async def get_signing_key(self, kid: str) -> jwt.PyJWK:
        return _key_from_set(self._set, kid)


class HttpJwksSource:
    """Fetches and caches the JWKS from the pinned local issuer (or explicit URI)."""

    def __init__(self, config: OidcConfig) -> None:
        self._config = config
        self._cached: jwt.PyJWKSet | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()
        # monotonic timestamp of the last UNKNOWN-KID forced refresh (None = never).
        self._last_forced_refresh_at: float | None = None
        # negative cache: kid -> monotonic time it was last missed (bounded).
        self._kid_misses: dict[str, float] = {}

    def _require_secure(self, url: str) -> None:
        # Only https:// is trusted; allow_insecure_http relaxes this to plain
        # http:// for the dev stack ONLY. Every other scheme (file://, ftp://,
        # ...) is rejected unconditionally — a compromised discovery document
        # must never be able to point the key fetch at a non-HTTP resource.
        scheme = urlparse(url).scheme
        if scheme == "https":
            return
        if scheme == "http" and self._config.allow_insecure_http:
            return
        raise JwksError(
            "refusing to fetch keys over non-https URL "
            "(allow_insecure_http permits plain http:// for the dev stack only)"
        )

    def _fetch_json_sync(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, method="GET")
        try:
            with _NO_PROXY_OPENER.open(req, timeout=self._config.http_timeout_seconds) as resp:
                raw = resp.read()
        except (urllib.error.URLError, OSError) as exc:
            raise JwksError(f"JWKS/discovery fetch failed: {exc}") from exc
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise JwksError(f"JWKS/discovery response was not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise JwksError("JWKS/discovery response was not a JSON object")
        return parsed

    def _resolve_jwks_uri_sync(self) -> str:
        if self._config.jwks_uri is not None:
            self._require_secure(self._config.jwks_uri)
            return self._config.jwks_uri
        discovery_url = f"{self._config.issuer}/.well-known/openid-configuration"
        self._require_secure(discovery_url)
        doc = self._fetch_json_sync(discovery_url)
        # Discovery-substitution guard: the document MUST self-report the pinned
        # issuer exactly, or we refuse to trust the keys it points at.
        if doc.get("issuer") != self._config.issuer:
            raise JwksError("discovery document issuer does not match the pinned issuer")
        jwks_uri = doc.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise JwksError("discovery document did not provide a jwks_uri")
        self._require_secure(jwks_uri)
        return jwks_uri

    def _fetch_set_sync(self) -> jwt.PyJWKSet:
        jwks_uri = self._resolve_jwks_uri_sync()
        payload = self._fetch_json_sync(jwks_uri)
        try:
            return jwt.PyJWKSet.from_dict(payload)
        except (jwt.PyJWKError, jwt.InvalidKeyError, KeyError, TypeError, ValueError) as exc:
            raise JwksError(f"invalid JWKS payload: {exc}") from exc

    def _fresh(self) -> bool:
        return (
            self._cached is not None
            and (time.monotonic() - self._cached_at) < self._config.jwks_cache_ttl_seconds
        )

    async def _refresh(self) -> jwt.PyJWKSet:
        jwk_set = await asyncio.to_thread(self._fetch_set_sync)
        self._cached = jwk_set
        self._cached_at = time.monotonic()
        return jwk_set

    async def get_signing_key(self, kid: str) -> jwt.PyJWK:
        # Branch 1 — LOCK-FREE fresh-cache hit. A known kid in the fresh cache is
        # served without awaiting the network OR the lock, so a degraded issuer /
        # unknown-kid spray can never stall legitimate auth.
        cached = self._cached
        if cached is not None and self._fresh():
            try:
                return _key_from_set(cached, kid)
            except JwksError:
                return await self._resolve_unknown_kid(kid)

        # Branch 2 — stale/absent cache: single-flight refresh under the lock.
        async with self._lock:
            if not self._fresh():
                await self._refresh()
            assert self._cached is not None
            try:
                return _key_from_set(self._cached, kid)
            except JwksError:
                pass
        return await self._resolve_unknown_kid(kid)

    async def _resolve_unknown_kid(self, kid: str) -> jwt.PyJWK:
        """Resolve a kid absent from the fresh cache, rate-limiting forced refreshes."""
        async with self._lock:
            now = time.monotonic()
            cooldown = self._config.jwks_refresh_cooldown_seconds

            # (d1) negative cache: a recently-missed kid fails closed with no fetch.
            missed_at = self._kid_misses.get(kid)
            if missed_at is not None and (now - missed_at) < cooldown:
                raise JwksError(f"no signing key for kid {kid!r} (negative-cached)")

            # (d2) re-check: another waiter may have refreshed while we queued.
            if self._cached is not None:
                try:
                    return _key_from_set(self._cached, kid)
                except JwksError:
                    pass

            # (d3) forced-refresh rate limit — FAIL CLOSED within the cooldown.
            if (
                self._last_forced_refresh_at is not None
                and (now - self._last_forced_refresh_at) < cooldown
            ):
                self._note_miss(kid, now)
                raise JwksError("forced JWKS refresh rate-limited; unknown kid rejected")

            # (d4) forced refresh (keys may have rotated), then give up fail-closed.
            self._last_forced_refresh_at = now
            jwk_set = await self._refresh()
            try:
                return _key_from_set(jwk_set, kid)
            except JwksError:
                self._note_miss(kid, time.monotonic())
                raise

    def _note_miss(self, kid: str, now: float) -> None:
        """Record a kid miss, pruning stale entries and bounding the cache size."""
        cooldown = self._config.jwks_refresh_cooldown_seconds
        stale = [k for k, t in self._kid_misses.items() if (now - t) >= cooldown]
        for k in stale:
            del self._kid_misses[k]
        while len(self._kid_misses) >= _MAX_KID_MISSES:
            self._kid_misses.pop(next(iter(self._kid_misses)))
        self._kid_misses[kid] = now
