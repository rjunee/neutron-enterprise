# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C2 source-URL validation matrix (validate_source_url)."""

from __future__ import annotations

import pytest

from ne.connect import validate_source_url
from ne.connect.endpoints import AirGapViolationError, UnsafeEndpointError
from ne.net.testing import SSRF_REDTEAM_REJECTS


@pytest.mark.parametrize("url", SSRF_REDTEAM_REJECTS)
@pytest.mark.parametrize("allow_external", [True, False])
def test_redteam_corpus_rejected_both_knobs(url: str, allow_external: bool) -> None:
    # AIR_GAP off: every shared SSRF vector raises UnsafeEndpointError whether or not
    # external sources are allowed (the corpus is never in-VPC and never a safe
    # external DNS host).
    with pytest.raises(UnsafeEndpointError):
        validate_source_url(url, allow_external=allow_external)


def test_in_vpc_dotless_service_ok() -> None:
    assert validate_source_url("http://qdrant:6333") == "http://qdrant:6333"


def test_in_vpc_private_ip_ok() -> None:
    assert validate_source_url("http://10.0.0.5:8080") == "http://10.0.0.5:8080"


def test_external_https_allowed_by_default() -> None:
    url = "https://acme.my.salesforce.com"
    assert validate_source_url(url) == url


def test_external_rejected_when_allow_external_false() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_source_url("https://acme.my.salesforce.com", allow_external=False)


def test_external_non_tls_rejected() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_source_url("http://acme.my.salesforce.com")


def test_embedded_credentials_rejected_without_leaking_password() -> None:
    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_source_url("https://user:sup3rs3cret@acme.example.com/")
    assert "sup3rs3cret" not in str(excinfo.value)


@pytest.mark.parametrize("host", ["169.254.169.254", "metadata.google.internal"])
def test_metadata_hosts_rejected(host: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_source_url(f"https://{host}/")


@pytest.mark.parametrize("host", ["2852039166", "0xA9FEA9FE"])
def test_packed_ipv4_rejected(host: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_source_url(f"https://{host}/")


def test_external_ip_literal_rejected() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_source_url("https://8.8.8.8/")


def test_air_gap_blocks_external_but_allows_in_vpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(AirGapViolationError):
        validate_source_url("https://acme.my.salesforce.com")
    # in-VPC still passes under AIR_GAP
    assert validate_source_url("http://qdrant:6333") == "http://qdrant:6333"
