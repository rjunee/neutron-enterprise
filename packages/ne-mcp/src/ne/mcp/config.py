# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layered settings for ne-mcp — the private in-VPC MCP server catalog (C2).

The catalog is a config-driven ALLOWLIST of trusted internal MCP servers. Each
entry is a frozen, ``extra='forbid'`` model (a typo in ``ne.toml`` fails loudly).
Two invariants are enforced at model construction, before any network activity:

  * ``name`` must match ``^[a-z][a-z0-9_-]{0,63}$`` — it is prefix-locked into every
    discovered tool name (``write.external.mcp.<name>.<tool>``), so it can never
    carry a dot or exotic char that might game tier classification.
  * ``tier`` may only be Tier-2 or Tier-3. An external MCP server is an UNTRUSTED
    tool provider; its class floor is Tier-2 (``write.external.``) and it may only
    ESCALATE to Tier-3, never relax below the floor.

Endpoint *reachability/SSRF* validation (C2) is NOT done here — it is done fail-loud
at composition time by :func:`ne.mcp.catalog.validated_catalog` via
``ne.net.validate_in_vpc_endpoint``. Constructing an entry only shapes it; wiring it
into the platform validates it. The validated output type is
:class:`ne.mcp.catalog.ValidatedCatalogEntry`, which is the ONLY entry type
:class:`~ne.mcp.client.ExternalMcpToolProvider` accepts — a raw ``McpServerCatalogEntry``
can shape config but can never reach a connection.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings
from ne.contracts.types import ApprovalTier

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class McpServerCatalogEntry(BaseModel):
    """One trusted in-VPC external MCP server (frozen allowlist entry)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Catalog id; prefix-locked into ``write.external.mcp.<name>.<tool>``.
    name: str
    #: The server endpoint (validated in-VPC/SSRF at composition, not here).
    endpoint: str
    #: Approval tier of every tool discovered from this server (Tier-2 floor).
    tier: ApprovalTier = ApprovalTier.TIER_2
    #: Extra in-VPC private-DNS hostnames to accept for THIS entry's endpoint,
    #: passed straight to ``ne.net.validate_in_vpc_endpoint(allowed_hosts=...)``.
    allowed_hosts: tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError(
                f"catalog server name {value!r} must match ^[a-z][a-z0-9_-]{{0,63}}$ "
                "(prefix-locked into the tool namespace; no dots/exotic chars)"
            )
        return value

    @field_validator("tier")
    @classmethod
    def _validate_tier(cls, value: ApprovalTier) -> ApprovalTier:
        if int(value) < int(ApprovalTier.TIER_2):
            raise ValueError(
                f"catalog server tier {int(value)} is below the Tier-2 floor; an external "
                "MCP server is an untrusted tool provider and may only escalate to Tier-3"
            )
        return value


class McpSettings(NeSettings):
    """Settings for the ne-mcp adapter — the in-VPC server catalog allowlist."""

    model_config = SettingsConfigDict(
        env_prefix="NE_MCP_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    #: The config-driven allowlist of trusted in-VPC external MCP servers.
    catalog: tuple[McpServerCatalogEntry, ...] = ()
