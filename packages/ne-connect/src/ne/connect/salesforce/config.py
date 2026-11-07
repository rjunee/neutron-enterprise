# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Settings for the Salesforce connector (NE-047).

``SalesforceConnectorSettings`` is an :class:`~ne.contracts.config.NeSettings`
subclass (values layer init > ``NE_SALESFORCE_*`` env > TOML > defaults). ``client_secret``
never leaves a ``SecretStr``; ``private_key_pem_path`` is a path only — the key
material itself is read from disk at signing time and never stored on the
settings object (C12, masked by ``compute_config_hash``'s ``model_dump``).

Two connected-app OAuth flows are supported:

* ``jwt_bearer`` (default) — a pre-authorized integration user + an uploaded
  certificate; the RS256 assertion is POSTed to the LOGIN host
  (``login_base_url`` — ``login.salesforce.com``/``test.salesforce.com``).
* ``client_credentials`` — a run-as user configured on the connected app; the
  request goes to the INSTANCE's own My Domain token endpoint (there is no
  ``login.salesforce.com`` client-credentials path).

``objects`` is REQUIRED non-empty — explicit per-object scoping, mirroring the
M365/SQL "no tenant-wide crawl in v1" discipline. Every object/field name a
:class:`SalesforceObjectConfig` carries is validated as a bare Salesforce API
name (``^[A-Za-z][A-Za-z0-9_]*$`` — no dots) at construction: this is the
SOQL-injection guard, since every one of these names is assembled directly into
a SOQL string by :mod:`ne.connect.salesforce.connector`. A relationship field
(e.g. ``Account.Name``) is a documented v1 cut.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from ne.connect.contracts import PiiClass
from ne.contracts.config import NeSettings

__all__ = ["SalesforceConnectorSettings", "SalesforceObjectConfig"]

#: No dots (relationship-field traversal, e.g. ``Account.Name``, is a
#: documented v1 cut) — every name entering a SOQL string must match this.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_API_VERSION_RE = re.compile(r"^v\d+\.\d$")


def _check_identifier(name: str, field: str) -> str:
    """Return ``name`` if it is a bare Salesforce API identifier, else raise.

    Blocks SOQL-injection-shaped object/field names (``"Account WHERE 1=1"``,
    ``"Name FROM Account--"``) at config-construction time — the ONLY names that
    ever enter a SOQL string built by the connector are validated here first.
    """
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(
            f"{field} {name!r} is not a valid bare Salesforce API name "
            "(must match ^[A-Za-z][A-Za-z0-9_]*$ — relationship fields with dots "
            "are a documented v1 cut)"
        )
    return name


class SalesforceObjectConfig(BaseModel):
    """One configured SObject: identity fields + document-shaping field lists."""

    model_config = ConfigDict(frozen=True)

    #: The SObject API name (e.g. ``"Account"``).
    name: str
    id_field: str = "Id"
    modstamp_field: str = "SystemModstamp"
    #: Fields joined (in order) into the document's text content.
    content_fields: tuple[str, ...] = Field(min_length=1)
    #: Fields stamped as typed metadata alongside the connector's own keys.
    metadata_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> SalesforceObjectConfig:
        _check_identifier(self.name, "name")
        _check_identifier(self.id_field, "id_field")
        _check_identifier(self.modstamp_field, "modstamp_field")
        for field in self.content_fields:
            _check_identifier(field, "content_fields item")
        for field in self.metadata_fields:
            _check_identifier(field, "metadata_fields item")
        return self


class SalesforceConnectorSettings(NeSettings):
    """Configuration for the connected-app OAuth Salesforce connector."""

    model_config = SettingsConfigDict(
        env_prefix="NE_SALESFORCE_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    # -- connected-app OAuth credentials --------------------------------------
    auth_method: Literal["jwt_bearer", "client_credentials"] = "jwt_bearer"
    #: The connected app's consumer key.
    client_id: str = ""
    #: The pre-authorized integration user (``jwt_bearer`` only).
    username: str = ""
    #: On-disk PEM path for the RS256 signing key (``jwt_bearer`` only) — the key
    #: bytes are read at signing time and never stored on this settings object.
    private_key_pem_path: str | None = None
    #: SECRET (C12) — the connected app's client secret (``client_credentials``).
    client_secret: SecretStr = SecretStr("")

    # -- endpoints (C2 allowlists) ---------------------------------------------
    #: The org's My Domain base, e.g. ``https://acme.my.salesforce.com``.
    instance_url: str = ""
    #: Entries starting with ``'.'`` are dot-suffix patterns matched via
    #: ``normalize_host(host).endswith(entry)`` (every ``*.my.salesforce.com``
    #: org); other entries must match the host EXACTLY.
    instance_allowed_hosts: tuple[str, ...] = (
        ".my.salesforce.com",
        ".sandbox.my.salesforce.com",
    )
    #: Separate authority for human-facing record links. Revoking this list
    #: must not disable the connector endpoint: the next governed sync can
    #: still remove previously persisted source links.
    source_link_allowed_hosts: tuple[str, ...] = (
        ".my.salesforce.com",
        ".sandbox.my.salesforce.com",
    )
    login_base_url: str = "https://login.salesforce.com"
    login_allowed_hosts: tuple[str, ...] = ("login.salesforce.com", "test.salesforce.com")
    #: Hosts permitted for the provider-issued OAuth Identity URL. This only
    #: validates the carrier; it never supplies or overrides organization identity.
    identity_allowed_hosts: tuple[str, ...] = (
        "login.salesforce.com",
        "test.salesforce.com",
        ".my.salesforce.com",
        ".sandbox.my.salesforce.com",
    )
    api_version: str = "v65.0"

    # -- crawl scope: explicit object list, no org-wide crawl in v1 -----------
    objects: tuple[SalesforceObjectConfig, ...] = ()

    # -- ODCS contract facets ---------------------------------------------------
    source_name: str = "salesforce"
    owner: str = ""
    freshness_sla: str = "PT1H"
    pii: PiiClass = PiiClass.PERSONAL

    # -- source-authority admission -------------------------------------------
    #: Opt-in because authority issuance is a deployment capability, not an
    #: incidental side effect of configuring Salesforce ingestion.
    authority_enabled: bool = False
    #: A complete quiet-point rotation must fit strictly inside ``freshness_sla``.
    #: All four bounds are explicit when authority is enabled; the worker derives
    #: its cron from this cadence instead of inheriting the generic connector cron.
    authority_sweep_cadence_seconds: Decimal | None = None
    authority_sweep_batch_size: int | None = None
    authority_sweep_max_batch_duration_seconds: Decimal | None = None
    authority_sweep_max_points_per_scope: int | None = None

    # -- throttle / limits --------------------------------------------------------
    requests_per_second: float = Field(default=4.0, gt=0)
    burst: int = Field(default=8, ge=1)
    max_attempts: int = Field(default=5, ge=1)
    retry_after_cap_seconds: int = 60
    timeout_seconds: float = 30.0
    #: How long a fetched access token is cached before it is refreshed.
    token_ttl_seconds: int = Field(default=1500, ge=60)
    bulk_poll_interval_seconds: float = Field(default=2.0, ge=0)
    bulk_max_polls: int = Field(default=120, ge=1)
    bulk_max_records_per_page: int = Field(default=10000, ge=1)
    #: Bound the number of ObjectPermissions REST result pages followed.
    rest_max_result_pages: int = Field(default=1000, ge=1)
    #: Bound the number of Bulk result pages followed for one object.
    bulk_max_result_pages: int = Field(default=1000, ge=1)
    #: Refuse a completed Bulk job whose declared result set exceeds this bound.
    bulk_max_records_per_job: int = Field(default=1_000_000, ge=1)

    @model_validator(mode="after")
    def _validate(self) -> SalesforceConnectorSettings:
        if not self.client_id:
            raise ValueError("Salesforce connector requires a non-empty client_id")
        if not self.owner.strip():
            raise ValueError(
                "Salesforce connector requires a non-empty owner (the ODCS "
                "data-contract owner; set NE_SALESFORCE_OWNER or owner=)"
            )
        if not self.instance_url:
            raise ValueError("Salesforce connector requires a non-empty instance_url")
        if not self.objects:
            raise ValueError(
                "Salesforce connector requires at least one configured object (no "
                "org-wide crawl in v1)"
            )
        names = [cfg.name for cfg in self.objects]
        if len(names) != len(set(names)):
            raise ValueError("SalesforceConnectorSettings.objects must have unique object names")
        if not _API_VERSION_RE.match(self.api_version):
            raise ValueError(f"api_version {self.api_version!r} must match ^v\\d+\\.\\d$")
        if self.auth_method == "jwt_bearer":
            if not self.username or not self.private_key_pem_path:
                raise ValueError(
                    "auth_method='jwt_bearer' requires both username and private_key_pem_path"
                )
        elif not self.client_secret.get_secret_value():
            raise ValueError("auth_method='client_credentials' requires a non-empty client_secret")
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
                    "authority_enabled=True requires an explicit sub-SLA Salesforce "
                    f"permission sweep; missing {sorted(missing)!r}"
                )
        return self
