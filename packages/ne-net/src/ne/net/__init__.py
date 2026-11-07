# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-net — stdlib-only network-security leaf.

Home of the ONE consolidated SSRF / sovereignty endpoint validator and the ONE
AIR_GAP egress guard (NE-CKC10 / P2-23), shared by ne-gateway, ne-brain, ne-serve,
ne-permissions, ne-permissions-rebac, ne-connect, ne-memory-graph, ne-brain-graph,
ne-govern, ne-eval, ne-chat, and ne-schema. It lives here — not in ne-gateway —
because importing ne.gateway drags litellm into the graph and the other consumers
must not couple to a provider SDK (C3). This package has ZERO third-party
dependencies (pure stdlib ``os`` / ``ipaddress`` / ``urllib.parse``); its
leaf-ness is enforced by the ``ne-net-leaf`` import-linter contract.
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.net.airgap import AirGapViolationError, air_gap_engaged
from ne.net.ssrf import (
    UnsafeEndpointError,
    allowlist_revoked_by_air_gap,
    looks_like_packed_ipv4,
    normalize_host,
    redact_url_for_logs,
    validate_endpoint_url,
    validate_external_https_url,
    validate_in_vpc_endpoint,
    validate_loopback_endpoint,
)

__all__ = [
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
]
