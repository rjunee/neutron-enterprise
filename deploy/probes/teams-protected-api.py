#!/usr/bin/env python3
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Live probe: can the M365 app registration read Teams channel messages app-only?

Answers the one question desk research cannot. Microsoft retired the Teams
protected-API request flow (the concept doc was deleted, every `aka.ms` shortlink
for it resolves to a Bing search, and Teams APIs stopped being metered on
2025-08-25), so the documented prerequisite is now a plain permission grant. This
probe proves that empirically against the live tenant rather than asserting it:
it signs a client-assertion JWT with the app's certificate, prints the granted
application roles, and calls the exact delta endpoint the connector uses.

Read-only. Prints no secrets and no message content.

Usage: uv run python deploy/probes/teams-protected-api.py
"""

import base64
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

SECRETS = pathlib.Path.home() / "ne-owner-secrets" / "m365"
GRAPH = "https://graph.microsoft.com/v1.0"
TIMEOUT_S = 30


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _read_ids() -> dict[str, str]:
    lines = (SECRETS / "ids.txt").read_text().splitlines()
    return dict(line.strip().split("=", 1) for line in lines if "=" in line)


def _client_assertion(tenant: str, client: str, audience: str) -> str:
    """A certificate-signed JWT bearer assertion (the app's configured auth method)."""
    key = serialization.load_pem_private_key(
        (SECRETS / "ne-m365-key.pem").read_bytes(), password=None
    )
    cert = x509.load_pem_x509_certificate((SECRETS / "ne-m365-cert.pem").read_bytes())
    thumbprint = _b64(cert.fingerprint(hashes.SHA1()))
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT", "x5t": thumbprint}).encode())
    payload = _b64(
        json.dumps(
            {
                "aud": audience,
                "iss": client,
                "sub": client,
                "jti": str(uuid.uuid4()),
                "nbf": now - 60,
                "exp": now + 600,
            }
        ).encode()
    )
    signature = _b64(key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()))
    print(f"tenant {tenant[:8]}…  client {client[:8]}…  cert x5t {thumbprint[:12]}…\n")
    return f"{header}.{payload}.{signature}"


def _acquire_token(tenant: str, client: str) -> str:
    audience = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    body = urllib.parse.urlencode(
        {
            "client_id": client,
            "scope": "https://graph.microsoft.com/.default",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": _client_assertion(tenant, client, audience),
            "grant_type": "client_credentials",
        }
    ).encode()
    request = urllib.request.Request(audience, data=body)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return str(json.load(response)["access_token"])
    except urllib.error.HTTPError as exc:
        detail = json.load(exc)
        description = str(detail.get("error_description"))[:300]
        print(f"TOKEN FAILED {exc.code}: {detail.get('error')} — {description}")
        raise SystemExit(1) from exc


def _get(url: str, token: str) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return response.status, dict(json.load(response))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, dict(json.load(exc))
        except (ValueError, TypeError):
            return exc.code, {}


def _error_line(body: dict[str, Any], limit: int = 200) -> str:
    error = body.get("error", {})
    return f"{error.get('code')}: {str(error.get('message'))[:limit]}"


def main() -> int:
    ids = _read_ids()
    token = _acquire_token(ids["tenant_id"], ids["client_id"])

    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    roles = sorted(claims.get("roles", []))
    print(f"TOKEN OK. {len(roles)} application permission(s) granted:")
    for role in roles:
        mark = " <-- Teams message read" if role.startswith("ChannelMessage") else ""
        print(f"   {role}{mark}")
    granted = any(role.startswith("ChannelMessage.Read") for role in roles)
    print(f"\nChannelMessage.Read.* granted: {'YES' if granted else 'NO'}")

    print("\n--- probing the actual endpoint the connector uses ---")
    query = urllib.parse.urlencode(
        {
            "$filter": "resourceProvisioningOptions/Any(x:x eq 'Team')",
            "$select": "id,displayName",
            "$top": "5",
        }
    )
    status, teams = _get(f"{GRAPH}/groups?{query}", token)
    print(f"list teams: HTTP {status}", end="")
    if status != 200:
        print(f" — {_error_line(teams)}")
        return 0
    items = teams.get("value", [])
    print(f" — {len(items)} team(s)")
    if not items:
        print("no teams in tenant; cannot probe messages. Create one Team to finish this check.")
        return 0

    team_id = items[0]["id"]
    status, channels = _get(f"{GRAPH}/teams/{team_id}/channels?$select=id,displayName", token)
    print(f"list channels: HTTP {status} — {len(channels.get('value', []))} channel(s)")
    if status != 200 or not channels.get("value"):
        print(f"   {_error_line(channels)}")
        return 0

    channel_id = channels["value"][0]["id"]
    status, messages = _get(f"{GRAPH}/teams/{team_id}/channels/{channel_id}/messages/delta", token)
    print(f"\nGET /teams/{{id}}/channels/{{id}}/messages/delta  ->  HTTP {status}")
    if status == 200:
        count = len(messages.get("value", []))
        print(f"   VERDICT: PROTECTED-API GATE IS OPEN. {count} message(s) readable app-only.")
        return 0

    print(f"   {_error_line(messages, limit=400)}")
    detail = str(messages.get("error", {}).get("message", ""))
    if "protected api" in detail.lower():
        print("   VERDICT: STILL GATED — the protected-API check is live despite the docs.")
    else:
        print("   VERDICT: failed for a DIFFERENT reason (not the protected-API gate).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
