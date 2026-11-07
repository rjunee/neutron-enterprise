# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Settings for the Microsoft Teams chat adapter (NE-080).

:class:`TeamsChatSettings` is an :class:`~ne.contracts.config.NeSettings`
subclass (init > ``NE_TEAMS_CHAT_`` env > TOML > defaults). The Bot Framework app
secret stays inside a ``SecretStr`` end-to-end — masked by
``compute_config_hash``'s ``model_dump`` and never echoed (C12, the Slack
posture).

Identity mapping (v1): ``identity_links`` is the ONLY mapping source — an
explicit, admin-configured ``{aad_object_id: platform_subject}`` table (keyed by
the Teams AAD object id on the inbound activity). Because it is config, every
mapping change re-stamps the platform ``config_hash`` and is auditable.
Email/UPN-based auto-linking is deliberately REJECTED (an impersonation vector),
exactly like the Slack adapter.

Bot Framework wire constants (documented, stable): the inbound activity JWT is
issued by ``https://api.botframework.com`` (multi-tenant) with signing keys under
the OpenID metadata at ``login.botframework.com``; the audience is the bot's app
id; the ``serviceurl`` claim must match the activity's ``serviceUrl`` (an
``smba.trafficmanager.net`` host). SINGLE-TENANT bots additionally accept their
``login.microsoftonline.com/{tid}/v2.0`` issuer via ``allowed_issuers``. Outbound
replies use a client-credentials token for scope
``https://api.botframework.com/.default``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings

__all__ = ["TeamsChatSettings"]


class TeamsChatSettings(NeSettings):
    """Configuration for the Microsoft Teams chat adapter (a governed chat bot)."""

    model_config = SettingsConfigDict(
        env_prefix="NE_TEAMS_CHAT_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    # -- Bot Framework credentials (C12 secrets) -----------------------------
    #: The bot's Azure AD app (client) id — the inbound JWT audience (REQUIRED).
    app_id: str = ""
    #: SECRET (C12) — the bot's app secret for the outbound client-credentials
    #: token (REQUIRED, never a literal).
    app_secret: SecretStr = SecretStr("")
    #: ``botframework.com`` (multi-tenant default); a single-tenant bot sets its
    #: directory (tenant) id here for the client-credentials token endpoint.
    tenant_id: str = "botframework.com"

    # -- login / token endpoints (C2 allowlists) -----------------------------
    login_base_url: str = "https://login.microsoftonline.com"
    login_allowed_hosts: tuple[str, ...] = (
        "login.microsoftonline.com",
        "login.microsoftonline.us",
        "login.chinacloudapi.cn",
    )
    token_scope: str = "https://api.botframework.com/.default"

    # -- inbound activity JWT validation (C2 + fail-closed) ------------------
    openid_metadata_url: str = "https://login.botframework.com/v1/.well-known/openidconfiguration"
    metadata_allowed_hosts: tuple[str, ...] = ("login.botframework.com",)
    #: Accepted ``iss`` claims. Multi-tenant bots trust api.botframework.com;
    #: single-tenant deployments APPEND their login.microsoftonline.com/{tid}/v2.0
    #: issuer here (documented in the README).
    allowed_issuers: tuple[str, ...] = ("https://api.botframework.com",)
    #: Allowlisted ``serviceUrl`` hosts (the Bot Connector regional endpoints).
    service_url_allowed_hosts: tuple[str, ...] = ("smba.trafficmanager.net",)
    jwt_leeway_s: float = Field(default=300.0, gt=0, le=600)

    # -- identity mapping (the ONLY v1 source; see module docstring) ---------
    #: Explicit admin-configured ``{aad_object_id: platform_subject}`` map.
    identity_links: dict[str, str] = Field(default_factory=dict)

    # -- caches + dedup + streaming ------------------------------------------
    jwks_cache_ttl_s: float = Field(default=300.0, gt=0)
    event_dedup_ttl_s: float = Field(default=300.0, gt=0)
    event_dedup_max: int = Field(default=10_000, ge=1)
    conversation_refs_max: int = Field(default=10_000, ge=1)
    min_update_interval_s: float = Field(default=1.0, gt=0)

    # -- Connector API client knobs ------------------------------------------
    requests_per_minute: float = Field(default=50.0, gt=0)
    max_attempts: int = Field(default=3, ge=1)
    retry_after_cap_seconds: int = 60
    timeout_seconds: float = 15.0

    @model_validator(mode="after")
    def _validate(self) -> TeamsChatSettings:
        if not self.app_id:
            raise ValueError("Teams chat adapter requires a non-empty app_id")
        if not self.app_secret.get_secret_value():
            raise ValueError("Teams chat adapter requires a non-empty app_secret")
        return self
