# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Loopback service-URL guard (require_loopback_or_allowlisted) — NE-CKC10 / P2-23.

The guard delegates to the shared ne.net loopback-lane classifier: it accepts only
loopback / localhost / allowlisted hosts, rejects the full SSRF red-team corpus
(raising ``ValueError`` — the type the facade's callers expect), and is STRICTER
than the in-VPC lane (a private non-loopback IP is refused).
"""

from __future__ import annotations

import pytest

from ne.memory_graph.config import MemoryGraphSettings, require_loopback_or_allowlisted
from ne.net.testing import LOOPBACK_ENDPOINTS, SSRF_REDTEAM_REJECTS


@pytest.mark.parametrize("url", SSRF_REDTEAM_REJECTS)
def test_redteam_corpus_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        require_loopback_or_allowlisted(url)


@pytest.mark.parametrize("url", LOOPBACK_ENDPOINTS)
def test_loopback_endpoints_accepted(url: str) -> None:
    assert require_loopback_or_allowlisted(url) == url


def test_allowlisted_host_accepted() -> None:
    assert (
        require_loopback_or_allowlisted("http://graphiti-sidecar:8093", ("graphiti-sidecar",))
        == "http://graphiti-sidecar:8093"
    )


def test_allowlist_entries_are_whitespace_stripped() -> None:
    # Padded allowlist entries (and empty ones) are normalized exactly as before.
    assert (
        require_loopback_or_allowlisted(
            "http://graphiti-sidecar:8093", ("  graphiti-sidecar  ", "")
        )
        == "http://graphiti-sidecar:8093"
    )


def test_stricter_than_in_vpc_rejects_private_ip() -> None:
    # A private (non-loopback) IP passes the in-VPC lane but NOT the loopback lane.
    with pytest.raises(ValueError) as excinfo:
        require_loopback_or_allowlisted("http://10.0.0.5:8093")
    assert "external" in str(excinfo.value)


def test_settings_default_service_url_is_loopback() -> None:
    assert MemoryGraphSettings().validated_service_url() == "http://127.0.0.1:8093"
