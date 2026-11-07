# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pure-unit matrix for the NE-CKC2 sovereignty guards (``ne.brain.sovereignty``).

Covers ``air_gap_engaged`` token semantics and ``validate_in_vpc_endpoint``'s
accept/reject matrix — SSRF shape (scheme/port/creds/query/fragment/metadata/
link-local), packed-IPv4 rejection (audit #10, decimal/hex/octal), in-VPC shapes,
the ``allowed_hosts`` escape hatch with case/trailing-dot normalization, and the
no-credential-in-error-message redaction rule.
"""

from __future__ import annotations

import pytest

import ne.brain.sovereignty as _brain_sov
import ne.net.airgap as _net_airgap
from ne.brain.sovereignty import (
    UnsafeEndpointError,
    air_gap_engaged,
    validate_in_vpc_endpoint,
)
from ne.net.testing import SSRF_REDTEAM_REJECTS


def test_shim_reexports_same_air_gap_objects() -> None:
    # NE-CKC10 / P2-23: ne.brain.sovereignty re-exports the ONE ne.net.airgap guard
    # as the SAME class object / function, so out-of-scope importers and any
    # cross-package isinstance keep working unchanged.
    assert _brain_sov.AirGapViolationError is _net_airgap.AirGapViolationError
    assert _brain_sov.air_gap_engaged is _net_airgap.air_gap_engaged


@pytest.mark.parametrize(
    "value",
    ["1", "true", "TRUE", " yes ", "on"],
)
def test_air_gap_engaged_truthy_tokens(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AIR_GAP", value)
    assert air_gap_engaged() is True


@pytest.mark.parametrize("value", ["", "0", "off", "no", "disabled"])
def test_air_gap_engaged_falsy_tokens(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AIR_GAP", value)
    assert air_gap_engaged() is False


def test_air_gap_engaged_unset_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIR_GAP", raising=False)
    assert air_gap_engaged() is False


@pytest.mark.parametrize(
    ("endpoint", "expected", "allowed_hosts"),
    [
        ("http://tei.local", "http://tei.local", ()),
        ("http://tei.local/", "http://tei.local", ()),
        ("http://localhost:8085", "http://localhost:8085", ()),
        ("http://tei-embed", "http://tei-embed", ()),
        ("http://10.0.0.5:8085", "http://10.0.0.5:8085", ()),
        # IPv4 and IPv6 loopback are both accepted. Per NE-CKC10, '::1' now matches
        # 127.0.0.1 exactly: the shared ne-net validator exempts loopback from the
        # is_reserved hard-reject (::1 is is_reserved=True on CPython, which used to
        # cause a spurious Phase-A rejection here).
        ("http://127.0.0.1:8085", "http://127.0.0.1:8085", ()),
        ("http://[::1]:8085", "http://[::1]:8085", ()),
        ("http://192.168.1.9", "http://192.168.1.9", ()),
        (
            "https://tei.corp.example.com",
            "https://tei.corp.example.com",
            ("tei.corp.example.com",),
        ),
        # case- and trailing-dot-insensitive allowlist match.
        (
            "https://tei.corp.example.com",
            "https://tei.corp.example.com",
            ("TEI.CORP.EXAMPLE.COM.",),
        ),
    ],
)
def test_validate_in_vpc_endpoint_accepts(
    endpoint: str, expected: str, allowed_hosts: tuple[str, ...]
) -> None:
    assert validate_in_vpc_endpoint(endpoint, allowed_hosts=allowed_hosts) == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://tei.local",  # scheme
        "http://tei.local:99999",  # out-of-range port
        "http://tei.local/embed?token=x",  # query string
        "http://tei.local/#frag",  # fragment
        "http://169.254.169.254",  # metadata host
        "http://metadata.google.internal",  # metadata host
        "http://[fe80::1]",  # link-local IPv6
        "http://2852039166",  # packed IPv4 (decimal) == 169.254.169.254
        "http://0xA9FEA9FE",  # packed IPv4 (hex)
        "http://025177524776",  # packed IPv4 (octal)
        "https://evil.example.com",  # external DNS, no allowlist
        "https://tei.corp.example.com",  # external-shaped, allowlist missing
        "http://",  # missing hostname
    ],
)
def test_validate_in_vpc_endpoint_rejects(endpoint: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_in_vpc_endpoint(endpoint)


def test_validate_in_vpc_endpoint_rejects_embedded_credentials_and_redacts() -> None:
    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_in_vpc_endpoint("http://user:pass@tei.local")
    # Redaction rule: an embedded credential never appears in the error message.
    assert "pass" not in str(excinfo.value)
    assert "user" not in str(excinfo.value)


@pytest.mark.parametrize("endpoint", SSRF_REDTEAM_REJECTS)
def test_redteam_corpus_rejected(endpoint: str) -> None:
    # NE-CKC10: the shared red-team corpus is rejected identically by all three
    # consumers of the ne-net validator.
    with pytest.raises(UnsafeEndpointError):
        validate_in_vpc_endpoint(endpoint)
