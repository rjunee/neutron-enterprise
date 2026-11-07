# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end verification against a REAL Keycloak — LIVE-PARKABLE.

Skips cleanly when Keycloak is unreachable (no docker on the build host,
NE-006 precedent). No mocking: a real password-grant token from the ne-dev
realm is verified via real JWKS discovery, then JIT-provisioned.

Start it with: ``ne dev up`` (Keycloak 26.7.4 on http://localhost:8081).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

import pytest

from ne.identity.config import OidcConfig, RoleMappingConfig
from ne.identity.jwks import HttpJwksSource
from ne.identity.provisioning import InMemoryPrincipalStore, JitProvisioner
from ne.identity.verifier import OidcTokenVerifier

from .conftest import run

_KEYCLOAK_URL = os.environ.get("NE_TEST_KEYCLOAK_URL", "http://localhost:8081")
_ISSUER = f"{_KEYCLOAK_URL}/realms/ne-dev"
_TOKEN_URL = f"{_ISSUER}/protocol/openid-connect/token"


def _keycloak_available() -> bool:
    try:
        with urllib.request.urlopen(f"{_ISSUER}/.well-known/openid-configuration", timeout=1.0):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _keycloak_available(),
    reason="Keycloak not reachable (start deploy/compose/dev: ne dev up)",
)


def _password_grant() -> str:
    data = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "ne-console",
            "username": "dev",
            "password": "dev",
        }
    ).encode()
    with urllib.request.urlopen(_TOKEN_URL, data=data, timeout=5.0) as resp:
        payload = json.loads(resp.read())
    return payload["access_token"]


def _verifier() -> OidcTokenVerifier:
    config = OidcConfig(issuer=_ISSUER, audience="ne-api", allow_insecure_http=True)
    return OidcTokenVerifier(config, key_source=HttpJwksSource(config))


def test_real_token_verified_and_provisioned() -> None:
    token = _password_grant()
    verifier = _verifier()
    claims = run(verifier.verify(token))
    assert claims.subject

    store = InMemoryPrincipalStore()
    provisioner = JitProvisioner(
        store,
        RoleMappingConfig(mapping={"platform-admins": ("admin",)}),
    )
    principal = run(provisioner.provision(claims))
    assert "platform-admins" in principal.groups
    assert "role:admin" in principal.groups


def test_tampered_real_token_rejected() -> None:
    from ne.identity.errors import TokenVerificationError

    token = _password_grant()
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload}.{sig[:-2]}XX"
    with pytest.raises(TokenVerificationError):
        run(_verifier().verify(tampered))
