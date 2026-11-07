# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance (b): the private in-VPC server catalog is C2-validated fail-loud.

``validated_catalog`` runs the consolidated ne.net in-VPC/SSRF validator over every
catalog endpoint: metadata hosts, packed-integer IPv4, external hosts, and
credential-embedded URLs are rejected (and the raised message never echoes a secret);
dotless service names, ``::1``, and explicitly allowlisted private-DNS hosts are
accepted; duplicate names and a tier below the Tier-2 floor are refused.
"""

from __future__ import annotations

import pytest

from ne.contracts.types import ApprovalTier
from ne.mcp.catalog import validated_catalog
from ne.mcp.config import McpServerCatalogEntry, McpSettings
from ne.mcp.errors import McpCatalogError


def _settings(*entries: McpServerCatalogEntry) -> McpSettings:
    return McpSettings(catalog=entries)


def _entry(endpoint: str, *, name: str = "ext1", **kw) -> McpServerCatalogEntry:
    return McpServerCatalogEntry(name=name, endpoint=endpoint, **kw)


def test_metadata_endpoint_rejected() -> None:
    with pytest.raises(McpCatalogError):
        validated_catalog(_settings(_entry("http://169.254.169.254/")))


def test_external_host_rejected() -> None:
    with pytest.raises(McpCatalogError):
        validated_catalog(_settings(_entry("https://api.evil.example.com")))


def test_packed_ipv4_rejected() -> None:
    with pytest.raises(McpCatalogError):
        validated_catalog(_settings(_entry("http://2852039166/")))


def test_embedded_credentials_rejected() -> None:
    with pytest.raises(McpCatalogError) as excinfo:
        validated_catalog(_settings(_entry("http://user:secret@qdrant:6333")))
    # ne.net omits userinfo from its messages by design — the secret never leaks.
    assert "secret" not in str(excinfo.value)


def test_dotless_service_name_accepted() -> None:
    catalog = validated_catalog(_settings(_entry("http://mcp-ext1:8080")))
    assert catalog[0].endpoint == "http://mcp-ext1:8080"


def test_allowed_hosts_entry_accepted() -> None:
    catalog = validated_catalog(
        _settings(_entry("https://tools.corp.example", allowed_hosts=("tools.corp.example",)))
    )
    assert catalog[0].endpoint == "https://tools.corp.example"
    # The SAME endpoint WITHOUT an allowlist entry looks external and is rejected.
    with pytest.raises(McpCatalogError):
        validated_catalog(_settings(_entry("https://tools.corp.example")))


def test_duplicate_server_names_rejected() -> None:
    with pytest.raises(McpCatalogError):
        validated_catalog(
            _settings(
                _entry("http://mcp-ext1:8080", name="dup"),
                _entry("http://mcp-ext2:8080", name="dup"),
            )
        )


def test_tier_below_2_rejected() -> None:
    with pytest.raises(ValueError):
        McpServerCatalogEntry(
            name="ext1", endpoint="http://mcp-ext1:8080", tier=ApprovalTier.TIER_1
        )


def test_ipv6_loopback_accepted() -> None:
    catalog = validated_catalog(_settings(_entry("http://[::1]:9000")))
    assert catalog[0].endpoint == "http://[::1]:9000"
