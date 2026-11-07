# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C2 source-URL validation for connectors (NE-040).

A connector that takes a source base URL from config MUST call
:func:`validate_source_url` at construction — an unvalidated config-supplied URL
is an egress/SSRF hole (the customer corpus would be POSTed to it, or it would
be used to reach cloud-metadata / internal services).

NE-CKC10 consolidation (P2-23): this module is a thin consumer of the stdlib-only
leaf ``ne.net``. In-VPC sources reuse ``ne.brain.sovereignty.validate_in_vpc_endpoint``
wholesale (kept via ne-brain so the UnsafeEndpointError class identity every
existing catcher relies on is preserved — that validator itself delegates to
``ne.net.ssrf``); the EXTERNAL-source path delegates to
``ne.net.ssrf.validate_external_https_url`` (https-only, DNS-name-only, any DNS
host — ``allowed_hosts=None``). The AIR_GAP guard comes from ``ne.net.airgap``.
"""

from __future__ import annotations

from collections.abc import Sequence

from ne.brain.sovereignty import UnsafeEndpointError, validate_in_vpc_endpoint
from ne.net.airgap import AirGapViolationError, air_gap_engaged
from ne.net.ssrf import validate_external_https_url

__all__ = [
    "AirGapViolationError",
    "UnsafeEndpointError",
    "validate_source_url",
]


def validate_source_url(
    url: str,
    *,
    allow_external: bool = True,
    allowed_hosts: Sequence[str] = (),
) -> str:
    """Validate a config-supplied connector source URL, fail-closed.

    In-VPC sources are always accepted (including under AIR_GAP) via
    ``ne.brain.sovereignty.validate_in_vpc_endpoint``. An external source is
    accepted ONLY when ``allow_external`` is true, AIR_GAP is not engaged, and it
    passes ``ne.net.ssrf.validate_external_https_url`` (``https`` scheme, a real
    DNS name — no IP literal / packed-IPv4 form, no embedded credentials / query /
    fragment / metadata host; ``allowed_hosts=None`` ⇒ any DNS name, preserving
    connect's historical external semantics).

    Returns ``url`` with any trailing ``/`` stripped. Error messages name ONLY
    the parsed hostname (never the full URL) — credential-leak discipline.
    """
    try:
        return validate_in_vpc_endpoint(url, allowed_hosts=allowed_hosts)
    except UnsafeEndpointError:
        if air_gap_engaged():
            raise AirGapViolationError(
                "AIR_GAP engaged: connector source endpoints must be in-VPC"
            ) from None
        if not allow_external:
            raise

    # External-source path (any DNS host): delegate to the shared external lane.
    return validate_external_https_url(url, allowed_hosts=None, error_cls=UnsafeEndpointError)
