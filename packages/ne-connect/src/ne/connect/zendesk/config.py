# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Settings for the Zendesk connector (NE-093).

``ZendeskConnectorSettings`` is an :class:`~ne.contracts.config.NeSettings`
subclass (values layer init > ``NE_ZENDESK_*`` env > TOML > defaults). ``api_token``
never leaves a ``SecretStr`` (C12, masked by ``compute_config_hash``'s
``model_dump``). At least one of ``sync_tickets`` / ``sync_articles`` must be
enabled. Zendesk's cursor-based incremental exports drive resumability
(``tickets_start_time`` / ``articles_start_time`` seed the FIRST backfill; later
runs resume from the persisted cursor / end_time).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from ne.connect.contracts import PiiClass
from ne.contracts.config import NeSettings

__all__ = ["ZendeskConnectorSettings"]


class ZendeskConnectorSettings(NeSettings):
    """Configuration for the Basic-auth Zendesk connector."""

    model_config = SettingsConfigDict(
        env_prefix="NE_ZENDESK_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    # -- Zendesk credentials + endpoint (C2 allowlist) -----------------------
    #: The Zendesk subdomain base, e.g. ``https://acme.zendesk.com``.
    base_url: str = ""
    #: Entries starting with ``'.'`` are dot-suffix patterns (every
    #: ``*.zendesk.com`` subdomain); other entries must match the host EXACTLY.
    allowed_hosts: tuple[str, ...] = (".zendesk.com",)
    #: The agent email paired with the API token (Basic ``email/token`` auth).
    email: str = ""
    #: SECRET (C12) — the Zendesk API token.
    api_token: SecretStr = SecretStr("")

    # -- crawl scope ---------------------------------------------------------
    sync_tickets: bool = True
    sync_articles: bool = True
    include_ticket_comments: bool = True
    #: Epoch seconds seeding the FIRST tickets/articles backfill.
    tickets_start_time: int = 1
    articles_start_time: int = 1
    page_size: int = Field(default=100, ge=1, le=1000)

    # -- ODCS contract facets ------------------------------------------------
    source_name: str = "zendesk"
    owner: str = ""
    freshness_sla: str = "PT1H"
    pii: PiiClass = PiiClass.PERSONAL

    # -- throttle / limits ---------------------------------------------------
    requests_per_second: float = Field(default=4.0, gt=0)
    burst: int = Field(default=8, ge=1)
    max_attempts: int = Field(default=5, ge=1)
    retry_after_cap_seconds: int = 60
    timeout_seconds: float = 30.0

    @model_validator(mode="after")
    def _validate(self) -> ZendeskConnectorSettings:
        if not self.base_url:
            raise ValueError("Zendesk connector requires a non-empty base_url")
        if not self.email:
            raise ValueError("Zendesk connector requires a non-empty email")
        if not self.api_token.get_secret_value():
            raise ValueError("Zendesk connector requires a non-empty api_token")
        if not self.owner.strip():
            raise ValueError(
                "Zendesk connector requires a non-empty owner (the ODCS data-contract "
                "owner; set NE_ZENDESK_OWNER or owner=)"
            )
        if not self.sync_tickets and not self.sync_articles:
            raise ValueError(
                "Zendesk connector requires at least one of sync_tickets / sync_articles enabled"
            )
        return self
