# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Settings for the Slack chat adapter (NE-063).

:class:`SlackChatSettings` is an :class:`~ne.contracts.config.NeSettings`
subclass (init > ``NE_CHAT_`` env > TOML > defaults). BOTH secrets — the signing
secret and the bot token — stay inside a ``SecretStr`` end-to-end: masked by
``compute_config_hash``'s ``model_dump`` and never echoed (C12, same posture as
``SlackConnectorSettings``).

Identity mapping (v1): ``identity_links`` is the ONLY mapping source — an
explicit, admin-configured ``{slack_user_id: platform_subject}`` table. Because
it is config, every mapping change re-stamps the platform ``config_hash`` and is
auditable. Email-based auto-linking is deliberately REJECTED: a Slack display
email is attacker-influenceable and would be an impersonation vector, so a Slack
user is only ever mapped by an explicit id->subject entry an admin wrote.

Approver authority (v1): ``approver_roles`` lists the ``Principal.groups`` entries
(ne-identity stamps platform roles as ``role:<name>``) permitted to ACTION a
mirrored approval from Slack. An EMPTY tuple means NOBODY can action approvals
from Slack — fail closed (enforced in ``approvals.py``, not here). The gate's
Tier-3 named-approver / distinct-second / expiry rules remain authoritative
regardless; this is only the chat-layer pre-check.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings

__all__ = ["SlackChatSettings"]


class SlackChatSettings(NeSettings):
    """Configuration for the Slack chat adapter (a customer-internal chat app)."""

    model_config = SettingsConfigDict(
        env_prefix="NE_CHAT_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    # -- Slack credentials + endpoint (C2 allowlist, C12 secrets) -------------
    #: v0 signing secret used to verify inbound request signatures (REQUIRED).
    signing_secret: SecretStr = SecretStr("")
    #: Bot token for chat.postMessage / chat.update writes (REQUIRED).
    bot_token: SecretStr = SecretStr("")
    api_base_url: str = "https://slack.com/api"
    api_allowed_hosts: tuple[str, ...] = ("slack.com",)

    # -- signature anti-replay ------------------------------------------------
    #: Max absolute skew (seconds) between now and the request timestamp. Slack's
    #: documented replay window is 300s; both past and future skew are rejected.
    signature_tolerance_s: float = Field(default=300.0, gt=0, le=600)

    # -- identity mapping (the ONLY v1 source; see module docstring) ----------
    #: Explicit admin-configured ``{slack_user_id: platform_subject}`` map.
    identity_links: dict[str, str] = Field(default_factory=dict)

    # -- approval-mirroring authority -----------------------------------------
    #: ``Principal.groups`` entries allowed to ACTION approvals from Slack.
    #: EMPTY ⇒ nobody can (fail closed, enforced in approvals.py).
    approver_roles: tuple[str, ...] = ("role:approver",)
    #: Channel that mirrored approvals post into; empty ⇒ mirroring disabled.
    approvals_channel: str = ""

    # -- streaming + dedup + throttle -----------------------------------------
    #: Minimum seconds between chat.update edits (≥ Slack's 1 msg/s/channel).
    min_update_interval_s: float = Field(default=1.0, gt=0)
    #: In-process event dedup TTL and cap (retries + re-deliveries ack, no re-run).
    event_dedup_ttl_s: float = Field(default=300.0, gt=0)
    event_dedup_max: int = Field(default=10_000, ge=1)

    # -- Slack Web API client knobs -------------------------------------------
    requests_per_minute: float = Field(default=50.0, gt=0)
    max_attempts: int = Field(default=5, ge=1)
    retry_after_cap_seconds: int = 120
    timeout_seconds: float = 30.0

    @model_validator(mode="after")
    def _validate(self) -> SlackChatSettings:
        if not self.signing_secret.get_secret_value():
            raise ValueError("Slack chat adapter requires a non-empty signing_secret")
        if not self.bot_token.get_secret_value():
            raise ValueError("Slack chat adapter requires a non-empty bot_token")
        return self
