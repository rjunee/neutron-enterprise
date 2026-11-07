# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SSRF endpoint validation for admin-registered vLLM/Ollama endpoints (NE-012).

Since NE-CKC10 the endpoint-classification logic lives ONCE in the stdlib-only
leaf package :mod:`ne.net.ssrf` (shared by ne-gateway, ne-brain, ne-serve — no
more triplicated security code). This module is now the gateway-lane wrapper: it
delegates :func:`validate_endpoint_url` to :func:`ne.net.ssrf.validate_endpoint_url`,
passing the gateway's :class:`~ne.gateway.errors.UnsafeEndpointError` and the
log-redactor as the message decorator.

Since NE-AUDIT-CRON (P2-1) the log-redaction helper :func:`redact_url_for_logs`
ALSO lives in :mod:`ne.net.ssrf` and is merely RE-EXPORTED here, so importers of
``ne.gateway.ssrf`` are unchanged. It moved because importing this submodule
first executes ``ne/gateway/__init__.py``, which drags the provider-SDK closure
(``litellm``) into the process — so a non-gateway consumer that wanted only the
~40-line ``re``/``urllib`` redactor (ne-worker's audit-checkpoint loop) was
paying +140MB RSS and +5.6s of boot for it. The redactor has no gateway coupling,
so it belongs in the leaf next to the classifier it decorates.

The classifier derives (via ne.net, NE-012/NE-CKC10) from ABI
(https://github.com/jupyter-naas/abi) provider_runtime.py endpoint validation.
MIT License, Copyright (C) 2025 Naas.ai.

Pure ``ne.net`` (itself stdlib-only): no ``litellm`` import (C3).
"""

from __future__ import annotations

import ne.net.ssrf
from ne.gateway.errors import UnsafeEndpointError

# Re-exported from the shared leaf so gateway callers (egress.py line 53 and its
# callers, registry.py) keep importing these names from ne.gateway.ssrf unchanged.
from ne.net.ssrf import looks_like_packed_ipv4, normalize_host, redact_url_for_logs

__all__ = [
    "looks_like_packed_ipv4",
    "normalize_host",
    "redact_url_for_logs",
    "validate_endpoint_url",
]


def validate_endpoint_url(endpoint: str, *, require_https: bool, allow_private: bool) -> str:
    """Validate an operator-supplied endpoint URL, raising
    :class:`~ne.gateway.errors.UnsafeEndpointError` on any SSRF-shaped input.

    Thin delegate to :func:`ne.net.ssrf.validate_endpoint_url` (the single shared
    classifier since NE-CKC10), passing the gateway's exception type and
    :func:`redact_url_for_logs` as the message decorator so every error message
    carries the redacted URL exactly as before.

    Rejected ALWAYS (regardless of ``allow_private``):

    * scheme not in ``{http, https}``;
    * ``http`` when ``require_https``;
    * a malformed or out-of-range port (e.g. >65535, or non-numeric);
    * missing hostname;
    * credentials embedded in the URL (``user:pass@host``);
    * a query string or fragment;
    * a metadata host (``169.254.169.254`` / ``metadata.google.internal``);
    * a packed-integer IPv4 form (libc-resolvable decimal / hex / octal,
      e.g. ``2852039166`` == ``169.254.169.254``);
    * IP literals that are link-local (metadata range ``169.254.0.0/16`` and
      ``fe80::/10``), multicast, non-loopback reserved, or unspecified.

    Rejected when ``allow_private=False`` (additionally):

    * private-range / loopback IP literals;
    * ``localhost`` / ``127.0.0.1`` / ``::1``;
    * hostnames ending ``.local`` / ``.localhost`` / ``.internal``.

    NE-CKC10 note: IPv6 loopback ``::1`` now follows the SAME ``allow_private``
    policy as ``127.0.0.1`` (previously it was spuriously rejected in every case
    because ``::1`` is ``is_reserved`` on CPython).

    Returns the endpoint with any trailing ``/`` stripped.
    """
    return ne.net.ssrf.validate_endpoint_url(
        endpoint,
        require_https=require_https,
        allow_private=allow_private,
        error_cls=UnsafeEndpointError,
        describe=redact_url_for_logs,
    )
