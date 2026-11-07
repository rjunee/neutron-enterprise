# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Construction-time endpoint validation for the ne-serve vLLM client (C2).

``ServeClient``'s config-supplied ``base_url`` is validated HERE, at
construction, before any request can be issued: an SSRF-shaped or
not-provably-in-VPC endpoint is refused with :class:`UnsafeEndpointError`
(fail-closed) so a compromised / misauthored endpoint never receives a byte.
This closes the ne-serve half of audit #1 (corpus-exfil); the ne-brain half is
NE-CKC2.

Thin wrapper over the shared ne-net leaf (NE-CKC10)
---------------------------------------------------
The classifier used to be a package-local mirror here — importing
``ne.gateway.ssrf`` at ne-serve runtime is impossible because
``ne/gateway/__init__.py`` drags ``litellm`` in (C3; ne-serve's
``test_import_pulls_no_provider_sdk`` and the root import-linter contract forbid
it). NE-CKC10 consolidated the three mirrors into the stdlib-only leaf
:mod:`ne.net.ssrf`; :func:`validate_in_vpc_endpoint` now delegates there with
ne-serve's own :class:`~ne.serve.errors.UnsafeEndpointError`. The leaf imports
stdlib only (enforced by the ``ne-net-leaf`` import-linter contract), so no
provider SDK enters ne-serve's import graph. ``tests/test_sovereignty_parity.py``
still checks agreement against the real ``ne.gateway.ssrf`` (a dev-only import).

PROVENANCE
----------
Endpoint-validation logic derived, via ``ne.net.ssrf`` (NE-012/NE-CKC10), from
ABI (https://github.com/jupyter-naas/abi) provider_runtime.py, MIT License,
Copyright (C) 2025 Naas.ai.

Pure stdlib + ``ne.net`` (itself stdlib-only): no ``litellm`` / provider SDK
import (C3).
"""

from __future__ import annotations

from collections.abc import Sequence

import ne.net.ssrf as _net_ssrf

# Re-exported so callers keep importing normalize_host from this module.
from ne.net.ssrf import normalize_host
from ne.serve.errors import UnsafeEndpointError

__all__ = ["normalize_host", "validate_in_vpc_endpoint"]


def validate_in_vpc_endpoint(endpoint: str, *, allowed_hosts: Sequence[str] = ()) -> str:
    """Validate a config-supplied serve endpoint, refusing anything SSRF-shaped or
    not provably in-VPC.

    Delegates to :func:`ne.net.ssrf.validate_in_vpc_endpoint` (the single shared
    classifier since NE-CKC10) with ne-serve's :class:`~ne.serve.errors.UnsafeEndpointError`.

    Raises (fail-closed) with a message that names ONLY the parsed hostname (never
    the full URL — no credential leak). Returns the endpoint with any trailing
    ``/`` stripped.

    **Phase A — ALWAYS reject** (SSRF shape): non-http(s) scheme; a
    malformed/out-of-range port; embedded credentials; a query string or fragment;
    a missing hostname; a cloud-metadata host; a packed-integer IPv4 form; an IP
    literal that is link-local / multicast / unspecified / non-loopback reserved.

    **Phase B — ACCEPT only in-VPC**: a private/loopback IP literal (now including
    IPv6 ``::1``, handled exactly like ``127.0.0.1`` per NE-CKC10); ``localhost``;
    a dotless service name (packed IPv4 already excluded in Phase A); a host ending
    ``.local`` / ``.localhost`` / ``.internal``; a host in ``allowed_hosts`` (case-
    and trailing-dot-normalized); otherwise the host is treated as external and
    rejected.
    """
    return _net_ssrf.validate_in_vpc_endpoint(
        endpoint,
        allowed_hosts=allowed_hosts,
        error_cls=UnsafeEndpointError,
        external_hint=(
            "set ServeSettings.allowed_endpoint_hosts if it is private DNS "
            "reachable only inside the VPC"
        ),
    )
