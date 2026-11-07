# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Settings for the principal store, sync sources, and query-time expansion.

``PermissionsSettings`` is an :class:`~ne.contracts.config.NeSettings` subclass,
so values layer init > ``NE_PERMISSIONS_`` env > TOML > defaults, and the client
secret is a ``SecretStr`` (masked by ``compute_config_hash``'s ``model_dump``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings

__all__ = ["PermissionsSettings"]


class PermissionsSettings(NeSettings):
    """Configuration for ne-permissions (mode, database, sync cadence, Entra).

    ``sync_interval_seconds`` IS THE REVOCATION LAG. Per the DECIDED ACL model
    (SPEC §5 NE-042) membership changes do NOT trigger re-index — chunks keep
    their stamped source ACLs and revocation moves purely at query time on the
    next sync. The target is ≤15 min (900s, the default); larger values are
    ALLOWED but move the revocation-lag SLA (documented, never silently capped).

    ``database_url`` is env ``NE_PERMISSIONS_DATABASE_URL`` (NE-PERMISSIONS-
    FOLLOWUP) — previously this class did not override the base ``NE_`` env
    prefix, so it silently read the bare ``NE_DATABASE_URL``, which caused the
    NE-103a pilot crash.
    """

    model_config = SettingsConfigDict(
        env_prefix="NE_PERMISSIONS_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    mode: Literal["entra", "oidc-claims", "fixture"] = "oidc-claims"
    database_url: str = "sqlite:///ne_permissions.db"

    #: Revocation lag = principal-sync interval (decided model). Default 900s
    #: (≤15-min target). >900 is permitted but moves the revocation-lag SLA.
    sync_interval_seconds: int = Field(default=900, ge=1)

    #: Token claim carrying the caller's group ids (oidc-claims mode).
    groups_claim: str = "groups"

    #: BFS depth guard for transitive group expansion.
    max_expansion_depth: int = Field(default=32, ge=1)

    #: Fixture JSON path (fixture mode).
    fixture_path: str | None = None

    #: Static product-principal to source-native ACL mapping. This composes with
    #: every source mode and is applied explicitly by ``ne permissions mapping apply``.
    mapping_path: Path | None = None

    # -- Entra ID (Microsoft Graph app-only) ---------------------------------
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: SecretStr = SecretStr("")
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

    @model_validator(mode="after")
    def _validate_mode_requirements(self) -> PermissionsSettings:
        if self.mode == "entra":
            if not self.entra_tenant_id or not self.entra_client_id:
                raise ValueError(
                    "mode='entra' requires non-empty entra_tenant_id and entra_client_id"
                )
        elif self.mode == "fixture" and not self.fixture_path:
            raise ValueError("mode='fixture' requires fixture_path")
        return self
