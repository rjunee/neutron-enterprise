# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Leaf guard for ne-net (C3/C4): import pulls no heavy / first-party deps."""

from __future__ import annotations

import subprocess
import sys


def test_import_and_version() -> None:
    import ne.net

    assert ne.net.__version__ == "0.1.0"


def test_public_exports_present() -> None:
    import ne.net

    expected = {
        "AirGapViolationError",
        "UnsafeEndpointError",
        "air_gap_engaged",
        "allowlist_revoked_by_air_gap",
        "looks_like_packed_ipv4",
        "normalize_host",
        "redact_url_for_logs",
        "validate_endpoint_url",
        "validate_external_https_url",
        "validate_in_vpc_endpoint",
        "validate_loopback_endpoint",
    }
    assert expected == set(ne.net.__all__)
    for name in expected:
        assert hasattr(ne.net, name), name


def test_import_pulls_no_provider_or_firstparty_sdk() -> None:
    """C3/C4: a fresh ``import ne.net`` must stay a stdlib-only leaf.

    Checked in a clean subprocess so an unrelated test importing a heavy module
    cannot pollute the assertion.
    """
    banned = ("litellm", "openai", "httpx", "ne.gateway", "ne.brain", "ne.serve")
    code = (
        "import ne.net, ne.net.ssrf, ne.net.airgap, ne.net.testing, sys; "
        f"present = [m for m in {banned!r} if m in sys.modules]; "
        "print(present)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout
