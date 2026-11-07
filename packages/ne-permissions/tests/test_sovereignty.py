# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Sovereignty validator + AIR_GAP construction guard (C2)."""

from __future__ import annotations

import pytest

import ne.net.airgap as _net_airgap
import ne.permissions.sovereignty as _perm_sov
from ne.net.testing import SSRF_REDTEAM_REJECTS
from ne.permissions.config import PermissionsSettings
from ne.permissions.entra import EntraPrincipalSource
from ne.permissions.oidc import OidcClaimsPrincipalSource
from ne.permissions.sovereignty import (
    AirGapViolationError,
    UnsafeEndpointError,
    air_gap_engaged,
    validate_external_https_url,
)
from ne.permissions.testing.fixtures import FixturePrincipalSource

_GRAPH_HOSTS = ("graph.microsoft.com", "graph.microsoft.us")


@pytest.mark.parametrize("url", SSRF_REDTEAM_REJECTS)
def test_redteam_corpus_rejected(url: str) -> None:
    # Every shared SSRF vector raises THIS package's UnsafeEndpointError (proving
    # error_cls plumbing through the shared ne.net classifier).
    with pytest.raises(UnsafeEndpointError):
        validate_external_https_url(url, allowed_hosts=_GRAPH_HOSTS)


def test_air_gap_error_is_shared_class_object() -> None:
    assert _perm_sov.AirGapViolationError is _net_airgap.AirGapViolationError


@pytest.mark.parametrize(
    "url",
    [
        "http://graph.microsoft.com/v1.0",  # not https
        "https://u:p@graph.microsoft.com/v1.0",  # userinfo
        "https://graph.microsoft.com/v1.0?x=1",  # query
        "https://graph.microsoft.com/v1.0#frag",  # fragment
        "https://10.0.0.1/v1.0",  # IP literal
        "https://2852039166/v1.0",  # packed-IPv4 (decimal 169.254.169.254)
        "https://0xA9FEA9FE/v1.0",  # packed-IPv4 (hex)
        "https://169.254.169.254/v1.0",  # metadata IP
        "https://metadata.google.internal/v1.0",  # metadata host
        "https://evil.example/v1.0",  # not allowlisted
    ],
)
def test_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_external_https_url(url, allowed_hosts=_GRAPH_HOSTS)


def test_userinfo_message_hides_credential() -> None:
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_external_https_url(
            "https://user:sekret@graph.microsoft.com/v1.0", allowed_hosts=_GRAPH_HOSTS
        )
    assert "sekret" not in str(exc.value)
    assert "user" not in str(exc.value)


def test_accepts_commercial_and_sovereign() -> None:
    assert (
        validate_external_https_url("https://graph.microsoft.com/v1.0/", allowed_hosts=_GRAPH_HOSTS)
        == "https://graph.microsoft.com/v1.0"
    )
    assert (
        validate_external_https_url("https://graph.microsoft.us/v1.0", allowed_hosts=_GRAPH_HOSTS)
        == "https://graph.microsoft.us/v1.0"
    )


@pytest.mark.parametrize("token", ["1", "true", "yes", "on", "TRUE", " On "])
def test_air_gap_truthy(token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", token)
    assert air_gap_engaged() is True


@pytest.mark.parametrize("token", ["0", "false", "no", "off", ""])
def test_air_gap_falsy(token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", token)
    assert air_gap_engaged() is False


def test_air_gap_blocks_entra_but_not_oidc_or_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    settings = PermissionsSettings(mode="entra", entra_tenant_id="t", entra_client_id="c")
    with pytest.raises(AirGapViolationError):
        EntraPrincipalSource(settings)
    # Egress-free sources still construct under AIR_GAP.
    OidcClaimsPrincipalSource(settings)
    FixturePrincipalSource("fixture", [])


def test_entra_rejects_non_allowlisted_graph_url() -> None:
    settings = PermissionsSettings(
        mode="entra",
        entra_tenant_id="t",
        entra_client_id="c",
        graph_base_url="https://evil.example/v1.0",
    )
    with pytest.raises(UnsafeEndpointError):
        EntraPrincipalSource(settings)
