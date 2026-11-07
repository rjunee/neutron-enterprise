# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the ne-identity suite.

Provides real RSA keypairs (a trusted signer + an attacker), a JWKS builder, and
a ``mint`` helper that produces JWTs with real cryptographic signatures. NO
Keycloak and NO network — the JWT-validation seam is exercised for real against
locally minted keys (SPEC §5 NE-060 CI requirement).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Coroutine
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from starlette.responses import JSONResponse

from ne.contracts.types import Principal
from ne.identity.config import OidcConfig, RoleMappingConfig
from ne.identity.jwks import StaticJwksSource
from ne.identity.middleware import OidcAuthMiddleware, current_principal
from ne.identity.provisioning import InMemoryPrincipalStore, JitProvisioner
from ne.identity.verifier import OidcTokenVerifier

TEST_ISSUER = "https://idp.ne.test/realms/ne-test"
TEST_AUDIENCE = "ne-api"
DEFAULT_KID = "test-key-1"
ATTACKER_KID = "attacker-key-1"


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive a coroutine to completion (no pytest-asyncio in this repo)."""
    return asyncio.run(coro)


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return jwk


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def attacker_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwks(signing_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    """A JWKS containing only the trusted signing key, under DEFAULT_KID."""
    return {"keys": [_public_jwk(signing_key, DEFAULT_KID)]}


@pytest.fixture(scope="session")
def signing_key_pem(signing_key: rsa.RSAPrivateKey) -> bytes:
    """The trusted PUBLIC key as PEM — the HS256-confusion attack 'secret'."""
    return signing_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


Mint = Callable[..., str]


@pytest.fixture
def mint(signing_key: rsa.RSAPrivateKey) -> Mint:
    """Return a helper that mints signed JWTs with sane, valid-by-default claims."""

    def _mint(
        claims_overrides: dict[str, Any] | None = None,
        *,
        key: Any = None,
        alg: str = "RS256",
        kid: str | None = DEFAULT_KID,
        headers: dict[str, Any] | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "sub": "user-1",
            "iat": now,
            "exp": now + 300,
            "groups": ["engineering"],
            "typ": "Bearer",
        }
        if claims_overrides:
            for k, v in claims_overrides.items():
                if v is _DROP:
                    claims.pop(k, None)
                else:
                    claims[k] = v
        hdr: dict[str, Any] = {}
        if kid is not None:
            hdr["kid"] = kid
        if headers:
            hdr.update(headers)
        signing = signing_key if key is None else key
        return jwt.encode(claims, signing, algorithm=alg, headers=hdr)

    return _mint


# Sentinel telling `mint` to DROP a default claim (vs. setting it to None).
class _DropSentinel:
    pass


_DROP = _DropSentinel()


@pytest.fixture
def DROP() -> _DropSentinel:
    return _DROP


@pytest.fixture
def oidc_config() -> OidcConfig:
    return OidcConfig(issuer=TEST_ISSUER, audience=TEST_AUDIENCE)


@pytest.fixture
def verifier(oidc_config: OidcConfig, jwks: dict[str, Any]) -> OidcTokenVerifier:
    return OidcTokenVerifier(oidc_config, key_source=StaticJwksSource(jwks))


@pytest.fixture
def role_config() -> RoleMappingConfig:
    return RoleMappingConfig(
        mapping={"platform-admins": ("admin",), "engineering": ("developer",)},
        default_roles=("viewer",),
    )


@pytest.fixture
def app_client(
    verifier: OidcTokenVerifier, role_config: RoleMappingConfig
) -> tuple[TestClient, list[str]]:
    """A middleware-wrapped FastAPI app + a record of protected-handler hits."""
    hits: list[str] = []
    app = FastAPI()

    @app.get("/whoami")
    def whoami(principal: Principal = Depends(current_principal)) -> JSONResponse:  # noqa: B008
        hits.append(principal.id)
        return JSONResponse({"id": principal.id, "groups": list(principal.groups)})

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/healthX")
    def health_x() -> JSONResponse:
        return JSONResponse({"status": "near-miss"})

    store = InMemoryPrincipalStore()
    provisioner = JitProvisioner(store, role_config)
    wrapped = OidcAuthMiddleware(app, verifier=verifier, provisioner=provisioner)
    return TestClient(wrapped), hits


# ==========================================================================
# Agent identity (NE-036) — doubles + fixtures for the agents.* test modules.
#
# The config-hash fixture below is NON-autouse (requested explicitly by the new
# tests) so it never touches the frozen NE-060 authn-spine tests, which do not
# freeze a config hash.
# ==========================================================================

from collections.abc import Iterator  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402

from ne.contracts import (  # noqa: E402
    freeze_config_hash,
    get_config_hash,
    reset_config_hash_for_testing,
)
from ne.contracts.events import AuditEventLike  # noqa: E402
from ne.contracts.governance import AuditVerification  # noqa: E402
from ne.contracts.testing.memory import MemorySecretStore  # noqa: E402
from ne.identity.agents import (  # noqa: E402
    AgentCredentialConfig,
    AgentCredentialService,
    AgentIdentitySettings,
    AgentRef,
    CredentialScope,
)


class ListSink:
    """An ``AuditSink`` that records every appended event (structural).

    ``events`` is typed ``list[Any]`` (mirroring the ne-runtime conftest
    ``RecordingAuditSink``) so tests can read the concrete ``AuditEvent`` fields
    (``action``/``resource``/``detail``) that the ``AuditEventLike`` surface does
    not statically expose.
    """

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: AuditEventLike) -> None:
        self.events.append(event)

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return AuditVerification(valid=True, checked=len(self.events))

    def actions(self) -> list[str]:
        return [getattr(e, "action", "") for e in self.events]

    def with_action(self, action: str) -> list[Any]:
        return [e for e in self.events if getattr(e, "action", "") == action]


class FailingSink:
    """An ``AuditSink`` whose ``append`` always raises (fail-closed double)."""

    async def append(self, event: AuditEventLike) -> None:
        raise RuntimeError("audit sink is down")

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return AuditVerification(valid=False, checked=0)


class MutableClock:
    """An injectable, advanceable wall clock (UTC)."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def frozen_agent_config_hash() -> Iterator[str]:
    """Freeze a config hash from ``AgentIdentitySettings()`` for one test."""
    reset_config_hash_for_testing()
    freeze_config_hash(AgentIdentitySettings())
    try:
        yield get_config_hash()
    finally:
        reset_config_hash_for_testing()


@pytest.fixture
def agent_clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def agent_store(agent_clock: MutableClock) -> MemorySecretStore:
    """A secret store bound to the injectable clock (versions age with it)."""
    return MemorySecretStore(now=agent_clock)


@pytest.fixture
def agent_sink() -> ListSink:
    return ListSink()


@pytest.fixture
def agent_service(
    agent_store: MemorySecretStore,
    agent_sink: ListSink,
    agent_clock: MutableClock,
    frozen_agent_config_hash: str,
) -> AgentCredentialService:
    """A service over the SAME clock-bound store/sink the test can inspect."""
    return AgentCredentialService(
        agent_store,
        agent_sink,
        AgentCredentialConfig(grace_seconds=300.0, rotate_after_seconds=86400.0),
        clock=agent_clock,
    )


def sample_agent(department: str = "sales", agent_name: str = "assistant") -> AgentRef:
    return AgentRef(department=department, agent_name=agent_name)


def sample_scope(*tools: str) -> CredentialScope:
    return CredentialScope(tools=tuple(tools))
