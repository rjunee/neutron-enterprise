# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared test helpers for the ne-chat Teams suites (NE-080).

A locally-minted RSA keypair + JWKS (ZERO network) backs the fail-closed Bot
Framework activity-JWT matrix; ``mint_activity_token`` produces a valid RS256
activity token whose claims tests override to exercise each rejection branch. The
Bot Connector / token / JWKS wire is stubbed with ``httpx.MockTransport`` BELOW
the TeamsChatClient; live Teams never runs in CI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr

from ne.chat.teams.client import TeamsChatClient
from ne.chat.teams.config import TeamsChatSettings
from ne.chat.teams.refs import ConversationRefs

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "teams"

APP_ID = "bot-app-id"
APP_SECRET = "bot-app-secret"
KID = "test-kid"
ISSUER = "https://api.botframework.com"
SERVICE_URL = "https://smba.trafficmanager.net/amer/"
CONVERSATION_ID = "a:conv1"
METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
JWKS_URI = "https://login.botframework.com/v1/keys"

# -- locally minted RSA key + JWKS (no network) ------------------------------
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()

_PRIVATE_PEM = _PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
_PUBLIC_PEM = _PUBLIC_KEY.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)

_JWK: dict[str, Any] = RSAAlgorithm.to_jwk(_PUBLIC_KEY, as_dict=True)
_JWK.update({"kid": KID, "use": "sig", "alg": "RS256"})
JWKS: dict[str, Any] = {"keys": [_JWK]}

PUBLIC_PEM = _PUBLIC_PEM


def teams_chat_settings(**overrides: Any) -> TeamsChatSettings:
    kwargs: dict[str, Any] = {
        "app_id": APP_ID,
        "app_secret": SecretStr(APP_SECRET),
        "identity_links": {"aad-ada": "user:ada"},
    }
    kwargs.update(overrides)
    return TeamsChatSettings(**kwargs)


def activity_claims(
    *,
    app_id: str = APP_ID,
    service_url: str = SERVICE_URL,
    iss: str = ISSUER,
    exp_delta: float = 3600.0,
    **overrides: Any,
) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": app_id,
        "serviceurl": service_url,
        "iat": now,
        "nbf": now,
        "exp": now + int(exp_delta),
    }
    claims.update(overrides)
    return claims


def mint_activity_token(
    *,
    app_id: str = APP_ID,
    service_url: str = SERVICE_URL,
    iss: str = ISSUER,
    kid: str = KID,
    exp_delta: float = 3600.0,
    **claim_overrides: Any,
) -> str:
    """Mint a valid RS256 Bot Framework activity token (claims overridable)."""
    claims = activity_claims(
        app_id=app_id, service_url=service_url, iss=iss, exp_delta=exp_delta, **claim_overrides
    )
    return jwt.encode(claims, _PRIVATE_PEM, algorithm="RS256", headers={"kid": kid})


def mint_raw_payload_activity_token(raw_payload: bytes, *, kid: str = KID) -> str:
    """Sign ``raw_payload`` bytes VERBATIM with the trusted RSA key under a well-formed header.

    Bypasses ``jwt.encode``'s ``json.dumps`` so the payload can be something a dict
    cannot round-trip (a 100,000-deep nesting, a non-finite JSON number).
    """
    return jwt.PyJWS().encode(raw_payload, _PRIVATE_PEM, algorithm="RS256", headers={"kid": kid})


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint_hs256_confusion_token(**claim_overrides: Any) -> str:
    """RS/HS confusion: an HS256 token HMAC-signed with the RSA PUBLIC key bytes.

    Built by hand — PyJWT deliberately refuses to ``encode`` HS256 with a PEM key,
    so a real attacker would forge the compact token directly, which is exactly
    what we hand the verifier to prove it rejects on ``alg`` before any signature
    check.
    """
    header = {"alg": "HS256", "typ": "JWT", "kid": KID}
    claims = activity_claims(**claim_overrides)
    signing_input = (
        _b64url(json.dumps(header).encode()) + "." + _b64url(json.dumps(claims).encode())
    )
    sig = hmac.new(_PUBLIC_PEM, signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


def mint_alg_none_token(**claim_overrides: Any) -> str:
    """An unsigned (``alg:none``) token."""
    claims = activity_claims(**claim_overrides)
    return jwt.encode(claims, "", algorithm="none", headers={"kid": KID})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# -- Bot Connector / token / JWKS recording transport (no sockets) -----------


def teams_recording_transport(recorder: list[httpx.Request]) -> httpx.MockTransport:
    """A MockTransport recording every request; serves token, activities, metadata, JWKS."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        url = str(request.url)
        if url.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "server-token", "expires_in": 3599})
        if url == METADATA_URL:
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URI})
        if url == JWKS_URI:
            return httpx.Response(200, json=JWKS)
        if url.endswith("/activities"):
            return httpx.Response(200, json={"id": "act-out-1"})
        if "/activities/" in url:
            return httpx.Response(200, json={})
        return httpx.Response(404)  # pragma: no cover

    return httpx.MockTransport(handler)


def recording_teams_client(
    recorder: list[httpx.Request],
    refs: ConversationRefs | None = None,
    *,
    seed_conversation: bool = True,
    **overrides: Any,
) -> tuple[TeamsChatClient, ConversationRefs]:
    """Build a TeamsChatClient over the recording transport (fast throttle)."""
    overrides.setdefault("requests_per_minute", 6000.0)
    refs = refs if refs is not None else ConversationRefs(max_size=100)
    if seed_conversation:
        refs.record(CONVERSATION_ID, SERVICE_URL.rstrip("/"))
    client = TeamsChatClient(
        teams_chat_settings(**overrides), refs, transport=teams_recording_transport(recorder)
    )
    return client, refs
