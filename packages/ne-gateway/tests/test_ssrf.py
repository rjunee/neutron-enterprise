# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the ported ABI SSRF endpoint validator (NE-012)."""

from __future__ import annotations

import pytest

from ne.gateway.errors import UnsafeEndpointError
from ne.gateway.ssrf import redact_url_for_logs, validate_endpoint_url
from ne.net.testing import SSRF_REDTEAM_REJECTS


def test_metadata_ip_rejected_even_when_private_allowed() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url(
            "https://169.254.169.254/latest", require_https=False, allow_private=True
        )


def test_google_metadata_host_rejected() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url(
            "http://metadata.google.internal/", require_https=False, allow_private=True
        )


def test_link_local_rejected_even_when_private_allowed() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url("http://169.254.10.10", require_https=False, allow_private=True)


def test_private_range_rejected_when_not_allowed() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url("https://10.0.0.5", require_https=False, allow_private=False)


def test_private_range_accepted_when_allowed() -> None:
    assert (
        validate_endpoint_url("http://192.168.1.10", require_https=False, allow_private=True)
        == "http://192.168.1.10"
    )


def test_localhost_rejected_when_not_allowed() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url("http://localhost:11434", require_https=False, allow_private=False)


def test_localhost_accepted_when_allowed() -> None:
    assert (
        validate_endpoint_url("http://localhost:11434", require_https=False, allow_private=True)
        == "http://localhost:11434"
    )


def test_credentials_in_url_rejected() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url(
            "https://user:pass@api.example.com", require_https=True, allow_private=False
        )


def test_query_and_fragment_rejected() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url(
            "https://api.example.com/?token=abc", require_https=True, allow_private=False
        )
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url(
            "https://api.example.com/#frag", require_https=True, allow_private=False
        )


def test_non_http_scheme_rejected() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url("ftp://x", require_https=False, allow_private=True)


def test_http_rejected_when_https_required() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url("http://api.example.com", require_https=True, allow_private=False)


def test_missing_hostname_rejected() -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url("https://", require_https=True, allow_private=False)


def test_trailing_slash_stripped() -> None:
    assert (
        validate_endpoint_url("https://api.example.com/", require_https=True, allow_private=False)
        == "https://api.example.com"
    )


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


def test_validate_endpoint_url_credentials_error_never_leaks_cleartext() -> None:
    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_endpoint_url(
            "https://admin:s3cr3tpass@vllm.internal.example/v1",
            require_https=True,
            allow_private=True,
        )
    assert "s3cr3tpass" not in str(excinfo.value)
    assert "admin" not in str(excinfo.value)


def test_malformed_port_rejected_as_unsafe_endpoint_error() -> None:
    # Reproduces the reported regression: an out-of-range port on a credentialed
    # URL previously raised a raw ValueError instead of the typed UnsafeEndpointError.
    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_endpoint_url(
            "https://user:pass@example.com:99999/v1",
            require_https=True,
            allow_private=True,
        )
    # pytest.raises(UnsafeEndpointError) already proves it is not a bare ValueError
    # (UnsafeEndpointError does not subclass ValueError).
    assert "pass" not in str(excinfo.value)
    assert "user" not in str(excinfo.value)


def test_malformed_port_non_numeric_rejected_as_unsafe_endpoint_error() -> None:
    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_endpoint_url(
            "https://user:pass@example.com:abc/v1",
            require_https=True,
            allow_private=True,
        )
    assert "pass" not in str(excinfo.value)
    assert "user" not in str(excinfo.value)


def test_malformed_port_without_credentials_also_rejected() -> None:
    # A non-credentialed URL with a bad port is SSRF-shaped invalid input and must
    # fail closed as UnsafeEndpointError. The pre-fix baseline silently ACCEPTED
    # this (it never touched parsed.port), so this is a genuine behavior change.
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url(
            "https://example.com:99999/v1",
            require_https=True,
            allow_private=False,
        )


def test_redact_url_for_logs_handles_malformed_port_without_raising() -> None:
    # Logging helper must be total: never propagate ValueError on a bad port,
    # and still mask embedded userinfo.
    redacted = redact_url_for_logs("https://admin:s3cr3tpass@example.com:99999/v1")
    assert "s3cr3tpass" not in redacted
    assert "admin" not in redacted
    assert "***" in redacted


# -- NE-CKC3 SECRET-LEAK: non-//netloc credentialed URLs ----------------------
# urllib populates parsed.username/password ONLY for //netloc URLs, so scheme-less
# and single-slash credentialed endpoints previously echoed cleartext credentials.


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


def test_validate_schemeless_credentialed_url_rejected_without_leak() -> None:
    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_endpoint_url("user:s3cr3tpass@host/v1", require_https=False, allow_private=True)
    assert "s3cr3tpass" not in str(excinfo.value)


def test_validate_single_slash_credentialed_url_rejected_without_leak() -> None:
    with pytest.raises(UnsafeEndpointError) as excinfo:
        validate_endpoint_url(
            "https:/user:s3cr3tpass@host", require_https=False, allow_private=True
        )
    assert "s3cr3tpass" not in str(excinfo.value)


# -- NE-CKC3 audit #10: packed-integer IPv4 metadata bypass -------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://2852039166/v1",  # decimal == 169.254.169.254 (metadata)
        "http://0xA9FEA9FE/v1",  # hex == 169.254.169.254
        "http://025177524776/v1",  # octal == 169.254.169.254
    ],
)
@pytest.mark.parametrize("allow_private", [True, False])
def test_packed_ipv4_forms_always_rejected(endpoint: str, allow_private: bool) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url(endpoint, require_https=False, allow_private=allow_private)


# -- NE-CKC10: IPv6 ::1 now follows the SAME policy as 127.0.0.1 --------------


def test_ipv6_loopback_accepted_when_private_allowed() -> None:
    # Previously ::1 was spuriously rejected in every case (is_reserved is True on
    # CPython); it now matches 127.0.0.1 exactly.
    assert (
        validate_endpoint_url("http://[::1]:11434", require_https=False, allow_private=True)
        == "http://[::1]:11434"
    )


def test_ipv6_loopback_rejected_when_not_allowed() -> None:
    # Same policy branch as 127.0.0.1: loopback is rejected when allow_private=False.
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url("http://[::1]:11434", require_https=False, allow_private=False)


# -- NE-CKC10: shared red-team corpus rejected under both allow_private knobs --


@pytest.mark.parametrize("endpoint", SSRF_REDTEAM_REJECTS)
@pytest.mark.parametrize("allow_private", [True, False])
def test_redteam_corpus_always_rejected(endpoint: str, allow_private: bool) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_endpoint_url(endpoint, require_https=False, allow_private=allow_private)


def test_redact_url_for_logs_is_reexported_from_the_net_leaf() -> None:
    """NE-AUDIT-CRON P2-1: the redactor now LIVES in ne.net.ssrf and is merely
    re-exported here, so a non-gateway consumer can import it without dragging
    the litellm provider closure into its process.

    Asserting object identity (not just equal behaviour) is the point: it makes a
    silent regression into two divergent redactors — the drift hazard NE-CKC10
    consolidated away — a test failure rather than a slow security bug.
    """
    import ne.net.ssrf

    assert redact_url_for_logs is ne.net.ssrf.redact_url_for_logs
