# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The exact Codex-P2 bypass: constructing :class:`ExternalMcpToolProvider` from
anything but a :class:`ValidatedCatalogEntry` is impossible, so no session is ever
created to an unvalidated endpoint.

Every construction path to an external endpoint passes ``ne.net.validate_in_vpc_endpoint``:
the proof type validates in its own before-validator (unsafe state unrepresentable), the
provider type-gates on it (``TypeError`` otherwise), and the provider re-runs the validator
to defeat a ``model_copy(update=...)`` forgery. The real ne.net validator is used — no
mocking past the seam — and an injected counting session factory proves NO session is ever
opened toward a rejected endpoint.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

from ne.mcp.catalog import ValidatedCatalogEntry, validated_catalog
from ne.mcp.client import ExternalMcpToolProvider
from ne.mcp.config import McpServerCatalogEntry, McpSettings
from ne.mcp.errors import McpCatalogError


class CountingFactory:
    """A SessionFactory test double that must NEVER be invoked for a rejected provider.

    Construction is connection-free, so any invocation would be a bug. ``calls`` proves
    no session was opened toward an unvalidated endpoint.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, entry: ValidatedCatalogEntry) -> AbstractAsyncContextManager[Any]:
        self.calls += 1
        raise AssertionError("session factory must never be invoked for a rejected provider")


def _raw(endpoint: str) -> McpServerCatalogEntry:
    # Raw entries construct fine — they carry NO endpoint validation, which is exactly the
    # point: shaping config is not the same as authorizing a connection.
    return McpServerCatalogEntry(name="ext1", endpoint=endpoint)


def test_raw_entry_external_host_rejected_at_construction() -> None:
    factory = CountingFactory()
    with pytest.raises(TypeError):
        # deliberate wrong type — the runtime TypeError is the whole point.
        ExternalMcpToolProvider(_raw("https://api.evil.example.com"), session_factory=factory)  # pyright: ignore[reportArgumentType]
    assert factory.calls == 0


def test_raw_entry_metadata_ip_rejected_at_construction() -> None:
    factory = CountingFactory()
    with pytest.raises(TypeError):
        # deliberate wrong type — the runtime TypeError is the whole point.
        ExternalMcpToolProvider(_raw("http://169.254.169.254/"), session_factory=factory)  # pyright: ignore[reportArgumentType]
    assert factory.calls == 0


def test_raw_settings_catalog_rejected_at_construction() -> None:
    # The finding's named path: an element straight off raw McpSettings.catalog.
    settings = McpSettings(
        catalog=(McpServerCatalogEntry(name="ext1", endpoint="http://169.254.169.254/"),)
    )
    factory = CountingFactory()
    with pytest.raises(TypeError):
        # deliberate wrong type (a raw McpSettings.catalog element) — TypeError expected.
        ExternalMcpToolProvider(settings.catalog[0], session_factory=factory)  # pyright: ignore[reportArgumentType]
    assert factory.calls == 0


def test_validated_entry_direct_construction_runs_c2() -> None:
    # Even DIRECT construction of the proof type cannot yield an unvalidated endpoint.
    with pytest.raises(McpCatalogError):
        ValidatedCatalogEntry(name="ext1", endpoint="http://169.254.169.254/")
    with pytest.raises(McpCatalogError):
        ValidatedCatalogEntry(name="ext1", endpoint="https://api.evil.example.com")


def test_validated_entry_normalizes_endpoint() -> None:
    entry = ValidatedCatalogEntry(name="ext1", endpoint="http://mcp-ext1:8080/")
    # Validator-normalized (trailing slash stripped) exactly like the old validated_catalog.
    assert entry.endpoint == "http://mcp-ext1:8080"


def test_forged_model_copy_rejected_by_provider() -> None:
    good = validated_catalog(
        McpSettings(catalog=(McpServerCatalogEntry(name="ext1", endpoint="http://mcp-ext1:8080"),))
    )[0]
    # model_copy skips validators by pydantic design — the forgery would otherwise carry an
    # unvalidated endpoint; the provider's defense-in-depth re-validation catches it.
    bad = good.model_copy(update={"endpoint": "http://169.254.169.254"})
    factory = CountingFactory()
    with pytest.raises(McpCatalogError):
        ExternalMcpToolProvider(bad, session_factory=factory)
    assert factory.calls == 0


def test_forged_allowed_hosts_smuggling_rejected() -> None:
    # NE-CLEAN1 item 4: the pre-fix defense-in-depth re-validation trusted the
    # entry's OWN (forgeable) allowed_hosts field, so a forgery that bundles a
    # matching allowed_hosts alongside the smuggled external endpoint sailed
    # through. The hardened provider consults ONLY the caller-supplied trusted
    # allowlist (default empty), never the forged entry's own field.
    good = validated_catalog(
        McpSettings(catalog=(McpServerCatalogEntry(name="ext1", endpoint="http://mcp-ext1:8080"),))
    )[0]
    bad = good.model_copy(
        update={
            "endpoint": "https://api.evil.example.com",
            "allowed_hosts": ("api.evil.example.com",),
        }
    )
    factory = CountingFactory()
    with pytest.raises(McpCatalogError):
        ExternalMcpToolProvider(bad, session_factory=factory)
    assert factory.calls == 0


def test_trusted_allowed_hosts_from_settings_admits_operator_declared_host() -> None:
    # Per-entry operator-declared allowlists still work — but ONLY when the
    # composition root explicitly passes the TRUSTED (settings-derived) list, not
    # when it trusts the (possibly forged) entry's own field.
    settings = McpSettings(
        catalog=(
            McpServerCatalogEntry(
                name="ext1",
                endpoint="http://mcp-gw.corp-vpc.example:8443",
                allowed_hosts=("mcp-gw.corp-vpc.example",),
            ),
        )
    )
    entry = validated_catalog(settings)[0]

    factory_ok = CountingFactory()
    ExternalMcpToolProvider(
        entry,
        trusted_allowed_hosts=settings.catalog[0].allowed_hosts,
        session_factory=factory_ok,
    )
    assert factory_ok.calls == 0

    factory_fail = CountingFactory()
    with pytest.raises(McpCatalogError):
        # No trusted_allowed_hosts passed — fail-closed default rejects the
        # dotted, non-.internal host even though the entry's OWN allowed_hosts
        # would have admitted it.
        ExternalMcpToolProvider(entry, session_factory=factory_fail)
    assert factory_fail.calls == 0


def test_validated_catalog_entries_accepted() -> None:
    good = validated_catalog(
        McpSettings(catalog=(McpServerCatalogEntry(name="ext1", endpoint="http://mcp-ext1:8080"),))
    )
    assert all(isinstance(e, ValidatedCatalogEntry) for e in good)
    factory = CountingFactory()
    # Construction is connection-free — the sanctioned path constructs without opening a
    # session (discovery-path behavior is covered by test_client_governed.py).
    ExternalMcpToolProvider(good[0], session_factory=factory)
    assert factory.calls == 0
