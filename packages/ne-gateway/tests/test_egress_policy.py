# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for EgressPolicy / EgressSettings (NE-012), built on ModelDef
directly (no gateway)."""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any

import pytest

from ne.contracts import Lane, ModelEntry
from ne.gateway import (
    EgressPolicy,
    EgressSettings,
    GatewayConfigError,
    ModelDef,
    entry_to_model_def,
)
from ne.gateway.errors import (
    AirGapViolationError,
    EgressBlockedError,
    UnsafeEndpointError,
)


def _model(
    name: str = "m",
    *,
    lane: Lane = Lane.FRONTIER_API,
    provider: str = "openai",
    api_base: str | None = None,
    **extra: Any,
) -> ModelDef:
    return ModelDef(
        name=name,
        litellm_model=f"{provider}/x",
        provider=provider,
        lane=lane,
        api_base=api_base,
        extra_params=dict(extra),
        context_window=32768,
    )


def _resolver(*addresses: str) -> Callable[[str], tuple[str, ...]]:
    """A deterministic DNS seam; no unit test depends on host DNS state."""

    def resolve(_host: str) -> tuple[str, ...]:
        return addresses

    return resolve


# -- frontier allowlist -------------------------------------------------------


def test_frontier_known_provider_allowlisted_passes() -> None:
    policy = EgressPolicy(EgressSettings(allowed_frontier_hosts=("api.openai.com",)))
    policy(_model(provider="openai"))  # no raise


def test_frontier_host_not_allowlisted_blocked() -> None:
    policy = EgressPolicy(EgressSettings(allowed_frontier_hosts=("api.anthropic.com",)))
    with pytest.raises(EgressBlockedError) as ei:
        policy(_model(provider="openai"))
    msg = str(ei.value)
    assert "api.openai.com" in msg
    assert "api.anthropic.com" in msg


def test_frontier_empty_allowlist_blocks_all() -> None:
    policy = EgressPolicy(EgressSettings(allowed_frontier_hosts=()))
    with pytest.raises(EgressBlockedError):
        policy(_model(provider="openai"))


def test_frontier_unknown_provider_without_api_base_blocked() -> None:
    policy = EgressPolicy(EgressSettings(allowed_frontier_hosts=("api.openai.com",)))
    with pytest.raises(EgressBlockedError) as ei:
        policy(_model(provider="acme-labs"))
    assert "acme-labs" in str(ei.value)


def test_frontier_api_base_host_checked_against_allowlist() -> None:
    host = "myco.openai.azure.com"
    ok = EgressPolicy(EgressSettings(allowed_frontier_hosts=(host,)))
    ok(_model(provider="azure", api_base=f"https://{host}/v1"))  # no raise
    blocked = EgressPolicy(EgressSettings(allowed_frontier_hosts=("api.openai.com",)))
    with pytest.raises(EgressBlockedError):
        blocked(_model(provider="azure", api_base=f"https://{host}/v1"))


def test_frontier_api_base_must_be_https() -> None:
    policy = EgressPolicy(EgressSettings(allowed_frontier_hosts=("myco.openai.azure.com",)))
    with pytest.raises(UnsafeEndpointError):
        policy(_model(provider="azure", api_base="http://myco.openai.azure.com/v1"))


# -- AIR_GAP ------------------------------------------------------------------


def test_air_gap_setting_blocks_frontier() -> None:
    policy = EgressPolicy(EgressSettings(air_gap=True, allowed_frontier_hosts=("api.openai.com",)))
    with pytest.raises(AirGapViolationError):
        policy(_model(provider="openai"))


def test_air_gap_env_var_overrides_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = EgressPolicy(EgressSettings(air_gap=False, allowed_frontier_hosts=("api.openai.com",)))
    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(AirGapViolationError):
        policy(_model(provider="openai"))
    monkeypatch.setenv("AIR_GAP", "0")
    policy(_model(provider="openai"))  # no raise — env off, allowlisted


def test_ne_egress_air_gap_env_sets_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_EGRESS_AIR_GAP", "true")
    assert EgressSettings().air_gap is True


# -- private-vllm -------------------------------------------------------------


def test_private_vllm_allowed_under_air_gap() -> None:
    policy = EgressPolicy(EgressSettings(air_gap=True), host_resolver=_resolver("10.0.0.8"))
    policy(_model(lane=Lane.PRIVATE_VLLM, provider="vllm", api_base="http://vllm:8000"))  # no raise


def test_private_vllm_requires_api_base() -> None:
    policy = EgressPolicy(EgressSettings())
    with pytest.raises(EgressBlockedError):
        policy(_model(lane=Lane.PRIVATE_VLLM, provider="vllm", api_base=None))


def test_private_vllm_public_host_blocked() -> None:
    policy = EgressPolicy(EgressSettings())
    with pytest.raises(EgressBlockedError):
        policy(
            _model(
                lane=Lane.PRIVATE_VLLM,
                provider="vllm",
                api_base="https://api.example.com",
            )
        )


def test_private_vllm_public_ipv6_host_blocked() -> None:
    # Regression (Argus P1): a public IPv6 literal is colon-delimited and dotless,
    # so the dotless service-name heuristic must NOT let it through as "in-VPC".
    policy = EgressPolicy(EgressSettings())
    with pytest.raises(EgressBlockedError):
        policy(
            _model(
                lane=Lane.PRIVATE_VLLM,
                provider="vllm",
                api_base="http://[2606:4700:4700::1111]:8000/v1",
            )
        )


def test_private_vllm_private_ipv6_host_allowed() -> None:
    # The complementary direction: a genuine private/ULA IPv6 endpoint is in-VPC.
    policy = EgressPolicy(EgressSettings())
    policy(
        _model(
            lane=Lane.PRIVATE_VLLM,
            provider="vllm",
            api_base="http://[fd00::1]:8000/v1",
        )
    )


def test_private_vllm_dotted_private_host_allowed_via_config() -> None:
    host_api = "http://vllm.prod.acme.com:8000"
    blocked = EgressPolicy(EgressSettings())
    with pytest.raises(EgressBlockedError):
        blocked(_model(lane=Lane.PRIVATE_VLLM, provider="vllm", api_base=host_api))
    allowed = EgressPolicy(
        EgressSettings(allowed_private_vllm_hosts=("VLLM.PROD.ACME.COM.",)),
        host_resolver=_resolver("10.77.0.2", "fd00::2"),
    )
    allowed(_model(lane=Lane.PRIVATE_VLLM, provider="vllm", api_base=host_api))


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "8.8.8.8.",  # normalization must happen before the literal check
        "2852039166",  # packed decimal form
        "0x08080808",  # packed hexadecimal form
        "2606:4700:4700::1111",
    ],
)
def test_private_vllm_allowlist_rejects_every_ip_representation(host: str) -> None:
    with pytest.raises(GatewayConfigError):
        EgressSettings(allowed_private_vllm_hosts=(host,))


@pytest.mark.parametrize(
    ("host", "api_base"),
    [
        ("10.77.0.2", "http://10.77.0.2:8000/v1"),
        ("fd00::2", "http://[fd00::2]:8000/v1"),
    ],
)
def test_private_vllm_allowlist_tolerates_generated_private_literal_entry(
    host: str,
    api_base: str,
) -> None:
    settings = EgressSettings(allowed_private_vllm_hosts=(host,))
    EgressPolicy(settings)(
        _model(
            lane=Lane.PRIVATE_VLLM,
            provider="vllm",
            api_base=api_base,
        )
    )


def test_private_vllm_public_literal_blocked_with_air_gap_on_or_off() -> None:
    model = _model(
        lane=Lane.PRIVATE_VLLM,
        provider="vllm",
        api_base="http://8.8.8.8:8000/v1",
    )
    for air_gap in (False, True):
        with pytest.raises(EgressBlockedError, match="public GPU endpoints"):
            EgressPolicy(EgressSettings(air_gap=air_gap))(model)


@pytest.mark.parametrize(
    "addresses",
    [
        ("8.8.8.8",),
        ("10.77.0.2", "8.8.8.8"),
        ("2606:4700:4700::1111",),
        ("::ffff:8.8.8.8",),
        ("169.254.169.254",),
        (),
    ],
)
def test_private_vllm_allowlisted_dns_refuses_public_mixed_and_empty_answers(
    addresses: tuple[str, ...],
) -> None:
    policy = EgressPolicy(
        EgressSettings(allowed_private_vllm_hosts=("gpu.private.example",)),
        host_resolver=_resolver(*addresses),
    )
    with pytest.raises(EgressBlockedError, match="resolve exclusively"):
        policy(
            _model(
                lane=Lane.PRIVATE_VLLM,
                provider="vllm",
                api_base="http://GPU.PRIVATE.EXAMPLE.:8000/v1",
            )
        )


@pytest.mark.parametrize("host", ["vllm", "gpu.internal", "gpu.localhost"])
def test_private_vllm_in_vpc_hostname_shapes_still_require_private_dns(host: str) -> None:
    public = EgressPolicy(EgressSettings(), host_resolver=_resolver("8.8.8.8"))
    with pytest.raises(EgressBlockedError):
        public(_model(lane=Lane.PRIVATE_VLLM, provider="vllm", api_base=f"http://{host}:8000"))

    private = EgressPolicy(EgressSettings(air_gap=True), host_resolver=_resolver("10.0.0.8"))
    private(_model(lane=Lane.PRIVATE_VLLM, provider="vllm", api_base=f"http://{host}:8000"))


def test_private_vllm_dns_resolution_failure_fails_closed() -> None:
    def failed(_host: str) -> tuple[str, ...]:
        raise OSError("resolver unavailable")

    policy = EgressPolicy(EgressSettings(), host_resolver=failed)
    with pytest.raises(EgressBlockedError, match="resolve exclusively"):
        policy(_model(lane=Lane.PRIVATE_VLLM, provider="vllm", api_base="http://vllm:8000"))


def test_private_vllm_dns_is_rechecked_on_every_call() -> None:
    answers = {"current": ("10.77.0.2",)}
    policy = EgressPolicy(
        EgressSettings(allowed_private_vllm_hosts=("gpu.private.example",)),
        host_resolver=lambda _host: answers["current"],
    )
    model = _model(
        lane=Lane.PRIVATE_VLLM,
        provider="vllm",
        api_base="http://gpu.private.example:8000/v1",
    )
    policy(model)
    answers["current"] = ("8.8.8.8",)
    with pytest.raises(EgressBlockedError):
        policy(model)


def test_build_egress_guard_uses_the_system_resolver_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ne.gateway.egress import build_egress_guard

    current = {"address": "8.8.8.8"}
    calls: list[str] = []

    def getaddrinfo(
        host: str,
        _port: None,
        *,
        type: socket.SocketKind,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        calls.append(host)
        return [(socket.AF_INET, type, 6, "", (current["address"], 0))]

    monkeypatch.setattr("ne.gateway.egress.socket.getaddrinfo", getaddrinfo)
    guard = build_egress_guard(EgressSettings(allowed_private_vllm_hosts=("gpu.private.example",)))
    model = _model(
        lane=Lane.PRIVATE_VLLM,
        provider="vllm",
        api_base="http://gpu.private.example:8000/v1",
    )
    with pytest.raises(EgressBlockedError):
        guard(model)
    current["address"] = "10.77.0.2"
    guard(model)
    assert calls == ["gpu.private.example", "gpu.private.example"]


def test_private_vllm_private_and_loopback_ip_literals_unchanged() -> None:
    # Invariant (d): private/loopback IP literals are unaffected by the new
    # allowlist/air-gap arm, with or without air-gap engaged.
    for api_base in ("http://10.1.2.3:8000/v1", "http://127.0.0.1:8091/v1"):
        policy = EgressPolicy(EgressSettings())
        policy(_model(lane=Lane.PRIVATE_VLLM, provider="vllm", api_base=api_base))  # no raise

        air_gapped = EgressPolicy(EgressSettings(air_gap=True))
        air_gapped(
            _model(lane=Lane.PRIVATE_VLLM, provider="vllm", api_base=api_base)
        )  # no raise — in-VPC IPs stay alive under AIR_GAP


def test_private_vllm_public_ipv6_literal_blocked_even_with_allowlist() -> None:
    # A public IPv6 literal is dotless but must never reach the hostname arm.
    policy = EgressPolicy(EgressSettings())
    with pytest.raises(EgressBlockedError):
        policy(
            _model(
                lane=Lane.PRIVATE_VLLM,
                provider="vllm",
                api_base="http://[2606:4700:4700::1111]:8000/v1",
            )
        )


def test_private_vllm_metadata_ip_not_allowlistable() -> None:
    # Config now rejects this before the URL validator ever needs to.
    with pytest.raises(GatewayConfigError, match="public/special IP"):
        EgressSettings(allowed_private_vllm_hosts=("169.254.169.254",))


def test_private_vllm_metadata_endpoint_rejected() -> None:
    policy = EgressPolicy(EgressSettings())
    with pytest.raises(UnsafeEndpointError):
        policy(
            _model(
                lane=Lane.PRIVATE_VLLM,
                provider="vllm",
                api_base="http://169.254.169.254",
            )
        )


@pytest.mark.parametrize(
    "api_base",
    [
        "http://2852039166",  # decimal packed IPv4 == 169.254.169.254 (metadata)
        "http://0xA9FEA9FE",  # hex packed IPv4 == 169.254.169.254 (metadata)
        "http://025177524776",  # octal packed IPv4 == 169.254.169.254 (metadata)
    ],
)
def test_private_vllm_packed_integer_ip_blocked(api_base: str) -> None:
    # Regression (Argus P1): ipaddress.ip_address rejects these strings, so they
    # slip past the IP-literal SSRF check; a libc resolver still maps them to the
    # metadata IP. They are dotless but must NOT get the in-VPC service-name
    # shortcut — fail closed.
    #
    # NE-CKC3 moved the packed-form rejection INTO validate_endpoint_url, so the
    # SSRF layer now fails FIRST (strictly earlier fail-closed) — the exception is
    # UnsafeEndpointError, not EgressBlockedError. EgressPolicy._is_in_vpc retains
    # its own packed-form exclusion as defense-in-depth.
    policy = EgressPolicy(EgressSettings())
    with pytest.raises(UnsafeEndpointError):
        policy(_model(lane=Lane.PRIVATE_VLLM, provider="vllm", api_base=api_base))


# -- allowlist normalization / validation -------------------------------------


def test_allowlist_normalized_case_and_trailing_dot() -> None:
    policy = EgressPolicy(EgressSettings(allowed_frontier_hosts=("API.OpenAI.com.",)))
    policy(_model(provider="openai"))  # no raise — normalized to api.openai.com


def test_allowlist_rejects_scheme_or_port_entries() -> None:
    with pytest.raises(GatewayConfigError):
        EgressSettings(allowed_frontier_hosts=("https://api.openai.com",))
    with pytest.raises(GatewayConfigError):
        EgressSettings(allowed_frontier_hosts=("api.openai.com:443",))


# -- registration-time SSRF via entry_to_model_def ----------------------------


def _entry(lane: Lane) -> ModelEntry:
    return ModelEntry(
        model_id="mdl",
        provider="vllm",
        license="MIT",
        origin="US",
        lanes=(lane,),
        capabilities=("streaming",),
        context_window=8192,
    )


def test_entry_to_model_def_rejects_unsafe_api_base() -> None:
    with pytest.raises(UnsafeEndpointError):
        entry_to_model_def(
            _entry(Lane.FRONTIER_API),
            litellm_model="openai/x",
            api_base="https://user:pass@api.example.com",
        )


def test_entry_to_model_def_accepts_compose_vllm_endpoint() -> None:
    md = entry_to_model_def(
        _entry(Lane.PRIVATE_VLLM),
        litellm_model="hosted_vllm/mdl",
        api_base="http://vllm:8000",
    )
    assert md.api_base == "http://vllm:8000"
    assert md.lane is Lane.PRIVATE_VLLM


# -- settings_provider (NE-AIRGAP-TOGGLE: the governed runtime toggle seam) ---


def test_provider_backed_policy_reads_settings_per_call_both_directions() -> None:
    """ONE policy object must flip behavior with the provider's answer — a
    policy that snapshots settings at construction fails BOTH leg-pairs here
    (bidirectional per docs/testing/vacuous-guards.md)."""
    box: dict[str, EgressSettings] = {
        "current": EgressSettings(air_gap=False, allowed_frontier_hosts=("api.openai.com",))
    }
    policy = EgressPolicy(settings_provider=lambda: box["current"])

    policy(_model(provider="openai"))  # loose: passes (control)
    box["current"] = EgressSettings(air_gap=True, allowed_frontier_hosts=("api.openai.com",))
    with pytest.raises(AirGapViolationError):
        policy(_model(provider="openai"))  # tightened: same object refuses
    box["current"] = EgressSettings(air_gap=False, allowed_frontier_hosts=("api.openai.com",))
    policy(_model(provider="openai"))  # loosened again: same object passes


def test_provider_governs_private_dns_admission_per_call_under_air_gap() -> None:
    host = "gpu.private.example"
    model = _model(lane=Lane.PRIVATE_VLLM, api_base=f"http://{host}:8000/v1")
    box = {"current": EgressSettings()}
    policy = EgressPolicy(
        settings_provider=lambda: box["current"],
        host_resolver=_resolver("10.77.0.2"),
    )
    with pytest.raises(EgressBlockedError):
        policy(model)
    box["current"] = EgressSettings(
        air_gap=True,
        allowed_private_vllm_hosts=(host,),
    )
    policy(model)  # private DNS stays alive under the provider-served air gap
    box["current"] = EgressSettings(air_gap=True)
    with pytest.raises(EgressBlockedError):
        policy(model)


def test_policy_requires_exactly_one_of_settings_or_provider() -> None:
    with pytest.raises(GatewayConfigError):
        EgressPolicy()
    with pytest.raises(GatewayConfigError):
        EgressPolicy(
            EgressSettings(),
            settings_provider=lambda: EgressSettings(),
        )


def test_build_egress_guard_provider_takes_precedence() -> None:
    from ne.gateway.egress import build_egress_guard

    guard = build_egress_guard(settings_provider=lambda: EgressSettings(air_gap=True))
    with pytest.raises(AirGapViolationError):
        guard(_model(provider="openai"))
