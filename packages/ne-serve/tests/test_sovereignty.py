# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Accept/reject matrix for the C2 construction-time endpoint validator.

Pure unit tests over :func:`ne.serve.sovereignty.validate_in_vpc_endpoint`,
mirroring NE-CKC2's ne-brain test shape. Phase A (unconditional SSRF-shape
rejections), Phase B (accept only provably in-VPC), the allowlist escape hatch
with case + trailing-dot normalization, the no-credential-in-error rule, and the
trailing-slash strip.
"""

from __future__ import annotations

import pytest

from ne.net.testing import SSRF_REDTEAM_REJECTS
from ne.serve.errors import UnsafeEndpointError
from ne.serve.sovereignty import validate_in_vpc_endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://vllm.local",  # scheme
        "http://host:99999",  # out-of-range port
        "http://vllm.local/?k=v",  # query
        "http://vllm.local/#f",  # fragment
        "http://",  # missing host
        "http://169.254.169.254",  # metadata IP
        "http://metadata.google.internal",  # metadata host
        "http://169.254.10.10",  # link-local range
        "http://224.0.0.1",  # multicast
        "http://0.0.0.0",  # unspecified
        "http://2852039166",  # packed decimal
        "http://0xA9FEA9FE",  # packed hex
        "http://025177524776",  # packed octal
        "https://evil.example.com",  # external (Phase B)
    ],
)
def test_rejects_ssrf_and_external(endpoint: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_in_vpc_endpoint(endpoint)


def test_credentials_rejected_and_not_leaked_in_message() -> None:
    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_in_vpc_endpoint("http://user:pass@vllm.local")
    # The no-credential-leak rule: the secret never enters the error text.
    assert "pass" not in str(excinfo.value)
    assert "user" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://localhost:8090/", "http://localhost:8090"),  # trailing-slash strip
        ("http://127.0.0.1:8090", "http://127.0.0.1:8090"),  # IPv4 loopback
        # NE-CKC10: IPv6 loopback ::1 now matches 127.0.0.1 exactly.
        ("http://[::1]:8090", "http://[::1]:8090"),  # IPv6 loopback
        ("http://10.0.0.5:8000", "http://10.0.0.5:8000"),  # private IP
        ("http://vllm-serve:8000", "http://vllm-serve:8000"),  # dotless service
        ("http://vllm.local", "http://vllm.local"),  # .local suffix
        ("http://svc.internal", "http://svc.internal"),  # .internal suffix
    ],
)
def test_accepts_in_vpc(endpoint: str, expected: str) -> None:
    assert validate_in_vpc_endpoint(endpoint) == expected


def test_allowlist_normalizes_case_and_trailing_dot() -> None:
    # Allowlisted private DNS: case + trailing-dot normalization on both sides.
    assert (
        validate_in_vpc_endpoint(
            "https://vllm.corp.example.com",
            allowed_hosts=("VLLM.corp.example.com.",),
        )
        == "https://vllm.corp.example.com"
    )


def test_external_without_allowlist_is_rejected() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_in_vpc_endpoint("https://vllm.corp.example.com")


@pytest.mark.parametrize("endpoint", SSRF_REDTEAM_REJECTS)
def test_redteam_corpus_rejected(endpoint: str) -> None:
    # NE-CKC10: the shared red-team corpus is rejected identically by all three
    # consumers of the ne-net validator.
    with pytest.raises(UnsafeEndpointError):
        validate_in_vpc_endpoint(endpoint)
