# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-permissions-rebac sovereignty guard: in-VPC OpenFGA URL validation.

C2 (data sovereignty / egress): the OpenFGA client takes a config-supplied
``api_url`` and a preshared key. A misauthored/hostile URL would send that key —
and directory-shaped relationship data — to an attacker-controlled host. This
module validates ``api_url`` at client CONSTRUCTION time, fail-closed: a bad URL
raises :class:`~ne.permissions_rebac.errors.UnsafeEndpointError` and never
receives the token.

Thin wrapper over the shared ne-net leaf (NE-CKC10 / P2-23)
----------------------------------------------------------
This used to be a package-local mirror of the in-VPC SSRF shape; that rationale is
now dead. NE-CKC10 hoisted the classifier into the stdlib-only leaf
:mod:`ne.net.ssrf`: :func:`validate_openfga_url` delegates to
:func:`ne.net.ssrf.validate_in_vpc_endpoint` (OpenFGA is an in-VPC sidecar like
OPA / Qdrant, so private / loopback / RFC-1918 / dotless-service-name /
``.internal`` / ``.local`` hosts PASS and public-internet hosts FAIL), and the
AIR_GAP guard is re-exported from :mod:`ne.net.airgap`.

INTENDED BEHAVIOR CHANGE (NE-CKC10): ``http://[::1]:8081`` is now ACCEPTED exactly
like ``http://127.0.0.1:8081``. The old local mirror hard-rejected any
``ip.is_reserved`` literal in Phase A, and ``IPv6Address('::1').is_reserved`` is
``True`` on CPython, so IPv6 loopback was double-classified and spuriously
refused. The shared classifier exempts loopback from ONLY the reserved test, so
``::1`` flows to the loopback-accept branch. Every other verdict is unchanged:
loopback / private IP, ``localhost``, a dotless service, and
``.internal`` / ``.local`` / ``.localhost`` hosts still pass; metadata /
packed-IPv4 / link-local / public IP / public DNS all still reject.

Because OpenFGA is in-VPC, the module stays ALIVE under ``AIR_GAP`` (no external
egress); the validator does not reject on air-gap. Every error message names ONLY
the parsed hostname (never the full URL / any embedded secret).
"""

from __future__ import annotations

import ne.net.ssrf as _net_ssrf
from ne.net.airgap import air_gap_engaged
from ne.net.ssrf import normalize_host
from ne.permissions_rebac.errors import UnsafeEndpointError

__all__ = ["air_gap_engaged", "normalize_host", "validate_openfga_url"]


def validate_openfga_url(url: str) -> str:
    """Validate a config-supplied in-VPC OpenFGA URL, fail-closed.

    Delegates to :func:`ne.net.ssrf.validate_in_vpc_endpoint` with this package's
    :class:`UnsafeEndpointError`. Rejects (raising it): a scheme other than
    ``http``/``https``; a malformed/out-of-range port; credentials embedded in the
    URL (userinfo); a query string / fragment; a missing hostname; a cloud-metadata
    host; a packed-integer IPv4 form; a public / link-local / reserved-non-loopback
    IP literal; and a PUBLIC DNS host (a dotted name not ending ``.internal`` /
    ``.local`` / ``.localhost``). Accepts: ``localhost``, a dotless service name
    (e.g. ``openfga``), an ``.internal`` / ``.local`` host, and a private / loopback
    IP literal (including IPv6 ``::1`` per NE-CKC10).

    Returns ``url`` with any trailing ``/`` stripped.
    """
    return _net_ssrf.validate_in_vpc_endpoint(
        url,
        allowed_hosts=(),
        error_cls=UnsafeEndpointError,
        external_hint=(
            "OpenFGA must be an in-VPC sidecar (a dotless service name, an "
            ".internal/.local host, or a private/loopback IP)"
        ),
    )
