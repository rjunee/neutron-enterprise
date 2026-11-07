# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Settings for the M365 / SharePoint connector (NE-044).

``M365ConnectorSettings`` is an :class:`~ne.contracts.config.NeSettings` subclass
(values layer init > ``NE_M365_*`` env > TOML > defaults). Shared Graph/login fields
mirror ``ne.permissions.config.PermissionsSettings`` field-for-field so a tenant
configured for principal sync configures identically here. The client secret and
private-key material never leave a ``SecretStr`` / on-disk PEM path — masked by
``compute_config_hash``'s ``model_dump`` and never echoed (C12).

Explicit scoping is MANDATORY: at least one of ``drive_ids`` / ``site_ids`` must
be set. There is deliberately NO tenant-wide crawl in v1 — an unbounded crawl is
both a throttling and a blast-radius hazard. File-body downloads are bounded and
redirects are deny-by-default: a tenant SharePoint download host must be named in
``content_allowed_hosts`` before a response-supplied redirect can be followed.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from ne.connect.contracts import PiiClass
from ne.contracts.config import NeSettings

__all__ = ["M365ConnectorSettings"]


class M365ConnectorSettings(NeSettings):
    """Configuration for the Graph app-only M365/SharePoint connector."""

    model_config = SettingsConfigDict(
        env_prefix="NE_M365_",
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
    # are additionally pinned to graph_base_url's OWN host, so a link to a
    # different cloud is rejected even though that other cloud appears in this list
    # (see GraphClient._guard_graph_url).
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
    #: Exact DNS hosts allowed for the short-lived, pre-authenticated redirect
    #: returned by Graph's canonical ``/content`` endpoint. Empty is deliberately
    #: fail-closed: direct Graph 200 responses still work, but no response-supplied
    #: cross-host URL is followed. Never put a URL or credential here, only hosts.
    content_allowed_hosts: tuple[str, ...] = ()
    #: Exact tenant SharePoint hosts accepted for stable, user-facing driveItem
    #: ``webUrl`` provenance. This is deliberately separate from
    #: ``content_allowed_hosts``: short-lived pre-authenticated download hosts
    #: are never source-of-record link authority. Empty omits ``source_url``.
    source_link_allowed_hosts: tuple[str, ...] = ()

    # -- crawl scope (explicit — no tenant-wide crawl in v1) ----------------
    drive_ids: tuple[str, ...] = ()
    site_ids: tuple[str, ...] = ()

    # -- ODCS contract facets -----------------------------------------------
    #: ``owner`` is validated non-empty EARLY in ``_validate`` (fail clearly at
    #: settings construction) rather than failing late at ``m365_data_contract``.
    source_name: str = "m365-sharepoint"
    owner: str = ""
    freshness_sla: str = "PT1H"
    pii: PiiClass = PiiClass.PERSONAL

    # -- adaptive throttling ------------------------------------------------
    requests_per_second: float = Field(default=4.0, gt=0)
    burst: int = Field(default=8, ge=1)
    max_attempts: int = Field(default=5, ge=1)
    retry_after_cap_seconds: int = 60
    timeout_seconds: float = 30.0

    # -- source-authority admission (015A0-AUTHORITY-M365) ------------------
    #: Whether this connector issues CurrentSourceAuthority receipts. Off by
    #: default: authority is a capability a deployment opts into, never something
    #: a connector acquires by being configured.
    authority_enabled: bool = False
    #: The explicit permission-sweep cadence, in seconds. An authority-enabled
    #: M365 source may NOT inherit the generic hourly connector cron: with a PT1H
    #: contract SLA an hourly cadence can never re-read every point inside the
    #: SLA. All four sweep values are REQUIRED once ``authority_enabled`` is set,
    #: they are checked against the SLA at composition, the worker DERIVES this
    #: source's cron from the cadence rather than inheriting one, and
    #: ``batch_size`` bounds each run's rotating re-read of admitted points.
    authority_sweep_cadence_seconds: Decimal | None = None
    #: Admitted points re-read per sweep batch.
    authority_sweep_batch_size: int | None = None
    #: The conservative ceiling on one batch's duration, in seconds.
    authority_sweep_max_batch_duration_seconds: Decimal | None = None
    #: The admitted maximum number of points in one reconciliation scope.
    authority_sweep_max_points_per_scope: int | None = None

    # -- file-body ingestion ------------------------------------------------
    #: Both Graph metadata and streamed response bytes are checked against this
    #: ceiling before Docling sees the file. A response without Content-Length is
    #: still bounded while streaming.
    max_content_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    max_content_redirects: int = Field(default=3, ge=0, le=5)

    @model_validator(mode="after")
    def _validate(self) -> M365ConnectorSettings:
        if not self.tenant_id or not self.client_id:
            raise ValueError("M365 connector requires non-empty tenant_id and client_id")
        if not self.owner.strip():
            raise ValueError(
                "M365 connector requires a non-empty owner (the ODCS data-contract "
                "owner; set NE_M365_OWNER or owner=)"
            )
        if not self.drive_ids and not self.site_ids:
            raise ValueError(
                "M365 connector requires explicit scoping: set at least one of "
                "drive_ids / site_ids (no tenant-wide crawl in v1)"
            )
        if self.auth_method == "certificate":
            if not self.certificate_pem_path or not self.private_key_pem_path:
                raise ValueError(
                    "auth_method='certificate' requires both certificate_pem_path and "
                    "private_key_pem_path"
                )
        elif not self.client_secret.get_secret_value():
            raise ValueError("auth_method='secret' requires a non-empty client_secret")
        if self.authority_enabled:
            missing = [
                name
                for name in (
                    "authority_sweep_cadence_seconds",
                    "authority_sweep_batch_size",
                    "authority_sweep_max_batch_duration_seconds",
                    "authority_sweep_max_points_per_scope",
                )
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    "authority_enabled=True requires an EXPLICIT sub-SLA permission sweep; "
                    f"missing {sorted(missing)!r}. An authority-bearing M365 source may not "
                    "inherit the generic hourly connector cron."
                )
        return self
