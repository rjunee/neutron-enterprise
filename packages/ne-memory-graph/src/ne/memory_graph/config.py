# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Settings for the temporal-graph memory facade + sidecar seam.

``MemoryGraphSettings`` (``NE_MEMORY_GRAPH_`` env prefix, ``extra='forbid'`` so a
typo in ``ne.toml`` fails loudly — the ``GatewaySettings`` style) configures the
host-side facade's thin HTTP client to the out-of-process Graphiti sidecar and
the two LLM lanes.

The LANE switch is ONE ``base_url`` config value (see the feasibility memo §3):

* ``lane='frontier'`` ⇒ Graphiti points at the loopback ``llm_facade`` ASGI app,
  which is backed by the in-process gateway-governed ``ModelGateway`` (C3-clean);
* ``lane='air_gap'`` ⇒ Graphiti points DIRECTLY at ne-serve's in-VPC vLLM
  ``/v1`` surface (natively OpenAI-compat, C3-exempt, no egress).

:func:`require_loopback_or_allowlisted` is a construction-time SSRF guard: the
sidecar/service URL host must be a loopback literal (``127.0.0.1`` / ``::1``) or
``localhost`` OR appear in ``allowed_service_hosts``; embedded credentials, a
query string, or a fragment are rejected outright so a misauthored URL never
receives a byte.

NE-CKC10 (P2-23): it delegates to the stdlib-only shared classifier
:func:`ne.net.ssrf.validate_loopback_endpoint`. This TIGHTENS the guard strictly
fail-closed — a malformed/out-of-range port, a cloud-metadata host, and a
packed-integer IPv4 form are now rejected in Phase A (they slipped through the old
local shape check) — while keeping the accept set identical (loopback / localhost
/ allowlisted only; private IPs and dotless service names still refused).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings
from ne.net.ssrf import validate_loopback_endpoint

__all__ = ["MemoryGraphSettings", "require_loopback_or_allowlisted"]


def require_loopback_or_allowlisted(url: str, allowed: tuple[str, ...] = ()) -> str:
    """Validate a service/base URL, fail-closed. Returns it with trailing ``/`` stripped.

    Delegates to :func:`ne.net.ssrf.validate_loopback_endpoint` (``error_cls=ValueError``
    so the raised type stays exactly what this facade's callers expect). Rejects
    (SSRF shape): a non-http(s) scheme; a malformed/out-of-range port; embedded
    credentials; a query string or fragment; a missing hostname; a cloud-metadata
    host; a packed-integer IPv4 form. Accepts a host that is a loopback IP literal
    (``127.0.0.0/8`` / ``::1``), ``localhost``, or a case-normalized member of
    ``allowed`` — everything else (including a private non-loopback IP or a dotless
    service name) is treated as external and refused.
    """
    allowed_hosts = tuple(h.strip() for h in allowed if h.strip())
    return validate_loopback_endpoint(url, allowed_hosts=allowed_hosts, error_cls=ValueError)


class MemoryGraphSettings(NeSettings):
    """Layered settings for the temporal-graph memory facade (NE_MEMORY_GRAPH_)."""

    model_config = SettingsConfigDict(
        env_prefix="NE_MEMORY_GRAPH_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: Base URL of the loopback-bound Graphiti sidecar HTTP surface.
    service_url: str = "http://127.0.0.1:8093"
    #: Bearer token for the sidecar; empty ⇒ unauthenticated local dev.
    service_token: str = ""
    #: Escape hatch: extra hostnames the loopback guard also accepts.
    allowed_service_hosts: tuple[str, ...] = ()
    #: Per-episode ingest POST timeout (append is best-effort; short).
    ingest_timeout_s: float = 2.0
    #: Search POST timeout (recall degrades to the keyword floor on timeout).
    search_timeout_s: float = 10.0
    #: Extraction lane. ``frontier`` ⇒ llm_facade loopback; ``air_gap`` ⇒ vLLM /v1.
    lane: Literal["frontier", "air_gap"] = "frontier"
    #: Bind host for the frontier-lane llm_facade ASGI app (loopback default).
    llm_facade_host: str = "127.0.0.1"
    #: Bind port for the frontier-lane llm_facade ASGI app.
    llm_facade_port: int = 8094
    #: Gateway routing task stamped on every extraction ``CompletionRequest``.
    llm_task: str = "memory-extraction"

    def validated_service_url(self) -> str:
        """The service URL, validated loopback-or-allowlisted (fail-closed)."""
        return require_loopback_or_allowlisted(self.service_url, self.allowed_service_hosts)
