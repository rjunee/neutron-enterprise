# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Settings for the Microsoft Teams knowledge connector (NE-080).

``TeamsConnectorSettings`` is an :class:`~ne.contracts.config.NeSettings`
subclass (values layer init > ``NE_TEAMS_*`` env > TOML > defaults). The shared
Entra/Graph surface mirrors ``ne.connect.m365.config.M365ConnectorSettings``
field-for-field so a tenant configured for the M365/SharePoint connector
configures identically here. The client secret / private-key material never
leaves a ``SecretStr`` / on-disk PEM path — masked by ``compute_config_hash``'s
``model_dump`` and never echoed (C12).

Explicit scoping is MANDATORY: ``team_ids`` must be non-empty. There is
deliberately NO tenant-wide crawl in v1 (the m365 precedent) — an unbounded
Teams crawl is both a throttling and a blast-radius hazard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from ne.connect.contracts import PiiClass
from ne.contracts.config import NeSettings

__all__ = ["TeamsConnectorSettings"]


class TeamsConnectorSettings(NeSettings):
    """Configuration for the Graph app-only Microsoft Teams connector."""

    model_config = SettingsConfigDict(
        env_prefix="NE_TEAMS_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    # -- Entra app-only credentials -----------------------------------------
    tenant_id: str = ""
    client_id: str = ""
    auth_method: Literal["certificate", "secret"] = "certificate"
    client_secret: SecretStr = SecretStr("")
    #: PEM file paths (certificate mode). The cert public part supplies the x5t
    #: thumbprint; the private key signs the RS256 client assertion.
    certificate_pem_path: str | None = None
    private_key_pem_path: str | None = None

    # -- Graph / login endpoints (C2 allowlists) ----------------------------
    # graph_allowed_hosts / login_allowed_hosts validate the CONFIGURED base URLs
    # at client construction — they decide which Microsoft sovereign clouds MAY be
    # configured (national-cloud support). At request time, followed Graph links
    # are additionally pinned to graph_base_url's OWN host (see TeamsGraphClient).
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    login_base_url: str = "https://login.microsoftonline.com"
    graph_allowed_hosts: tuple[str, ...] = (
        "graph.microsoft.com",
        "graph.microsoft.us",
        "dod-graph.microsoft.us",
        "microsoftgraph.chinacloudapi.cn",
    )
    login_allowed_hosts: tuple[str, ...] = (
        "login.microsoftonline.com",
        "login.microsoftonline.us",
        "login.chinacloudapi.cn",
    )

    # -- crawl scope (explicit — no tenant-wide crawl in v1) ----------------
    team_ids: tuple[str, ...] = ()
    include_replies: bool = True

    # -- ODCS contract facets -----------------------------------------------
    source_name: str = "teams"
    owner: str = ""
    freshness_sla: str = "PT1H"
    pii: PiiClass = PiiClass.PERSONAL

    # -- adaptive throttling ------------------------------------------------
    requests_per_second: float = Field(default=4.0, gt=0)
    burst: int = Field(default=8, ge=1)
    max_attempts: int = Field(default=5, ge=1)
    retry_after_cap_seconds: int = 60
    timeout_seconds: float = 30.0

    @model_validator(mode="after")
    def _validate(self) -> TeamsConnectorSettings:
        if not self.tenant_id or not self.client_id:
            raise ValueError("Teams connector requires non-empty tenant_id and client_id")
        if not self.owner.strip():
            raise ValueError(
                "Teams connector requires a non-empty owner (the ODCS data-contract "
                "owner; set NE_TEAMS_OWNER or owner=)"
            )
        if not self.team_ids:
            raise ValueError(
                "Teams connector requires explicit scoping: set at least one team in "
                "team_ids (no tenant-wide crawl in v1)"
            )
        if self.auth_method == "certificate":
            if not self.certificate_pem_path or not self.private_key_pem_path:
                raise ValueError(
                    "auth_method='certificate' requires both certificate_pem_path and "
                    "private_key_pem_path"
                )
        elif not self.client_secret.get_secret_value():
            raise ValueError("auth_method='secret' requires a non-empty client_secret")
        return self
