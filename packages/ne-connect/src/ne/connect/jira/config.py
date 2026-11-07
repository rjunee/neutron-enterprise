# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Settings for the Jira Cloud connector (NE-093).

``JiraConnectorSettings`` is an :class:`~ne.contracts.config.NeSettings`
subclass (values layer init > ``NE_JIRA_*`` env > TOML > defaults). ``api_token``
never leaves a ``SecretStr`` (C12, masked by ``compute_config_hash``'s
``model_dump``).

``projects`` is REQUIRED non-empty — explicit per-project scoping (no site-wide
crawl in v1), mirroring the Salesforce/M365/SQL discipline. Every project key
and issue field name is validated at construction: the project key against
``^[A-Z][A-Z0-9_]{1,63}$`` and each issue field against
``^[a-zA-Z][a-zA-Z0-9_]*$``. This is the **JQL-injection guard**, since the
project key is assembled directly into a JQL string (``project = ACME ...``) by
:mod:`ne.connect.jira.connector` and the fields join a REST ``fields`` param —
mirroring Salesforce's ``_check_identifier`` discipline.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from ne.connect.contracts import PiiClass
from ne.contracts.config import NeSettings

__all__ = ["JiraConnectorSettings", "JiraProjectConfig"]

#: Uppercase Jira project key (``ACME``, ``SECRET``) — the JQL-injection guard.
_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
#: A bare Jira field name entering a REST ``fields`` param (no dots/operators).
_FIELD_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


class JiraProjectConfig(BaseModel):
    """One configured Jira project: key + per-project stream toggles."""

    model_config = ConfigDict(frozen=True)

    #: The project key (e.g. ``"ACME"``) — assembled into JQL, so validated.
    key: str
    include_comments: bool = True
    include_sprints: bool = True

    @model_validator(mode="after")
    def _validate(self) -> JiraProjectConfig:
        if not _PROJECT_KEY_RE.match(self.key):
            raise ValueError(
                f"Jira project key {self.key!r} is not a valid key "
                "(must match ^[A-Z][A-Z0-9_]{1,63}$ — this is the JQL-injection guard)"
            )
        return self


class JiraConnectorSettings(NeSettings):
    """Configuration for the Basic-auth Jira Cloud connector."""

    model_config = SettingsConfigDict(
        env_prefix="NE_JIRA_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    # -- Jira Cloud credentials + endpoint (C2 allowlist) --------------------
    #: The Jira Cloud site base, e.g. ``https://acme.atlassian.net``.
    base_url: str = ""
    #: Entries starting with ``'.'`` are dot-suffix patterns (every
    #: ``*.atlassian.net`` site); other entries must match the host EXACTLY.
    allowed_hosts: tuple[str, ...] = (".atlassian.net",)
    #: The Atlassian account email paired with the API token (Basic auth).
    email: str = ""
    #: SECRET (C12) — the Atlassian API token.
    api_token: SecretStr = SecretStr("")

    # -- crawl scope: explicit project list, no site-wide crawl in v1 --------
    projects: tuple[JiraProjectConfig, ...] = ()
    #: Issue fields requested from the search API and shaped into documents.
    issue_fields: tuple[str, ...] = (
        "summary",
        "description",
        "status",
        "issuetype",
        "updated",
        "security",
    )
    page_size: int = Field(default=100, ge=1, le=100)

    # -- ODCS contract facets ------------------------------------------------
    source_name: str = "jira"
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
    def _validate(self) -> JiraConnectorSettings:
        if not self.base_url:
            raise ValueError("Jira connector requires a non-empty base_url")
        if not self.email:
            raise ValueError("Jira connector requires a non-empty email")
        if not self.api_token.get_secret_value():
            raise ValueError("Jira connector requires a non-empty api_token")
        if not self.owner.strip():
            raise ValueError(
                "Jira connector requires a non-empty owner (the ODCS data-contract "
                "owner; set NE_JIRA_OWNER or owner=)"
            )
        if not self.projects:
            raise ValueError(
                "Jira connector requires at least one configured project (no site-wide crawl in v1)"
            )
        keys = [cfg.key for cfg in self.projects]
        if len(keys) != len(set(keys)):
            raise ValueError("JiraConnectorSettings.projects must have unique keys")
        for field in self.issue_fields:
            if not _FIELD_RE.match(field):
                raise ValueError(
                    f"issue_fields item {field!r} is not a valid bare Jira field name "
                    "(must match ^[a-zA-Z][a-zA-Z0-9_]*$)"
                )
        return self
