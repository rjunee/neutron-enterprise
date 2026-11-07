# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared SSRF red-team corpus (NE-CKC10).

Plain data (stdlib-only, no third-party imports) importable by every consumer's
test suite so all three validators are proven to reject the identical bypass set.
Mirrors the ``ne.contracts.testing`` shared-fixture precedent.

Contract of :data:`SSRF_REDTEAM_REJECTS`: every vector is rejected by ALL THREE
consumers under EVERY policy knob — gateway ``validate_endpoint_url`` with
``allow_private`` both ``True`` and ``False``, and in-VPC ``validate_in_vpc_endpoint``
with and without an allowlist. Vectors that any validator accepts must be removed,
never worked around.
"""

from __future__ import annotations

SSRF_REDTEAM_REJECTS: tuple[str, ...] = (
    "ftp://vllm.local",  # non-http(s) scheme
    "http://host:99999",  # out-of-range port
    "http://user:pass@vllm.local",  # embedded credentials
    "http://vllm.local/?k=v",  # query string
    "http://vllm.local/#f",  # fragment
    "http://",  # missing hostname
    "http://169.254.169.254",  # cloud-metadata IP
    "https://169.254.169.254/latest",  # cloud-metadata IP, https
    "http://metadata.google.internal",  # cloud-metadata DNS
    "http://METADATA.GOOGLE.INTERNAL.",  # metadata DNS, case + trailing-dot variant
    "http://169.254.10.10",  # link-local range
    "http://[fe80::1]",  # link-local IPv6
    "http://224.0.0.1",  # multicast IPv4
    "http://[ff02::1]",  # multicast IPv6
    "http://0.0.0.0",  # unspecified IPv4
    "http://[::]",  # unspecified IPv6 (reserved, NOT loopback)
    "http://2852039166",  # packed IPv4 (decimal) == 169.254.169.254
    "http://0xA9FEA9FE",  # packed IPv4 (hex, upper)
    "http://0xa9fea9fe",  # packed IPv4 (hex, lower)
    "http://025177524776",  # packed IPv4 (octal)
)
"""Every vector here is rejected by all three validators under every policy knob."""

LOOPBACK_ENDPOINTS: tuple[str, ...] = (
    "http://127.0.0.1:8080",
    "http://[::1]:8080",
    "http://localhost:8080",
)
"""IPv4 loopback, IPv6 loopback (the ::1 fix), and the bare loopback hostname.

Accepted by ``validate_in_vpc_endpoint`` and by ``validate_endpoint_url`` when
``allow_private=True``; all three rejected by ``validate_endpoint_url`` when
``allow_private=False`` (the loopback policy).
"""
