# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Middleware HTTP behavior — ABI RBAC/security test-taxonomy lift.

Member/non-member, forged JWT, cross-user leakage, no-auth-oracle, query-param
anti-pattern, exact-match exemption — all over a real FastAPI TestClient.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from ne.contracts.identity import AuthenticatedRequestContext, TokenClaims
from ne.contracts.types import Principal
from ne.identity.config import RoleMappingConfig
from ne.identity.middleware import (
    OidcAuthMiddleware,
    current_authenticated_context,
    current_principal,
)
from ne.identity.provisioning import InMemoryPrincipalStore, JitProvisioner
from ne.identity.verifier import OidcTokenVerifier

from .conftest import DEFAULT_KID, TEST_ISSUER


def test_no_auth_header_401_with_challenge(app_client: tuple[TestClient, list[str]]) -> None:
    client, hits = app_client
    resp = client.get("/whoami")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Bearer")
    assert hits == []  # handler never reached


@pytest.mark.parametrize(
    "header",
    ["Token abc", "Bearer", "Bearer a b", "Basic dXNlcjpwYXNz", "bearer"],
)
def test_malformed_auth_headers_401(app_client: tuple[TestClient, list[str]], header: str) -> None:
    client, _ = app_client
    resp = client.get("/whoami", headers={"Authorization": header})
    assert resp.status_code == 401


def test_query_param_token_ignored(app_client: tuple[TestClient, list[str]], mint: Any) -> None:
    client, hits = app_client
    token = mint()
    r1 = client.get(f"/whoami?token={token}")
    r2 = client.get(f"/whoami?access_token={token}")
    assert r1.status_code == 401
    assert r2.status_code == 401
    assert hits == []


def test_scheme_case_insensitive(app_client: tuple[TestClient, list[str]], mint: Any) -> None:
    client, _ = app_client
    resp = client.get("/whoami", headers={"Authorization": f"bearer {mint()}"})
    assert resp.status_code == 200


def test_valid_token_reaches_handler_with_principal(
    app_client: tuple[TestClient, list[str]], mint: Any
) -> None:
    client, hits = app_client
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {mint()}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "user-1"
    assert "engineering" in body["groups"]
    assert "role:developer" in body["groups"]
    assert hits == ["user-1"]


def test_real_middleware_attaches_minimal_hash_bound_authenticated_context(
    verifier: OidcTokenVerifier,
    role_config: RoleMappingConfig,
    mint: Any,
) -> None:
    app = FastAPI()

    @app.get("/context")
    def context(
        value: AuthenticatedRequestContext = Depends(current_authenticated_context),  # noqa: B008
    ) -> JSONResponse:
        return JSONResponse(value.model_dump(mode="json"))

    provisioner = JitProvisioner(
        InMemoryPrincipalStore(),
        role_config,
        principal_claims=("department", "email", "preferred_username", "name"),
    )
    client = TestClient(OidcAuthMiddleware(app, verifier=verifier, provisioner=provisioner))
    now = int(time.time())
    token = mint(
        {
            "iat": now,
            "exp": now + 300,
            "department": "engineering",
            "email": "ada@example.test",
            "preferred_username": "ada",
            "name": "Ada",
            "sid": "session-private",
        }
    )
    response = client.get("/context", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "schema_version": 1,
        "principal_id": "user-1",
        "authorization_groups": ["engineering", "role:developer", "role:viewer"],
        "department": "engineering",
        "issuer": TEST_ISSUER,
        "expires_at": now + 300,
        "context_hash": body["context_hash"],
    }
    assert len(body["context_hash"]) == 64
    assert not ({"email", "preferred_username", "name", "sid", "claims", "token"} & body.keys())


def test_legacy_verifier_without_expiry_authenticates_sync_but_context_refuses(
    role_config: RoleMappingConfig,
) -> None:
    class LegacyVerifier:
        async def verify(self, token: str) -> TokenClaims:
            return TokenClaims(
                subject="user-legacy",
                issuer=TEST_ISSUER,
                claims={"groups": ["engineering"]},
            )

    app = FastAPI()

    @app.get("/context")
    def context(
        value: AuthenticatedRequestContext = Depends(current_authenticated_context),  # noqa: B008
    ) -> JSONResponse:
        return JSONResponse(value.model_dump(mode="json"))

    @app.get("/principal")
    def principal(value: Principal = Depends(current_principal)) -> JSONResponse:  # noqa: B008
        return JSONResponse({"id": value.id})

    provisioner = JitProvisioner(InMemoryPrincipalStore(), role_config)
    client = TestClient(OidcAuthMiddleware(app, verifier=LegacyVerifier(), provisioner=provisioner))
    synchronous = client.get("/principal", headers={"Authorization": "Bearer legacy"})
    assert synchronous.status_code == 200
    assert synchronous.json() == {"id": "user-legacy"}
    response = client.get("/context", headers={"Authorization": "Bearer legacy"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Authenticated context unavailable"}


def test_expired_token_401_no_oracle(app_client: tuple[TestClient, list[str]], mint: Any) -> None:
    import time

    client, _ = app_client
    now = int(time.time())
    token = mint({"exp": now - 3600, "iat": now - 3700})
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert 'error="invalid_token"' in resp.headers["www-authenticate"]
    body = resp.text.lower()
    # No auth oracle: body must not leak the reason.
    for leak in ("expired", "signature", "audience", "issuer", "exp", "traceback"):
        assert leak not in body


def test_forged_signature_401(
    app_client: tuple[TestClient, list[str]], mint: Any, attacker_key: Any
) -> None:
    client, _ = app_client
    token = mint(key=attacker_key, kid=DEFAULT_KID)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_cross_user_isolation(app_client: tuple[TestClient, list[str]], mint: Any) -> None:
    client, hits = app_client
    t1 = mint({"sub": "user-1"})
    t2 = mint({"sub": "user-2"})
    r1 = client.get("/whoami", headers={"Authorization": f"Bearer {t1}"})
    r2 = client.get("/whoami", headers={"Authorization": f"Bearer {t2}"})
    r3 = client.get("/whoami", headers={"Authorization": f"Bearer {t1}"})
    assert r1.json()["id"] == "user-1"
    assert r2.json()["id"] == "user-2"
    assert r3.json()["id"] == "user-1"  # no state bleed
    assert hits == ["user-1", "user-2", "user-1"]


def test_health_exempt_without_auth(app_client: tuple[TestClient, list[str]]) -> None:
    client, _ = app_client
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_near_miss_requires_auth(app_client: tuple[TestClient, list[str]]) -> None:
    client, _ = app_client
    # Exact-match exemption: /healthX is NOT exempt.
    resp = client.get("/healthX")
    assert resp.status_code == 401
