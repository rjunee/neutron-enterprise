# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""IdentitySettings / OidcConfig layering and fail-closed construction."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ne.identity.config import IdentitySettings, OidcConfig


def test_env_layer_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NE_IDENTITY_OIDC__ISSUER", "https://idp.ne.test/realms/ne")
    monkeypatch.setenv("NE_IDENTITY_OIDC__AUDIENCE", "ne-api")
    monkeypatch.setenv("NE_IDENTITY_OIDC__ALLOW_INSECURE_HTTP", "true")
    settings = IdentitySettings()  # type: ignore[call-arg]  # oidc from env
    assert settings.verifier == "keycloak-oidc"
    assert settings.resolver == "jit"
    assert settings.oidc.issuer == "https://idp.ne.test/realms/ne"
    assert settings.oidc.audience == "ne-api"
    assert settings.oidc.allow_insecure_http is True


def test_missing_issuer_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NE_IDENTITY_OIDC__ISSUER", raising=False)
    monkeypatch.delenv("NE_IDENTITY_OIDC__AUDIENCE", raising=False)
    with pytest.raises(ValidationError):
        IdentitySettings()  # type: ignore[call-arg]


def test_missing_audience_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NE_IDENTITY_OIDC__ISSUER", "https://idp.ne.test/realms/ne")
    monkeypatch.delenv("NE_IDENTITY_OIDC__AUDIENCE", raising=False)
    with pytest.raises(ValidationError):
        IdentitySettings()  # type: ignore[call-arg]


def test_trailing_slash_issuer_rejected() -> None:
    with pytest.raises(ValidationError):
        OidcConfig(issuer="https://idp.ne.test/realms/ne/", audience="ne-api")


def test_leeway_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        OidcConfig(issuer="https://idp.ne.test/realms/ne", audience="ne-api", leeway_seconds=120)


def test_principal_claims_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    oidc = OidcConfig(issuer="https://idp.ne.test/realms/ne", audience="ne-api")
    settings = IdentitySettings(oidc=oidc)
    assert settings.principal_claims == ("email", "preferred_username", "name")


def test_extra_forbidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    oidc = OidcConfig(issuer="https://idp.ne.test/realms/ne", audience="ne-api")
    with pytest.raises(ValidationError):
        IdentitySettings(oidc=oidc, bogus="x")  # type: ignore[call-arg]


def test_idp_claim_default() -> None:
    oidc = OidcConfig(issuer="https://idp.ne.test/realms/ne", audience="ne-api")
    assert oidc.idp_claim == "identity_provider"


def test_idp_claim_env_override_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NE_IDENTITY_OIDC__ISSUER", "https://idp.ne.test/realms/ne")
    monkeypatch.setenv("NE_IDENTITY_OIDC__AUDIENCE", "ne-api")
    monkeypatch.setenv("NE_IDENTITY_OIDC__IDP_CLAIM", "custom_idp_alias")
    settings = IdentitySettings()  # type: ignore[call-arg]  # oidc from env
    assert settings.oidc.idp_claim == "custom_idp_alias"


def test_idp_claim_rejects_empty_string() -> None:
    """NE-BEW-GROUPS-ZEROGROUPS Argus P3: an empty idp_claim would silently
    and permanently disable the zero-mapped-groups signal (claims.get(...)
    always returns None) with no startup error — reject it at construction.
    """
    with pytest.raises(ValidationError):
        OidcConfig(issuer="https://idp.ne.test/realms/ne", audience="ne-api", idp_claim="")
