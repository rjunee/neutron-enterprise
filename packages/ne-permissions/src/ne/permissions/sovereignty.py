# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-permissions sovereignty guard: external Graph/login URL validation + AIR_GAP.

C2 (data sovereignty / egress): :class:`ne.permissions.entra.EntraPrincipalSource`
takes a *config-supplied* Microsoft Graph base URL and login (token) base URL. A
misauthored or hostile URL would send the client-credentials secret and pull
directory data to an attacker-controlled host. This module validates each such
URL at CONSTRUCTION time, fail-closed: a bad URL raises
:class:`UnsafeEndpointError` and never receives the token request.

Thin wrapper over the shared ne-net leaf (NE-CKC10 / P2-23)
----------------------------------------------------------
This used to be a package-local mirror of the ``ne.brain.sovereignty`` shape (kept
separate to avoid a ``ne-brain → ne-permissions → ne-brain`` workspace cycle).
NE-CKC10 hoisted the classifier into the stdlib-only leaf :mod:`ne.net.ssrf`:
:func:`validate_external_https_url` now delegates to
:func:`ne.net.ssrf.validate_external_https_url` (https-only, DNS-name-only, exact
allowlist) with this package's own :class:`UnsafeEndpointError`, and the AIR_GAP
guard is re-exported from :mod:`ne.net.airgap` (the ONE platform-wide home — SAME
class object). The leaf imports stdlib only (``ne-net-leaf`` import-linter
contract), so no cycle and no ``litellm`` / ``ne.brain`` enters this graph.

The EXTERNAL policy is stricter than the in-VPC brain validator: allowlist-exact,
https-only, no in-VPC escape hatch — sovereign Graph clouds are a fixed set.
Every error message names ONLY the parsed hostname (never the full URL / any
embedded secret).
"""

from __future__ import annotations

from collections.abc import Sequence

import ne.net.ssrf as _net_ssrf

# Re-exported: the AIR_GAP guard from the ONE home (SAME class object) and
# normalize_host from ne.net.ssrf.
from ne.net.airgap import AirGapViolationError, air_gap_engaged
from ne.net.ssrf import normalize_host

__all__ = [
    "AirGapViolationError",
    "UnsafeEndpointError",
    "air_gap_engaged",
    "normalize_host",
    "validate_external_https_url",
]


class UnsafeEndpointError(ValueError):
    """A config-supplied external URL is SSRF-shaped or not on the allowlist.

    Raised at CONSTRUCTION time (fail-closed) so a bad/exfil endpoint never
    receives the token request. Messages interpolate ONLY the parsed hostname
    (never the full URL), so an embedded credential can never appear in an error
    message or log line.
    """


def validate_external_https_url(url: str, *, allowed_hosts: Sequence[str]) -> str:
    """Validate a config-supplied external HTTPS URL against ``allowed_hosts``.

    Delegates to :func:`ne.net.ssrf.validate_external_https_url` (the single shared
    classifier since NE-CKC10) with this package's :class:`UnsafeEndpointError`.
    Fail-closed. Rejects: a scheme that is not exactly ``https``; a
    malformed/out-of-range port; embedded credentials (userinfo); a query string
    or fragment; a missing hostname; a cloud-metadata host; an IP-literal host; a
    packed-integer IPv4 form. The normalized host (lowercase, trailing dot
    stripped) MUST be present in ``allowed_hosts`` by exact match — there is NO
    in-VPC escape hatch and NO wildcard (``allowed_hosts`` stays REQUIRED here).

    Returns ``url`` with any trailing ``/`` stripped. Every error message names
    ONLY the parsed hostname (never the full URL / any embedded secret).
    """
    return _net_ssrf.validate_external_https_url(
        url, allowed_hosts=allowed_hosts, error_cls=UnsafeEndpointError
    )
