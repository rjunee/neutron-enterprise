# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-073 AIR_GAP phase A — frontier-dead + install-healthy.

Runs INSIDE the sealed `ne-airgap` namespace while the real `ne up --profile
core --profile obs --bundle ...` installer has the core+obs stack up. Proves
the frontier lane is dead at BOTH layers:

  * network-impossibility — no default route, raw-IP connect fails, frontier
    hostnames don't even resolve, and a direct httpx POST simulating a fully
    policy-bypassed client still fails closed (the NETWORK refuses, not just
    our software);
  * software-refusal — the real ``LiteLLMGateway`` with ``EgressPolicy(air_gap=
    True)`` raises :class:`AirGapViolationError` BEFORE litellm is ever
    invoked, with an empty cost sink.

And that the real install is genuinely healthy (not merely "up"): every core
service reports docker healthcheck status ``healthy`` — now INCLUDING the
first-party ``ne-pilot-ne-api-1`` (NE-103a), the first live boot of the full
ne-api composition root in a container inside the sealed namespace (JWKS /
fastembed / qdrant / TEI edges are all lazy, so it boots healthy on core-only
infra with zero egress).

LOCAL-DIGEST PIN (owner dec ii): ``test_ne_api_runs_the_staged_local_image``
asserts the running container's image id equals the ``--iidfile`` id captured
when the image was built in the staging window (before seal) — that match IS the
C11 pin for a registry-free first-party build.

NE-103b (now IN scope): console same-origin serving (StaticFiles at ``/`` in
ne-api), the Caddy 443 → ne-api vhost, the pilot realm import, and a browserless
OIDC-live login (Keycloak password grant → a real minted token → ne-api authorizes
it via the in-network JWKS). The NE-CONSOLE-ISSUER runtime-config path is now also
exercised: ``/config.json`` serves this deployment's issuer, so one built ne-app
image self-configures its console login from the pilot's env. Still parked: the
browser auth-code+PKCE redirect UX (the NE-077 console demo) and the brain-profile
retrieval e2e.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import subprocess
from pathlib import Path

import httpx
import pytest

from ne.contracts import (
    ChatMessage,
    CompletionRequest,
    Lane,
    freeze_config_hash,
    reset_config_hash_for_testing,
)
from ne.contracts.config import NeSettings
from ne.gateway import (
    EgressPolicy,
    EgressSettings,
    GatewaySettings,
    LiteLLMGateway,
    ModelDef,
    TaskRoute,
)
from ne.gateway.errors import AirGapViolationError
from ne.schema import CostEvent

from .conftest import AIRGAP_ACTIVE, docker

if not AIRGAP_ACTIVE:
    pytest.skip(
        "AIR_GAP harness only runs inside the sealed netns (no-egress.yml)",
        allow_module_level=True,
    )


_CORE_HEALTHY_CONTAINERS = (
    "ne-pilot-caddy-1",
    "ne-pilot-postgres-1",
    "ne-pilot-keycloak-1",
    "ne-pilot-opa-1",
    "ne-pilot-ne-api-1",
    # NE-103c: the durable ne-worker joins the sealed core stack (same ne-app image,
    # different command). It launches DBOS-Postgres + cron with zero egress.
    "ne-pilot-ne-worker-1",
)
_OBS_RUNNING_CONTAINERS = ("ne-pilot-otel-collector-1",)

# The staging-window --iidfile the no-egress workflow writes (see
# .github/workflows/no-egress.yml "Stage: build ne-app image").
_NE_APP_IID = "/mnt/ne-airgap/ne-app.iid"

# The per-customer bundle the installer brought the stack up from (its manifest
# carries the hostname Caddy/Keycloak use for TLS SNI + the issuer).
_BUNDLE_MANIFEST = Path("/mnt/ne-airgap/bundle/manifest.json")


def test_no_default_route() -> None:
    v4 = subprocess.run(
        ["ip", "route", "show", "default"], capture_output=True, text=True, check=False
    )
    assert v4.stdout.strip() == "", f"IPv4 default route present: {v4.stdout!r}"
    v6 = subprocess.run(
        ["ip", "-6", "route", "show", "default"], capture_output=True, text=True, check=False
    )
    assert v6.stdout.strip() == "", f"IPv6 default route present: {v6.stdout!r}"


def test_raw_ip_egress_dead() -> None:
    """A DNS-independent raw-IP connect must fail — no route out at all."""
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=5)


@pytest.mark.parametrize("host", ["api.anthropic.com", "api.openai.com"])
def test_frontier_dns_dead(host: str) -> None:
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo(host, 443)


def test_frontier_https_bypass_attempt_fails_closed() -> None:
    """Simulate a fully-hijacked/policy-bypassed client that skips our
    EgressPolicy entirely and tries to talk to the frontier API directly.
    The NETWORK must refuse — proving the frontier lane is dead even if our
    software guard were somehow removed or bypassed."""
    with pytest.raises(httpx.TransportError), httpx.Client(timeout=10) as client:
        client.post(
            "https://api.anthropic.com/v1/messages",
            json={"model": "claude-sonnet-4-5", "messages": []},
        )


class _RecordingCostSink:
    """A real CostSink implementation (structural, mirrors ne-gateway's own
    test double) that appends emitted events to a list."""

    def __init__(self) -> None:
        self.events: list[CostEvent] = []

    def emit(self, event: CostEvent) -> None:
        self.events.append(event)


class _DemoSettings(NeSettings):
    demo: int = 1


def test_gateway_air_gap_refuses_frontier() -> None:
    """The real gateway, wired with ``air_gap=True``, refuses the frontier
    lane BEFORE litellm is ever invoked — the policy-layer half of the
    frontier-dead proof (the network-layer half is proven above)."""
    reset_config_hash_for_testing()
    freeze_config_hash(_DemoSettings())
    try:
        settings = GatewaySettings(
            models=[
                ModelDef(
                    name="frontier",
                    litellm_model="claude-sonnet-4-5",
                    provider="anthropic",
                    lane=Lane.FRONTIER_API,
                    extra_params={"api_key": "test"},
                    context_window=200000,
                )
            ],
            tasks={"draft": TaskRoute(model="frontier")},
        )
        sink = _RecordingCostSink()
        guard = EgressPolicy(
            EgressSettings(air_gap=True, allowed_frontier_hosts=("api.anthropic.com",))
        )
        gw = LiteLLMGateway(settings, sink, egress_guard=guard)
        request = CompletionRequest(
            messages=(ChatMessage(role="user", content="hi"),), task="draft"
        )
        with pytest.raises(AirGapViolationError):
            asyncio.run(gw.complete(request))
        assert sink.events == []
    finally:
        reset_config_hash_for_testing()


@pytest.mark.parametrize("container", _CORE_HEALTHY_CONTAINERS)
def test_core_stack_healthy_in_namespace(container: str) -> None:
    result = docker(["inspect", "-f", "{{.State.Health.Status}}", container])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "healthy", f"{container}: {result.stdout.strip()!r}"


@pytest.mark.parametrize("container", _OBS_RUNNING_CONTAINERS)
def test_obs_stack_running_in_namespace(container: str) -> None:
    result = docker(["inspect", "-f", "{{.State.Running}}", container])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "true", f"{container}: {result.stdout.strip()!r}"


def test_ne_api_runs_the_staged_local_image() -> None:
    """The local-digest pin (owner dec ii): the running ne-api container's image
    id must equal the id captured by --iidfile when the image was built in the
    staging window (before seal). No registry digest exists; this match IS the
    C11 pin for the first-party build."""
    iid = Path(_NE_APP_IID).read_text(encoding="utf-8").strip()
    assert iid.startswith("sha256:"), f"unexpected iidfile content: {iid!r}"
    # NE-103c: BOTH ne-api and ne-worker are the SAME staged ne-app image (one image,
    # different command) — assert each runs the exact --iidfile id.
    for container in ("ne-pilot-ne-api-1", "ne-pilot-ne-worker-1"):
        result = docker(["inspect", "-f", "{{.Image}}", container])
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == iid, (
            f"{container} runs image {result.stdout.strip()!r}, not the staged id {iid!r}"
        )


def test_ne_api_healthz_in_container() -> None:
    """/healthz answers {"status":"ok"} in-container with zero egress — the first
    live boot of the full composition root inside the sealed namespace."""
    result = docker(
        [
            "exec",
            "ne-pilot-ne-api-1",
            "/app/.venv/bin/python",
            "-c",
            "import urllib.request as u; "
            "print(u.urlopen('http://127.0.0.1:8000/healthz', timeout=5).read().decode())",
        ]
    )
    assert result.returncode == 0, result.stderr
    assert '"status"' in result.stdout and '"ok"' in result.stdout, result.stdout


# --- NE-103b: the loginable stack (realm import + console serving + OIDC login) --


def test_pilot_realm_discovery_served() -> None:
    """Keycloak serves the imported ne-pilot realm's OIDC discovery doc — proves
    `start --import-realm` applied the checked-in realm inside the sealed netns."""
    probe = (
        "exec 3<>/dev/tcp/127.0.0.1/8080; "
        'printf "GET /realms/ne-pilot/.well-known/openid-configuration HTTP/1.1\r\n'
        'Host: localhost\r\nConnection: close\r\n\r\n" >&3; cat <&3'
    )
    result = docker(["exec", "ne-pilot-keycloak-1", "bash", "-c", probe])
    assert result.returncode == 0, result.stderr
    assert "200 OK" in result.stdout, result.stdout
    assert '"issuer"' in result.stdout, result.stdout


def test_console_served_same_origin_in_container() -> None:
    """ne-api serves the console SPA index at / (same-origin with /api), OUTSIDE
    the OIDC wrap — the console dist the NE-103a image carries is mounted."""
    result = docker(
        [
            "exec",
            "ne-pilot-ne-api-1",
            "/app/.venv/bin/python",
            "-c",
            "import urllib.request as u; "
            "print(u.urlopen('http://127.0.0.1:8000/', timeout=5).read().decode())",
        ]
    )
    assert result.returncode == 0, result.stderr
    assert 'id="root"' in result.stdout, result.stdout


def test_runtime_console_config_served_with_deployment_issuer() -> None:
    """NE-CONSOLE-ISSUER: ne-api serves the RUNTIME console config at /config.json
    with THIS deployment's issuer — the same-image-different-issuer proof. The
    payload issuer is the EXACT NE_IDENTITY_OIDC__ISSUER the pilot compose sets
    (https://<bundle-hostname>:8443/realms/ne-pilot), client_id is ne-console, and
    no secret substring appears — proving one built ne-app image self-configures
    its console login from the deployment's env, not a Vite-baked constant."""
    result = docker(
        [
            "exec",
            "ne-pilot-ne-api-1",
            "/app/.venv/bin/python",
            "-c",
            "import urllib.request as u; "
            "print(u.urlopen('http://127.0.0.1:8000/config.json', timeout=5).read().decode())",
        ]
    )
    assert result.returncode == 0, result.stderr
    hostname = json.loads(_BUNDLE_MANIFEST.read_text(encoding="utf-8"))["hostname"]
    cfg = json.loads(result.stdout)
    assert cfg["issuer"] == f"https://{hostname}:8443/realms/ne-pilot", cfg
    assert cfg["client_id"] == "ne-console", cfg
    assert "secret" not in result.stdout.lower(), result.stdout


def test_static_mount_does_not_bypass_oidc_in_container() -> None:
    """The static mount does NOT open an auth hole: /api/v1/me is still 401 for an
    unauthenticated request even with the console mounted at /."""
    result = docker(
        [
            "exec",
            "ne-pilot-ne-api-1",
            "/app/.venv/bin/python",
            "-c",
            "import urllib.request as u, urllib.error as e\n"
            "try:\n"
            "    u.urlopen('http://127.0.0.1:8000/api/v1/me', timeout=5)\n"
            "    print('NO-ERROR')\n"
            "except e.HTTPError as ex:\n"
            "    print(ex.code)",
        ]
    )
    assert result.returncode == 0, result.stderr
    assert "401" in result.stdout, result.stdout


def test_password_grant_login_end_to_end() -> None:
    """THE loginable proof: the seeded `approver` logs in against the imported
    realm (password grant → a real Keycloak-minted token whose iss is the external
    https issuer and aud is ne-console), then ne-api authorizes that token on
    /api/v1/me by verifying it via the in-network JWKS. Fully browserless, zero
    egress."""
    script = (
        "import urllib.request as u, urllib.parse as p, json\n"
        "body = p.urlencode({\n"
        "    'grant_type': 'password', 'client_id': 'ne-console',\n"
        "    'username': 'approver', 'password': 'approver-demo', 'scope': 'openid',\n"
        "}).encode()\n"
        "tok = json.loads(u.urlopen(u.Request(\n"
        "    'http://keycloak:8080/realms/ne-pilot/protocol/openid-connect/token',\n"
        "    data=body, headers={'Content-Type': 'application/x-www-form-urlencoded'},\n"
        "), timeout=10).read())['access_token']\n"
        "me = u.urlopen(u.Request('http://127.0.0.1:8000/api/v1/me',\n"
        "    headers={'Authorization': 'Bearer ' + tok}), timeout=10).read().decode()\n"
        "print(me)"
    )
    result = docker(["exec", "ne-pilot-ne-api-1", "/app/.venv/bin/python", "-c", script])
    assert result.returncode == 0, result.stderr
    assert "approver" in result.stdout, result.stdout


def test_caddy_443_serves_console_with_sni() -> None:
    """The front door: Caddy 443 (TLS, SNI-correct) reverse-proxies ne-api, serving
    the console SPA at / while /api/v1/me stays 401-guarded through the same
    ingress. Self-signed local CA, so we skip cert verification (the point is
    reachability + the guarded API, not PKI)."""
    hostname = json.loads(_BUNDLE_MANIFEST.read_text(encoding="utf-8"))["hostname"]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def _get(path: str) -> str:
        raw = socket.create_connection(("127.0.0.1", 443), timeout=10)
        with ctx.wrap_socket(raw, server_hostname=hostname) as sock:
            req = f"GET {path} HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n"
            sock.sendall(req.encode())
            chunks: list[bytes] = []
            while True:
                buf = sock.recv(4096)
                if not buf:
                    break
                chunks.append(buf)
        return b"".join(chunks).decode(errors="replace")

    root = _get("/")
    assert "200" in root.split("\r\n", 1)[0], root[:200]
    assert 'id="root"' in root, root[:400]

    me = _get("/api/v1/me")
    assert "401" in me.split("\r\n", 1)[0], me[:200]
