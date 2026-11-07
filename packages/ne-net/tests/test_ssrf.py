# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Unit matrix for the consolidated SSRF/sovereignty validator (NE-CKC10).

Exercises both public validators (:func:`validate_endpoint_url`,
:func:`validate_in_vpc_endpoint`) directly against the shared red-team corpus and
the loopback-parity vectors, plus the ::1 fix, error_cls plumbing, message
hygiene, and packed-IPv4 forms.
"""

from __future__ import annotations

import pytest

from ne.net.ssrf import (
    UnsafeEndpointError,
    normalize_host,
    redact_url_for_logs,
    validate_endpoint_url,
    validate_external_https_url,
    validate_in_vpc_endpoint,
    validate_loopback_endpoint,
)
from ne.net.testing import LOOPBACK_ENDPOINTS, SSRF_REDTEAM_REJECTS


class _CustomError(ValueError):
    """A caller-supplied exception type, to prove error_cls plumbing."""


# -- Shared red-team corpus: rejected under EVERY policy knob ------------------


@pytest.mark.parametrize("endpoint", SSRF_REDTEAM_REJECTS)
@pytest.mark.parametrize("allow_private", [True, False])
def test_gateway_rejects_corpus_every_knob(endpoint: str, allow_private: bool) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url(endpoint, require_https=False, allow_private=allow_private)


@pytest.mark.parametrize("endpoint", SSRF_REDTEAM_REJECTS)
@pytest.mark.parametrize("allowed_hosts", [(), ("something.example.com",)])
def test_in_vpc_rejects_corpus_with_and_without_allowlist(
    endpoint: str, allowed_hosts: tuple[str, ...]
) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_in_vpc_endpoint(endpoint, allowed_hosts=allowed_hosts)


# -- Loopback parity: ::1 handled exactly like 127.0.0.1 ----------------------


@pytest.mark.parametrize("endpoint", LOOPBACK_ENDPOINTS)
def test_loopback_accepted_by_in_vpc(endpoint: str) -> None:
    assert validate_in_vpc_endpoint(endpoint) == endpoint


@pytest.mark.parametrize("endpoint", LOOPBACK_ENDPOINTS)
def test_loopback_accepted_by_gateway_when_private_allowed(endpoint: str) -> None:
    assert validate_endpoint_url(endpoint, require_https=False, allow_private=True) == endpoint


@pytest.mark.parametrize("endpoint", LOOPBACK_ENDPOINTS)
def test_loopback_rejected_by_gateway_when_private_disallowed(endpoint: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url(endpoint, require_https=False, allow_private=False)


def test_ipv6_loopback_matches_ipv4_loopback_in_vpc() -> None:
    assert validate_in_vpc_endpoint("http://[::1]:8085") == "http://[::1]:8085"
    assert validate_in_vpc_endpoint("http://127.0.0.1:8085") == "http://127.0.0.1:8085"


# -- Post-fix: reserved-but-not-loopback IPv6 STILL rejected ------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://[::]",  # unspecified + reserved, NOT loopback
        "http://[fe80::1]",  # link-local
        "http://[64:ff9b::7f00:1]",  # reserved (NAT64), NOT loopback
        "http://[ff02::1]",  # multicast
    ],
)
def test_reserved_non_loopback_ipv6_still_rejected(endpoint: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url(endpoint, require_https=False, allow_private=True)
    with pytest.raises(UnsafeEndpointError):
        validate_in_vpc_endpoint(endpoint)


# -- In-VPC accept matrix + trailing-slash + allowlist normalization ----------


@pytest.mark.parametrize(
    ("endpoint", "expected", "allowed_hosts"),
    [
        ("http://tei.local/", "http://tei.local", ()),
        ("http://localhost:8085", "http://localhost:8085", ()),
        ("http://tei-embed", "http://tei-embed", ()),
        ("http://10.0.0.5:8085", "http://10.0.0.5:8085", ()),
        ("http://192.168.1.9", "http://192.168.1.9", ()),
        ("http://svc.internal", "http://svc.internal", ()),
        (
            "https://tei.corp.example.com",
            "https://tei.corp.example.com",
            ("TEI.CORP.EXAMPLE.COM.",),  # case + trailing-dot normalization
        ),
    ],
)
def test_in_vpc_accepts(endpoint: str, expected: str, allowed_hosts: tuple[str, ...]) -> None:
    assert validate_in_vpc_endpoint(endpoint, allowed_hosts=allowed_hosts) == expected


def test_in_vpc_rejects_public_ip_literal() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_in_vpc_endpoint("http://8.8.8.8")


def test_in_vpc_accepts_allowlisted_public_ip_literal() -> None:
    """NE-SEARCH-LATENCY: the documented contract ("or a host in allowed_hosts")
    holds for IP literals too — the implementation used to consult the allowlist
    only for DNS names, silently un-allowlisting a public-IP endpoint (e.g. a
    rented demo-GPU rerank server). ``test_in_vpc_rejects_public_ip_literal``
    above is this test's fail-closed control: the SAME class of host without the
    allowlist stays rejected."""
    assert (
        validate_in_vpc_endpoint("http://8.8.8.8:1837", allowed_hosts=("8.8.8.8",))
        == "http://8.8.8.8:1837"
    )


@pytest.mark.parametrize(
    ("endpoint", "allowlisted"),
    [
        ("http://169.254.169.254", "169.254.169.254"),  # cloud-metadata host
        ("http://[fe80::1]", "fe80::1"),  # link-local
        ("http://2852039166", "2852039166"),  # packed IPv4 == 169.254.169.254
        ("http://[ff02::1]", "ff02::1"),  # multicast
    ],
)
def test_in_vpc_allowlist_cannot_readmit_phase_a_rejections(
    endpoint: str, allowlisted: str
) -> None:
    """The allowlist is a Phase-B relaxation ONLY: a Phase-A SSRF shape
    (metadata range, packed IPv4, link-local/multicast) stays rejected even when
    its exact host string is explicitly allowlisted."""
    with pytest.raises(UnsafeEndpointError):
        validate_in_vpc_endpoint(endpoint, allowed_hosts=(allowlisted,))


# -- AIR_GAP dominates the allowlist for a public IP literal -------------------
#
# The rule and its derivation live in ne.net.ssrf.allowlist_revoked_by_air_gap.
# What is pinned here is the whole rule, in BOTH directions and from BOTH air-gap
# sources, because the defect this closes was precisely a rule that existed only
# in prose ("mirrors allowed_private_vllm_hosts") with nothing executable behind
# it. Each denial below has the *same call with the gap disengaged* as its
# control, so a test that passed by denying everything would fail the control.


@pytest.mark.parametrize("air_gap_source", ["env", "argument", "both"])
def test_in_vpc_air_gap_revokes_allowlisted_public_ip(
    air_gap_source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The P0 direction: an engaged air gap denies an explicitly allowlisted
    PUBLIC IP literal — the rented demo-GPU rerank endpoint shape."""
    if air_gap_source in {"env", "both"}:
        monkeypatch.setenv("AIR_GAP", "1")
    else:
        monkeypatch.delenv("AIR_GAP", raising=False)
    air_gap = air_gap_source in {"argument", "both"}

    with pytest.raises(UnsafeEndpointError, match="AIR_GAP is engaged"):
        validate_in_vpc_endpoint("http://8.8.8.8:1837", allowed_hosts=("8.8.8.8",), air_gap=air_gap)


def test_in_vpc_allowlisted_public_ip_is_admitted_with_no_air_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CONTROL for the battery above: the identical call, gap disengaged from
    BOTH sources, still admits the host. Without this a blanket deny would pass."""
    monkeypatch.delenv("AIR_GAP", raising=False)
    assert (
        validate_in_vpc_endpoint("http://8.8.8.8:1837", allowed_hosts=("8.8.8.8",), air_gap=False)
        == "http://8.8.8.8:1837"
    )


def test_in_vpc_air_gap_revokes_an_allowlisted_dns_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the rule: an air gap revokes the grant for a NAME too.

    An earlier revision admitted allowlisted DNS names, citing ne-gateway's
    ``_is_in_vpc`` (whose trailing ``return host in allowed_private_vllm_hosts``
    carries no air-gap test). That made the control defeatable by the SPELLING of
    a string — the same server was refused as ``8.8.8.8`` and admitted as
    ``dns.google`` — which is not a boundary. The platform rule, written down
    first in ``ne.storage.s3`` ("an env var must not be able to punch a hole
    through the air gap"), is that the allowlist contributes NOTHING under an
    engaged gap. ne-gateway is a tracked divergence, harmonized by its own unit.

    Cost, accepted deliberately: a sealed deployment whose in-VPC sidecar carries
    a corporate FQDN outside the ``.internal``/``.local``/dotless/private-IP
    shapes now fails loudly at construction. ``test_in_vpc_air_gap_leaves_in_vpc_shapes_alone``
    and the private-IP case below are the controls that keep this from being a
    blanket kill."""
    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(UnsafeEndpointError, match="AIR_GAP is engaged"):
        validate_in_vpc_endpoint(
            "https://tei.corp.example.com", allowed_hosts=("tei.corp.example.com",), air_gap=True
        )


def test_air_gap_revokes_an_allowlisted_name_that_resolves_publicly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case that decided the rule, kept as the regression it is.

    ``dns.google`` is a real, globally-resolvable name whose A/AAAA records are
    all PUBLIC addresses (``8.8.8.8``, ``8.8.4.4``, ``2001:4860:4860::8888/8844``
    — resolved 2026-08-05; asserted here without a lookup so the test stays
    hermetic and offline-safe). Under an engaged air gap it must be refused
    exactly like the address it resolves to, so the outcome no longer depends on
    which of the two the operator typed. Both spellings are asserted together
    because their DIVERGENCE was the defect."""
    monkeypatch.setenv("AIR_GAP", "1")

    with pytest.raises(UnsafeEndpointError, match="AIR_GAP is engaged"):
        validate_in_vpc_endpoint(
            "http://dns.google:1837", allowed_hosts=("dns.google",), air_gap=True
        )
    with pytest.raises(UnsafeEndpointError, match="AIR_GAP is engaged"):
        validate_in_vpc_endpoint("http://8.8.8.8:1837", allowed_hosts=("8.8.8.8",), air_gap=True)


def test_air_gap_leaves_an_allowlisted_private_ip_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTROL for the revocation, and the reason it is not ``return air_gap``.

    An operator may allowlist a host whose shape ALREADY proves it in-VPC — a
    private IP, ``localhost``, a dotless service name, a ``.internal`` name. Those
    would pass with no allowlist at all, so revoking them would break the sealed
    on-box lane an air gap is supposed to protect. A naive "gap engaged and host
    was allowlisted ⇒ refuse" would fail every case below."""
    monkeypatch.setenv("AIR_GAP", "1")
    for endpoint, host in [
        ("http://10.0.0.5:8085", "10.0.0.5"),
        ("http://127.0.0.1:8085", "127.0.0.1"),
        ("http://localhost:8085", "localhost"),
        ("http://tei-rerank", "tei-rerank"),
        ("http://svc.internal", "svc.internal"),
    ]:
        assert (
            validate_in_vpc_endpoint(endpoint, allowed_hosts=(host,), air_gap=True) == endpoint
        ), f"{host} is provably in-VPC and must survive the gap even when allowlisted"


@pytest.mark.parametrize(
    "endpoint",
    ["http://10.0.0.5:8080", "http://127.0.0.1:8085", "http://localhost:8085", "http://tei-rerank"],
)
def test_in_vpc_air_gap_leaves_in_vpc_shapes_alone(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AIR_GAP restricts EGRESS, not in-VPC traffic: private/loopback literals,
    localhost and dotless service names are untouched by the gap (SPEC C2 — the
    open-weight in-VPC lane stays alive under AIR_GAP)."""
    monkeypatch.setenv("AIR_GAP", "1")
    assert validate_in_vpc_endpoint(endpoint, air_gap=True) == endpoint


def test_in_vpc_air_gap_argument_can_only_add_restriction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``air_gap=False`` must NEVER override an engaged process-global AIR_GAP.

    The env term is read unconditionally inside the rule, so a caller that
    forgets to thread its runtime posture still fails closed on the flag."""
    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(UnsafeEndpointError, match="AIR_GAP is engaged"):
        validate_in_vpc_endpoint("http://8.8.8.8:1837", allowed_hosts=("8.8.8.8",), air_gap=False)


def test_in_vpc_air_gap_message_names_the_host_not_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Message hygiene holds on the new branch too: the host, never the URL, and
    never a path/port-bearing string that could carry operator config."""
    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_in_vpc_endpoint("http://8.8.8.8:1837/v1", allowed_hosts=("8.8.8.8",))
    assert "8.8.8.8" in str(exc.value)
    assert "http://8.8.8.8:1837/v1" not in str(exc.value)


def test_in_vpc_rejects_external_without_allowlist() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_in_vpc_endpoint("https://tei.corp.example.com")


# -- Gateway accept matrix + trailing-slash -----------------------------------


def test_gateway_trailing_slash_stripped() -> None:
    assert (
        validate_endpoint_url("https://api.example.com/", require_https=True, allow_private=False)
        == "https://api.example.com"
    )


def test_gateway_accepts_public_host_when_not_requiring_private() -> None:
    assert (
        validate_endpoint_url("https://api.example.com", require_https=True, allow_private=False)
        == "https://api.example.com"
    )


def test_gateway_https_required_rejects_http() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url("http://api.example.com", require_https=True, allow_private=False)


# -- Packed-IPv4 forms (decimal / hex upper+lower / octal) --------------------


@pytest.mark.parametrize(
    "host",
    ["2852039166", "0xA9FEA9FE", "0xa9fea9fe", "025177524776"],
)
@pytest.mark.parametrize("allow_private", [True, False])
def test_packed_ipv4_rejected_gateway(host: str, allow_private: bool) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url(f"http://{host}", require_https=False, allow_private=allow_private)


@pytest.mark.parametrize(
    "host",
    ["2852039166", "0xA9FEA9FE", "0xa9fea9fe", "025177524776"],
)
def test_packed_ipv4_rejected_in_vpc(host: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_in_vpc_endpoint(f"http://{host}")


# -- error_cls plumbing: the caller's exact type is raised --------------------


def test_error_cls_plumbing_gateway() -> None:
    with pytest.raises(_CustomError):
        validate_endpoint_url(
            "http://169.254.169.254",
            require_https=False,
            allow_private=True,
            error_cls=_CustomError,
        )


def test_error_cls_plumbing_in_vpc() -> None:
    with pytest.raises(_CustomError):
        validate_in_vpc_endpoint("https://evil.example.com", error_cls=_CustomError)


# -- Message hygiene: default path never echoes URL / userinfo ----------------


def test_default_path_credentials_message_names_no_url_component() -> None:
    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_in_vpc_endpoint("http://user:s3cr3tpass@tei.local")
    msg = str(excinfo.value)
    assert "s3cr3tpass" not in msg
    assert "user" not in msg
    assert "tei.local" not in msg  # credential message names no host at all


def test_describe_path_includes_redacted_form() -> None:
    # The gateway lane passes describe=redactor; here a trivial redactor proves the
    # prefix is applied and the raw endpoint is routed through describe().
    def _redact(url: str) -> str:
        return "REDACTED-URL"

    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_endpoint_url(
            "http://169.254.169.254",
            require_https=False,
            allow_private=True,
            describe=_redact,
        )
    assert "REDACTED-URL" in str(excinfo.value)


def test_external_hint_appears_in_message() -> None:
    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_in_vpc_endpoint("https://evil.example.com", external_hint="HINT-TOKEN")
    assert "HINT-TOKEN" in str(excinfo.value)


def test_normalize_host_lowercases_and_strips_trailing_dot() -> None:
    assert normalize_host("Example.COM.") == "example.com"


# -- validate_external_https_url (external lane: https-only, DNS-only) ---------


@pytest.mark.parametrize("endpoint", SSRF_REDTEAM_REJECTS)
@pytest.mark.parametrize("allowed_hosts", [None, ("graph.microsoft.com",)])
def test_external_https_rejects_corpus_every_knob(
    endpoint: str, allowed_hosts: tuple[str, ...] | None
) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_external_https_url(endpoint, allowed_hosts=allowed_hosts)


def test_external_https_rejects_public_ip_literal() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_external_https_url("https://8.8.8.8")


def test_external_https_accepts_any_dns_when_allowlist_none() -> None:
    assert (
        validate_external_https_url("https://anything.example.com")
        == "https://anything.example.com"
    )


def test_external_https_accepts_matching_allowlist_and_rstrips() -> None:
    assert (
        validate_external_https_url(
            "https://graph.microsoft.com/v1.0/", allowed_hosts=("graph.microsoft.com",)
        )
        == "https://graph.microsoft.com/v1.0"
    )


def test_external_https_rejects_non_matching_allowlist() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_external_https_url(
            "https://graph.microsoft.com/v1.0", allowed_hosts=("graph.microsoft.us",)
        )


def test_external_https_rejects_http_when_https_required() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_external_https_url("http://graph.microsoft.com")


def test_external_https_error_cls_plumbing() -> None:
    with pytest.raises(_CustomError):
        validate_external_https_url("https://8.8.8.8", error_cls=_CustomError)


def test_external_https_credentials_message_hides_secret() -> None:
    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_external_https_url("https://user:s3cr3t@graph.microsoft.com")
    msg = str(excinfo.value)
    assert "s3cr3t" not in msg
    assert "user" not in msg


# -- validate_loopback_endpoint (strict loopback lane) ------------------------


@pytest.mark.parametrize("endpoint", SSRF_REDTEAM_REJECTS)
@pytest.mark.parametrize("allowed_hosts", [(), ("svc.example.com",)])
def test_loopback_rejects_corpus_every_knob(endpoint: str, allowed_hosts: tuple[str, ...]) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_loopback_endpoint(endpoint, allowed_hosts=allowed_hosts)


@pytest.mark.parametrize("endpoint", LOOPBACK_ENDPOINTS)
def test_loopback_accepts_all_loopback_endpoints(endpoint: str) -> None:
    assert validate_loopback_endpoint(endpoint) == endpoint


def test_loopback_accepts_allowlisted_host() -> None:
    assert (
        validate_loopback_endpoint(
            "http://graphiti-sidecar:8093", allowed_hosts=("graphiti-sidecar",)
        )
        == "http://graphiti-sidecar:8093"
    )


@pytest.mark.parametrize("endpoint", ["http://10.0.0.5", "http://svc"])
def test_loopback_is_stricter_than_in_vpc(endpoint: str) -> None:
    # Private (non-loopback) IPs and dotless service names pass the in-VPC lane but
    # NOT the strict loopback lane.
    assert validate_in_vpc_endpoint(endpoint) == endpoint
    with pytest.raises(UnsafeEndpointError):
        validate_loopback_endpoint(endpoint)


def test_loopback_reject_message_contains_external_and_only_host() -> None:
    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_loopback_endpoint("http://user:s3cr3t@evil.example.com/path?q=1")
    # Credential/query vectors die in Phase A and never echo the secret; the
    # external-shaped host reject (when it fires) contains the word 'external'.
    assert "s3cr3t" not in str(excinfo.value)
    with pytest.raises(UnsafeEndpointError) as exc2:
        validate_loopback_endpoint("http://plainexternal.example.com")
    assert "external" in str(exc2.value)
    assert "plainexternal.example.com" in str(exc2.value)


def test_loopback_error_cls_plumbing() -> None:
    with pytest.raises(_CustomError):
        validate_loopback_endpoint("http://10.0.0.5", error_cls=_CustomError)


# -- redact_url_for_logs: canonical home (NE-AUDIT-CRON P2-1) -----------------
# The credential/log redactor moved here from ne.gateway.ssrf so a non-gateway
# consumer (ne-worker's audit-checkpoint loop) can use it without dragging the
# litellm provider closure into its process. ne.gateway.ssrf re-exports it, and
# packages/ne-gateway/tests/test_ssrf.py still exercises that re-export path —
# so the behaviour is pinned from BOTH import paths.


def test_redact_url_for_logs_masks_sensitive_query_keys() -> None:
    redacted = redact_url_for_logs(
        "https://api.example.com/v1?token=secret&api_key=abc&model=gpt-4"
    )
    assert "secret" not in redacted
    assert "abc" not in redacted
    assert "REDACTED" in redacted
    assert "model=gpt-4" in redacted


def test_redact_url_for_logs_masks_userinfo() -> None:
    redacted = redact_url_for_logs("https://admin:s3cr3tpass@vllm.internal.example/v1")
    assert "s3cr3tpass" not in redacted
    assert "admin" not in redacted
    assert "***" in redacted
    assert "vllm.internal.example" in redacted


def test_redact_url_for_logs_masks_username_only_userinfo() -> None:
    redacted = redact_url_for_logs("https://tok_abc123@vllm.internal.example/v1")
    assert "tok_abc123" not in redacted
    assert "***" in redacted
    assert "vllm.internal.example" in redacted


def test_redact_url_for_logs_handles_malformed_port_without_raising() -> None:
    # Logging helper must be total: never propagate ValueError on a bad port,
    # and still mask embedded userinfo.
    redacted = redact_url_for_logs("https://admin:s3cr3tpass@example.com:99999/v1")
    assert "s3cr3tpass" not in redacted
    assert "admin" not in redacted
    assert "***" in redacted


def test_redact_masks_schemeless_credentialed_url() -> None:
    redacted = redact_url_for_logs("user:s3cr3tpass@host/v1")
    assert "user:" not in redacted
    assert "s3cr3tpass" not in redacted
    assert "***:***@host" in redacted


def test_redact_masks_single_slash_credentialed_url() -> None:
    redacted = redact_url_for_logs("https:/user:s3cr3tpass@host")
    assert "s3cr3tpass" not in redacted
    assert "user" not in redacted
    assert "***" in redacted


def test_redact_masks_schemeless_bearer_token() -> None:
    redacted = redact_url_for_logs("tok_abc123@host/v1")
    assert "tok_abc123" not in redacted
    assert redacted == "***@host/v1"


def test_redact_url_for_logs_identity_when_nothing_sensitive() -> None:
    url = "https://vllm.internal.example/v1/chat?model=gpt-4"
    assert redact_url_for_logs(url) == url
