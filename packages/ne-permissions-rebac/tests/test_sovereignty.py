# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""In-VPC OpenFGA URL validation — SSRF/public rejections, hostname-only errors."""

from __future__ import annotations

import pytest

from ne.net.testing import SSRF_REDTEAM_REJECTS
from ne.permissions_rebac.errors import UnsafeEndpointError
from ne.permissions_rebac.sovereignty import validate_openfga_url


@pytest.mark.parametrize(
    "url",
    [
        "http://openfga:8081",
        "https://10.0.0.5:8081",
        "http://localhost:8081",
        "http://127.0.0.1:8081",
        "http://openfga.internal:8081",
    ],
)
def test_in_vpc_urls_pass(url: str) -> None:
    assert validate_openfga_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", SSRF_REDTEAM_REJECTS)
def test_redteam_corpus_rejected(url: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_openfga_url(url)


def test_ipv6_loopback_now_accepted_like_ipv4() -> None:
    # INTENDED FIX (NE-CKC10 / P2-23): ::1 is accepted exactly like 127.0.0.1. The
    # old local mirror double-classified ::1 via is_reserved and rejected it — this
    # assertion FAILS on main@528aeab, proving the fix is real.
    assert validate_openfga_url("http://[::1]:8081") == "http://[::1]:8081"
    assert validate_openfga_url("http://127.0.0.1:8081") == "http://127.0.0.1:8081"


def test_public_dns_host_rejected() -> None:
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_openfga_url("https://evil.example.com:8081/path")
    assert "evil.example.com" in str(exc.value)
    assert "example.com:8081/path" not in str(exc.value)  # not the full URL


def test_cloud_metadata_ip_rejected() -> None:
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_openfga_url("http://169.254.169.254/latest/meta-data")
    assert "169.254.169.254" in str(exc.value)


def test_packed_ipv4_rejected() -> None:
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_openfga_url("http://0x7f000001/")
    assert "0x7f000001" in str(exc.value)


def test_userinfo_rejected_without_leaking_secret() -> None:
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_openfga_url("http://user:sekret@openfga/")
    assert "sekret" not in str(exc.value)


def test_non_http_scheme_rejected() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_openfga_url("ftp://openfga:8081")
