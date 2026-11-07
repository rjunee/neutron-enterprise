# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Cert-signed client-assertion JWT + token-body shape + secret-leak hygiene (NE-044)."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ne.connect.errors import (
    GraphParseError,
    GraphRedirectError,
    GraphResponseParseError,
    GraphSyncError,
    GraphUnauthorizedError,
)
from ne.connect.m365.auth import build_client_assertion
from ne.connect.m365.graph import GraphClient

from .m365_transport import TOKEN_URL, build_m365_transport, m365_settings


def _self_signed() -> tuple[bytes, bytes, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ne-044-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
        .not_valid_after(dt.datetime(2027, 1, 1, tzinfo=dt.UTC))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem, cert


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _pub_pem(cert: x509.Certificate) -> bytes:
    return cert.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def test_assertion_header_thumbprints_and_claims_are_correct() -> None:
    cert_pem, key_pem, cert = _self_signed()
    token_url = "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"
    assertion = build_client_assertion(
        "client-abc", token_url, cert_pem, key_pem, now=1_700_000_000.0
    )

    headers = jwt.get_unverified_header(assertion)
    assert headers["alg"] == "RS256"
    assert headers["x5t"] == _b64url_no_pad(cert.fingerprint(hashes.SHA1()))
    assert headers["x5t#S256"] == _b64url_no_pad(cert.fingerprint(hashes.SHA256()))

    claims = jwt.decode(
        assertion,
        _pub_pem(cert),
        algorithms=["RS256"],
        audience=token_url,
        options={"verify_exp": False},
    )
    assert claims["iss"] == claims["sub"] == "client-abc"
    assert claims["aud"] == token_url
    assert claims["iat"] == claims["nbf"] == 1_700_000_000
    assert claims["exp"] == 1_700_000_000 + 600
    assert claims["jti"]


def test_two_assertions_have_distinct_jti() -> None:
    cert_pem, key_pem, cert = _self_signed()
    url = "https://login.microsoftonline.com/t/oauth2/v2.0/token"
    a1 = build_client_assertion("c", url, cert_pem, key_pem)
    a2 = build_client_assertion("c", url, cert_pem, key_pem)
    j1 = jwt.decode(a1, _pub_pem(cert), algorithms=["RS256"], options={"verify_aud": False})
    j2 = jwt.decode(a2, _pub_pem(cert), algorithms=["RS256"], options={"verify_aud": False})
    assert j1["jti"] != j2["jti"]


def _token_body(recorder: list[httpx.Request]) -> dict[str, list[str]]:
    post = next(r for r in recorder if str(r.url) == TOKEN_URL)
    return parse_qs(post.content.decode())


def test_certificate_mode_sends_assertion_not_secret(tmp_path) -> None:
    cert_pem, key_pem, _ = _self_signed()
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(key_pem)
    settings = m365_settings(
        auth_method="certificate",
        client_secret="",
        certificate_pem_path=str(cert_file),
        private_key_pem_path=str(key_file),
    )
    recorder: list[httpx.Request] = []
    transport = build_m365_transport(recorder=recorder)

    async def scenario() -> None:
        client = GraphClient(settings, transport=transport)
        async with httpx.AsyncClient(transport=transport) as http:
            await client._get_token(http)

    asyncio.run(scenario())
    body = _token_body(recorder)
    assert body["client_assertion_type"] == [
        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    ]
    assert body["client_assertion"][0]
    assert "client_secret" not in body
    assert body["grant_type"] == ["client_credentials"]


def test_secret_mode_sends_secret_not_assertion() -> None:
    settings = m365_settings(auth_method="secret", client_secret="s3cr3t-value")
    recorder: list[httpx.Request] = []
    transport = build_m365_transport(recorder=recorder)

    async def scenario() -> None:
        client = GraphClient(settings, transport=transport)
        async with httpx.AsyncClient(transport=transport) as http:
            await client._get_token(http)

    asyncio.run(scenario())
    body = _token_body(recorder)
    assert body["client_secret"] == ["s3cr3t-value"]
    assert "client_assertion" not in body


def test_token_failure_message_excludes_secret_and_url() -> None:
    settings = m365_settings(auth_method="secret", client_secret="super-secret-xyz")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    transport = httpx.MockTransport(handler)

    async def scenario() -> None:
        client = GraphClient(settings, transport=transport)
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphUnauthorizedError) as exc:
                await client._get_token(http)
            assert isinstance(exc.value, GraphSyncError)
            assert exc.value.status == 401
            message = str(exc.value)
            assert "401" in message
            assert "super-secret-xyz" not in message
            assert "oauth2" not in message

    asyncio.run(scenario())


def test_token_redirect_is_refused_without_using_location_or_payload() -> None:
    sentinel_location = "https://evil.example/steal-token"
    sentinel_body = "REDIRECT-PAYLOAD-MUST-NOT-LEAK"
    recorder: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(
            302,
            headers={"Location": sentinel_location},
            json={"access_token": sentinel_body, "expires_in": 3600},
        )

    transport = httpx.MockTransport(handler)

    async def scenario() -> None:
        client = GraphClient(m365_settings(), transport=transport)
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphRedirectError) as exc:
                await client._get_token(http)
            assert type(exc.value) is GraphRedirectError
            assert exc.value.status == 302
            assert sentinel_location not in str(exc.value)
            assert sentinel_body not in str(exc.value)

    asyncio.run(scenario())
    assert [str(request.url) for request in recorder] == [TOKEN_URL]


@pytest.mark.parametrize(
    "token_payload",
    [
        {},
        {"access_token": None},
        {"access_token": []},
        {"access_token": {}},
        {"access_token": ""},
        {"access_token": "   "},
        {"access_token": " T"},
        {"access_token": "T "},
        {"access_token": "T", "expires_in": None},
        {"access_token": "T", "expires_in": "3600"},
        {"access_token": "T", "expires_in": True},
        {"access_token": "T", "expires_in": 0},
        {"access_token": "T", "expires_in": -1},
        {"access_token": "T", "expires_in": 86_401},
    ],
)
def test_malformed_token_envelope_is_closed_parse_failure(token_payload: object) -> None:
    sentinel = "PROVIDER-TOKEN-PAYLOAD-MUST-NOT-LEAK"
    assert isinstance(token_payload, dict)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={**token_payload, "provider_detail": sentinel})
    )

    async def scenario() -> None:
        client = GraphClient(m365_settings(), transport=transport)
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphResponseParseError) as exc:
                await client._get_token(http)
            assert type(exc.value) is GraphResponseParseError
            assert isinstance(exc.value, GraphParseError)
            assert sentinel not in str(exc.value)
            assert client._token is None

    asyncio.run(scenario())
