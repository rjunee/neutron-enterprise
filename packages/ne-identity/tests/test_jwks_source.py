# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""JWKS source tests.

StaticJwksSource is exercised end-to-end. For HttpJwksSource, only the
below-the-seam sync JSON fetch (``_fetch_json_sync``) is monkeypatched — the
seam under test elsewhere is token verification, not HTTP transport; the real
HTTP path is covered, skip-gated, by test_keycloak_live.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ne.identity.config import OidcConfig
from ne.identity.errors import JwksError
from ne.identity.jwks import HttpJwksSource, StaticJwksSource

from .conftest import DEFAULT_KID, TEST_AUDIENCE, TEST_ISSUER, run


def test_static_source_kid_lookup(jwks: dict[str, Any]) -> None:
    source = StaticJwksSource(jwks)
    key = run(source.get_signing_key(DEFAULT_KID))
    assert key.key_id == DEFAULT_KID


def test_static_source_unknown_kid_raises(jwks: dict[str, Any]) -> None:
    source = StaticJwksSource(jwks)
    with pytest.raises(JwksError):
        run(source.get_signing_key("nope"))


def _https_config(**overrides: Any) -> OidcConfig:
    base: dict[str, Any] = {"issuer": TEST_ISSUER, "audience": TEST_AUDIENCE}
    base.update(overrides)
    return OidcConfig(**base)


def test_discovery_issuer_mismatch_fails_closed(
    jwks: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _https_config()
    source = HttpJwksSource(config)

    def fake_fetch(url: str) -> dict[str, Any]:
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": "https://evil.example/realms/x", "jwks_uri": "https://x/certs"}
        return jwks

    monkeypatch.setattr(source, "_fetch_json_sync", fake_fetch)
    with pytest.raises(JwksError):
        run(source.get_signing_key(DEFAULT_KID))


def test_explicit_jwks_uri_skips_discovery(
    jwks: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _https_config(jwks_uri="https://idp.ne.test/certs")
    source = HttpJwksSource(config)
    seen: list[str] = []

    def fake_fetch(url: str) -> dict[str, Any]:
        seen.append(url)
        return jwks

    monkeypatch.setattr(source, "_fetch_json_sync", fake_fetch)
    key = run(source.get_signing_key(DEFAULT_KID))
    assert key.key_id == DEFAULT_KID
    assert seen == ["https://idp.ne.test/certs"]  # no discovery fetch


def test_ttl_cache_serves_without_refetch(
    jwks: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _https_config(jwks_uri="https://idp.ne.test/certs")
    source = HttpJwksSource(config)
    calls = {"n": 0}

    def fake_fetch(url: str) -> dict[str, Any]:
        calls["n"] += 1
        return jwks

    monkeypatch.setattr(source, "_fetch_json_sync", fake_fetch)
    run(source.get_signing_key(DEFAULT_KID))
    run(source.get_signing_key(DEFAULT_KID))
    assert calls["n"] == 1  # second lookup served from the TTL cache


def test_unknown_kid_triggers_one_refresh_then_fails(
    jwks: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _https_config(jwks_uri="https://idp.ne.test/certs")
    source = HttpJwksSource(config)
    calls = {"n": 0}

    def fake_fetch(url: str) -> dict[str, Any]:
        calls["n"] += 1
        return jwks

    monkeypatch.setattr(source, "_fetch_json_sync", fake_fetch)
    with pytest.raises(JwksError):
        run(source.get_signing_key("rotated-out"))
    # One initial fetch + exactly one forced refresh.
    assert calls["n"] == 2


def test_insecure_http_refused_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    config = OidcConfig(
        issuer="http://localhost:8081/realms/ne-dev",
        audience=TEST_AUDIENCE,
    )
    source = HttpJwksSource(config)
    with pytest.raises(JwksError):
        run(source.get_signing_key(DEFAULT_KID))


@pytest.mark.parametrize("bad_uri", ["file:///etc/passwd", "ftp://idp.ne.test/certs"])
def test_non_http_scheme_rejected_even_when_insecure_allowed(
    jwks: dict[str, Any], monkeypatch: pytest.MonkeyPatch, bad_uri: str
) -> None:
    """allow_insecure_http relaxes https->http ONLY; file://, ftp:// etc. stay banned.

    Covers both intake paths: an explicitly configured jwks_uri and a jwks_uri
    returned by a (potentially compromised) discovery document.
    """
    # Path 1: an explicitly configured non-http(s) jwks_uri is already rejected
    # at construction time by OidcConfig's validator.
    with pytest.raises(ValidationError):
        OidcConfig(
            issuer="http://localhost:8081/realms/ne-dev",
            audience=TEST_AUDIENCE,
            allow_insecure_http=True,
            jwks_uri=bad_uri,
        )

    # Path 2: discovery document points jwks_uri at a non-HTTP scheme.
    config2 = OidcConfig(
        issuer="http://localhost:8081/realms/ne-dev",
        audience=TEST_AUDIENCE,
        allow_insecure_http=True,
    )
    source = HttpJwksSource(config2)
    fetched: list[str] = []

    def fake_fetch(url: str) -> dict[str, Any]:
        fetched.append(url)
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": "http://localhost:8081/realms/ne-dev", "jwks_uri": bad_uri}
        return jwks

    monkeypatch.setattr(source, "_fetch_json_sync", fake_fetch)
    with pytest.raises(JwksError):
        run(source.get_signing_key(DEFAULT_KID))
    # The poisoned jwks_uri must never be fetched.
    assert bad_uri not in fetched


def test_insecure_http_allowed_when_opted_in(
    jwks: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = OidcConfig(
        issuer="http://localhost:8081/realms/ne-dev",
        audience=TEST_AUDIENCE,
        allow_insecure_http=True,
    )
    source = HttpJwksSource(config)

    def fake_fetch(url: str) -> dict[str, Any]:
        if url.endswith("/.well-known/openid-configuration"):
            return {
                "issuer": "http://localhost:8081/realms/ne-dev",
                "jwks_uri": "http://localhost:8081/realms/ne-dev/protocol/openid-connect/certs",
            }
        return jwks

    monkeypatch.setattr(source, "_fetch_json_sync", fake_fetch)
    key = run(source.get_signing_key(DEFAULT_KID))
    assert key.key_id == DEFAULT_KID
