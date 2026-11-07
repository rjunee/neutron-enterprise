# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TeamsGraphClient — C2 fail-closed construction, SSRF link guard, retry/token semantics."""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ne.connect.endpoints import AirGapViolationError, UnsafeEndpointError
from ne.connect.errors import GraphSyncError
from ne.connect.teams.graph import TeamsGraphClient

from .teams_transport import GRAPH, TEAM_ID, TOKEN_URL, build_teams_transport, teams_settings

_HOSTILE_GRAPH = [
    "http://graph.microsoft.com/v1.0",  # not https
    "https://169.254.169.254/v1.0",  # metadata IP literal
    "https://2130706433/v1.0",  # packed-integer IPv4
    "https://graph.evil.example.com/v1.0",  # https but NOT allowlisted
]


@pytest.mark.parametrize("bad_url", _HOSTILE_GRAPH)
def test_construction_rejects_hostile_graph_url(bad_url: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        TeamsGraphClient(teams_settings(graph_base_url=bad_url))


def test_construction_rejects_off_allowlist_login_url() -> None:
    with pytest.raises(UnsafeEndpointError):
        TeamsGraphClient(teams_settings(login_base_url="https://login.evil.example.com"))


def test_air_gap_aborts_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    recorder: list[httpx.Request] = []
    with pytest.raises(AirGapViolationError):
        TeamsGraphClient(teams_settings(), transport=build_teams_transport(recorder=recorder))
    assert recorder == []


# -- host-pinning of followed AND persisted delta links ----------------------

_HOSTILE_LINK = [
    "http://graph.microsoft.com/v1.0/teams/T0001/channels/C0STD/messages/delta",  # cleartext
    "https://user:pass@graph.microsoft.com/v1.0/teams/T0001/channels/C0STD/messages/delta",
    "https://graph.evil.example.com/v1.0/x",  # off-allowlist host
    "https://169.254.169.254/v1.0/x",  # metadata IP literal
    # A PERSISTED delta cursor pointing at a DIFFERENT valid Microsoft sovereign
    # cloud must be refused (the token stays in the configured cloud).
    "https://microsoftgraph.chinacloudapi.cn/v1.0/teams/T0001/channels/C0STD/messages/delta",
]


@pytest.mark.parametrize("bad_url", _HOSTILE_LINK)
def test_hostile_or_persisted_cross_cloud_link_refused_without_request(bad_url: str) -> None:
    recorder: list[httpx.Request] = []
    transport = build_teams_transport(recorder=recorder)
    client = TeamsGraphClient(teams_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphSyncError):
                # Re-entering via a persisted cursor exercises the same guard.
                await client.channel_messages_delta(http, TEAM_ID, "C0STD", cursor=bad_url)

    asyncio.run(scenario())
    assert recorder == []  # ZERO requests — no token POST, no GET


def test_secret_token_flow_hits_token_route() -> None:
    recorder: list[httpx.Request] = []
    transport = build_teams_transport(recorder=recorder)
    client = TeamsGraphClient(teams_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            channels = await client.list_channels(http, TEAM_ID)
            assert len(channels) == 5

    asyncio.run(scenario())
    assert sum(1 for r in recorder if str(r.url) == TOKEN_URL) == 1


def _write_cert(tmp_path: Path) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "teams-test")])
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


def test_certificate_token_flow_hits_token_route(tmp_path: Path) -> None:
    cert_path, key_path = _write_cert(tmp_path)
    recorder: list[httpx.Request] = []
    transport = build_teams_transport(recorder=recorder)
    client = TeamsGraphClient(
        teams_settings(
            auth_method="certificate",
            client_secret="",
            certificate_pem_path=cert_path,
            private_key_pem_path=key_path,
        ),
        transport=transport,
    )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            await client.list_channels(http, TEAM_ID)

    asyncio.run(scenario())
    token_posts = [r for r in recorder if str(r.url) == TOKEN_URL]
    assert len(token_posts) == 1
    body = token_posts[0].content.decode()
    assert "client_assertion" in body  # cert-signed assertion presented


def test_429_then_success_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    recorder: list[httpx.Request] = []
    transport = build_teams_transport(recorder=recorder, one_shot_429=True)
    client = TeamsGraphClient(teams_settings(requests_per_second=6000.0), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            channels = await client.list_channels(http, TEAM_ID)
            assert len(channels) == 5

    asyncio.run(scenario())
    assert slept  # honored the Retry-After


def test_graph_sync_error_status_and_host_only() -> None:
    recorder: list[httpx.Request] = []
    transport = build_teams_transport(recorder=recorder, fail_after=0)
    client = TeamsGraphClient(teams_settings(max_attempts=1), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphSyncError) as exc:
                await client.list_channels(http, TEAM_ID)
            text = str(exc.value)
            assert "500" in text
            assert "fixture-secret" not in text  # no secret leaks
            assert GRAPH not in text  # no full URL, only the host
            assert exc.value.status == 500

    asyncio.run(scenario())
