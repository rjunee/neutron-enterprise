# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layered settings for ne-identity — pins the local OIDC issuer and role mapping.

SECURITY DECISIONS (fail-closed at construction time):

* ``OidcConfig.issuer`` and ``OidcConfig.audience`` are REQUIRED with no
  defaults — issuer pinning must be an explicit, deliberate choice (one issuer per deployment: every
  service trusts only the local issuer).
* ``OidcConfig.allowed_algs`` is validated to a strict subset of asymmetric,
  JWKS-backed algorithms. This makes ``alg:none`` and any ``HS*`` symmetric
  algorithm IMPOSSIBLE to configure — the algorithm-confusion class of attack is
  closed structurally, not merely at verify time.
* ``allow_insecure_http`` defaults False, so any ``http://`` issuer/JWKS URL
  raises at construction. Only the local dev stack (http://localhost:8081) opts
  in.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, field_validator
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings

# Asymmetric, JWKS-backed signature algorithms. Symmetric (HS*) and unsigned
# (none) algorithms are deliberately absent — they can never be configured, so
# algorithm-confusion and alg:none are structurally impossible.
_ASYMMETRIC_ALGS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "ES256",
        "ES384",
        "ES512",
        "PS256",
        "PS384",
        "PS512",
        "EdDSA",
    }
)


class OidcConfig(BaseModel):
    """OIDC verification config, pinned to a single local issuer."""

    issuer: str
    audience: str
    jwks_uri: str | None = None
    allowed_algs: tuple[str, ...] = ("RS256",)
    leeway_seconds: float = 10.0
    jwks_cache_ttl_seconds: float = 300.0
    #: Minimum interval between unknown-kid FORCED JWKS refreshes (NE-FIX-IDENTITY
    #: P2-6 anti-amplification). Within this window an unknown kid fails closed
    #: instead of triggering another Keycloak round trip; 0 disables the rate
    #: limit (tests only).
    jwks_refresh_cooldown_seconds: float = 30.0
    http_timeout_seconds: float = 5.0
    allow_insecure_http: bool = False
    groups_claim: str = "groups"
    #: Token claim carrying the Keycloak broker alias (emitted by the
    #: ne-groups scope's user-session-note mapper, NE-BEW-GROUPS-ZEROGROUPS). A
    #: non-empty string value here means the login is BROKERED (came through an
    #: upstream IdP via Keycloak); absent/empty means a local or service-account
    #: login. Mirrors the ``groups_claim`` config pattern exactly.
    idp_claim: str = "identity_provider"
    exempt_paths: tuple[str, ...] = ("/health",)

    @field_validator("issuer")
    @classmethod
    def _validate_issuer(cls, v: str) -> str:
        if not v:
            raise ValueError("issuer must be a non-empty URL")
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"issuer must be an http(s) URL, got {v!r}")
        if v.endswith("/"):
            raise ValueError(
                "issuer must not have a trailing slash (exact-match iss comparison "
                f"requires an unambiguous value): {v!r}"
            )
        return v

    @field_validator("audience")
    @classmethod
    def _validate_audience(cls, v: str) -> str:
        if not v:
            raise ValueError("audience must be a non-empty string")
        return v

    @field_validator("allowed_algs")
    @classmethod
    def _validate_allowed_algs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("allowed_algs must be non-empty")
        offenders = set(v) - _ASYMMETRIC_ALGS
        if offenders:
            raise ValueError(
                "allowed_algs may only contain asymmetric JWKS-backed algorithms "
                f"(alg:none and HS* are forbidden); rejected: {sorted(offenders)}"
            )
        return v

    @field_validator("leeway_seconds")
    @classmethod
    def _validate_leeway(cls, v: float) -> float:
        if not (0.0 <= v <= 60.0):
            raise ValueError(f"leeway_seconds must be within [0, 60], got {v}")
        return v

    @field_validator("jwks_refresh_cooldown_seconds")
    @classmethod
    def _validate_jwks_refresh_cooldown(cls, v: float) -> float:
        if not (0.0 <= v <= 3600.0):
            raise ValueError(f"jwks_refresh_cooldown_seconds must be within [0, 3600], got {v}")
        return v

    @field_validator("jwks_uri")
    @classmethod
    def _validate_jwks_uri(cls, v: str | None) -> str | None:
        if v is None:
            return v
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"jwks_uri must be an http(s) URL, got {v!r}")
        return v

    @field_validator("idp_claim")
    @classmethod
    def _validate_idp_claim(cls, v: str) -> str:
        # NE-BEW-GROUPS-ZEROGROUPS (Argus P3): an empty value here would make
        # claims.claims.get(idp_claim) always return None, permanently and
        # silently disabling the zero-mapped-groups observability signal with
        # no startup error. Fail closed at construction instead.
        if not v:
            raise ValueError(
                "idp_claim must be a non-empty string (an empty value silently "
                "disables the NE-BEW-GROUPS-ZEROGROUPS zero-mapped-groups signal)"
            )
        return v


class RoleMappingConfig(BaseModel):
    """Config-driven IdP-group -> platform-role mapping.

    Only groups present as keys in ``mapping`` contribute roles. An unmapped
    group contributes nothing (the anti-escalation default). ``default_roles``
    are always granted.
    """

    mapping: dict[str, tuple[str, ...]] = {}
    default_roles: tuple[str, ...] = ()


class IdentitySettings(NeSettings):
    """Layered settings selecting the verifier/resolver and pinning the issuer."""

    model_config = SettingsConfigDict(
        env_prefix="NE_IDENTITY_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    verifier: str = "keycloak-oidc"
    resolver: str = "jit"
    oidc: OidcConfig
    roles: RoleMappingConfig = RoleMappingConfig()
    #: Str-valued verified-token claims the JIT provisioner copies into
    #: ``Principal.claims``; claims propagate ONLY from the JWKS-verified token,
    #: never request input; mirrors the configurable ``groups_claim`` (AMENDMENT
    #: A1, ne-api-service-spec.md). The default is byte-identical to the historic
    #: hardcoded whitelist, so an un-configured deployment behaves EXACTLY as before.
    principal_claims: tuple[str, ...] = ("email", "preferred_username", "name")
