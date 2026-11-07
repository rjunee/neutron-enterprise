# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Settings for the Slack connector (NE-045).

``SlackConnectorSettings`` is an :class:`~ne.contracts.config.NeSettings`
subclass (values layer init > ``NE_SLACK_*`` env > TOML > defaults). The bot token
never leaves a ``SecretStr`` — masked by ``compute_config_hash``'s
``model_dump`` and never echoed (C12).

Two Slack rate-limit regimes exist (both post-2026-03 realities — see
``docs.slack.dev`` changelogs 2025-05-29 / 2025-06-03):

* **customer-internal app** (the default this connector ships for, via
  ``manifest.json`` — see the README install steps): Tier-3-class limits,
  roughly 50+ requests/minute and up to ~1000 objects per ``conversations.history``
  /``conversations.replies`` page. This is why the SPEC mandates shipping as a
  customer-internal app rather than a public/Marketplace distributed one.
* **non-Marketplace DISTRIBUTED app**: as of 2026-03-03, ``conversations.history``
  /``conversations.replies`` are capped at 1 request/minute and 15 objects per
  page for EXISTING installs. If this connector is ever run against an app
  classified this way, set ``history_requests_per_minute<=1.0`` and
  ``history_page_size<=15`` (the defaults below are conservative but still
  assume the customer-internal regime; ``Retry-After`` is always honored
  regardless of regime).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from ne.connect.contracts import PiiClass
from ne.contracts.config import NeSettings

__all__ = ["SlackConnectorSettings"]

_VALID_CONVERSATION_TYPES = frozenset({"public_channel", "private_channel", "mpim", "im"})


class SlackConnectorSettings(NeSettings):
    """Configuration for the customer-internal-app Slack connector."""

    model_config = SettingsConfigDict(
        env_prefix="NE_SLACK_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    # -- Slack Web API credentials + endpoint (C2 allowlist) ----------------
    bot_token: SecretStr = SecretStr("")
    api_base_url: str = "https://slack.com/api"
    api_allowed_hosts: tuple[str, ...] = ("slack.com",)

    # -- crawl scope ----------------------------------------------------------
    conversation_types: tuple[str, ...] = ("public_channel", "private_channel", "mpim")
    #: empty == all conversations listed for ``conversation_types``.
    channel_ids: tuple[str, ...] = ()
    include_archived: bool = False

    # -- ODCS contract facets -------------------------------------------------
    source_name: str = "slack"
    owner: str = ""
    freshness_sla: str = "PT1H"
    pii: PiiClass = PiiClass.PERSONAL

    # -- adaptive throttling (two tiers: history reads vs. general reads) ----
    #: customer-internal apps see roughly Tier-3 (50+ rpm); a non-Marketplace
    #: distributed app is capped at 1 rpm since 2026-03-03 — set this to
    #: ``<=1.0`` if ever running as one.
    history_requests_per_minute: float = Field(default=30.0, gt=0)
    requests_per_minute: float = Field(default=15.0, gt=0)
    #: objects per ``conversations.history``/``conversations.replies`` page;
    #: a non-Marketplace distributed app is capped at 15 since 2026-03-03.
    history_page_size: int = Field(default=200, ge=1, le=999)
    page_size: int = Field(default=200, ge=1, le=1000)
    max_attempts: int = Field(default=5, ge=1)
    retry_after_cap_seconds: int = 120
    timeout_seconds: float = 30.0

    @model_validator(mode="after")
    def _validate(self) -> SlackConnectorSettings:
        if not self.bot_token.get_secret_value():
            raise ValueError("Slack connector requires a non-empty bot_token")
        if not self.owner.strip():
            raise ValueError(
                "Slack connector requires a non-empty owner (the ODCS data-contract "
                "owner; set NE_SLACK_OWNER or owner=)"
            )
        if not self.conversation_types:
            raise ValueError("Slack connector requires a non-empty conversation_types")
        unknown = set(self.conversation_types) - _VALID_CONVERSATION_TYPES
        if unknown:
            raise ValueError(
                f"conversation_types contains unsupported value(s) {sorted(unknown)!r}; "
                f"must be a subset of {sorted(_VALID_CONVERSATION_TYPES)!r}"
            )
        return self
