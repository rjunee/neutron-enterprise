# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Egress policy + AIR_GAP enforcement (NE-012) — the C2 sovereignty proof.

This is the real implementation of NE-010's ``egress_guard`` seam
(``Callable[[ModelDef], None]``, invoked before every provider attempt). It
enforces the two privacy lanes on every gateway call:

* **frontier-api** — allowed ONLY to a per-deployment, config-driven hostname
  allowlist (:attr:`EgressSettings.allowed_frontier_hosts`). Anything not on the
  allowlist is blocked (fail-closed). Under ``AIR_GAP`` the frontier lane is
  killed entirely — a frontier call raises
  :class:`~ne.gateway.errors.AirGapViolationError`, provably (verified by test),
  so the only sanctioned egress can be disabled.
* **private-vllm** — must stay in-VPC. The endpoint is SSRF-validated. An IP
  literal must be private/loopback; a hostname must have an admitted in-VPC
  shape (or be explicitly listed in
  :attr:`EgressSettings.allowed_private_vllm_hosts`) AND resolve, on every
  guard call, to a non-empty set containing only private/loopback addresses.
  Public literals and public/mixed DNS answers are never accepted. External
  GPU rigs must therefore be reached over a private overlay, not their provider
  public address. The genuinely-private lane keeps working under ``AIR_GAP``.

``AIR_GAP`` is read PER CALL from the bare global environment flag and ORed with
:attr:`EgressSettings.air_gap`, so it can only ever ADD restriction (fail-closed;
SPEC C2 'global AIR_GAP flag').

DESIGN DECISION (deliberate, do not revisit): NE-010's
``LiteLLMGateway(egress_guard=None)`` remains a no-op — the frozen seam behavior
and its tests depend on it. Production composition roots (NE-013/014) MUST wire
the real guard via :func:`build_egress_guard`; ``None`` is never a silent
open-egress default in production, only in the seam's unit tests.

Pure stdlib (``ipaddress``, ``socket``, ``urllib.parse``) + pydantic-settings via
``NeSettings``: no ``litellm`` import (C3).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import SettingsConfigDict

from ne.contracts import Lane
from ne.contracts.config import NeSettings
from ne.gateway.config import ModelDef
from ne.gateway.errors import (
    AirGapViolationError,
    EgressBlockedError,
    GatewayConfigError,
)
from ne.gateway.ssrf import looks_like_packed_ipv4, normalize_host, validate_endpoint_url

# The bare global AIR_GAP read is the ONE shared guard (NE-CKC10 / P2-23); aliased
# to preserve the module-attribute name the egress-policy tests toggle via env var.
from ne.net.airgap import air_gap_engaged as _air_gap_env

KNOWN_FRONTIER_PROVIDER_HOSTS: dict[str, str] = {
    "anthropic": "api.anthropic.com",
    "openai": "api.openai.com",
    "mistral": "api.mistral.ai",
    "xai": "api.x.ai",
    "google": "generativelanguage.googleapis.com",
    "openrouter": "openrouter.ai",
    "perplexity": "api.perplexity.ai",
}
"""Default egress host per frontier provider (derived from ABI's official
endpoints). Azure is intentionally absent — Azure OpenAI is per-tenant
``api_base`` and must be named explicitly in the allowlist."""

HostResolver = Callable[[str], tuple[str, ...]]
"""Resolve one normalized host to every address the guard must evaluate."""


def _system_resolve_host(host: str) -> tuple[str, ...]:
    """Resolve ``host`` through the process resolver without making a connection.

    The caller treats an exception or empty result as denial.  Resolution is
    repeated per gateway attempt so a changed DNS answer cannot inherit a prior
    decision.  This does not pin the provider client's later connection to the
    checked address; the deployment's network egress boundary remains the
    authority against DNS rebinding.
    """
    answers = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return tuple(sorted({str(answer[4][0]) for answer in answers}))


def _is_private_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether a literal/resolved address is safe for the private lane."""
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if address.is_link_local or address.is_multicast or address.is_unspecified:
        return False
    return address.is_private or address.is_loopback


class EgressSettings(NeSettings):
    """Per-deployment egress policy config (``NE_EGRESS_`` env / ``ne.toml``).

    List fields use pydantic-settings JSON env form, e.g.
    ``NE_EGRESS_ALLOWED_FRONTIER_HOSTS='["api.anthropic.com"]'``.
    """

    model_config = SettingsConfigDict(
        env_prefix="NE_EGRESS_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    air_gap: bool = False
    allowed_frontier_hosts: tuple[str, ...] = ()
    allowed_private_vllm_hosts: tuple[str, ...] = ()
    """Dotted private-DNS vLLM hosts that are not otherwise admitted by shape.

    Public/special IP literals are rejected at settings load. A private literal
    is accepted as a harmless redundant entry for generated configurations that
    list the selected endpoint host; it already needs no grant. A listed DNS
    name must still resolve only to private/loopback addresses on every call.
    """

    @model_validator(mode="after")
    def _normalize_hosts(self) -> EgressSettings:
        # Raise GatewayConfigError directly: pydantic v2 propagates non-ValueError
        # exceptions from after-validators unwrapped.
        object.__setattr__(
            self,
            "allowed_frontier_hosts",
            self._normalize_tuple(self.allowed_frontier_hosts, "allowed_frontier_hosts"),
        )
        object.__setattr__(
            self,
            "allowed_private_vllm_hosts",
            self._normalize_private_hosts(self.allowed_private_vllm_hosts),
        )
        return self

    @classmethod
    def _normalize_private_hosts(cls, hosts: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw_host in hosts:
            if not raw_host or not raw_host.strip():
                raise GatewayConfigError(
                    "allowed_private_vllm_hosts: entries must be non-empty bare hostnames"
                )
            host = normalize_host(raw_host)
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if looks_like_packed_ipv4(host):
                    raise GatewayConfigError(
                        "allowed_private_vllm_hosts: public/special IP forms are never allowed; "
                        "use a private overlay address for an external GPU"
                    ) from None
                if "/" in host or ":" in host:
                    raise GatewayConfigError(
                        f"allowed_private_vllm_hosts: entry {raw_host!r} must be a bare "
                        "hostname (no scheme, no port, no path)"
                    ) from None
            else:
                if not _is_private_address(address):
                    raise GatewayConfigError(
                        "allowed_private_vllm_hosts: public/special IP forms are never allowed; "
                        "use a private overlay address for an external GPU"
                    )
            normalized.append(host)
        return tuple(normalized)

    @staticmethod
    def _normalize_tuple(hosts: tuple[str, ...], field: str) -> tuple[str, ...]:
        out: list[str] = []
        for h in hosts:
            if not h or not h.strip():
                raise GatewayConfigError(f"{field}: entries must be non-empty bare hostnames")
            if "/" in h or ":" in h:
                raise GatewayConfigError(
                    f"{field}: entry {h!r} must be a bare hostname (no scheme, no port, no path)"
                )
            out.append(normalize_host(h))
        return tuple(out)


class EgressPolicy:
    """The real egress guard: ``__call__(model)`` raises (fail-closed) on any
    disallowed egress, and returns ``None`` when the call is permitted. Plugged
    into :class:`ne.gateway.gateway.LiteLLMGateway` via its ``egress_guard`` seam.

    NE-AIRGAP-TOGGLE: the policy reads its :class:`EgressSettings` PER CALL from
    either a static instance (the historical shape — boot-frozen for the life of
    the process) or a ``settings_provider`` callable (the governed runtime-toggle
    shape: the composition root's provider re-reads the durable egress-posture
    row so a governed flip takes effect on the very next call, in EVERY process
    sharing the store). The provider owns its OWN fail-closed semantics — it must
    return the most-restrictive settings rather than raise (see
    ``ne.api.network_egress.build_egress_settings_provider``); a provider that
    raises anyway propagates, which still fails the call closed (no egress
    happens on an error).
    """

    def __init__(
        self,
        settings: EgressSettings | None = None,
        *,
        settings_provider: Callable[[], EgressSettings] | None = None,
        host_resolver: HostResolver | None = None,
    ) -> None:
        if (settings is None) == (settings_provider is None):
            raise GatewayConfigError(
                "EgressPolicy requires exactly one of settings= or settings_provider="
            )
        self._static_settings = settings
        self._settings_provider = settings_provider
        self._host_resolver = host_resolver or _system_resolve_host

    def current_settings(self) -> EgressSettings:
        """The settings in force RIGHT NOW (one provider read).

        ``__call__`` snapshots this ONCE per check so a concurrent governed flip
        can never split one call's decision across two different postures; the
        posture surface reads the same method so reported == enforced (§0 law 5).
        """
        if self._settings_provider is not None:
            return self._settings_provider()
        assert self._static_settings is not None  # __init__ enforced exactly-one
        return self._static_settings

    def __call__(self, model: ModelDef) -> None:
        settings = self.current_settings()
        if model.lane is Lane.PRIVATE_VLLM:
            self._check_private_vllm(model, settings)
        elif model.lane is Lane.FRONTIER_API:
            self._check_frontier(model, settings)
        else:  # pragma: no cover - defensive fail-closed default arm
            raise EgressBlockedError(
                f"model {model.name!r}: unknown lane {model.lane!r}; egress denied (fail-closed)"
            )

    # -- private-vllm (allowed under AIR_GAP) ---------------------------------

    def _check_private_vllm(self, model: ModelDef, settings: EgressSettings) -> None:
        if model.api_base is None:
            raise EgressBlockedError(
                f"private-vllm lane model {model.name!r} has no api_base; in-VPC "
                "inference requires an explicit endpoint"
            )
        # SSRF: metadata / link-local / creds / query always die here.
        validate_endpoint_url(model.api_base, require_https=False, allow_private=True)
        parsed = urlparse(model.api_base)
        host = normalize_host(parsed.hostname or "")
        if self._is_in_vpc(host, settings):
            return
        raise EgressBlockedError(
            f"private-vllm lane must stay in-VPC; host {host!r} is not a private "
            "literal or did not resolve exclusively to private addresses; public "
            "GPU endpoints are never allowlistable"
        )

    def _is_in_vpc(self, host: str, settings: EgressSettings) -> bool:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            # Never let a public v4/v6 literal fall through to a DNS or dotless
            # hostname arm, and never let an allowlist entry resurrect it.
            return _is_private_address(ip)

        admitted_name = host == "localhost"
        if "." not in host and not looks_like_packed_ipv4(host):
            # A dotless compose/service name, e.g. "vllm". Packed-integer IPv4
            # forms ('2852039166', '0xA9FEA9FE') are excluded above so they fall
            # through to denial rather than being trusted as in-VPC (they resolve
            # to real, possibly metadata, IPs). NE-CKC3 rejects them upstream too.
            admitted_name = True
        if host.endswith((".local", ".internal", ".localhost")):
            admitted_name = True
        if host in settings.allowed_private_vllm_hosts:
            admitted_name = True
        if not admitted_name:
            return False

        try:
            resolved = self._host_resolver(host)
        except (OSError, ValueError):
            return False
        if not resolved:
            return False
        try:
            addresses = tuple(ipaddress.ip_address(address) for address in resolved)
        except ValueError:
            return False
        return all(_is_private_address(address) for address in addresses)

    # -- frontier-api (killed under AIR_GAP) ----------------------------------

    def _check_frontier(self, model: ModelDef, settings: EgressSettings) -> None:
        if settings.air_gap or _air_gap_env():
            raise AirGapViolationError(
                f"frontier egress for model {model.name!r} is blocked: AIR_GAP is "
                "engaged; only the private-vllm lane is available"
            )
        host = self._resolve_frontier_host(model)
        if host not in settings.allowed_frontier_hosts:
            raise EgressBlockedError(
                f"frontier egress for model {model.name!r} to host {host!r} is not "
                "allowlisted; configured allowed_frontier_hosts: "
                f"{sorted(settings.allowed_frontier_hosts)!r}; add it via "
                "NE_EGRESS_ALLOWED_FRONTIER_HOSTS / ne.toml allowed_frontier_hosts"
            )

    def _resolve_frontier_host(self, model: ModelDef) -> str:
        if model.api_base is not None:
            validated = validate_endpoint_url(
                model.api_base, require_https=True, allow_private=False
            )
            return normalize_host(urlparse(validated).hostname or "")
        known = KNOWN_FRONTIER_PROVIDER_HOSTS.get(model.provider)
        if known is None:
            raise EgressBlockedError(
                f"cannot determine egress host for frontier model {model.name!r}: "
                f"provider {model.provider!r} is not a known frontier provider and "
                "no api_base is set"
            )
        return known


def build_egress_guard(
    settings: EgressSettings | None = None,
    *,
    settings_provider: Callable[[], EgressSettings] | None = None,
) -> EgressPolicy:
    """Construct the real egress guard for wiring into ``LiteLLMGateway``.

    ``settings`` defaults to ``EgressSettings()`` (env / ``ne.toml`` layered).
    Production composition roots (NE-013/014) call this and pass the result as
    ``LiteLLMGateway(..., egress_guard=build_egress_guard())`` — ``egress_guard``
    left ``None`` is a deliberate no-op only by the NE-010 seam contract.

    NE-AIRGAP-TOGGLE: when ``settings_provider`` is given it takes precedence and
    the guard re-reads the settings PER CALL from it — the governed runtime
    air-gap toggle path (see :class:`EgressPolicy`). The provider must be
    fail-closed (return most-restrictive settings on any store failure), never
    fail-open.
    """
    if settings_provider is not None:
        return EgressPolicy(settings_provider=settings_provider)
    return EgressPolicy(settings if settings is not None else EgressSettings())
